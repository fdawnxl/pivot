"""FIFO admission control for activations of the persistent main agent."""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import TypeVar

from .activation import AgentCancelled, CancellationToken

T = TypeVar("T")


class MainAgentMailbox:
    """Issue FIFO sequence numbers and serialize main-agent activations."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._issued = 0
        self._next = 0
        self._skipped: set[int] = set()

    def issue(self) -> int:
        """Reserve and return the next FIFO position."""

        with self._condition:
            sequence = self._issued
            self._issued += 1
            return sequence

    def execute(
        self,
        sequence: int,
        cancellation: CancellationToken,
        callback: Callable[[], T],
        *,
        on_started: Callable[[], None] | None = None,
    ) -> T:
        """Wait for a sequence position, execute it, then release the next one."""

        with self._condition:
            while sequence > self._next:
                if cancellation.is_cancelled():
                    self._skip_locked(sequence)
                    raise AgentCancelled("Main-agent request was cancelled while queued")
                self._condition.wait(timeout=0.05)
            if sequence < self._next or cancellation.is_cancelled():
                self._skip_locked(sequence)
                raise AgentCancelled("Main-agent request was cancelled while queued")
        try:
            if on_started is not None:
                on_started()
            return callback()
        finally:
            with self._condition:
                if sequence == self._next:
                    self._next += 1
                self._advance_locked()
                self._condition.notify_all()

    def skip(self, sequence: int) -> None:
        """Remove a cancelled position without disturbing later FIFO order."""

        with self._condition:
            self._skip_locked(sequence)
            self._condition.notify_all()

    def _skip_locked(self, sequence: int) -> None:
        if sequence < self._next:
            return
        self._skipped.add(sequence)
        self._advance_locked()

    def _advance_locked(self) -> None:
        while self._next in self._skipped:
            self._skipped.remove(self._next)
            self._next += 1


__all__ = ["MainAgentMailbox"]
