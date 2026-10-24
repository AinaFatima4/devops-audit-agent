import pytest

from redaction import is_placeholder, redact_line, redact_text


@pytest.mark.parametrize(
    "line",
    [
        'API_KEY = "sk-live-abcdefghij0123456789xyz"',
        "DB_PASSWORD=Sup3rS3cretValue",
        '"client_secret": "a8f5f167f44f4964e6c998dee827110c"',
        "token: 'q9w8e7r6t5y4u3i2'",
    ],
)
def test_redact_line_masks_literal_secret_values(line: str) -> None:
    redacted = redact_line(line)
    assert "<REDACTED" in redacted
    assert "Sup3rS3cretValue" not in redacted
    assert "a8f5f167f44f4964e6c998dee827110c" not in redacted


def test_redact_line_masks_known_token_formats_anywhere() -> None:
    github_token = "ghp_" + "A" * 36
    redacted = redact_line(f"curl -H 'Authorization: {github_token}'")
    assert github_token not in redacted
    assert "<REDACTED_GITHUB_TOKEN>" in redacted


@pytest.mark.parametrize(
    "line",
    [
        "API_KEY=",
        "API_KEY=your-api-key-here",
        "password: changeme",
        "token: ${{ secrets.GITHUB_TOKEN }}",
        "api_key = os.environ['API_KEY']",
        "tokenizer_name = 'bert-base-uncased'",
        "author = 'Aina'",
    ],
)
def test_redact_line_keeps_placeholders_and_non_secrets(line: str) -> None:
    assert redact_line(line) == line


def test_redact_text_masks_private_key_body_and_keeps_line_count() -> None:
    text = (
        "-----BEGIN RSA PRIVATE KEY-----\nMIIEsecretbody\nmoresecret\n-----END RSA PRIVATE KEY-----"
    )
    redacted = redact_text(text)
    assert "MIIEsecretbody" not in redacted
    assert len(redacted.split("\n")) == len(text.split("\n"))


@pytest.mark.parametrize("value", ["", "xxxx", "<your-key>", "$SECRET", "example-value", "TODO"])
def test_is_placeholder_recognizes_placeholders(value: str) -> None:
    assert is_placeholder(value)


def test_is_placeholder_rejects_real_looking_value() -> None:
    assert not is_placeholder("q9w8e7r6t5y4u3i2")
