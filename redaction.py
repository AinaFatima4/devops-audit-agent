"""Masks secret values so they never reach logs, the LLM, or the final report."""

import re

PLACEHOLDER_PATTERN = re.compile(
    r"^("
    r"|x+|\*+|\.+|-+"
    r"|changeme|change_me|change-me|replace_me|replaceme"
    r"|example.*|sample.*|dummy.*|fake.*|placeholder.*|test.*|todo|tbd"
    r"|your[_-].*|my[_-].*|insert[_-].*"
    r"|none|null|nil|true|false|redacted.*"
    r"|<.*>|\$.*|\{\{.*|%\(.*\)s"
    r")$",
    re.IGNORECASE,
)

KNOWN_TOKEN_PATTERNS = [
    ("AWS_ACCESS_KEY", re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("GITHUB_TOKEN", re.compile(r"\b(ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,}\b")),
    ("GITHUB_FINE_GRAINED_TOKEN", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{40,}\b")),
    ("STRIPE_KEY", re.compile(r"\b(sk|rk|pk)_(live|test)_[A-Za-z0-9]{16,}\b")),
    ("OPENAI_STYLE_KEY", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("GOOGLE_API_KEY", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("SLACK_TOKEN", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
]

SECRET_ASSIGNMENT_PATTERN = re.compile(
    r"(?P<name>[A-Za-z0-9_.-]*"
    r"(?:password|passwd|secret|token|api[_-]?key|apikey|access[_-]?key|private[_-]?key)"
    r"(?![A-Za-z])[A-Za-z0-9_.-]*)"
    r"(?P<name_quote>[\"']?)"
    r"(?P<separator>\s*(?:=>|:|=)\s*)"
    r"(?P<quote>[\"']?)"
    r"(?P<value>[^\s\"',;#]+)",
    re.IGNORECASE,
)

PRIVATE_KEY_BEGIN_PATTERN = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
PRIVATE_KEY_END_PATTERN = re.compile(r"-----END [A-Z ]*PRIVATE KEY-----")


def is_placeholder(value: str) -> bool:
    """Return True when a value is clearly a placeholder rather than a real secret."""
    return PLACEHOLDER_PATTERN.match(value) is not None


def _looks_like_literal_secret(value: str, is_quoted: bool) -> bool:
    if is_placeholder(value):
        return False
    if is_quoted:
        return True
    has_letter = re.search(r"[A-Za-z]", value) is not None
    has_digit = re.search(r"[0-9]", value) is not None
    looks_like_code = re.search(r"[(\[{.]", value) is not None
    return has_letter and has_digit and not looks_like_code and len(value) >= 8


def _redact_assignment(match: re.Match) -> str:
    value = match.group("value")
    is_quoted = match.group("quote") != ""
    if not _looks_like_literal_secret(value, is_quoted):
        return match.group(0)
    masked_value = f"<REDACTED_VALUE length={len(value)}>"
    return (
        match.group("name")
        + match.group("name_quote")
        + match.group("separator")
        + match.group("quote")
        + masked_value
    )


def redact_line(line: str) -> str:
    """Mask known token formats and literal values assigned to secret-like names."""
    redacted_line = line
    for token_type, token_pattern in KNOWN_TOKEN_PATTERNS:
        redacted_line = token_pattern.sub(f"<REDACTED_{token_type}>", redacted_line)
    redacted_line = SECRET_ASSIGNMENT_PATTERN.sub(_redact_assignment, redacted_line)
    return redacted_line


def redact_text(text: str) -> str:
    """Redact a block of text line by line, keeping the number of lines unchanged."""
    output_lines = []
    inside_private_key = False
    for line in text.split("\n"):
        if inside_private_key:
            if PRIVATE_KEY_END_PATTERN.search(line):
                inside_private_key = False
                output_lines.append(line)
            else:
                output_lines.append("<REDACTED_PRIVATE_KEY_CONTENT>")
            continue
        if PRIVATE_KEY_BEGIN_PATTERN.search(line):
            inside_private_key = PRIVATE_KEY_END_PATTERN.search(line) is None
            output_lines.append(line)
            continue
        output_lines.append(redact_line(line))
    return "\n".join(output_lines)
