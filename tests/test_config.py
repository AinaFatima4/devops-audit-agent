import pytest

from config import DEFAULT_BASE_URL, DEFAULT_MODEL, ConfigurationError, load_settings


@pytest.fixture(autouse=True)
def clear_llm_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for variable_name in [
        "LLM_API_KEY",
        "LLM_BASE_URL",
        "LLM_MODEL",
        "LLM_REQUEST_TIMEOUT_SECONDS",
        "LLM_MAX_RETRIES",
    ]:
        monkeypatch.delenv(variable_name, raising=False)


def test_load_settings_requires_api_key() -> None:
    with pytest.raises(ConfigurationError, match="LLM_API_KEY"):
        load_settings(env_file=None)


def test_load_settings_uses_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    settings = load_settings(env_file=None)
    assert settings.base_url == DEFAULT_BASE_URL
    assert settings.model == DEFAULT_MODEL


def test_load_settings_reads_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("LLM_BASE_URL", "https://api.groq.com/openai/v1")
    monkeypatch.setenv("LLM_MODEL", "llama-3.3-70b-versatile")
    monkeypatch.setenv("LLM_MAX_RETRIES", "2")
    settings = load_settings(env_file=None)
    assert settings.base_url == "https://api.groq.com/openai/v1"
    assert settings.model == "llama-3.3-70b-versatile"
    assert settings.max_retries == 2


def test_load_settings_rejects_non_numeric_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("LLM_REQUEST_TIMEOUT_SECONDS", "soon")
    with pytest.raises(ConfigurationError, match="must be a number"):
        load_settings(env_file=None)
