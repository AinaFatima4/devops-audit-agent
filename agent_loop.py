"""The observe -> reason -> select tool -> execute -> interpret loop, written without frameworks."""

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from findings import EvidenceLedger, FindingsRecorder, Violation
from llm_client import (
    ChatModel,
    ContextTooLargeError,
    LLMError,
    MalformedToolCallError,
    ToolCall,
)
from prompts import (
    COMPLETION_CHECK_MESSAGE,
    MALFORMED_TOOL_CALL_MESSAGE,
    SYSTEM_PROMPT,
    TEXT_ONLY_REPLY_MESSAGE,
    build_initial_user_message,
    build_low_steps_message,
)
from tool_schemas import ALL_TOOL_SCHEMAS
from tools import RepositoryTools, ToolError

logger = logging.getLogger("audit_agent")

MAX_CONSECUTIVE_TEXT_REPLIES = 2
MAX_CONSECUTIVE_MALFORMED_CALLS = 3
MAX_CONTEXT_RECOVERY_ATTEMPTS = 2
MAX_IDENTICAL_CALLS = 2
TRACE_PREVIEW_CHARACTERS = 300


@dataclass
class AgentOptions:
    max_steps: int = 40
    max_tool_result_characters: int = 6000
    context_character_budget: int = 16000
    recent_tool_results_to_keep: int = 3
    low_steps_warning_at: int = 5


@dataclass
class TraceEntry:
    step: int
    tool: str
    arguments: dict[str, Any]
    status: str
    duration_milliseconds: int
    result_preview: str


@dataclass
class AuditResult:
    repository_url: str
    violations: list[Violation]
    completed: bool
    stop_reason: str
    steps_used: int
    summary: str = ""
    total_tokens: int = 0
    trace: list[TraceEntry] = field(default_factory=list)


class AuditError(Exception):
    """Raised when the audit cannot continue; carries the partial result gathered so far."""

    def __init__(self, message: str, partial_result: AuditResult) -> None:
        super().__init__(message)
        self.partial_result = partial_result


def _integer_argument(arguments: dict[str, Any], name: str, default_value: int) -> int:
    raw_value = arguments.get(name, default_value)
    if raw_value is None or isinstance(raw_value, bool):
        return default_value
    try:
        return int(raw_value)
    except (TypeError, ValueError) as conversion_error:
        raise ToolError(f"'{name}' must be an integer, got {raw_value!r}") from conversion_error


def _optional_string_argument(arguments: dict[str, Any], name: str) -> str | None:
    raw_value = arguments.get(name)
    if raw_value is None or str(raw_value).strip() == "":
        return None
    return str(raw_value)


class AuditAgent:
    """Runs one audit. The LLM chooses every tool call; Python executes and checks them."""

    def __init__(
        self,
        model: ChatModel,
        repository_tools: RepositoryTools,
        repository_url: str,
        options: AgentOptions | None = None,
    ) -> None:
        self.model = model
        self.repository_tools = repository_tools
        self.repository_url = repository_url
        self.options = options or AgentOptions()
        self.evidence_ledger = EvidenceLedger()
        self.findings_recorder = FindingsRecorder(self.evidence_ledger)
        self.messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": build_initial_user_message(repository_url)},
        ]
        self.trace: list[TraceEntry] = []
        self.tool_call_labels: dict[str, str] = {}
        self.compacted_call_ids: set[str] = set()
        self.identical_call_counts: dict[str, int] = {}
        self.completion_check_done = False
        self.finished = False
        self.summary = ""
        self.total_tokens = 0
        self.steps_used = 0

    def build_result(self, completed: bool, stop_reason: str) -> AuditResult:
        return AuditResult(
            repository_url=self.repository_url,
            violations=self.findings_recorder.sorted_violations(),
            completed=completed,
            stop_reason=stop_reason,
            steps_used=self.steps_used,
            summary=self.summary,
            total_tokens=self.total_tokens,
            trace=self.trace,
        )

    def run(self) -> AuditResult:
        consecutive_text_replies = 0
        consecutive_malformed_calls = 0
        context_recovery_attempts = 0
        for step_number in range(1, self.options.max_steps + 1):
            self.steps_used = step_number
            steps_remaining = self.options.max_steps - step_number + 1
            if steps_remaining == self.options.low_steps_warning_at:
                self.messages.append(
                    {"role": "user", "content": build_low_steps_message(steps_remaining)}
                )
            self.compact_context(self.options.recent_tool_results_to_keep)
            try:
                response = self.model.chat(self.messages, ALL_TOOL_SCHEMAS)
            except ContextTooLargeError as context_error:
                context_recovery_attempts += 1
                if context_recovery_attempts > MAX_CONTEXT_RECOVERY_ATTEMPTS:
                    raise AuditError(str(context_error), self.build_result(False, "error")) from (
                        context_error
                    )
                logger.warning("Request too large; removing older tool results and retrying")
                self.compact_context(
                    recent_to_keep=1, budget=self.options.context_character_budget // 2
                )
                continue
            except MalformedToolCallError as malformed_error:
                consecutive_malformed_calls += 1
                if consecutive_malformed_calls > MAX_CONSECUTIVE_MALFORMED_CALLS:
                    raise AuditError(
                        str(malformed_error), self.build_result(False, "error")
                    ) from malformed_error
                logger.warning("Model produced a malformed tool call; asking it to retry")
                self.messages.append({"role": "user", "content": MALFORMED_TOOL_CALL_MESSAGE})
                continue
            except LLMError as llm_error:
                raise AuditError(str(llm_error), self.build_result(False, "error")) from llm_error
            consecutive_malformed_calls = 0
            self.total_tokens += response.total_tokens or 0
            self.messages.append(response.assistant_message)
            if not response.tool_calls:
                consecutive_text_replies += 1
                if consecutive_text_replies > MAX_CONSECUTIVE_TEXT_REPLIES:
                    logger.info("Model stopped calling tools; ending the audit")
                    self.summary = response.text.strip()[:1000]
                    return self.build_result(True, "model_stopped")
                self.messages.append({"role": "user", "content": TEXT_ONLY_REPLY_MESSAGE})
                continue
            consecutive_text_replies = 0
            for tool_call in response.tool_calls:
                self.handle_tool_call(step_number, tool_call)
            if self.finished:
                return self.build_result(True, "finished")
        logger.warning("Step limit of %d reached", self.options.max_steps)
        return self.build_result(True, "step_limit")

    def handle_tool_call(self, step_number: int, tool_call: ToolCall) -> None:
        started_at = time.monotonic()
        label = f"{tool_call.name}({json.dumps(tool_call.arguments, sort_keys=True)})"
        logger.info("Step %d: %s", step_number, label[:200])
        if tool_call.arguments_error is not None:
            status = "error"
            result_text = f"ERROR: {tool_call.arguments_error}"
        else:
            status, result_text = self.dispatch(tool_call)
        result_text = self.limit_result_size(result_text)
        self.tool_call_labels[tool_call.call_id] = label
        self.messages.append(
            {"role": "tool", "tool_call_id": tool_call.call_id, "content": result_text}
        )
        duration_milliseconds = int((time.monotonic() - started_at) * 1000)
        self.trace.append(
            TraceEntry(
                step=step_number,
                tool=tool_call.name,
                arguments=tool_call.arguments,
                status=status,
                duration_milliseconds=duration_milliseconds,
                result_preview=result_text[:TRACE_PREVIEW_CHARACTERS],
            )
        )

    def dispatch(self, tool_call: ToolCall) -> tuple[str, str]:
        """Run the requested tool and return (status, text for the LLM). Never raises."""
        if tool_call.name == "report_violation":
            result_text = self.findings_recorder.record(tool_call.arguments)
            status = "error" if result_text.startswith("ERROR") else "ok"
            return status, result_text
        if tool_call.name == "finish_audit":
            return "ok", self.handle_finish(tool_call.arguments)
        call_key = f"{tool_call.name}:{json.dumps(tool_call.arguments, sort_keys=True)}"
        self.identical_call_counts[call_key] = self.identical_call_counts.get(call_key, 0) + 1
        if self.identical_call_counts[call_key] > MAX_IDENTICAL_CALLS:
            return "error", (
                "ERROR: this exact call has already run twice. Use a different tool or arguments."
            )
        try:
            result_text = self.run_inspection_tool(tool_call.name, tool_call.arguments)
        except ToolError as tool_error:
            return "error", f"ERROR: {tool_error}"
        except Exception as unexpected_error:  # noqa: BLE001 - keep the audit alive
            logger.exception("Unexpected failure in tool %s", tool_call.name)
            return "error", f"ERROR: tool failed unexpectedly ({type(unexpected_error).__name__})"
        self.evidence_ledger.record_tool_output(tool_call.name, result_text)
        return "ok", result_text

    def run_inspection_tool(self, tool_name: str, arguments: dict[str, Any]) -> str:
        if tool_name == "list_files":
            return self.repository_tools.list_files(
                directory=_optional_string_argument(arguments, "directory") or ""
            )
        if tool_name == "read_file":
            path = _optional_string_argument(arguments, "path")
            if path is None:
                raise ToolError("'path' is required")
            return self.repository_tools.read_file(
                path=path,
                start_line=_integer_argument(arguments, "start_line", 1),
                max_lines=_integer_argument(arguments, "max_lines", 300),
            )
        if tool_name == "search_code":
            pattern = _optional_string_argument(arguments, "pattern")
            if pattern is None:
                raise ToolError("'pattern' is required")
            return self.repository_tools.search_code(
                pattern=pattern,
                file_glob=_optional_string_argument(arguments, "file_glob"),
                case_sensitive=bool(arguments.get("case_sensitive", False)),
                max_results=_integer_argument(arguments, "max_results", 80),
            )
        if tool_name == "git_log":
            return self.repository_tools.git_log(
                max_commits=_integer_argument(arguments, "max_commits", 30),
                path=_optional_string_argument(arguments, "path"),
            )
        if tool_name == "run_ruff":
            return self.repository_tools.run_ruff(
                path=_optional_string_argument(arguments, "path") or ""
            )
        raise ToolError(f"Unknown tool {tool_name!r}")

    def handle_finish(self, arguments: dict[str, Any]) -> str:
        if not self.completion_check_done:
            self.completion_check_done = True
            return COMPLETION_CHECK_MESSAGE
        self.finished = True
        self.summary = str(arguments.get("summary", "")).strip()[:1000]
        return "Audit finished."

    def limit_result_size(self, result_text: str) -> str:
        limit = self.options.max_tool_result_characters
        if len(result_text) <= limit:
            return result_text
        return (
            result_text[:limit]
            + f"\n[result shortened to {limit} characters; narrow the request to see more]"
        )

    def compact_context(self, recent_to_keep: int, budget: int | None = None) -> None:
        """Replace the oldest tool results with short notes until the history fits the budget."""
        character_budget = budget or self.options.context_character_budget
        conversation = self.messages[1:]
        total_characters = sum(len(str(message.get("content") or "")) for message in conversation)
        if total_characters <= character_budget:
            return
        tool_messages = [message for message in conversation if message.get("role") == "tool"]
        candidates = tool_messages[: max(0, len(tool_messages) - recent_to_keep)]
        for tool_message in candidates:
            if total_characters <= character_budget:
                break
            call_id = tool_message["tool_call_id"]
            if call_id in self.compacted_call_ids:
                continue
            label = self.tool_call_labels.get(call_id, "tool call")[:200]
            note = f"[Earlier result of {label} removed to save space. Re-run it if needed.]"
            total_characters -= len(tool_message["content"]) - len(note)
            tool_message["content"] = note
            self.compacted_call_ids.add(call_id)
