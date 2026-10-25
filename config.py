"""Loads agent settings from environment variables (and a local .env file if present)."""

import os
from dataclasses import dataclass

from dotenv import load_dotenv

DEFAULT_BASE_URL = "https://api.groq.com/openai/v1"
DEFAULT_MODEL = "openai/gpt-oss-120b"
DEFAULT_REQUEST_TIMEOUT_SECONDS = 120.0
DEFAULT_MAX_RETRIES = 5
ALLOWED_REASONING_EFFORTS = ("low", "medium", "high")


class ConfigurationError(Exception):
    """Raised when a required setting is missing or invalid."""


@dataclass(frozen=True)
class Settings:
    api_key: str
    base_url: str
    model: str
    request_timeout_seconds: float
    max_retries: int
    reasoning_effort: str | None = None


def _read_number(variable_name: str, default_value: float) -> float:
    raw_value = os.environ.get(variable_name, "").strip()
    if raw_value == "":
        return default_value
    try:
        return float(raw_value)
    except ValueError as conversion_error:
        raise ConfigurationError(
            f"{variable_name} must be a number, got {raw_value!r}"
        ) from conversion_error


def load_settings(env_file: str | None = ".env") -> Settings:
    """Read settings, preferring real environment variables over values in the env file."""
    if env_file is not None:
        load_dotenv(env_file, override=False)
    api_key = os.environ.get("LLM_API_KEY", "").strip()
    if api_key == "":
        raise ConfigurationError(
            "LLM_API_KEY is not set. Copy .env.example to .env and add your API key."
        )
    reasoning_effort = os.environ.get("LLM_REASONING_EFFORT", "").strip().lower() or None
    if reasoning_effort is not None and reasoning_effort not in ALLOWED_REASONING_EFFORTS:
        raise ConfigurationError(
            f"LLM_REASONING_EFFORT must be one of {ALLOWED_REASONING_EFFORTS}, "
            f"got {reasoning_effort!r}"
        )
    return Settings(
        api_key=api_key,
        base_url=os.environ.get("LLM_BASE_URL", "").strip() or DEFAULT_BASE_URL,
        model=os.environ.get("LLM_MODEL", "").strip() or DEFAULT_MODEL,
        request_timeout_seconds=_read_number(
            "LLM_REQUEST_TIMEOUT_SECONDS", DEFAULT_REQUEST_TIMEOUT_SECONDS
        ),
        max_retries=int(_read_number("LLM_MAX_RETRIES", DEFAULT_MAX_RETRIES)),
        reasoning_effort=reasoning_effort,
    )
