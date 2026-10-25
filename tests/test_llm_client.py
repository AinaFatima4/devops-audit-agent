import httpx2
import openai
import pytest
from openai.types.chat import ChatCompletion

from config import Settings
from llm_client import (
    LLMError,
    MalformedToolCallError,
    OpenAICompatibleClient,
    parse_completion,
    parse_tool_arguments,
)
from tool_schemas import ALL_TOOL_SCHEMAS, REPORT_VIOLATION_SCHEMA, SEVERITIES


def build_completion(message: dict, finish_reason: str = "tool_calls") -> ChatCompletion:
    return ChatCompletion.model_validate(
        {
            "id": "completion-1",
            "object": "chat.completion",
            "created": 0,
            "model": "test-model",
            "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
    )


def test_parse_completion_extracts_tool_calls() -> None:
    completion = build_completion(
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_abc",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path": "README.md"}'},
                }
            ],
        }
    )
    response = parse_completion(completion)
    assert response.tool_calls[0].name == "read_file"
    assert response.tool_calls[0].arguments == {"path": "README.md"}
    assert response.tool_calls[0].call_id == "call_abc"
    assert response.total_tokens == 15


def test_parse_completion_keeps_provider_specific_fields() -> None:
    completion = build_completion(
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_abc",
                    "type": "function",
                    "function": {"name": "list_files", "arguments": "{}"},
                    "extra_content": {"google": {"thought_signature": "signature-123"}},
                }
            ],
        }
    )
    response = parse_completion(completion)
    echoed_call = response.assistant_message["tool_calls"][0]
    assert echoed_call["extra_content"]["google"]["thought_signature"] == "signature-123"


def test_parse_completion_fills_missing_call_id() -> None:
    completion = build_completion(
        {
            "role": "assistant",
            "tool_calls": [
                {"id": "", "type": "function", "function": {"name": "git_log", "arguments": ""}}
            ],
        }
    )
    response = parse_completion(completion)
    assert response.tool_calls[0].call_id == "call_0"
    assert response.assistant_message["tool_calls"][0]["id"] == "call_0"
    assert response.tool_calls[0].arguments == {}


def test_parse_completion_returns_plain_text() -> None:
    completion = build_completion({"role": "assistant", "content": "Done."}, "stop")
    response = parse_completion(completion)
    assert response.text == "Done."
    assert response.tool_calls == []


def test_parse_completion_rejects_empty_choices() -> None:
    completion = build_completion({"role": "assistant", "content": "x"})
    completion.choices = []
    with pytest.raises(LLMError):
        parse_completion(completion)


@pytest.mark.parametrize(
    ("raw_arguments", "expected_error_fragment"),
    [("{not json", "not valid JSON"), ("[1, 2]", "JSON object")],
)
def test_parse_tool_arguments_reports_bad_arguments(
    raw_arguments: str, expected_error_fragment: str
) -> None:
    arguments, error_message = parse_tool_arguments(raw_arguments)
    assert arguments == {}
    assert expected_error_fragment in error_message


def test_tool_schemas_have_unique_names_and_required_fields() -> None:
    names = []
    for schema in ALL_TOOL_SCHEMAS:
        function_definition = schema["function"]
        names.append(function_definition["name"])
        for required_name in function_definition["parameters"]["required"]:
            assert required_name in function_definition["parameters"]["properties"]
    assert len(names) == len(set(names))
    assert {"list_files", "read_file", "search_code", "git_log"}.issubset(names)


def test_report_violation_schema_restricts_severity() -> None:
    properties = REPORT_VIOLATION_SCHEMA["function"]["parameters"]["properties"]
    assert properties["severity"]["enum"] == SEVERITIES


def test_groq_output_parse_failure_is_retryable(monkeypatch: pytest.MonkeyPatch) -> None:
    client = OpenAICompatibleClient(Settings("key", "https://example.invalid/v1", "model", 5, 0))
    request = httpx2.Request("POST", "https://example.invalid/v1/chat/completions")
    response = httpx2.Response(400, request=request)
    error = openai.BadRequestError(
        "Error code: 400 - {'code': 'output_parse_failed'}", response=response, body=None
    )

    def raise_error(**keyword_arguments: object) -> None:
        raise error

    monkeypatch.setattr(client.client.chat.completions, "create", raise_error)
    with pytest.raises(MalformedToolCallError):
        client.chat([{"role": "user", "content": "hi"}], [])
