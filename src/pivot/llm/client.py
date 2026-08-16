"""Provider-neutral LLM interface backed by LiteLLM."""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Sequence
from typing import Any, Protocol

from ..logging import configure_dependency_logging
from ..models import Message

LOGGER = logging.getLogger(__name__)


class LLMError(RuntimeError):
    """A sanitized error raised by an LLM adapter."""


class LLMClient(Protocol):
    def complete(
        self, messages: Sequence[Message], *, tools: Sequence[dict[str, Any]] = ()
    ) -> Any:
        """Return a provider response for the supplied message sequence."""


class LiteLLMClient:
    """Thin LiteLLM wrapper with lazy importing and injectable completion."""

    def __init__(
        self,
        model: str,
        *,
        api_base: str | None = None,
        api_key: str | None = None,
        timeout: float = 120.0,
        completion: Callable[..., Any] | None = None,
    ) -> None:
        self.model = model
        self.api_base = api_base
        self.api_key = api_key
        self.timeout = timeout
        self._completion = completion

    def complete(
        self, messages: Sequence[Message], *, tools: Sequence[dict[str, Any]] = ()
    ) -> Any:
        completion = self._completion
        if completion is None:
            configure_dependency_logging()
            previous_log_level = os.environ.get("LITELLM_LOG")
            os.environ["LITELLM_LOG"] = "ERROR"
            try:
                from litellm import completion as litellm_completion
            except ImportError as exc:
                raise LLMError("LiteLLM is not installed; run 'uv sync'") from exc
            finally:
                if previous_log_level is None:
                    os.environ.pop("LITELLM_LOG", None)
                else:
                    os.environ["LITELLM_LOG"] = previous_log_level
            configure_dependency_logging()
            completion = litellm_completion
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": _openai_compatible_messages(messages),
            "timeout": self.timeout,
        }
        if self.api_base:
            kwargs["api_base"] = self.api_base
        if self.api_key:
            kwargs["api_key"] = self.api_key
        if tools:
            kwargs["tools"] = list(tools)
        LOGGER.info(
            "LLM request started model=%s messages=%d tools=%d",
            self.model,
            len(messages),
            len(tools),
        )
        LOGGER.debug(
            "LLM request routing api_base_configured=%s timeout=%g roles=%s",
            self.api_base is not None,
            self.timeout,
            [message.role for message in messages],
        )
        try:
            response = completion(**kwargs)
        except Exception as exc:
            LOGGER.error(
                "LLM request failed model=%s error_type=%s",
                self.model,
                type(exc).__name__,
            )
            raise LLMError(f"LLM request failed: {type(exc).__name__}") from exc
        LOGGER.info("LLM request completed model=%s", self.model)
        return response


def _openai_compatible_messages(messages: Sequence[Message]) -> list[dict[str, Any]]:
    """Move media from tool results to a following role that accepts it.

    OpenAI-compatible chat APIs only guarantee text content on ``tool``
    messages. Keep the linked tool response, then expose returned media in a
    synthetic user message after the complete group of tool results.
    """

    converted: list[dict[str, Any]] = []
    pending_media: list[dict[str, Any]] = []
    for index, message in enumerate(messages):
        value = message.as_dict()
        if message.role == "tool" and isinstance(message.content, tuple):
            text_parts = [
                dict(part) for part in message.content if part.get("type") == "text"
            ]
            media_parts = [
                dict(part) for part in message.content if part.get("type") != "text"
            ]
            if media_parts:
                if not text_parts:
                    text_parts = [
                        {"type": "text", "text": "The tool returned media context."}
                    ]
                value["content"] = text_parts
                label = message.name or "tool"
                pending_media.append(
                    {
                        "type": "text",
                        "text": (
                            f"Media returned by {label} for the preceding tool call. "
                            "Continue the current request using this evidence."
                        ),
                    }
                )
                pending_media.extend(media_parts)
        converted.append(value)
        next_is_tool = index + 1 < len(messages) and messages[index + 1].role == "tool"
        if pending_media and not next_is_tool:
            converted.append({"role": "user", "content": pending_media})
            pending_media = []
    return converted
