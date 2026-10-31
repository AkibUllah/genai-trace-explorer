"""OpenTelemetry GenAI semantic conventions: classify a span and pull out the fields we index.

Instrumentation libraries are at different points of the (still evolving) conventions, so the
older attribute names are read as fallbacks: ``gen_ai.system`` for ``gen_ai.provider.name`` and
``gen_ai.usage.prompt_tokens`` / ``completion_tokens`` for ``input_tokens`` / ``output_tokens``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

OPERATION_KINDS = {
    "chat": "llm",
    "generate_content": "llm",
    "text_completion": "llm",
    "embeddings": "embeddings",
    "execute_tool": "tool",
    "invoke_agent": "agent",
    "create_agent": "agent",
    "retrieval": "retrieval",
}

# Attributes (and event names) that carry prompts, completions or tool payloads.
CONTENT_ATTRIBUTES = frozenset(
    {
        "gen_ai.input.messages",
        "gen_ai.output.messages",
        "gen_ai.system_instructions",
        "gen_ai.tool.call.arguments",
        "gen_ai.tool.call.result",
        "gen_ai.tool.definitions",
        "gen_ai.prompt",
        "gen_ai.completion",
    }
)
CONTENT_PREFIXES = ("gen_ai.prompt.", "gen_ai.completion.")
CONTENT_EVENTS = frozenset(
    {
        "gen_ai.content.prompt",
        "gen_ai.content.completion",
        "gen_ai.system.message",
        "gen_ai.user.message",
        "gen_ai.assistant.message",
        "gen_ai.tool.message",
        "gen_ai.choice",
        "gen_ai.client.inference.operation.details",
    }
)


def is_content_attribute(key: str) -> bool:
    return key in CONTENT_ATTRIBUTES or key.startswith(CONTENT_PREFIXES)


@dataclass(frozen=True)
class GenAIFields:
    kind: str
    operation: str | None
    provider: str | None
    request_model: str | None
    response_model: str | None
    input_tokens: int | None
    output_tokens: int | None
    tool_name: str | None
    agent_name: str | None
    conversation_id: str | None
    error_type: str | None

    @property
    def model(self) -> str | None:
        return self.response_model or self.request_model


def _str(attrs: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = attrs.get(key)
        if value is not None and value != "":
            return str(value)
    return None


def _int(attrs: dict[str, Any], *keys: str) -> int | None:
    for key in keys:
        value = attrs.get(key)
        if value is None or isinstance(value, bool):
            continue
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if number >= 0:
            return number
    return None


def extract(name: str, attrs: dict[str, Any]) -> GenAIFields:
    operation = _str(attrs, "gen_ai.operation.name")
    kind = OPERATION_KINDS.get(operation or "", "")
    if not kind:
        if "gen_ai.tool.name" in attrs:
            kind = "tool"
        elif "gen_ai.request.model" in attrs or "gen_ai.response.model" in attrs:
            kind = "llm"
        elif "gen_ai.agent.name" in attrs:
            kind = "agent"
        else:
            kind = "other"
    return GenAIFields(
        kind=kind,
        operation=operation,
        provider=_str(attrs, "gen_ai.provider.name", "gen_ai.system"),
        request_model=_str(attrs, "gen_ai.request.model"),
        response_model=_str(attrs, "gen_ai.response.model"),
        input_tokens=_int(attrs, "gen_ai.usage.input_tokens", "gen_ai.usage.prompt_tokens"),
        output_tokens=_int(attrs, "gen_ai.usage.output_tokens", "gen_ai.usage.completion_tokens"),
        tool_name=_str(attrs, "gen_ai.tool.name"),
        agent_name=_str(attrs, "gen_ai.agent.name"),
        conversation_id=_str(attrs, "gen_ai.conversation.id", "session.id"),
        error_type=_str(attrs, "error.type"),
    )
