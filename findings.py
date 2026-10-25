"""Validates, de-duplicates and stores the violations the LLM reports."""

import re
from dataclasses import asdict, dataclass, field
from pathlib import PurePosixPath
from typing import Any

from redaction import redact_text
from tool_schemas import SEVERITIES, VIOLATION_CATEGORIES

RULE_ID_PATTERN = re.compile(r"^[A-Z]{2,4}-\d{3}$")
MAX_TEXT_FIELD_CHARACTERS = 600
SEVERITY_ORDER = {severity: rank for rank, severity in enumerate(SEVERITIES)}
CATEGORY_BY_RULE_PREFIX = {
    "HYG": "repository_hygiene",
    "DEP": "dependency_management",
    "CQ": "code_quality",
    "TST": "testing",
    "CI": "ci",
    "SEC": "security",
    "CFG": "configuration",
    "COL": "collaboration",
    "BLD": "build_release",
}

READ_FILE_HEADER_PATTERN = re.compile(
    r"^File: (?P<path>.+) \(lines (?P<first>\d+)-(?P<last>\d+) of"
)
SEARCH_RESULT_PATTERN = re.compile(r"^(?P<path>[^:\n]+):(?P<line>\d+): ")
RUFF_RESULT_PATTERN = re.compile(r"^(?P<path>[^:\n]+):(?P<line>\d+):\d+: ")
LIST_FILES_PATTERN = re.compile(r"^(?P<path>\S.*?) \((?:\d+ bytes|not present in working tree)\)$")
GIT_LOG_FILE_PATTERN = re.compile(r"^[AMDRCT]\d*\t(?P<paths>.+)$")


class InvalidViolationError(ValueError):
    """Raised when a reported violation is incomplete or not backed by observed evidence."""


def normalize_path(raw_path: str) -> str:
    cleaned_path = raw_path.strip().replace("\\", "/")
    while cleaned_path.startswith("./"):
        cleaned_path = cleaned_path[2:]
    return cleaned_path.strip("/")


@dataclass
class EvidenceLedger:
    """Remembers which files and lines the agent has actually seen through its tools."""

    observed_files: set[str] = field(default_factory=set)
    observed_lines: dict[str, set[int]] = field(default_factory=dict)

    def _add_line(self, file_path: str, line_number: int) -> None:
        normalized_path = normalize_path(file_path)
        self.observed_files.add(normalized_path)
        self.observed_lines.setdefault(normalized_path, set()).add(line_number)

    def record_tool_output(self, tool_name: str, tool_output: str) -> None:
        output_lines = tool_output.split("\n")
        if tool_name == "read_file":
            header_match = READ_FILE_HEADER_PATTERN.match(output_lines[0])
            if header_match:
                first_line = int(header_match.group("first"))
                last_line = int(header_match.group("last"))
                for line_number in range(first_line, last_line + 1):
                    self._add_line(header_match.group("path"), line_number)
            return
        for output_line in output_lines:
            if tool_name == "search_code":
                search_match = SEARCH_RESULT_PATTERN.match(output_line)
                if search_match:
                    self._add_line(search_match.group("path"), int(search_match.group("line")))
            elif tool_name == "run_ruff":
                ruff_match = RUFF_RESULT_PATTERN.match(output_line)
                if ruff_match:
                    self._add_line(ruff_match.group("path"), int(ruff_match.group("line")))
            elif tool_name == "list_files":
                list_match = LIST_FILES_PATTERN.match(output_line)
                if list_match:
                    self.observed_files.add(normalize_path(list_match.group("path")))
            elif tool_name == "git_log":
                log_match = GIT_LOG_FILE_PATTERN.match(output_line)
                if log_match:
                    for file_path in log_match.group("paths").split("\t"):
                        self.observed_files.add(normalize_path(file_path))

    def has_seen_file(self, file_path: str) -> bool:
        return normalize_path(file_path) in self.observed_files

    def has_seen_line(self, file_path: str, line_number: int) -> bool:
        return line_number in self.observed_lines.get(normalize_path(file_path), set())


@dataclass
class Violation:
    rule_id: str
    category: str
    severity: str
    file: str | None
    line: int | None
    description: str
    evidence: str
    recommendation: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _clean_text_field(arguments: dict[str, Any], field_name: str) -> str:
    raw_value = arguments.get(field_name)
    if not isinstance(raw_value, str) or raw_value.strip() == "":
        raise InvalidViolationError(f"'{field_name}' must be a non-empty string")
    cleaned_value = redact_text(" ".join(raw_value.split()))
    return cleaned_value[:MAX_TEXT_FIELD_CHARACTERS]


def _clean_optional_file(arguments: dict[str, Any]) -> str | None:
    raw_file = arguments.get("file")
    if raw_file is None:
        return None
    if not isinstance(raw_file, str):
        raise InvalidViolationError("'file' must be a string path or omitted")
    normalized_file = normalize_path(raw_file)
    if normalized_file.lower() in ("", "null", "none", "n/a"):
        return None
    if PurePosixPath(normalized_file).is_absolute() or ".." in PurePosixPath(normalized_file).parts:
        raise InvalidViolationError("'file' must be relative to the repository root")
    return normalized_file


def _clean_optional_line(arguments: dict[str, Any]) -> int | None:
    raw_line = arguments.get("line")
    if raw_line is None or raw_line == "":
        return None
    if isinstance(raw_line, bool):
        raise InvalidViolationError("'line' must be a positive integer or omitted")
    try:
        line_number = int(raw_line)
    except (TypeError, ValueError) as conversion_error:
        raise InvalidViolationError(
            "'line' must be a positive integer or omitted"
        ) from conversion_error
    if line_number < 1:
        raise InvalidViolationError("'line' must be a positive integer or omitted")
    return line_number


class FindingsRecorder:
    """Accepts report_violation calls only when they are complete and backed by evidence."""

    def __init__(self, evidence_ledger: EvidenceLedger) -> None:
        self.evidence_ledger = evidence_ledger
        self.violations: list[Violation] = []

    def build_violation(self, arguments: dict[str, Any]) -> Violation:
        rule_id = str(arguments.get("rule_id", "")).strip().upper()
        if not RULE_ID_PATTERN.match(rule_id):
            raise InvalidViolationError("'rule_id' must look like SEC-001")
        severity = str(arguments.get("severity", "")).strip().lower()
        if severity not in SEVERITIES:
            raise InvalidViolationError(f"'severity' must be one of {SEVERITIES}")
        rule_prefix = rule_id.split("-")[0]
        category = CATEGORY_BY_RULE_PREFIX.get(rule_prefix)
        if category is None:
            category = str(arguments.get("category", "")).strip().lower()
        if category not in VIOLATION_CATEGORIES:
            raise InvalidViolationError(f"'category' must be one of {VIOLATION_CATEGORIES}")
        file_path = _clean_optional_file(arguments)
        line_number = _clean_optional_line(arguments)
        if file_path is None and line_number is not None:
            raise InvalidViolationError("'line' requires 'file'")
        if file_path is not None and not self.evidence_ledger.has_seen_file(file_path):
            raise InvalidViolationError(
                f"You have not observed '{file_path}' with any tool. Inspect it first, "
                "or omit 'file' if the violation is about something missing."
            )
        if line_number is not None and not self.evidence_ledger.has_seen_line(
            file_path, line_number
        ):
            raise InvalidViolationError(
                f"Line {line_number} of '{file_path}' has not been shown by read_file, "
                "search_code or run_ruff. Read that line first or omit 'line'."
            )
        return Violation(
            rule_id=rule_id,
            category=category,
            severity=severity,
            file=file_path,
            line=line_number,
            description=_clean_text_field(arguments, "description"),
            evidence=_clean_text_field(arguments, "evidence"),
            recommendation=_clean_text_field(arguments, "recommendation"),
        )

    def find_duplicate(self, new_violation: Violation) -> int | None:
        """Same rule on the same file (unless both name different lines), or same description."""
        new_description = new_violation.description.lower()
        for finding_number, existing in enumerate(self.violations, start=1):
            if existing.rule_id != new_violation.rule_id:
                continue
            if existing.description.lower() == new_description:
                return finding_number
            if existing.file != new_violation.file:
                continue
            if existing.line is None or new_violation.line is None:
                return finding_number
            if existing.line == new_violation.line:
                return finding_number
        return None

    def record(self, arguments: dict[str, Any]) -> str:
        """Validate and store one violation; return a message for the LLM either way."""
        try:
            violation = self.build_violation(arguments)
        except InvalidViolationError as validation_error:
            return f"ERROR: violation not recorded. {validation_error}"
        existing_number = self.find_duplicate(violation)
        if existing_number is not None:
            return f"Duplicate of finding #{existing_number}; not recorded."
        self.violations.append(violation)
        finding_number = len(self.violations)
        location = violation.file or "repository"
        if violation.line is not None:
            location += f":{violation.line}"
        return f"Recorded finding #{finding_number}: {violation.rule_id} at {location}."

    def sorted_violations(self) -> list[Violation]:
        return sorted(
            self.violations,
            key=lambda violation: (
                SEVERITY_ORDER[violation.severity],
                violation.file or "",
                violation.line or 0,
            ),
        )
