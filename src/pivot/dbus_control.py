"""D-Bus transport for the shared pivot control surface."""

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
    """Raised when the D-Bus control service cannot start or stop safely."""


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))


def _arguments(value: str) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ControlError(f"Arguments contain invalid JSON: {exc.msg}") from exc
    if not isinstance(parsed, Mapping):
        raise ControlError("Arguments JSON must be an object")
    return dict(parsed)


def _session_id(value: str) -> str | None:
    return value or None


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
                LOGGER.error("D-Bus control method failed error_type=%s", type(exc).__name__)
                raise DBusError(CONTROL_DBUS_ERROR, f"{type(exc).__name__}: {exc}") from exc

        @method()
        def Ping(self) -> "s":
            return "pivot"

        @method()
        def GetRuntime(self) -> "s":
            return self._call(lambda: _json(control.runtime_snapshot()))

        @method()
        def ListOperations(self) -> "s":
            return self._call(lambda: _json([item.as_dict() for item in control.operations()]))

        @method()
        def Invoke(self, operation: "s", arguments_json: "s") -> "s":
            return self._call(lambda: control.submit(operation, _arguments(arguments_json)))

        @method()
        def GetTask(self, task_id: "s") -> "s":
            return self._call(lambda: _json(control.task(task_id).as_dict()))

        @method()
        def ListTasks(self) -> "s":
            return self._call(lambda: _json([task.as_dict() for task in control.tasks()]))

        @method()
        def CancelTask(self, task_id: "s") -> "b":
            return self._call(lambda: control.cancel_task(task_id))

        @method()
        def CreateSession(self, select: "b") -> "s":
            return self._call(lambda: _json(control.session_snapshot(control.create_session(select=select).session_id)))

        @method()
        def SelectSession(self, session_id: "s") -> "s":
            return self._call(lambda: _json(control.session_snapshot(control.select_session(session_id).session_id)))

        @method()
        def GetSelectedSession(self) -> "s":
            return self._call(lambda: _json(control.session_snapshot()))

        @method()
        def ListSessions(self) -> "s":
            return self._call(lambda: _json(control.list_sessions()))

        @method()
        def GetSession(self, session_id: "s") -> "s":
            return self._call(lambda: _json(control.session_snapshot(_session_id(session_id))))

        @method()
        def GetHistory(self, session_id: "s") -> "s":
            return self._call(lambda: _json(control.history(_session_id(session_id))))

        @method()
        def SendMessage(self, session_id: "s", message: "s") -> "s":
            return self._call(lambda: control.submit_message(message, session_id=_session_id(session_id)))

        @method()
        def CancelSession(self, session_id: "s") -> "b":
            return self._call(lambda: control.cancel_session(_session_id(session_id)))

        @method()
        def RequestShutdown(self) -> "b":
            self._call(control.request_shutdown)
            return True

        @signal()
        def ControlEvent(self, event: str, payload_json: str) -> "ss":
            return [event, payload_json]

        def emit_control_event(self, event: str, payload: Mapping[str, Any]) -> None:
            self.ControlEvent(event, _json(payload))

    return PivotControlInterface()


class ControlDBusService:
    """Host the pivot control interface on a dedicated D-Bus event-loop thread."""

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
        self._interface: Any = None
        self._unsubscribe: Any = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive() and self._ready.is_set() and self._startup_error is None

    def start(self) -> None:
        """Start the service and wait until the well-known name is owned."""

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
        """Stop the event loop and disconnect from D-Bus."""

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
            self._interface = interface
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
