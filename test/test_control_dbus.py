from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from pivot.dbus_control import CONTROL_DBUS_INTERFACE, CONTROL_DBUS_PATH
from pivot.runtime import PivotClient
from test_control import _runtime


@pytest.mark.skipif(shutil.which("dbus-daemon") is None, reason="dbus-daemon is unavailable")
def test_dbus_control_only_exposes_framework_and_stimulus_operations(tmp_path: Path) -> None:
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
            runtime = json.loads(await interface.call_get_runtime())
            assert runtime["main_agent_id"] == client.main_agent_id
            methods = {method.name for method in introspection.interfaces[-1].methods}
            assert {"Inject", "GetStimulus", "ListStimuli", "ListOutputs", "RequestShutdown"} <= methods
            assert {"Invoke", "SendMessage", "GetHistory", "GetMainAgent"}.isdisjoint(methods)

            stimulus_id = await interface.call_inject(
                json.dumps(
                    {
                        "kind": "command",
                        "source": "dbus-test",
                        "payload": {"content": "remote"},
                    }
                )
            )
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                stimulus = json.loads(await interface.call_get_stimulus(stimulus_id))
                if stimulus["state"] not in {"queued", "processing"}:
                    break
                await asyncio.sleep(0.01)
            assert stimulus["state"] == "completed"
            assert stimulus["response"] == "ack: remote"
            outputs = json.loads(await interface.call_list_outputs(10))
            assert outputs[0]["stimulus_id"] == stimulus_id
        finally:
            bus.disconnect()

    try:
        asyncio.run(exercise())
    finally:
        client.close()
        bus_process.terminate()
        bus_process.wait(timeout=3)
