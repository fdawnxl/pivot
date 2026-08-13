# pivot dependency protocol

A dependency is an external long-running program that should not share pivot's interpreter or package set. Typical dependencies are sensor servers used by measure capabilities and data-source servers used by events. Every dependency is a standalone uv project under the instance `dependencies` directory:

```text
instance/dependencies/sensor-server/
├── dependency.toml
├── pyproject.toml
├── uv.lock                 # recommended for reproducible installations
└── server.py
```

Only first-level child directories are scanned. A project is ignored when either manifest is missing, the dependency metadata is invalid, or its dependency id is duplicated by another project.

## Manifest

`dependency.toml` uses this schema:

```toml
id = "sensor-server"
description = "Provide sensor readings over D-Bus"
command = ["python", "server.py"]

[dbus]
bus = "session"
service = "org.example.SensorServer"
```

`id` is a stable, instance-local dependency id using lowercase kebab-case. It identifies logs, status snapshots, and lifecycle operations; it is not a D-Bus name. `dbus.bus` selects the `session` or `system` bus, while `dbus.service` is the dependency's well-known D-Bus service name. `command` is passed directly to `uv run`; it is an argument array rather than a shell command.

The first successful start runs:

```text
uv sync --project <dependency-project>
uv run --project <dependency-project> --no-sync <command...>
```

After `uv sync` succeeds, pivot atomically creates `.pivot-installed` in the dependency root. Later scans skip package synchronization and launch with `--no-sync`. Remove this marker explicitly when the project's declared packages need to be synchronized again. A failed synchronization never creates the marker.

Each process receives `PIVOT_INSTANCE_PATH`, `PIVOT_DEPENDENCY_ID`, `PIVOT_DEPENDENCY_DBUS_BUS`, and `PIVOT_DEPENDENCY_DBUS_SERVICE`. Its working directory is its project root, and stdout/stderr are written to `logs/dependencies/<id>.log`. Pivot stops processes it owns when the runtime closes. One dependency's installation, startup, or status failure is logged and does not prevent other dependencies or pivot services from loading.

Pivot preserves `DBUS_SESSION_BUS_ADDRESS` and `DBUS_SYSTEM_BUS_ADDRESS` when it launches dependency, measure, work, and event subprocesses. A dependency and its capability/event clients can therefore communicate over the same inherited bus without private bus processes, address files, or shell environment reconstruction.

The installation and lifecycle timeouts are configurable through `config.toml` or the corresponding `PIVOT_<NAME>` environment variable:

```toml
dependency_install_timeout = 300
dependency_start_timeout = 15
dependency_dbus_timeout = 1
dependency_stop_timeout = 5
```

## Common D-Bus status interface

Every dependency must acquire its manifest `dbus.service` on the selected bus and export:

```text
Object path: /org/pivot/Dependency
Interface:   org.pivot.Dependency1
GetStatus() -> s
Ping()      -> s
```

`Ping` returns either the dependency id or `pong`. `GetStatus` returns a JSON string with the following shape:

```json
{
  "id": "sensor-server",
  "state": "ready",
  "message": "Sensor server is available",
  "details": {"sensors": 4}
}
```

`state` is one of `starting`, `ready`, `degraded`, `stopping`, or `error`. Startup succeeds after both the heartbeat and a `ready` or `degraded` status are observed. A dependency can use additional D-Bus interfaces and object paths for its own data protocol; the interface above is reserved for common lifecycle status only.

A minimal `dbus-next` service implementation is:

```python
import asyncio
import json
import os

from dbus_next.aio import MessageBus
from dbus_next.service import ServiceInterface, method


class DependencyService(ServiceInterface):
    def __init__(self, dependency_id: str) -> None:
        super().__init__("org.pivot.Dependency1")
        self.dependency_id = dependency_id

    @method()
    def Ping(self) -> "s":
        return self.dependency_id

    @method()
    def GetStatus(self) -> "s":
        return json.dumps(
            {
                "id": self.dependency_id,
                "state": "ready",
                "message": "Dependency is available",
                "details": {},
            }
        )


async def main() -> None:
    dependency_id = os.environ["PIVOT_DEPENDENCY_ID"]
    service_name = os.environ["PIVOT_DEPENDENCY_DBUS_SERVICE"]
    bus = await MessageBus().connect()
    bus.export("/org/pivot/Dependency", DependencyService(dependency_id))
    await bus.request_name(service_name)
    await asyncio.Future()


asyncio.run(main())
```

The dependency's own `pyproject.toml` must declare `dbus-next`; it does not share pivot's installed copy.
