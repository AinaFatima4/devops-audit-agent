"""Command-line entry point: python agent.py --repo <github_url> --output report.json"""

import argparse
import json
import logging
import os
import sys
import tempfile
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any

from agent_loop import AgentOptions, AuditAgent, AuditError, AuditResult
from config import ConfigurationError, Settings, load_settings, read_number_setting
from llm_client import ChatModel, OpenAICompatibleClient
from tools import (
    InvalidRepositoryUrlError,
    RepositoryTools,
    ToolError,
    clone_repository,
    normalize_github_url,
    remove_directory,
)

EXIT_SUCCESS = 0
EXIT_AUDIT_FAILED = 1
EXIT_INVALID_INPUT = 2
EXIT_INTERRUPTED = 130
PROJECT_ROOT = Path(__file__).resolve().parent
WORKSPACE_DIRECTORY = PROJECT_ROOT / ".audit_workspace"
DEFAULT_MAX_STEPS = 30
DEFAULT_CONTEXT_CHARACTER_BUDGET = 12000

logger = logging.getLogger("audit_agent")


def parse_arguments(argument_list: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit a public GitHub repository for DevOps best-practice violations."
    )
    parser.add_argument("--repo", required=True, help="Public GitHub repository URL")
    parser.add_argument("--output", default="report.json", help="Path of the JSON report")
    parser.add_argument(
        "--trace",
        default=None,
        help="Path of the tool-call trace (default: <output name>.trace.json)",
    )
    parser.add_argument(
        "--max-steps",
        type=int,
        default=None,
        help=f"Maximum LLM steps (default: AGENT_MAX_STEPS or {DEFAULT_MAX_STEPS})",
    )
    parser.add_argument("--verbose", action="store_true", help="Show debug logging")
    return parser.parse_args(argument_list)


def configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )
    for noisy_logger_name in ("httpx", "httpx2", "httpcore", "openai"):
        logging.getLogger(noisy_logger_name).setLevel(logging.WARNING)


def build_agent_options(max_steps_argument: int | None) -> AgentOptions:
    max_steps = max_steps_argument or int(read_number_setting("AGENT_MAX_STEPS", DEFAULT_MAX_STEPS))
    if max_steps < 3:
        raise ConfigurationError("Maximum steps must be at least 3")
    context_budget = int(
        read_number_setting("AGENT_CONTEXT_CHAR_BUDGET", DEFAULT_CONTEXT_CHARACTER_BUDGET)
    )
    return AgentOptions(max_steps=max_steps, context_character_budget=context_budget)


def build_report(repository_url: str, result: AuditResult) -> dict[str, Any]:
    violations = [violation.to_dict() for violation in result.violations]
    return {"repository": repository_url, "violations": violations}


def build_trace(result: AuditResult, model_name: str) -> dict[str, Any]:
    return {
        "repository": result.repository_url,
        "model": model_name,
        "completed": result.completed,
        "stop_reason": result.stop_reason,
        "steps_used": result.steps_used,
        "total_tokens": result.total_tokens,
        "summary": result.summary,
        "tool_calls": [asdict(entry) for entry in result.trace],
    }


def write_json_atomically(output_path: Path, data: dict[str, Any]) -> None:
    """Write to a temporary file first so a crash never leaves half-written JSON behind."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=".tmp-", suffix=".json", dir=output_path.parent
    )
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as file_handle:
            json.dump(data, file_handle, indent=2, ensure_ascii=True)
            file_handle.write("\n")
        os.replace(temporary_name, output_path)
    except BaseException:
        Path(temporary_name).unlink(missing_ok=True)
        raise


def default_trace_path(output_path: Path) -> Path:
    return output_path.with_name(f"{output_path.stem}.trace.json")


def main(
    argument_list: list[str] | None = None,
    model_factory: Callable[[Settings], ChatModel] = OpenAICompatibleClient,
    clone_function: Callable[[str, Path], Path] = clone_repository,
) -> int:
    arguments = parse_arguments(argument_list)
    configure_logging(arguments.verbose)
    output_path = Path(arguments.output)
    trace_path = Path(arguments.trace) if arguments.trace else default_trace_path(output_path)
    try:
        repository_url = normalize_github_url(arguments.repo)
        settings = load_settings()
        options = build_agent_options(arguments.max_steps)
    except (InvalidRepositoryUrlError, ConfigurationError) as input_error:
        logger.error("%s", input_error)
        return EXIT_INVALID_INPUT

    clone_directory: Path | None = None
    try:
        logger.info("Cloning %s", repository_url)
        clone_directory = clone_function(repository_url, WORKSPACE_DIRECTORY)
        model = model_factory(settings)
        agent = AuditAgent(model, RepositoryTools(clone_directory), repository_url, options)
        logger.info(
            "Starting audit with model %s (max %d steps)", settings.model, options.max_steps
        )
        result = agent.run()
    except ToolError as clone_error:
        logger.error("%s", clone_error)
        return EXIT_AUDIT_FAILED
    except AuditError as audit_error:
        partial_path = output_path.with_name(f"{output_path.stem}.partial.json")
        write_json_atomically(
            partial_path, build_report(repository_url, audit_error.partial_result)
        )
        write_json_atomically(trace_path, build_trace(audit_error.partial_result, settings.model))
        logger.error("Audit failed: %s", audit_error)
        logger.error("Findings gathered before the failure were saved to %s", partial_path)
        return EXIT_AUDIT_FAILED
    except KeyboardInterrupt:
        logger.error("Interrupted by user")
        return EXIT_INTERRUPTED
    finally:
        if clone_directory is not None:
            remove_directory(clone_directory)

    write_json_atomically(output_path, build_report(repository_url, result))
    write_json_atomically(trace_path, build_trace(result, settings.model))
    if result.stop_reason != "finished":
        logger.warning("Audit ended early (%s); the report may be incomplete", result.stop_reason)
    logger.info(
        "Wrote %d violations to %s (%d steps, %d tokens). Trace: %s",
        len(result.violations),
        output_path,
        result.steps_used,
        result.total_tokens,
        trace_path,
    )
    return EXIT_SUCCESS


if __name__ == "__main__":
    sys.exit(main())
