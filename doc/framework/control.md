# Control and presentation

The public control plane exposes framework lifecycle and the single main-Agent ingress. It does not expose direct capability execution, memory mutation, executor invocation, or worker management.

## Python client

`PivotClient.open(instance_path=...)` loads configuration, acquires the instance lease, builds the runtime, and starts its reactor. The principal methods are:

```text
run_main(content)                   synchronous command convenience
inject(envelope)                   durable asynchronous ingress
wait_stimulus(stimulus_id)         wait for terminal state
main_history()                     read-only visible main history
start_dbus(...)                    export the same control surface
close()                            release all owned resources
```

`run_main` still goes through the durable inbox. Applications that need retries, cancellation, multiple sources, or output cursors should use `inject` and control methods directly.

## Shared control facade

`PivotControl` provides stimulus inspection, output listing, cancellation, global interruption, runtime snapshots, reload requests, and shutdown requests. Listeners receive:

- `stimulus_changed` for queue and terminal transitions;
- `output_available` for durable outputs;
- `activation_progress` for structured execution progress;
- `reload_requested` and `shutdown_requested` lifecycle events.

Progress describes observable phases and results; it does not expose private chain-of-thought.

## D-Bus contract

The default endpoint is:

```text
Bus:       session
Service:   org.pivot.Control
Path:      /org/pivot/Control
Interface: org.pivot.Control1
```

Methods:

```text
Ping()                                   -> s
GetRuntime()                             -> s JSON
Inject(envelope_json: s)                 -> s stimulus_id
GetStimulus(stimulus_id: s)              -> s JSON
ListStimuli(limit: u)                    -> s JSON
ListOutputs(after_sequence: t, limit: u) -> s JSON
CancelStimulus(stimulus_id: s)           -> b
InterruptMain()                          -> b
RequestReload()                          -> s JSON
RequestShutdown()                        -> b
```

Signals:

```text
StimulusChanged(payload_json: s)
OutputAvailable(payload_json: s)
RuntimeEvent(event: s, payload_json: s)
```

All control events other than stimulus and output notifications use `RuntimeEvent`, including activation progress.

## Host behavior

`RequestReload` validates current instance configuration and asks the host loop to rebuild the runtime. `RequestShutdown` asks the host to exit; it does not terminate an arbitrary caller-owned process.

The D-Bus session bus relies on the operating-system user boundary. A system-bus deployment must add an explicit D-Bus policy.

## CLI and TUI

The CLI supports one-shot commands, stdin, an interactive Textual UI, and `--dbus-only` headless operation. The TUI submits commands through the same envelope path and renders main-Agent messages, workflow progress, worker lifecycle, and dependency health. Workers are observable in the sidebar but are not selectable conversations.
