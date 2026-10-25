"""JSON Schema descriptions of every tool, in the format the chat completions API expects."""

VIOLATION_CATEGORIES = [
    "repository_hygiene",
    "dependency_management",
    "code_quality",
    "testing",
    "ci",
    "security",
    "configuration",
    "collaboration",
    "build_release",
]

SEVERITIES = ["critical", "high", "medium", "low"]


def _function_schema(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
    }


LIST_FILES_SCHEMA = _function_schema(
    name="list_files",
    description=(
        "List files tracked by git, with sizes in bytes. Call with no directory first to see "
        "the whole repository structure, then with a subdirectory for large repositories."
    ),
    properties={
        "directory": {
            "type": "string",
            "description": "Directory relative to the repository root. Empty string for root.",
        },
    },
    required=[],
)

READ_FILE_SCHEMA = _function_schema(
    name="read_file",
    description=(
        "Read a text file with line numbers. Secret values appear as <REDACTED_...> markers; "
        "a marker means a literal value was present. Long files are returned in pages."
    ),
    properties={
        "path": {"type": "string", "description": "File path relative to the repository root."},
        "start_line": {"type": "integer", "description": "First line to return (default 1)."},
        "max_lines": {"type": "integer", "description": "Number of lines (default 300)."},
    },
    required=["path"],
)

SEARCH_CODE_SCHEMA = _function_schema(
    name="search_code",
    description=(
        "Search the CONTENTS of tracked text files with a Python regular expression. Returns "
        "file:line: snippet. It does not match file names: use list_files to check whether a "
        "file exists. Use it to find suspicious patterns or confirm an alternative is configured."
    ),
    properties={
        "pattern": {"type": "string", "description": "Python regular expression."},
        "file_glob": {
            "type": "string",
            "description": "Optional filter such as '*.py' or '.github/workflows/*'.",
        },
        "case_sensitive": {"type": "boolean", "description": "Default false."},
        "max_results": {"type": "integer", "description": "Default 80."},
    },
    required=["pattern"],
)

GIT_LOG_SCHEMA = _function_schema(
    name="git_log",
    description=(
        "Show recent commits with files added (A), modified (M) or deleted (D). Pass a path to "
        "see the history of one file, for example to check whether a secret file was committed."
    ),
    properties={
        "max_commits": {"type": "integer", "description": "Default 30, maximum 100."},
        "path": {"type": "string", "description": "Optional file path to filter history."},
    },
    required=[],
)

RUN_RUFF_SCHEMA = _function_schema(
    name="run_ruff",
    description=(
        "Run the Ruff Python linter statically using the repository's own configuration. "
        "Never executes repository code. Only useful when Python files exist."
    ),
    properties={
        "path": {"type": "string", "description": "Optional file or directory to lint."},
    },
    required=[],
)

REPORT_VIOLATION_SCHEMA = _function_schema(
    name="report_violation",
    description=(
        "Record one confirmed DevOps violation. Only call this after inspecting the evidence "
        "with another tool. Never include secret values in any field."
    ),
    properties={
        "rule_id": {
            "type": "string",
            "description": "Rule identifier from the catalogue in your instructions, e.g. SEC-001.",
        },
        "category": {"type": "string", "enum": VIOLATION_CATEGORIES},
        "severity": {"type": "string", "enum": SEVERITIES},
        "file": {
            "type": "string",
            "description": "File path relative to the root. Omit when the issue has no file.",
        },
        "line": {
            "type": "integer",
            "description": "1-based line number. Omit when the issue has no specific line.",
        },
        "description": {"type": "string", "description": "One sentence naming the problem."},
        "evidence": {
            "type": "string",
            "description": "What you observed and which tool showed it, without secret values.",
        },
        "recommendation": {"type": "string", "description": "A concrete, actionable fix."},
    },
    required=["rule_id", "category", "severity", "description", "evidence", "recommendation"],
)

FINISH_AUDIT_SCHEMA = _function_schema(
    name="finish_audit",
    description="Call once when every relevant inspection area has been investigated.",
    properties={
        "summary": {"type": "string", "description": "Short summary of what was inspected."},
    },
    required=["summary"],
)

INSPECTION_TOOL_SCHEMAS = [
    LIST_FILES_SCHEMA,
    READ_FILE_SCHEMA,
    SEARCH_CODE_SCHEMA,
    GIT_LOG_SCHEMA,
    RUN_RUFF_SCHEMA,
]

ALL_TOOL_SCHEMAS = INSPECTION_TOOL_SCHEMAS + [REPORT_VIOLATION_SCHEMA, FINISH_AUDIT_SCHEMA]
