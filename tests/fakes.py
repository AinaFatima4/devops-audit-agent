"""Test doubles shared by several test modules: a scripted fake LLM and response builders."""

import json
from collections.abc import Callable
from typing import Any

from llm_client import LLMResponse, ToolCall

REPOSITORY_URL = "https://github.com/example/sample"


def tool_response(*calls: tuple[str, dict[str, Any]]) -> LLMResponse:
    tool_calls = []
    for index, (name, arguments) in enumerate(calls):
        tool_calls.append(ToolCall(call_id=f"call_{name}_{index}", name=name, arguments=arguments))
    assistant_message = {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": tool_call.call_id,
                "type": "function",
                "function": {"name": tool_call.name, "arguments": json.dumps(tool_call.arguments)},
            }
            for tool_call in tool_calls
        ],
    }
    return LLMResponse(assistant_message=assistant_message, text="", tool_calls=tool_calls)


def text_response(text: str) -> LLMResponse:
    return LLMResponse(assistant_message={"role": "assistant", "content": text}, text=text)


class ScriptedModel:
    """Fake LLM: each item is a response, an exception, or a function of the conversation."""

    def __init__(self, script: list[LLMResponse | Exception | Callable[[list], LLMResponse]]):
        self.script = list(script)
        self.received_messages: list[list[dict[str, Any]]] = []

    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> LLMResponse:
        self.received_messages.append([dict(message) for message in messages])
        if not self.script:
            return tool_response(("finish_audit", {"summary": "done"}))
        next_item = self.script.pop(0)
        if isinstance(next_item, Exception):
            raise next_item
        if callable(next_item):
            return next_item(messages)
        return next_item


SECRET_FINDING = {
    "rule_id": "SEC-001",
    "category": "security",
    "severity": "critical",
    "file": "src/app.py",
    "line": 3,
    "description": "Hardcoded database password",
    "evidence": "read_file shows DATABASE_PASSWORD assigned a redacted literal",
    "recommendation": "Read the password from an environment variable",
}

FINISH = tool_response(("finish_audit", {"summary": "done"}))
