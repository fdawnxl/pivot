"""Shared, dependency-free data models used across pivot modules."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, TypeAlias

CapabilityKind = Literal["think", "measure", "work"]
ContentPart: TypeAlias = dict[str, Any]
MessageContent: TypeAlias = str | tuple[ContentPart, ...]


def normalize_content(value: Any) -> MessageContent:
    """Validate provider-neutral text or multimodal content parts."""

    if isinstance(value, str):
        return value
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        parts: list[ContentPart] = []
        for part in value:
            if not isinstance(part, Mapping):
                raise TypeError("Message content parts must be objects")
            part_type = part.get("type")
            if not isinstance(part_type, str) or not part_type.strip():
                raise ValueError("Message content parts require a type")
            parts.append(dict(part))
        try:
            json.dumps(parts, ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            raise TypeError("Message content parts must be JSON serializable") from exc
        return tuple(parts)
    raise TypeError("Message content must be text or a sequence of content parts")


@dataclass(frozen=True, slots=True)
class Message:
    """A chat message in the internal provider-neutral representation."""

    role: str
    content: MessageContent | None = None
    name: str | None = None
    tool_calls: tuple["ToolCall", ...] = ()
    tool_call_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {"role": self.role}
        if self.content is not None:
            value["content"] = (
                list(self.content) if isinstance(self.content, tuple) else self.content
            )
        if self.name is not None:
            value["name"] = self.name
        if self.tool_calls:
            value["tool_calls"] = [call.as_dict() for call in self.tool_calls]
        if self.tool_call_id is not None:
            value["tool_call_id"] = self.tool_call_id
        return value


@dataclass(frozen=True, slots=True)
class ToolCall:
    """A model request to invoke a capability."""

    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    call_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        # OpenAI-compatible APIs represent function arguments as a JSON string.
        function: dict[str, Any] = {
            "name": self.name,
            "arguments": json.dumps(self.arguments, ensure_ascii=False),
        }
        return {"id": self.call_id, "type": "function", "function": function}


@dataclass(frozen=True, slots=True)
class ParsedResponse:
    """The user-facing text and capability calls extracted from an LLM response."""

    text: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    raw: Any = None
    content: MessageContent | None = None


@dataclass(frozen=True, slots=True)
class CapabilityDescriptor:
    """Metadata injected into prompts and used for dispatch validation."""

    name: str
    kind: CapabilityKind
    description: str
    parameters: dict[str, Any] = field(default_factory=dict)
    source: str | None = None

    def as_prompt_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "description": self.description,
            "parameters": self.parameters,
        }


@dataclass(frozen=True, slots=True)
class EventDescriptor:
    """A generic observable field with supported dynamic conditions."""

    name: str
    description: str
    field: str
    operators: tuple[str, ...]
    templates: dict[str, str] = field(default_factory=dict)
    timeout_template: str = (
        "Waiting for {condition} timed out after {timeout:g} seconds."
    )
    error_template: str = "Waiting for {condition} failed: {error}."
    source: str | None = None

    def as_prompt_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "field": self.field,
            "operators": list(self.operators),
        }
