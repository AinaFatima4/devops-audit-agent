import pytest

from findings import EvidenceLedger, FindingsRecorder, InvalidViolationError


def build_arguments(**overrides: object) -> dict:
    arguments = {
        "rule_id": "SEC-001",
        "category": "security",
        "severity": "critical",
        "file": "src/app.py",
        "line": 3,
        "description": "Hardcoded database password",
        "evidence": "read_file shows DATABASE_PASSWORD assigned a literal value",
        "recommendation": "Load the password from an environment variable",
    }
    arguments.update(overrides)
    return arguments


@pytest.fixture
def ledger_with_app_file() -> EvidenceLedger:
    ledger = EvidenceLedger()
    ledger.record_tool_output("read_file", "File: src/app.py (lines 1-6 of 6)\n    1 | import os")
    return ledger


def test_ledger_records_read_file_line_range(ledger_with_app_file: EvidenceLedger) -> None:
    assert ledger_with_app_file.has_seen_line("src/app.py", 6)
    assert not ledger_with_app_file.has_seen_line("src/app.py", 7)


def test_ledger_records_search_ruff_list_and_git_log_output() -> None:
    ledger = EvidenceLedger()
    ledger.record_tool_output("search_code", "config/settings.py:12: DEBUG = True")
    ledger.record_tool_output("run_ruff", "src\\main.py:4:1: F401 unused import")
    ledger.record_tool_output("list_files", "2 tracked files under /:\nbuild/out.log (10 bytes)")
    ledger.record_tool_output("git_log", "commit abc | 2026-01-01 | A | msg\n\nD\t.env")
    assert ledger.has_seen_line("config/settings.py", 12)
    assert ledger.has_seen_line("src/main.py", 4)
    assert ledger.has_seen_file("build/out.log")
    assert ledger.has_seen_file(".env")


def test_record_accepts_evidence_backed_violation(ledger_with_app_file: EvidenceLedger) -> None:
    recorder = FindingsRecorder(ledger_with_app_file)
    message = recorder.record(build_arguments())
    assert message.startswith("Recorded finding #1")
    assert recorder.violations[0].to_dict()["line"] == 3


def test_record_rejects_file_never_observed() -> None:
    recorder = FindingsRecorder(EvidenceLedger())
    message = recorder.record(build_arguments(file="secrets.py", line=None))
    assert message.startswith("ERROR")
    assert recorder.violations == []


def test_record_rejects_line_never_observed(ledger_with_app_file: EvidenceLedger) -> None:
    recorder = FindingsRecorder(ledger_with_app_file)
    message = recorder.record(build_arguments(line=40))
    assert "Line 40" in message
    assert recorder.violations == []


def test_record_allows_missing_file_with_null_location() -> None:
    recorder = FindingsRecorder(EvidenceLedger())
    message = recorder.record(
        build_arguments(
            rule_id="TST-001", category="testing", severity="high", file=None, line=None
        )
    )
    assert message.startswith("Recorded")
    assert recorder.violations[0].file is None
    assert recorder.violations[0].line is None


def test_record_rejects_duplicates(ledger_with_app_file: EvidenceLedger) -> None:
    recorder = FindingsRecorder(ledger_with_app_file)
    recorder.record(build_arguments())
    message = recorder.record(build_arguments(description="Same problem described differently"))
    assert message.startswith("Duplicate of finding #1")
    assert len(recorder.violations) == 1


def test_category_follows_rule_prefix(ledger_with_app_file: EvidenceLedger) -> None:
    recorder = FindingsRecorder(ledger_with_app_file)
    recorder.record(build_arguments(category="configuration"))
    assert recorder.violations[0].category == "security"


@pytest.mark.parametrize(
    ("overrides", "expected_fragment"),
    [
        ({"rule_id": "security-1"}, "rule_id"),
        ({"severity": "urgent"}, "severity"),
        ({"description": "   "}, "description"),
        ({"file": None, "line": 3}, "requires 'file'"),
        ({"line": 0}, "positive integer"),
        ({"file": "../outside.py"}, "relative"),
    ],
)
def test_build_violation_rejects_invalid_fields(
    ledger_with_app_file: EvidenceLedger, overrides: dict, expected_fragment: str
) -> None:
    recorder = FindingsRecorder(ledger_with_app_file)
    with pytest.raises(InvalidViolationError, match=expected_fragment):
        recorder.build_violation(build_arguments(**overrides))


def test_text_fields_are_redacted(ledger_with_app_file: EvidenceLedger) -> None:
    recorder = FindingsRecorder(ledger_with_app_file)
    recorder.record(build_arguments(evidence='Found password = "Sup3rS3cretValue" on line 3'))
    assert "Sup3rS3cretValue" not in recorder.violations[0].evidence


def test_sorted_violations_orders_by_severity(ledger_with_app_file: EvidenceLedger) -> None:
    recorder = FindingsRecorder(ledger_with_app_file)
    recorder.record(build_arguments(rule_id="CQ-001", severity="low", file=None, line=None))
    recorder.record(build_arguments())
    severities = [violation.severity for violation in recorder.sorted_violations()]
    assert severities == ["critical", "low"]
