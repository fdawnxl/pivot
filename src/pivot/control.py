"""Framework control surface and unified main-agent stimulus ingress."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

from .activation import AgentCancelled, CancellationToken, ProgressCallback
from .config import PivotConfig
from .stimuli import (
    OutputEnvelope,
    StimulusEnvelope,
    StimulusError,
    StimulusKind,
    StimulusState,
)

if TYPE_CHECKING:
    from .runtime import Runtime

LOGGER = logging.getLogger(__name__)


class ControlError(RuntimeError):
    """Raised when a framework control request is invalid or unavailable."""


ControlListener = Callable[[str, Mapping[str, Any]], None]


class PivotControl:
    """Expose lifecycle controls while routing every agent input through envelopes."""

    def __init__(self, runtime: Runtime) -> None:
        runtime.start()
        if runtime.reactor is None or runtime.inbox is None:
            raise ControlError("Main-agent reactor is unavailable")
        self.runtime = runtime
        self.reactor = runtime.reactor
        self.inbox = runtime.inbox
        self._listeners: set[ControlListener] = set()
        self._lock = threading.RLock()
        self._unsubscribe = self.reactor.subscribe(self._emit)
        self._closed = False

    def subscribe(self, listener: ControlListener) -> Callable[[], None]:
        with self._lock:
            self._listeners.add(listener)

        def unsubscribe() -> None:
            with self._lock:
                self._listeners.discard(listener)

        return unsubscribe

    def inject(
        self,
        envelope: Mapping[str, Any],
        *,
        progress: ProgressCallback | None = None,
        cancellation: CancellationToken | None = None,
    ) -> str:
        """Validate and durably inject one envelope into the main-agent inbox."""

        if self._closed:
            raise ControlError("Control service is closed")
        try:
            return self.reactor.inject(envelope, progress=progress, cancellation=cancellation)
        except StimulusError as exc:
            raise ControlError(str(exc)) from exc

    def inject_command(
        self,
        content: Any,
        *,
        source: str = "client",
        correlation_id: str | None = None,
        progress: ProgressCallback | None = None,
        cancellation: CancellationToken | None = None,
    ) -> str:
        """Convenience adapter for local clients; it still uses the envelope ingress."""

        value: dict[str, Any] = {
            "kind": StimulusKind.COMMAND,
            "source": source,
            "payload": {"content": content},
        }
        if correlation_id is not None:
            value["correlation_id"] = correlation_id
        return self.inject(value, progress=progress, cancellation=cancellation)

    def run_main(
        self,
        stimulus: Any,
        *,
        progress: ProgressCallback | None = None,
        cancellation: CancellationToken | None = None,
    ) -> str:
        """Compatibility helper that injects a command and waits for its output."""

        stimulus_id = self.inject_command(
            stimulus,
            source="local-client",
            progress=progress,
            cancellation=cancellation,
        )
        completed = self.wait_stimulus(stimulus_id)
        if completed.state == StimulusState.CANCELLED:
            raise AgentCancelled(completed.error or "Main-agent stimulus was cancelled")
        if completed.state == StimulusState.FAILED:
            raise StimulusError(completed.error or "Main-agent stimulus failed")
        return completed.response or ""

    def wait_stimulus(self, stimulus_id: str, *, timeout: float | None = None) -> StimulusEnvelope:
        try:
            return self.inbox.wait(stimulus_id, timeout=timeout)
        except StimulusError as exc:
            raise ControlError(str(exc)) from exc

    def stimulus(self, stimulus_id: str) -> StimulusEnvelope:
        try:
            return self.inbox.get(stimulus_id)
        except StimulusError as exc:
            raise ControlError(str(exc)) from exc

    def stimuli(self, *, limit: int = 100) -> tuple[StimulusEnvelope, ...]:
        return self.inbox.list(limit=limit)

    def outputs(self, *, limit: int = 100) -> tuple[OutputEnvelope, ...]:
        return self.inbox.outputs(limit=limit)

    def cancel_stimulus(self, stimulus_id: str) -> bool:
        try:
            return self.reactor.cancel(stimulus_id)
        except StimulusError as exc:
            raise ControlError(str(exc)) from exc

    def interrupt_main(self) -> bool:
        """Interrupt the active main activation and active delegated workers."""

        return self.reactor.interrupt()

    def runtime_snapshot(self) -> dict[str, Any]:
        config = self.runtime.config
        queued = self.inbox.pending_count()
        return {
            "provider": config.provider.name,
            "model": config.provider.model,
            "instance_path": str(config.instance_path),
            "main_agent_id": self.runtime.main_agent.agent_id,
            "main_agent_state": self.runtime.main_agent.state,
            "queued_stimuli": queued,
            "capabilities": len(self.runtime.registry.descriptors()),
            "events": len(self.runtime.events.descriptors()),
            "dependencies": len(self.runtime.dependencies.descriptors()) if self.runtime.dependencies else 0,
        }

    def request_reload(self) -> dict[str, Any]:
        """Validate current instance configuration and request an orderly host reload."""

        try:
            candidate = PivotConfig.load(instance_path=self.runtime.config.instance_path)
        except Exception as exc:
            raise ControlError(f"Configuration reload validation failed: {type(exc).__name__}: {exc}") from exc
        payload = {
            "requested_at": time.time(),
            "provider": candidate.provider.name,
            "model": candidate.provider.model,
        }
        self.reactor.emit_runtime("reload_requested", payload)
        return {"requested": True, **payload}

    def request_shutdown(self) -> None:
        self.reactor.emit_runtime("shutdown_requested", {"requested_at": time.time()})

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._unsubscribe()
        with self._lock:
            self._listeners.clear()

    def _emit(self, event: str, payload: Mapping[str, Any]) -> None:
        with self._lock:
            listeners = tuple(self._listeners)
        for listener in listeners:
            try:
                listener(event, payload)
            except Exception as exc:
                LOGGER.warning("Control listener failed event=%s error_type=%s", event, type(exc).__name__)


__all__ = ["ControlError", "PivotControl"]
