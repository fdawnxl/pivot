"""Central logging setup for pivot runtime modules."""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, TextIO

_HANDLER_MARKER = "_pivot_handler"
_LEVEL_ALIASES = {"WARN": "WARNING"}
_LOG_CONTEXT: ContextVar[dict[str, str] | None] = ContextVar(
    "pivot_log_context", default=None
)
_STANDARD_RECORD_FIELDS = set(logging.makeLogRecord({}).__dict__) | {
    "message",
    "asctime",
}
_DEPENDENCY_LOGGERS = (
    "LiteLLM",
    "LiteLLM Proxy",
    "LiteLLM Router",
    "litellm",
    "openai",
    "httpcore",
    "httpx",
)


def observe(event: str, *, value: float | None = None, **fields: Any) -> None:
    """Emit a uniform operational observation without exposing prompt contents."""

    safe = {key: item for key, item in fields.items() if isinstance(item, (str, int, float, bool))}
    if value is not None:
        safe["value"] = value
    logging.getLogger("pivot.observe").info(event, extra={"event": event, **safe})


@contextmanager
def log_context(**values: str | None) -> Iterator[None]:
    """Bind correlation fields to all logs emitted in the current context."""

    current = dict(_LOG_CONTEXT.get() or {})
    current.update({key: value for key, value in values.items() if value is not None})
    token = _LOG_CONTEXT.set(current)
    try:
        yield
    finally:
        _LOG_CONTEXT.reset(token)


class ContextFilter(logging.Filter):
    """Copy context variables onto each log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        for key, value in (_LOG_CONTEXT.get() or {}).items():
            if not hasattr(record, key):
                setattr(record, key, value)
        return True


class JsonFormatter(logging.Formatter):
    """Serialize durable logs as one JSON object per line."""

    def format(self, record: logging.LogRecord) -> str:
        document: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in (
            "correlation_id",
            "activation_id",
            "agent_id",
            "capability",
            "event",
        ):
            value = getattr(record, key, None)
            if value is not None:
                document[key] = value
        extras = {
            key: value
            for key, value in record.__dict__.items()
            if key not in _STANDARD_RECORD_FIELDS
            and key not in document
            and key
            not in {
                "correlation_id",
                "activation_id",
                "agent_id",
                "capability",
                "event",
            }
            and isinstance(value, (str, int, float, bool, type(None)))
        }
        if extras:
            document["fields"] = extras
        if record.exc_info:
            document["exception"] = self.formatException(record.exc_info)
        return json.dumps(document, ensure_ascii=False)


def parse_log_level(level: str, *, default: int = logging.INFO) -> int:
    """Return a standard logging level, accepting the user-facing ``warn`` alias."""

    normalized = _LEVEL_ALIASES.get(level.upper(), level.upper())
    value = getattr(logging, normalized, None)
    return value if isinstance(value, int) else default


def configure_dependency_logging() -> None:
    """Route warning-level dependency logs through pivot's handlers only."""

    for name in _DEPENDENCY_LOGGERS:
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True
        logger.setLevel(logging.WARNING)


def configure_tui_logging() -> None:
    """Disable pivot's console handler while preserving durable file logging."""

    root = logging.getLogger()
    for handler in tuple(root.handlers):
        if getattr(handler, _HANDLER_MARKER, False) and not isinstance(
            handler, logging.FileHandler
        ):
            root.removeHandler(handler)
            handler.close()
    if root.handlers:
        root.setLevel(min(handler.level for handler in root.handlers))
    configure_dependency_logging()


def configure_logging(
    console_level: str = "INFO",
    *,
    file_level: str = "DEBUG",
    log_path: str | Path | None = None,
    stream: TextIO | None = None,
) -> None:
    """Configure independent terminal and rotating-file logging levels."""

    root = logging.getLogger()
    for handler in tuple(root.handlers):
        if getattr(handler, _HANDLER_MARKER, False):
            root.removeHandler(handler)
            handler.close()

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    console = logging.StreamHandler(stream or sys.stderr)
    console.setLevel(parse_log_level(console_level))
    console.setFormatter(formatter)
    console.addFilter(ContextFilter())
    setattr(console, _HANDLER_MARKER, True)
    root.addHandler(console)

    if log_path is not None:
        path = Path(log_path).expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        file_handler.setLevel(parse_log_level(file_level, default=logging.DEBUG))
        file_handler.setFormatter(JsonFormatter())
        file_handler.addFilter(ContextFilter())
        setattr(file_handler, _HANDLER_MARKER, True)
        root.addHandler(file_handler)

    root.setLevel(min(handler.level for handler in root.handlers))
    configure_dependency_logging()
    logging.getLogger(__name__).debug(
        "Logging configured console_level=%s file_level=%s log_path=%s",
        console_level.upper(),
        file_level.upper(),
        log_path,
    )


__all__ = [
    "JsonFormatter",
    "configure_dependency_logging",
    "configure_logging",
    "configure_tui_logging",
    "log_context",
    "observe",
    "parse_log_level",
]
