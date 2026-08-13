from __future__ import annotations

import subprocess
import shutil
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from pivot.dependencies import (
    DEPENDENCY_READY_MARKER,
    DBusDependencyStatusClient,
    DependencyDBus,
    DependencyDescriptor,
    DependencyError,
    DependencyManager,
    DependencyState,
    DependencyStatus,
    discover_dependencies,
    load_dependency_manifest,
)


def _write_project(
    root: Path,
    dependency_id: str = "sensor-server",
    service: str = "org.pivot.SensorServer",
) -> Path:
    root.mkdir(parents=True)
    (root / "pyproject.toml").write_text(
        '[project]\nname = "sensor-server"\nversion = "0.1.0"\nrequires-python = ">=3.11"\ndependencies = []\n',
        encoding="utf-8",
    )
    (root / "dependency.toml").write_text(
        f'id = "{dependency_id}"\n'
        'description = "Test sensor server"\n'
        'command = ["python", "server.py"]\n'
        '[dbus]\n'
        'bus = "session"\n'
        f'service = "{service}"\n',
        encoding="utf-8",
    )
    return root


def test_dependency_manifest_requires_uv_project_and_valid_meaningful_id(tmp_path: Path) -> None:
    project = _write_project(tmp_path / "sensor")
    descriptor = load_dependency_manifest(project)

    assert descriptor.dependency_id == "sensor-server"
    assert descriptor.dbus == DependencyDBus("session", "org.pivot.SensorServer")
    assert descriptor.command == ("python", "server.py")
    assert descriptor.root == project.resolve()

    (project / "dependency.toml").write_text('id = "Sensor"\ncommand = ["python"]\n', encoding="utf-8")
    with pytest.raises(DependencyError, match="Invalid dependency id"):
        load_dependency_manifest(project)


def test_dependency_discovery_is_first_level_and_skips_invalid_or_duplicate_projects(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    root = tmp_path / "dependencies"
    first = _write_project(root / "first", "shared", "org.pivot.SharedOne")
    _write_project(root / "duplicate", "shared", "org.pivot.SharedTwo")
    valid = _write_project(root / "valid", "valid", "org.pivot.Valid")
    _write_project(root / "service-one", "service-one", "org.pivot.Conflict")
    _write_project(root / "service-two", "service-two", "org.pivot.Conflict")
    invalid = root / "invalid"
    invalid.mkdir(parents=True)
    (invalid / "dependency.toml").write_text('id = "invalid"\ncommand = "python server.py"\n', encoding="utf-8")
    _write_project(first / "nested", "nested", "org.pivot.Nested")

    descriptors = discover_dependencies(root)

    assert [(item.dependency_id, item.root) for item in descriptors] == [("valid", valid.resolve())]
    assert "Skipping duplicate instance dependency" in caplog.text
    assert "Skipping duplicate dependency D-Bus service" in caplog.text
    assert "Skipping instance dependency" in caplog.text


class FakeStatusClient:
    def __init__(self) -> None:
        self.queries: list[tuple[str, float]] = []
        self.pings: list[tuple[str, float]] = []

    def query(self, descriptor, *, timeout: float) -> DependencyStatus:
        self.queries.append((descriptor.dbus.service, timeout))
        return DependencyStatus(descriptor.dependency_id, DependencyState.READY, "available", {"samples": 1})

    def ping(self, descriptor, *, timeout: float) -> bool:
        self.pings.append((descriptor.dbus.service, timeout))
        return True


class FakeProcess:
    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.returncode: int | None = None
        self.terminated = False

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 0

    def kill(self) -> None:
        self.returncode = -9

    def wait(self, timeout: float | None = None) -> int:
        assert timeout is not None
        assert self.returncode is not None
        return self.returncode


def test_manager_syncs_only_once_runs_without_sync_and_uses_dbus_status(tmp_path: Path) -> None:
    instance = tmp_path / "instance"
    project = _write_project(instance / "dependencies" / "sensor")
    sync_calls: list[tuple[list[str], dict[str, Any]]] = []
    launch_calls: list[tuple[list[str], dict[str, Any]]] = []
    processes: list[FakeProcess] = []

    def runner(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        sync_calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, "", "")

    def process_factory(command: list[str], **kwargs: Any) -> Any:
        launch_calls.append((command, kwargs))
        process = FakeProcess(100 + len(processes))
        processes.append(process)
        return process

    status_client = FakeStatusClient()
    manager = DependencyManager(
        instance,
        status_client=status_client,
        runner=runner,
        process_factory=process_factory,
        dbus_timeout=0.25,
    )

    first = manager.start_all()
    assert first == (DependencyStatus("sensor-server", DependencyState.READY, "available", {"samples": 1}),)
    assert sync_calls[0][0] == ["uv", "sync", "--project", str(project.resolve())]
    assert (project / DEPENDENCY_READY_MARKER).read_text(encoding="utf-8") == "sensor-server\n"
    assert launch_calls[0][0] == [
        "uv",
        "run",
        "--project",
        str(project.resolve()),
        "--no-sync",
        "python",
        "server.py",
    ]
    assert launch_calls[0][1]["cwd"] == project.resolve()
    assert launch_calls[0][1]["env"]["PIVOT_DEPENDENCY_ID"] == "sensor-server"
    assert launch_calls[0][1]["env"]["PIVOT_DEPENDENCY_DBUS_BUS"] == "session"
    assert launch_calls[0][1]["env"]["PIVOT_DEPENDENCY_DBUS_SERVICE"] == "org.pivot.SensorServer"
    assert status_client.pings == [("org.pivot.SensorServer", 0.25)]
    assert status_client.queries == [("org.pivot.SensorServer", 0.25)]
    assert manager.statuses() == first
    processes[0].returncode = 7
    assert manager.statuses(refresh=True)[0] == DependencyStatus(
        "sensor-server",
        DependencyState.ERROR,
        "Process exited with code 7",
    )
    processes[0].returncode = None

    assert manager.stop("sensor-server")
    assert processes[0].terminated
    assert manager.statuses()[0].state == DependencyState.STOPPED
    manager.start("sensor-server")
    assert len(sync_calls) == 1
    manager.close()
    assert processes[1].terminated


def test_failed_sync_does_not_create_first_run_marker_or_launch(tmp_path: Path) -> None:
    instance = tmp_path / "instance"
    project = _write_project(instance / "dependencies" / "sensor")
    launched = False

    def runner(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 2, "", "resolution failed")

    def process_factory(command: list[str], **kwargs: Any) -> Any:
        nonlocal launched
        launched = True
        return FakeProcess(1)

    manager = DependencyManager(
        instance,
        status_client=FakeStatusClient(),
        runner=runner,
        process_factory=process_factory,
    )
    manager.scan()

    with pytest.raises(DependencyError, match="installation failed"):
        manager.start("sensor-server")

    assert not (project / DEPENDENCY_READY_MARKER).exists()
    assert not launched
    assert manager.statuses()[0].state == DependencyState.ERROR


def test_start_all_isolates_one_dependency_installation_failure(tmp_path: Path) -> None:
    instance = tmp_path / "instance"
    _write_project(instance / "dependencies" / "broken", "broken", "org.pivot.Broken")
    _write_project(instance / "dependencies" / "healthy", "healthy", "org.pivot.Healthy")
    launched: list[str] = []

    def runner(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if command[-1].endswith("broken"):
            return subprocess.CompletedProcess(command, 1, "", "broken project")
        return subprocess.CompletedProcess(command, 0, "", "")

    def process_factory(command: list[str], **kwargs: Any) -> Any:
        launched.append(kwargs["env"]["PIVOT_DEPENDENCY_ID"])
        return FakeProcess(200)

    status_client = FakeStatusClient()
    manager = DependencyManager(
        instance,
        status_client=status_client,
        runner=runner,
        process_factory=process_factory,
    )

    statuses = manager.start_all()

    assert [status.dependency_id for status in statuses] == ["healthy"]
    assert launched == ["healthy"]
    assert [(status.dependency_id, status.state) for status in manager.statuses()] == [
        ("broken", DependencyState.ERROR),
        ("healthy", DependencyState.READY),
    ]
    manager.close()


def test_status_payload_rejects_mismatched_id_and_invalid_state() -> None:
    with pytest.raises(DependencyError, match="different id"):
        DependencyStatus.from_payload("sensor", {"id": "other", "state": "ready"})
    with pytest.raises(DependencyError, match="invalid state"):
        DependencyStatus.from_payload("sensor", {"id": "sensor", "state": "unknown"})


@pytest.mark.skipif(shutil.which("dbus-daemon") is None, reason="dbus-daemon is unavailable")
def test_common_dbus_client_queries_status_and_heartbeat(tmp_path: Path) -> None:
    bus_process = subprocess.Popen(
        ["dbus-daemon", "--session", "--nofork", "--print-address=1"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert bus_process.stdout is not None
    address = bus_process.stdout.readline().strip()
    assert address
    service = tmp_path / "service.py"
    service.write_text(
        "import asyncio,json\n"
        "from dbus_next.aio import MessageBus\n"
        "from dbus_next.service import ServiceInterface,method\n"
        "class Service(ServiceInterface):\n"
        " def __init__(self): super().__init__('org.pivot.Dependency1')\n"
        " @method()\n"
        " def Ping(self)->'s': return 'integration'\n"
        " @method()\n"
        " def GetStatus(self)->'s': return json.dumps({'id':'integration','state':'ready','message':'ok'})\n"
        "async def main():\n"
        " bus=await MessageBus(bus_address='" + address + "').connect()\n"
        " bus.export('/org/pivot/Dependency',Service())\n"
        " await bus.request_name('org.pivot.Integration')\n"
        " await asyncio.Future()\n"
        "asyncio.run(main())\n",
        encoding="utf-8",
    )
    service_process = subprocess.Popen([sys.executable, str(service)])
    descriptor = DependencyDescriptor(
        "integration",
        tmp_path,
        ("python", "service.py"),
        DependencyDBus("session", "org.pivot.Integration"),
    )
    client = DBusDependencyStatusClient(bus_addresses={"session": address})
    try:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not client.ping(descriptor, timeout=0.5):
            time.sleep(0.05)
        assert client.ping(descriptor, timeout=0.5)
        assert client.query(descriptor, timeout=0.5) == DependencyStatus(
            "integration", DependencyState.READY, "ok"
        )
    finally:
        service_process.terminate()
        service_process.wait(timeout=3)
        bus_process.terminate()
        bus_process.wait(timeout=3)
