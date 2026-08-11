"""Central logging setup for pivot runtime modules."""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import TextIO


_HANDLER_MARKER = "_pivot_handler"
_LEVEL_ALIASES = {"WARN": "WARNING"}


def parse_log_level(level: str, *, default: int = logging.INFO) -> int:
    """Return a standard logging level, accepting the user-facing ``warn`` alias."""

    normalized = _LEVEL_ALIASES.get(level.upper(), level.upper())
    value = getattr(logging, normalized, None)
    return value if isinstance(value, int) else default


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
    setattr(console, _HANDLER_MARKER, True)
    root.addHandler(console)

    if log_path is not None:
        path = Path(log_path).expanduser().resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
        file_handler.setLevel(parse_log_level(file_level, default=logging.DEBUG))
        file_handler.setFormatter(formatter)
        setattr(file_handler, _HANDLER_MARKER, True)
        root.addHandler(file_handler)

    root.setLevel(min(handler.level for handler in root.handlers))
    logging.getLogger(__name__).debug(
        "Logging configured console_level=%s file_level=%s log_path=%s",
        console_level.upper(),
        file_level.upper(),
        log_path,
    )


__all__ = ["configure_logging", "parse_log_level"]
