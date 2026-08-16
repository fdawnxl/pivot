# Events and dependencies

Dependencies own long-running device access. Event scripts expose current fields. Bridge rules decide when a field deserves autonomous main-Agent attention.

## Event source script

Place event scripts directly under `events/`. One script may describe multiple fields with `-l`, then returns one shared snapshot with `-p`.

```python
import json
import sys


EVENTS = [
    {
        "name": "obstacle_distance",
        "description": "Monitor the nearest obstacle distance in meters.",
        "field": "distance_m",
        "operators": ["<", "<=", ">", ">=", "==", "!="],
        "templates": {
            "<": "Obstacle alert: {condition}; current distance is {value} meters."
        },
        "timeout_template": "No obstacle matched {condition} within {timeout:g} seconds.",
        "error_template": "Obstacle monitoring failed for {condition}: {error}.",
    }
]


def snapshot() -> dict[str, object]:
    # Replace with a dependency D-Bus query or bounded sensor read.
    return {"distance_m": 1.4, "quality": "good"}


if "-l" in sys.argv:
    print(json.dumps(EVENTS))
elif "-p" in sys.argv:
    print(json.dumps(snapshot()))
else:
    raise SystemExit(2)
```

Supported operators are `==`, `!=`, `>`, `>=`, `<`, and `<=`. Templates may use `condition`, `value`, `timeout`, and `error`.

The event environment is `environment/event`. Keep polling bounded and side-effect free. pivot polls a source only while an Agent wait or bridge requires it.

## Agent event semantics

An Agent wait selects a descriptor, comparison, expected value, timeout, and trigger:

- `level`: complete when the condition is currently true;
- `rising`: require false-to-true;
- `falling`: require true-to-false.

User requests are one-shot unless they explicitly ask for every occurrence or continuous monitoring. Repeated monitoring belongs to a reusable worker, which reports each edge to the main Agent and re-arms a new wait.

## Autonomous bridge

Create `events/bridges.toml` when a condition must activate pivot without an existing user task:

```toml
[[bridge]]
id = "near-obstacle"
event = "obstacle_distance"
operator = "<"
expected = 0.8
delivery = "activate"
priority = 90
replay_safe = false
cooldown = 3
```

Use `delivery="state"` for observations that should update world state without invoking the LLM. Activating rules should normally remain `replay_safe=false` when responding may cause a side effect.

Bridge ids are stable persistence keys. Keep an id when tuning only operational metadata; changing event, operator, or expected value automatically starts fresh edge state.

## Dependency project

Use a dependency when a driver must stay connected, initialization is expensive, multiple capabilities share state, or one process must own a hardware interface.

```text
dependencies/sensor-hub/
├── dependency.toml
├── pyproject.toml
├── uv.lock
└── server.py
```

`dependency.toml`:

```toml
id = "sensor-hub"
description = "Own environmental and distance sensors"
command = ["python", "server.py"]

[dbus]
bus = "session"
service = "org.example.SensorHub"
```

The id uses lowercase kebab-case. `command` is an argument array passed to `uv run`, never a shell string. Each `(bus, service)` endpoint must be unique.

## Required health interface

Every dependency exports this interface in addition to its own data API:

```text
Object path: /org/pivot/Dependency
Interface:   org.pivot.Dependency1
GetStatus() -> s JSON
Ping()      -> s
```

Example status:

```json
{
  "id": "sensor-hub",
  "state": "ready",
  "message": "Sensors are available",
  "details": {"devices": 3}
}
```

`state` is `starting`, `ready`, `degraded`, `stopping`, or `error`. Startup succeeds when heartbeat responds and status becomes `ready` or `degraded`.

The process receives `PIVOT_INSTANCE_PATH`, `PIVOT_DEPENDENCY_ID`, `PIVOT_DEPENDENCY_DBUS_BUS`, and `PIVOT_DEPENDENCY_DBUS_SERVICE`. Its cwd is its project root.

A minimal `dbus-next` health service looks like this. Export device-specific methods on a separate interface; capabilities and event scripts can call that interface through the inherited bus address.

```python
import asyncio
import json
import os

from dbus_next import BusType
from dbus_next.aio import MessageBus
from dbus_next.service import ServiceInterface, method


class DependencyHealth(ServiceInterface):
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
                "message": "Sensors are available",
                "details": {},
            }
        )


async def main() -> None:
    dependency_id = os.environ["PIVOT_DEPENDENCY_ID"]
    service_name = os.environ["PIVOT_DEPENDENCY_DBUS_SERVICE"]
    bus_type = (
        BusType.SESSION
        if os.environ["PIVOT_DEPENDENCY_DBUS_BUS"] == "session"
        else BusType.SYSTEM
    )
    bus = await MessageBus(bus_type=bus_type).connect()
    bus.export("/org/pivot/Dependency", DependencyHealth(dependency_id))
    await bus.request_name(service_name)
    await asyncio.Future()


asyncio.run(main())
```

## Installation lifecycle

On the first successful start pivot runs:

```text
uv sync --project <dependency-project>
uv run --project <dependency-project> --no-sync <command...>
```

It then creates `.pivot-installed`. Remove that marker after changing dependency declarations so the next start synchronizes again. A failed sync never creates it.

One broken dependency is isolated from other projects. Its state and log remain visible, while unrelated capabilities and dependencies continue loading.
