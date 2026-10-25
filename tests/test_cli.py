import json
from pathlib import Path

import pytest
from fakes import FINISH, REPOSITORY_URL, SECRET_FINDING, ScriptedModel, tool_response

import agent
import config
from config import Settings
from llm_client import LLMError


@pytest.fixture(autouse=True)
def fake_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.delenv("AGENT_MAX_STEPS", raising=False)
    monkeypatch.delenv("AGENT_CONTEXT_CHAR_BUDGET", raising=False)
    monkeypatch.setattr(agent, "WORKSPACE_DIRECTORY", tmp_path / "workspace")
    monkeypatch.setattr(agent, "load_settings", lambda: config.load_settings(env_file=None))


def fake_clone_factory(sample_repository: Path):
    def fake_clone(repository_url: str, workspace_directory: Path) -> Path:
        return sample_repository

    return fake_clone


def run_cli(arguments: list[str], script: list, sample_repository: Path) -> int:
    def model_factory(settings: Settings) -> ScriptedModel:
        return ScriptedModel(script)

    return agent.main(arguments, model_factory, fake_clone_factory(sample_repository))


def test_successful_audit_writes_report_and_trace(
    tmp_path: Path, sample_repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(agent, "remove_directory", lambda directory: None)
    output_path = tmp_path / "out" / "report.json"
    exit_code = run_cli(
        ["--repo", REPOSITORY_URL + ".git", "--output", str(output_path)],
        [
            tool_response(("read_file", {"path": "src/app.py"})),
            tool_response(("report_violation", SECRET_FINDING)),
            tool_response(
                (
                    "report_violation",
                    {**SECRET_FINDING, "rule_id": "TST-001", "file": None, "line": None},
                )
            ),
            FINISH,
            FINISH,
        ],
        sample_repository,
    )
    assert exit_code == agent.EXIT_SUCCESS
    report = json.loads(output_path.read_text(encoding="utf-8"))
    assert set(report) == {"repository", "violations"}
    assert report["repository"] == REPOSITORY_URL
    expected_fields = {
        "rule_id", "category", "severity", "file", "line",
        "description", "evidence", "recommendation",
    }  # fmt: skip
    for violation in report["violations"]:
        assert set(violation) == expected_fields
    violations_by_rule = {violation["rule_id"]: violation for violation in report["violations"]}
    assert violations_by_rule["TST-001"]["file"] is None
    assert violations_by_rule["TST-001"]["line"] is None
    assert violations_by_rule["SEC-001"]["line"] == 3
    trace = json.loads((tmp_path / "out" / "report.trace.json").read_text(encoding="utf-8"))
    assert trace["stop_reason"] == "finished"
    assert trace["tool_calls"][0]["tool"] == "read_file"


def test_clean_repository_produces_empty_violations_array(
    tmp_path: Path, sample_repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(agent, "remove_directory", lambda directory: None)
    output_path = tmp_path / "report.json"
    exit_code = run_cli(
        ["--repo", REPOSITORY_URL, "--output", str(output_path)],
        [FINISH, FINISH],
        sample_repository,
    )
    assert exit_code == agent.EXIT_SUCCESS
    assert json.loads(output_path.read_text(encoding="utf-8"))["violations"] == []


def test_invalid_url_exits_with_input_error(tmp_path: Path, sample_repository: Path) -> None:
    output_path = tmp_path / "report.json"
    exit_code = run_cli(
        ["--repo", "https://example.com/not-github", "--output", str(output_path)],
        [],
        sample_repository,
    )
    assert exit_code == agent.EXIT_INVALID_INPUT
    assert not output_path.exists()


def test_missing_api_key_exits_with_input_error(
    tmp_path: Path, sample_repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("LLM_API_KEY")
    exit_code = run_cli(
        ["--repo", REPOSITORY_URL, "--output", str(tmp_path / "report.json")],
        [],
        sample_repository,
    )
    assert exit_code == agent.EXIT_INVALID_INPUT


def test_llm_failure_exits_nonzero_and_saves_partial_findings(
    tmp_path: Path, sample_repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(agent, "remove_directory", lambda directory: None)
    output_path = tmp_path / "report.json"
    exit_code = run_cli(
        ["--repo", REPOSITORY_URL, "--output", str(output_path)],
        [
            tool_response(("read_file", {"path": "src/app.py"})),
            tool_response(("report_violation", SECRET_FINDING)),
            LLMError("quota exceeded"),
        ],
        sample_repository,
    )
    assert exit_code == agent.EXIT_AUDIT_FAILED
    assert not output_path.exists()
    partial_report = json.loads((tmp_path / "report.partial.json").read_text(encoding="utf-8"))
    assert len(partial_report["violations"]) == 1


def test_clone_failure_exits_nonzero(tmp_path: Path, sample_repository: Path) -> None:
    def failing_clone(repository_url: str, workspace_directory: Path) -> Path:
        raise agent.ToolError("Could not clone: repository not found")

    exit_code = agent.main(
        ["--repo", REPOSITORY_URL, "--output", str(tmp_path / "report.json")],
        lambda settings: ScriptedModel([]),
        failing_clone,
    )
    assert exit_code == agent.EXIT_AUDIT_FAILED


def test_clone_is_removed_after_audit(tmp_path: Path, sample_repository: Path) -> None:
    run_cli(
        ["--repo", REPOSITORY_URL, "--output", str(tmp_path / "report.json")],
        [FINISH, FINISH],
        sample_repository,
    )
    assert not sample_repository.exists()


def test_too_few_steps_is_rejected(tmp_path: Path, sample_repository: Path) -> None:
    exit_code = run_cli(
        ["--repo", REPOSITORY_URL, "--output", str(tmp_path / "r.json"), "--max-steps", "1"],
        [],
        sample_repository,
    )
    assert exit_code == agent.EXIT_INVALID_INPUT
