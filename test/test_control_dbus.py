from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from pivot.dbus_control import (
    CONTROL_DBUS_INTERFACE,
    CONTROL_DBUS_PATH,
)
from pivot.runtime import PivotClient
from test_control import _runtime


@pytest.mark.skipif(shutil.which("dbus-daemon") is None, reason="dbus-daemon is unavailable")
def test_dbus_control_creates_session_sends_message_and_reads_history(tmp_path: Path) -> None:
    bus_process = subprocess.Popen(
        ["dbus-daemon", "--session", "--nofork", "--print-address=1"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert bus_process.stdout is not None
    address = bus_process.stdout.readline().strip()
    assert address
    client = PivotClient(_runtime(tmp_path))
    control = client.control
    service_name = "org.pivot.ControlTest"
    service = client.start_dbus(service_name=service_name, bus_address=address)
    assert service.running

    async def exercise() -> None:
        from dbus_next.aio import MessageBus

        bus = await MessageBus(bus_address=address).connect()
        try:
            introspection = await bus.introspect(service_name, CONTROL_DBUS_PATH)
            proxy = bus.get_proxy_object(service_name, CONTROL_DBUS_PATH, introspection)
            interface = proxy.get_interface(CONTROL_DBUS_INTERFACE)
            assert await interface.call_ping() == "pivot"
            created = json.loads(await interface.call_create_session(True))
            session_id = created["session_id"]
            task_id = await interface.call_send_message(session_id, "remote")
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                task = json.loads(await interface.call_get_task(task_id))
                if task["state"] not in {"queued", "running"}:
                    break
                await asyncio.sleep(0.01)
            assert task["state"] == "completed"
            assert task["result"]["response"] == "ack: remote"
            history = json.loads(await interface.call_get_history(session_id))
            assert history[-1]["content"] == "ack: remote"
            operations = json.loads(await interface.call_list_operations())
            assert "dependency.start" in {item["name"] for item in operations}
            assert "event.wait" in {item["name"] for item in operations}
            invoked = await interface.call_invoke("session.get", json.dumps({"session_id": session_id}))
            invoked_task = control.wait_task(invoked, timeout=2)
            assert invoked_task.result["session_id"] == session_id
        finally:
            bus.disconnect()

    try:
        asyncio.run(exercise())
    finally:
        client.close()
        bus_process.terminate()
        bus_process.wait(timeout=3)
