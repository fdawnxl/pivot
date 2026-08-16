"""D-Bus transport for framework lifecycle control and unified stimulus ingress."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
from collections.abc import Mapping
from typing import Any, Literal

from .control import ControlError, PivotControl

LOGGER = logging.getLogger(__name__)

CONTROL_DBUS_SERVICE = "org.pivot.Control"
CONTROL_DBUS_PATH = "/org/pivot/Control"
CONTROL_DBUS_INTERFACE = "org.pivot.Control1"
CONTROL_DBUS_ERROR = "org.pivot.Control.Error"
_DBUS_SERVICE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*(?:\.[A-Za-z_][A-Za-z0-9_-]*)+$")


class ControlDBusError(RuntimeError):
    """Raised when the framework D-Bus service cannot start or stop safely."""


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))


def _envelope(value: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ControlError(f"Stimulus envelope contains invalid JSON: {exc.msg}") from exc
    if not isinstance(parsed, Mapping):
        raise ControlError("Stimulus envelope JSON must be an object")
    return dict(parsed)


def _service_interface(control: PivotControl) -> Any:
    try:
        from dbus_next import DBusError
        from dbus_next.service import ServiceInterface, method, signal
    except ImportError as exc:  # pragma: no cover - declared runtime dependency
        raise ControlDBusError("dbus-next is required for the pivot control service") from exc

    class PivotControlInterface(ServiceInterface):
        def __init__(self) -> None:
            super().__init__(CONTROL_DBUS_INTERFACE)

        def _call(self, callback: Any) -> Any:
            try:
                return callback()
            except ControlError as exc:
                raise DBusError(CONTROL_DBUS_ERROR, str(exc)) from exc
            except Exception as exc:
                LOGGER.error("D-Bus framework method failed error_type=%s", type(exc).__name__)
                raise DBusError(CONTROL_DBUS_ERROR, f"{type(exc).__name__}: {exc}") from exc

        @method()
        def Ping(self) -> "s":
            return "pivot"

        @method()
        def GetRuntime(self) -> "s":
            return self._call(lambda: _json(control.runtime_snapshot()))

        @method()
        def Inject(self, envelope_json: "s") -> "s":
            return self._call(lambda: control.inject(_envelope(envelope_json)))

        @method()
        def GetStimulus(self, stimulus_id: "s") -> "s":
            return self._call(lambda: _json(control.stimulus(stimulus_id).as_dict()))

        @method()
        def ListStimuli(self, limit: "u") -> "s":
            return self._call(lambda: _json([item.as_dict() for item in control.stimuli(limit=limit)]))

        @method()
        def ListOutputs(self, after_sequence: "t", limit: "u") -> "s":
            return self._call(
                lambda: _json(
                    [
                        item.as_dict()
                        for item in control.outputs(after_sequence=after_sequence, limit=limit)
                    ]
                )
            )

        @method()
        def CancelStimulus(self, stimulus_id: "s") -> "b":
            return self._call(lambda: control.cancel_stimulus(stimulus_id))

        @method()
        def InterruptMain(self) -> "b":
            return self._call(control.interrupt_main)

        @method()
        def RequestReload(self) -> "s":
            return self._call(lambda: _json(control.request_reload()))

        @method()
        def RequestShutdown(self) -> "b":
            self._call(control.request_shutdown)
            return True

        @signal()
        def StimulusChanged(self, payload_json: str) -> "s":
            return payload_json

        @signal()
        def OutputAvailable(self, payload_json: str) -> "s":
            return payload_json

        @signal()
        def RuntimeEvent(self, event: str, payload_json: str) -> "ss":
            return [event, payload_json]

        def emit_control_event(self, event: str, payload: Mapping[str, Any]) -> None:
            encoded = _json(payload)
            if event == "stimulus_changed":
                self.StimulusChanged(encoded)
            elif event == "output_available":
                self.OutputAvailable(encoded)
            elif event != "activation_progress":
                self.RuntimeEvent(event, encoded)

    return PivotControlInterface()


class ControlDBusService:
    """Host the narrow framework interface on a dedicated D-Bus loop thread."""

    def __init__(
        self,
        control: PivotControl,
        *,
        bus: Literal["session", "system"] = "session",
        service_name: str = CONTROL_DBUS_SERVICE,
        bus_address: str | None = None,
        start_timeout: float = 5.0,
    ) -> None:
        if bus not in {"session", "system"}:
            raise ValueError("Control D-Bus must use the session or system bus")
        if not _DBUS_SERVICE.fullmatch(service_name):
            raise ValueError(f"Invalid control D-Bus service name: {service_name!r}")
        if start_timeout <= 0:
            raise ValueError("Control D-Bus start timeout must be positive")
        self.control = control
        self.bus = bus
        self.service_name = service_name
        self.bus_address = bus_address
        self.start_timeout = start_timeout
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_event: asyncio.Event | None = None
        self._ready = threading.Event()
        self._startup_error: BaseException | None = None
        self._unsubscribe: Any = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive() and self._ready.is_set() and self._startup_error is None

    def start(self) -> None:
        if self._thread is not None:
            if self.running:
                return
            raise ControlDBusError("D-Bus control service has already stopped")
        self._thread = threading.Thread(target=self._thread_main, name="pivot-control-dbus", daemon=True)
        self._thread.start()
        if not self._ready.wait(self.start_timeout):
            self.stop()
            raise ControlDBusError("D-Bus control service startup timed out")
        if self._startup_error is not None:
            error = self._startup_error
            self.stop()
            raise ControlDBusError(f"Unable to start D-Bus control service: {type(error).__name__}: {error}") from error
        LOGGER.info("D-Bus control service started bus=%s service=%s", self.bus, self.service_name)

    def stop(self) -> None:
        loop = self._loop
        stop_event = self._stop_event
        if loop is not None and stop_event is not None and loop.is_running():
            loop.call_soon_threadsafe(stop_event.set)
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=self.start_timeout)
        self._thread = None
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        LOGGER.info("D-Bus control service stopped service=%s", self.service_name)

    def _thread_main(self) -> None:
        try:
            asyncio.run(self._serve())
        except BaseException as exc:
            self._startup_error = exc
            self._ready.set()

    async def _serve(self) -> None:
        try:
            from dbus_next import BusType, NameFlag, RequestNameReply
            from dbus_next.aio import MessageBus
        except ImportError as exc:  # pragma: no cover - declared runtime dependency
            raise ControlDBusError("dbus-next is required for the pivot control service") from exc
        self._loop = asyncio.get_running_loop()
        self._stop_event = asyncio.Event()
        bus_type = BusType.SESSION if self.bus == "session" else BusType.SYSTEM
        message_bus = MessageBus(bus_address=self.bus_address) if self.bus_address else MessageBus(bus_type=bus_type)
        connection = await message_bus.connect()
        try:
            interface = _service_interface(self.control)
            connection.export(CONTROL_DBUS_PATH, interface)
            reply = await connection.request_name(self.service_name, NameFlag.DO_NOT_QUEUE)
            if reply not in {RequestNameReply.PRIMARY_OWNER, RequestNameReply.ALREADY_OWNER}:
                raise ControlDBusError(f"D-Bus service name is already owned: {self.service_name}")

            def listener(event: str, payload: Mapping[str, Any]) -> None:
                loop = self._loop
                if loop is not None and loop.is_running():
                    loop.call_soon_threadsafe(interface.emit_control_event, event, payload)

            self._unsubscribe = self.control.subscribe(listener)
            self._ready.set()
            await self._stop_event.wait()
        finally:
            if self._unsubscribe is not None:
                self._unsubscribe()
                self._unsubscribe = None
            connection.disconnect()


__all__ = [
    "CONTROL_DBUS_ERROR",
    "CONTROL_DBUS_INTERFACE",
    "CONTROL_DBUS_PATH",
    "CONTROL_DBUS_SERVICE",
    "ControlDBusError",
    "ControlDBusService",
]
