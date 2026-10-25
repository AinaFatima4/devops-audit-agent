"""Thin wrapper around any OpenAI-compatible chat completions API (Gemini, Groq, Ollama...)."""

import json
from dataclasses import dataclass, field
from typing import Any, Protocol

import openai
from openai import OpenAI

from config import Settings


class LLMError(Exception):
    """Raised when the LLM provider cannot return a usable response."""


@dataclass
class ToolCall:
    call_id: str
    name: str
    arguments: dict[str, Any]
    arguments_error: str | None = None


@dataclass
class LLMResponse:
    assistant_message: dict[str, Any]
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str | None = None
    total_tokens: int | None = None


class ChatModel(Protocol):
    """Anything with this method can drive the agent, including fake models in tests."""

    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> LLMResponse: ...


def parse_tool_arguments(raw_arguments: str | None) -> tuple[dict[str, Any], str | None]:
    """Turn the model's JSON argument string into a dict, or explain why that failed."""
    if raw_arguments is None or raw_arguments.strip() == "":
        return {}, None
    try:
        parsed_arguments = json.loads(raw_arguments)
    except json.JSONDecodeError as decode_error:
        return {}, f"Arguments were not valid JSON: {decode_error}"
    if not isinstance(parsed_arguments, dict):
        return {}, "Arguments must be a JSON object"
    return parsed_arguments, None


def parse_completion(completion: Any) -> LLMResponse:
    """Convert an SDK ChatCompletion object into our provider-neutral LLMResponse."""
    if not completion.choices:
        raise LLMError("The model returned no choices")
    choice = completion.choices[0]
    message = choice.message
    # Keep every field the provider sent (e.g. Gemini thought signatures) so it can be echoed back
    assistant_message = message.model_dump(exclude_none=True)
    assistant_message["role"] = "assistant"
    tool_calls = []
    for index, raw_tool_call in enumerate(message.tool_calls or []):
        arguments, arguments_error = parse_tool_arguments(raw_tool_call.function.arguments)
        call_id = raw_tool_call.id or f"call_{index}"
        tool_calls.append(
            ToolCall(
                call_id=call_id,
                name=raw_tool_call.function.name,
                arguments=arguments,
                arguments_error=arguments_error,
            )
        )
    if tool_calls:
        for index, tool_call in enumerate(tool_calls):
            assistant_message["tool_calls"][index]["id"] = tool_call.call_id
    total_tokens = completion.usage.total_tokens if completion.usage else None
    return LLMResponse(
        assistant_message=assistant_message,
        text=message.content or "",
        tool_calls=tool_calls,
        finish_reason=choice.finish_reason,
        total_tokens=total_tokens,
    )


class OpenAICompatibleClient:
    """Sends chat requests with tool definitions; the SDK retries rate limits and 5xx errors."""

    def __init__(self, settings: Settings) -> None:
        self.model = settings.model
        self.client = OpenAI(
            api_key=settings.api_key,
            base_url=settings.base_url,
            timeout=settings.request_timeout_seconds,
            max_retries=settings.max_retries,
        )

    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> LLMResponse:
        try:
            completion = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                tools=tools,
                tool_choice="auto",
                temperature=0.1,
            )
        except openai.AuthenticationError as auth_error:
            raise LLMError(
                "The LLM provider rejected the API key (check LLM_API_KEY)"
            ) from auth_error
        except openai.NotFoundError as not_found_error:
            raise LLMError(
                f"Model {self.model!r} or base URL not found (check LLM_MODEL and LLM_BASE_URL)"
            ) from not_found_error
        except openai.RateLimitError as rate_limit_error:
            raise LLMError(
                "Rate limit or quota exceeded after retries; wait or switch provider"
            ) from rate_limit_error
        except openai.APITimeoutError as timeout_error:
            raise LLMError("The LLM request timed out after retries") from timeout_error
        except openai.APIConnectionError as connection_error:
            raise LLMError("Could not connect to the LLM provider") from connection_error
        except openai.APIStatusError as status_error:
            raise LLMError(
                f"LLM provider returned HTTP {status_error.status_code}"
            ) from status_error
        return parse_completion(completion)


if __name__ == "__main__":
    from config import load_settings
    from tool_schemas import LIST_FILES_SCHEMA

    client = OpenAICompatibleClient(load_settings())
    test_messages = [
        {"role": "system", "content": "You inspect code repositories using the provided tools."},
        {"role": "user", "content": "Start by looking at the repository structure."},
    ]
    response = client.chat(test_messages, [LIST_FILES_SCHEMA])
    print(f"Model: {client.model}")
    print(f"Text: {response.text!r}")
    for tool_call in response.tool_calls:
        print(f"Tool call: {tool_call.name}({tool_call.arguments})")
    print(f"Tokens used: {response.total_tokens}")
