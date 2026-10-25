import json
from collections.abc import Callable
from typing import Any

import pytest

from agent_loop import AgentOptions, AuditAgent, AuditError
from llm_client import (
    ContextTooLargeError,
    LLMError,
    LLMResponse,
    MalformedToolCallError,
    ToolCall,
)
from tools import RepositoryTools

REPOSITORY_URL = "https://github.com/example/sample"


def tool_response(*calls: tuple[str, dict[str, Any]]) -> LLMResponse:
    tool_calls = []
    for index, (name, arguments) in enumerate(calls):
        tool_calls.append(ToolCall(call_id=f"call_{name}_{index}", name=name, arguments=arguments))
    assistant_message = {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": tool_call.call_id,
                "type": "function",
                "function": {"name": tool_call.name, "arguments": json.dumps(tool_call.arguments)},
            }
            for tool_call in tool_calls
        ],
    }
    return LLMResponse(assistant_message=assistant_message, text="", tool_calls=tool_calls)


def text_response(text: str) -> LLMResponse:
    return LLMResponse(assistant_message={"role": "assistant", "content": text}, text=text)


class ScriptedModel:
    """Fake LLM: each item is a response, an exception, or a function of the conversation."""

    def __init__(self, script: list[LLMResponse | Exception | Callable[[list], LLMResponse]]):
        self.script = list(script)
        self.received_messages: list[list[dict[str, Any]]] = []

    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> LLMResponse:
        self.received_messages.append([dict(message) for message in messages])
        if not self.script:
            return tool_response(("finish_audit", {"summary": "done"}))
        next_item = self.script.pop(0)
        if isinstance(next_item, Exception):
            raise next_item
        if callable(next_item):
            return next_item(messages)
        return next_item


SECRET_FINDING = {
    "rule_id": "SEC-001",
    "category": "security",
    "severity": "critical",
    "file": "src/app.py",
    "line": 3,
    "description": "Hardcoded database password",
    "evidence": "read_file shows DATABASE_PASSWORD assigned a redacted literal",
    "recommendation": "Read the password from an environment variable",
}

FINISH = tool_response(("finish_audit", {"summary": "done"}))


def run_agent(
    repository_tools: RepositoryTools, script: list, options: AgentOptions | None = None
) -> tuple[Any, ScriptedModel]:
    model = ScriptedModel(script)
    agent = AuditAgent(model, repository_tools, REPOSITORY_URL, options)
    return agent.run(), model


def test_full_audit_records_violation_and_finishes(repository_tools: RepositoryTools) -> None:
    result, _ = run_agent(
        repository_tools,
        [
            tool_response(("list_files", {})),
            tool_response(("read_file", {"path": "src/app.py"})),
            tool_response(("report_violation", SECRET_FINDING)),
            FINISH,
            FINISH,
        ],
    )
    assert result.completed
    assert result.stop_reason == "finished"
    assert [violation.rule_id for violation in result.violations] == ["SEC-001"]
    assert [entry.tool for entry in result.trace] == [
        "list_files",
        "read_file",
        "report_violation",
        "finish_audit",
        "finish_audit",
    ]


def test_tool_results_are_sent_back_to_the_model(repository_tools: RepositoryTools) -> None:
    def read_first_python_file(messages: list[dict[str, Any]]) -> LLMResponse:
        list_output = messages[-1]["content"]
        python_files = [line.split(" ")[0] for line in list_output.split("\n") if ".py " in line]
        return tool_response(("read_file", {"path": python_files[0]}))

    result, model = run_agent(
        repository_tools,
        [tool_response(("list_files", {})), read_first_python_file, FINISH, FINISH],
    )
    assert result.trace[1].arguments == {"path": "src/app.py"}
    assert "src/app.py" in model.received_messages[1][-1]["content"]


def test_first_finish_triggers_completion_check(repository_tools: RepositoryTools) -> None:
    result, model = run_agent(repository_tools, [FINISH, FINISH])
    assert result.steps_used == 2
    assert "check your coverage" in model.received_messages[1][-1]["content"]


def test_secret_values_never_reach_the_model(repository_tools: RepositoryTools) -> None:
    _, model = run_agent(
        repository_tools,
        [
            tool_response(("read_file", {"path": "src/app.py"})),
            tool_response(("search_code", {"pattern": "PASSWORD"})),
            FINISH,
            FINISH,
        ],
    )
    all_sent_text = json.dumps(model.received_messages)
    assert "hunter2-real-value" not in all_sent_text


def test_unsupported_violation_is_rejected_and_explained(
    repository_tools: RepositoryTools,
) -> None:
    result, model = run_agent(
        repository_tools,
        [tool_response(("report_violation", SECRET_FINDING)), FINISH, FINISH],
    )
    assert result.violations == []
    assert model.received_messages[1][-1]["content"].startswith("ERROR")


def test_tool_errors_are_returned_to_the_model(repository_tools: RepositoryTools) -> None:
    result, model = run_agent(
        repository_tools,
        [
            tool_response(("read_file", {"path": "missing.py"})),
            tool_response(("delete_everything", {})),
            FINISH,
            FINISH,
        ],
    )
    assert "File not found" in model.received_messages[1][-1]["content"]
    assert "Unknown tool" in model.received_messages[2][-1]["content"]
    assert result.completed


def test_bad_argument_types_are_reported_not_raised(repository_tools: RepositoryTools) -> None:
    _, model = run_agent(
        repository_tools,
        [
            tool_response(("read_file", {"path": "README.md", "start_line": "first"})),
            FINISH,
            FINISH,
        ],
    )
    assert "must be an integer" in model.received_messages[1][-1]["content"]


def test_repeated_identical_calls_are_limited(repository_tools: RepositoryTools) -> None:
    repeated_call = tool_response(("git_log", {}))
    _, model = run_agent(repository_tools, [repeated_call, repeated_call, repeated_call, FINISH])
    assert "already run twice" in model.received_messages[3][-1]["content"]


def test_text_only_replies_are_nudged_then_end_the_audit(
    repository_tools: RepositoryTools,
) -> None:
    result, model = run_agent(
        repository_tools,
        [text_response("thinking"), text_response("still thinking"), text_response("bye")],
    )
    assert result.stop_reason == "model_stopped"
    assert "Reply only with tool calls" in model.received_messages[1][-1]["content"]


def test_step_limit_stops_the_audit(repository_tools: RepositoryTools) -> None:
    options = AgentOptions(max_steps=3, low_steps_warning_at=2)
    result, model = run_agent(
        repository_tools,
        [tool_response(("list_files", {"directory": str(index)})) for index in range(5)],
        options,
    )
    assert result.stop_reason == "step_limit"
    assert result.steps_used == 3
    assert "Only 2 steps remain" in model.received_messages[1][-1]["content"]


def test_llm_failure_raises_audit_error_with_partial_findings(
    repository_tools: RepositoryTools,
) -> None:
    with pytest.raises(AuditError) as error_info:
        run_agent(
            repository_tools,
            [
                tool_response(("read_file", {"path": "src/app.py"})),
                tool_response(("report_violation", SECRET_FINDING)),
                LLMError("quota exceeded"),
            ],
        )
    partial_result = error_info.value.partial_result
    assert not partial_result.completed
    assert len(partial_result.violations) == 1


def test_malformed_tool_call_is_retried(repository_tools: RepositoryTools) -> None:
    result, model = run_agent(
        repository_tools, [MalformedToolCallError("bad call"), FINISH, FINISH]
    )
    assert result.completed
    assert "malformed" in model.received_messages[1][-1]["content"]


def test_context_too_large_triggers_compaction(repository_tools: RepositoryTools) -> None:
    options = AgentOptions(context_character_budget=400, recent_tool_results_to_keep=1)
    result, model = run_agent(
        repository_tools,
        [
            tool_response(("read_file", {"path": "src/app.py"})),
            tool_response(("list_files", {})),
            ContextTooLargeError("too large"),
            FINISH,
            FINISH,
        ],
        options,
    )
    assert result.completed
    last_conversation = json.dumps(model.received_messages[-1])
    assert "removed to save space" in last_conversation


def test_repeated_context_errors_abort(repository_tools: RepositoryTools) -> None:
    with pytest.raises(AuditError):
        run_agent(repository_tools, [ContextTooLargeError("too large")] * 3)


def test_audit_leaves_repository_unchanged(
    repository_tools: RepositoryTools, sample_repository
) -> None:
    before = sorted(
        path.as_posix() for path in sample_repository.rglob("*") if ".git" not in path.parts
    )
    run_agent(
        repository_tools,
        [
            tool_response(("list_files", {}), ("git_log", {}), ("run_ruff", {})),
            FINISH,
            FINISH,
        ],
    )
    after = sorted(
        path.as_posix() for path in sample_repository.rglob("*") if ".git" not in path.parts
    )
    assert before == after
