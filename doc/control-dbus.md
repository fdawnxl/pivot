# pivot D-Bus control protocol

Pivot exports a narrow framework control surface. Agent, capability, event, executor, and memory operations are model-facing actions and are not exposed as remote control methods. External clients inject a transport-neutral stimulus envelope; the main Agent decides what work is appropriate.

The default endpoint is:

```text
Bus:       session
Service:   org.pivot.Control
Path:      /org/pivot/Control
Interface: org.pivot.Control1
```

`session` names the D-Bus transport scope. It is unrelated to Agent state. Use `pivot --dbus-only` to run a headless persistent reactor. If D-Bus is unavailable, local clients and the TUI continue through the same `PivotControl` methods.

## Methods

```text
Ping()                                  -> s
GetRuntime()                            -> s JSON
Inject(envelope_json: s)                -> s stimulus_id
GetStimulus(stimulus_id: s)             -> s JSON
ListStimuli(limit: u)                   -> s JSON
ListOutputs(limit: u)                   -> s JSON
CancelStimulus(stimulus_id: s)         -> b
InterruptMain()                         -> b
RequestReload()                         -> s JSON
RequestShutdown()                       -> b
```

`Inject` accepts the JSON shape documented in [the stimulus protocol](stimuli.md). Pivot binds the envelope to its one durable main Agent, validates it, and persists it before returning. `RequestReload` validates the current instance configuration and emits a reload request for the host process; applying provider, dependency, or environment changes requires an orderly runtime restart. `RequestShutdown` only requests shutdown and does not terminate a caller-owned process directly.

## Signals

```text
StimulusChanged(payload_json: s)
OutputAvailable(payload_json: s)
RuntimeEvent(event: s, payload_json: s)
```

`StimulusChanged` carries queue, processing, and terminal state snapshots. `OutputAvailable` carries a durable `OutputEnvelope`. Runtime events currently include `reload_requested` and `shutdown_requested`. Activation trace details remain local presentation data; the D-Bus ABI does not expose hidden model reasoning.

## Configuration

`config.toml` supports:

```toml
dbus_control_enabled = true
dbus_control_bus = "session"
dbus_control_service = "org.pivot.Control"
dbus_control_start_timeout = 5
```

The corresponding environment variables are `PIVOT_DBUS_CONTROL_ENABLED`, `PIVOT_DBUS_CONTROL_BUS`, `PIVOT_DBUS_CONTROL_SERVICE`, and `PIVOT_DBUS_CONTROL_START_TIMEOUT`.

The D-Bus session bus trusts the operating-system user boundary. Deployments exporting pivot on a system bus must add an appropriate D-Bus policy.
