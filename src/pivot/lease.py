"""Exclusive process lease for one pivot instance runtime."""

from __future__ import annotations

import fcntl
import logging
import os
from pathlib import Path
from typing import TextIO

LOGGER = logging.getLogger(__name__)


class RuntimeLeaseError(RuntimeError):
    """Raised when another process already owns an instance runtime."""


class RuntimeLease:
    """Hold a non-blocking advisory lock for the lifetime of one runtime."""

    def __init__(self, instance_path: str | Path) -> None:
        self.instance_path = Path(instance_path).expanduser().resolve()
        self.path = self.instance_path / "runtime.lock"
        self._handle: TextIO | None = None

    @property
    def acquired(self) -> bool:
        """Return whether this object currently owns the lease."""

        return self._handle is not None

    def acquire(self) -> None:
        """Acquire the instance lease without waiting."""

        if self._handle is not None:
            return
        self.instance_path.mkdir(parents=True, exist_ok=True)
        try:
            handle = self.path.open("a+", encoding="utf-8")
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            handle.seek(0)
            handle.truncate()
            handle.write(f"{os.getpid()}\n")
            handle.flush()
            os.fsync(handle.fileno())
        except BlockingIOError as exc:
            handle.close()
            raise RuntimeLeaseError(
                f"A pivot runtime already owns instance {self.instance_path}"
            ) from exc
        except OSError as exc:
            try:
                handle.close()
            except UnboundLocalError:
                pass
            raise RuntimeLeaseError(
                f"Cannot acquire pivot runtime lease for {self.instance_path}: {type(exc).__name__}"
            ) from exc
        self._handle = handle
        LOGGER.info(
            "Runtime lease acquired instance=%s pid=%d", self.instance_path, os.getpid()
        )

    def release(self) -> None:
        """Release the lease if it is held."""

        handle = self._handle
        if handle is None:
            return
        self._handle = None
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
        LOGGER.info("Runtime lease released instance=%s", self.instance_path)

    def __enter__(self) -> "RuntimeLease":
        self.acquire()
        return self

    def __exit__(self, *_: object) -> None:
        self.release()


__all__ = ["RuntimeLease", "RuntimeLeaseError"]
