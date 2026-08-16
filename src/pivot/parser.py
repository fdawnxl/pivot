"""Parse LiteLLM/OpenAI-compatible responses into pivot models."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import Any

from .models import MessageContent, ParsedResponse, ToolCall, normalize_content

LOGGER = logging.getLogger(__name__)


class ResponseParseError(ValueError):
    """Raised when an LLM response does not contain a usable message."""


def _value(source: Any, key: str, default: Any = None) -> Any:
    if isinstance(source, Mapping):
        return source.get(key, default)
    return getattr(source, key, default)


def _arguments(value: Any, *, name: str) -> dict[str, Any]:
    if value in (None, ""):
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ResponseParseError(
                f"Invalid JSON arguments for capability {name!r}: {exc.msg}"
            ) from exc
        if isinstance(parsed, Mapping):
            return dict(parsed)
    raise ResponseParseError(f"Arguments for capability {name!r} must be a JSON object")


def parse_response(response: Any) -> ParsedResponse:
    """Extract assistant text and all tool calls from a provider response."""

    choices = _value(response, "choices")
    if not choices:
        raise ResponseParseError("LLM response contains no choices")
    message = _value(choices[0], "message")
    if message is None:
        raise ResponseParseError("LLM response choice contains no message")
    content = _value(message, "content", "")
    if content is None:
        text = ""
    elif isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text = "".join(
            str(_value(part, "text", ""))
            for part in content
            if _value(part, "type", "text") == "text"
        )
    else:
        text = str(content)

    parsed_calls: list[ToolCall] = []
    for raw_call in _value(message, "tool_calls", ()) or ():
        function = _value(raw_call, "function", raw_call)
        name = _value(function, "name")
        if not isinstance(name, str) or not name.strip():
            raise ResponseParseError("Capability call is missing a valid function name")
        parsed_calls.append(
            ToolCall(
                name=name,
                arguments=_arguments(_value(function, "arguments"), name=name),
                call_id=_value(raw_call, "id"),
            )
        )
    normalized_source = (
        content
        if isinstance(content, (str, list, tuple))
        else ("" if content is None else str(content))
    )
    try:
        normalized_content: MessageContent = normalize_content(normalized_source)
    except (TypeError, ValueError) as exc:
        raise ResponseParseError(f"LLM response content is invalid: {exc}") from exc
    LOGGER.debug(
        "LLM response parsed text_length=%d tool_calls=%d multimodal=%s",
        len(text),
        len(parsed_calls),
        isinstance(normalized_content, tuple),
    )
    return ParsedResponse(
        text=text,
        content=normalized_content,
        tool_calls=tuple(parsed_calls),
        raw=response,
    )
