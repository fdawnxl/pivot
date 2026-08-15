# pivot D-Bus control protocol

Pivot exports its application control surface on D-Bus while keeping local Python methods as the primary in-process path used by the CLI and TUI. Both transports use the same `PivotControl` object, so local and remote messages share the main-Agent FIFO mailbox.

The default endpoint is:

```text
Bus:       session
Service:   org.pivot.Control
Path:      /org/pivot/Control
Interface: org.pivot.Control1
```

Here `session` names the D-Bus transport scope; it is unrelated to pivot Agent state.

Use `pivot --dbus-only` to run a headless control process. Ordinary one-shot and interactive CLI processes also attempt to export the interface. If the bus is unavailable, ordinary CLI/TUI operation continues through local methods. `--no-dbus` explicitly disables export.

## Method model

Short reads return immediately. Long-running work returns a task UUID and executes in a bounded worker pool, so model calls, capabilities, event waits, and dependency startup never block the D-Bus event loop.

Convenience methods:

```text
Ping()                                      -> s
GetRuntime()                                -> s JSON
ListOperations()                            -> s JSON
GetTask(task_id: s)                         -> s JSON
ListTasks()                                 -> s JSON
CancelTask(task_id: s)                      -> b
GetMainAgent()                              -> s JSON
GetHistory()                                -> s JSON
SendMessage(message: s)                     -> s task_id
InterruptMain()                             -> b
RequestShutdown()                           -> b
```

`SendMessage` reserves its FIFO position before control-pool execution. A task remains `queued` until all earlier main-Agent inputs finish. `InterruptMain` cooperatively interrupts the active activation, queued main-Agent tasks, and active workers.

## Extensible operations

The transport ABI does not need a new D-Bus method for each future pivot feature:

```text
Invoke(operation: s, arguments_json: s) -> s task_id
```

The initial registry includes:

```text
runtime.get
runtime.shutdown
agent.main
agent.history
agent.message
agent.interrupt
agent.list
agent.get
agent.create
agent.assign
agent.delegate
capability.list
capability.execute
event.list
event.wait
executor.list
executor.execute
memory.remember
memory.recall
memory.forget
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

Current events are `task_changed` and `shutdown_requested`.

## Configuration

`config.toml` supports:

```toml
dbus_control_enabled = true
dbus_control_bus = "session"
dbus_control_service = "org.pivot.Control"
dbus_control_start_timeout = 5
```

The corresponding environment variables are `PIVOT_DBUS_CONTROL_ENABLED`, `PIVOT_DBUS_CONTROL_BUS`, `PIVOT_DBUS_CONTROL_SERVICE`, and `PIVOT_DBUS_CONTROL_START_TIMEOUT`.

The D-Bus session bus trusts the operating-system user boundary. Deployments exporting pivot on a system bus must add an appropriate D-Bus policy before enabling that configuration.
