# pivot D-Bus control protocol

Pivot exports its application control surface on D-Bus while keeping local Python method calls as the primary in-process path used by the CLI and TUI. Both transports use the same `PivotControl` object, so remote actions and local UI state remain consistent without making the terminal interface depend on D-Bus availability.

The default endpoint is:

```text
Bus:       session
Service:   org.pivot.Control
Path:      /org/pivot/Control
Interface: org.pivot.Control1
```

Use `pivot --dbus-only` to run a headless control process. Ordinary one-shot and interactive CLI processes also attempt to export the interface. If the bus is unavailable or another process owns the service name, ordinary CLI/TUI operation continues through local method calls. `--no-dbus` explicitly disables export.

## Method model

Short read and state-selection operations return immediately. Long-running work returns a task UUID and executes in a bounded worker pool, so model calls, capabilities, event waits, and dependency startup never block the D-Bus event loop.

Convenience methods:

```text
Ping()                                      -> s
GetRuntime()                                -> s JSON
ListOperations()                            -> s JSON
GetTask(task_id: s)                         -> s JSON
ListTasks()                                 -> s JSON
CancelTask(task_id: s)                      -> b
CreateSession(select: b)                    -> s JSON
SelectSession(session_id: s)                -> s JSON
GetSelectedSession()                        -> s JSON
ListSessions()                              -> s JSON
GetSession(session_id: s)                   -> s JSON
GetHistory(session_id: s)                   -> s JSON
SendMessage(session_id: s, message: s)      -> s task_id
CancelSession(session_id: s)                -> b
RequestShutdown()                           -> b
```

An empty `session_id` means the main agent. In the default runtime, creation and selection compatibility methods resolve to that same main agent; another UUID is rejected. `CancelSession` interrupts main-agent work and its active delegated worker at the next cooperative boundary.

## Extensible operations

The transport ABI does not need a new D-Bus method for each future pivot feature:

```text
Invoke(operation: s, arguments_json: s) -> s task_id
```

`ListOperations` returns the currently registered operations. The initial registry includes:

```text
runtime.get
runtime.shutdown
session.create
session.select
session.list
session.get
session.history
session.send
session.cancel
capability.list
capability.execute
event.list
event.wait
executor.list
executor.execute
agent.list
agent.get
agent.create
agent.assign
agent.delegate
dependency.list
dependency.refresh
dependency.start
dependency.stop
```

Arguments and results are JSON values. Task states are `queued`, `running`, `completed`, `failed`, and `cancelled`. A failed task contains a sanitized `error`; a completed task contains `result`.

The interface emits:

```text
ControlEvent(event: s, payload_json: s)
```

Events include `session_created`, `session_selected`, `task_changed`, and `shutdown_requested`.

## Configuration

`config.toml` supports:

```toml
dbus_control_enabled = true
dbus_control_bus = "session"
dbus_control_service = "org.pivot.Control"
dbus_control_start_timeout = 5
```

The corresponding environment variables are `PIVOT_DBUS_CONTROL_ENABLED`, `PIVOT_DBUS_CONTROL_BUS`, `PIVOT_DBUS_CONTROL_SERVICE`, and `PIVOT_DBUS_CONTROL_START_TIMEOUT`.

The session bus trusts the operating-system user boundary. Deployments exporting pivot on a system bus must add an appropriate D-Bus policy before enabling that configuration.
