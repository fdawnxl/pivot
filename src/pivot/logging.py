"""Logging setup for pivot. Product output must flow through this module."""

from __future__ import annotations

import logging
import sys


def configure_logging(level: str = "INFO", *, stream: object | None = None) -> None:
    """Configure one predictable English log format without duplicating handlers."""

    numeric_level = getattr(logging, level.upper(), None)
    if not isinstance(numeric_level, int):
        numeric_level = logging.INFO
    root = logging.getLogger()
    root.setLevel(numeric_level)
    if not root.handlers:
        handler = logging.StreamHandler(stream or sys.stderr)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root.addHandler(handler)


__all__ = ["configure_logging"]
