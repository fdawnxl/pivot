"""Provider-neutral LLM interface backed by LiteLLM."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any, Protocol

from ..models import Message


class LLMError(RuntimeError):
    """A sanitized error raised by an LLM adapter."""


class LLMClient(Protocol):
    def complete(self, messages: Sequence[Message], *, tools: Sequence[dict[str, Any]] = ()) -> Any:
        """Return a provider response for the supplied conversation."""


class LiteLLMClient:
    """Thin LiteLLM wrapper with lazy importing and injectable completion."""

    def __init__(
        self,
        model: str,
        *,
        api_base: str | None = None,
        timeout: float = 120.0,
        completion: Callable[..., Any] | None = None,
    ) -> None:
        self.model = model
        self.api_base = api_base
        self.timeout = timeout
        self._completion = completion

    def complete(self, messages: Sequence[Message], *, tools: Sequence[dict[str, Any]] = ()) -> Any:
        completion = self._completion
        if completion is None:
            try:
                from litellm import completion as litellm_completion
            except ImportError as exc:
                raise LLMError("LiteLLM is not installed; run 'uv sync --extra llm'") from exc
            completion = litellm_completion
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": [message.as_dict() for message in messages],
            "timeout": self.timeout,
        }
        if self.api_base:
            kwargs["api_base"] = self.api_base
        if tools:
            kwargs["tools"] = list(tools)
        try:
            return completion(**kwargs)
        except Exception as exc:
            # Provider SDKs expose many exception classes. Keep the public surface
            # stable and do not include request content or credentials.
            raise LLMError(f"LLM request failed: {type(exc).__name__}: {exc}") from exc
