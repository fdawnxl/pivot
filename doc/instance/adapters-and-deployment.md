# Device adapters and deployment

An adapter translates a device-specific transport into pivot stimuli and translates pivot outputs back to the device. Wake-word detection, ASR, TTS, camera capture, display rendering, sensor sampling, and upstream messaging belong here, not in the framework core.

## Select the right stimulus

| Input | Kind | Delivery | Typical replay policy |
| --- | --- | --- | --- |
| Final user transcript | `command` | `activate` | `false` |
| Continuous telemetry | `observation` | `state` | `true` |
| Attention-worthy observation | `observation` | `activate` | depends on side effects |
| Scheduled maintenance | `timer` | `activate` | explicit |
| Adapter/runtime notice | `system` | `activate` | explicit |

Worker reports are internal and should not be injected by device adapters.

## Python adapter

For an adapter hosted in the same process:

```python
from pivot import PivotClient


with PivotClient.open(instance_path="/opt/pivot/device") as client:
    stimulus_id = client.inject(
        {
            "kind": "observation",
            "source": "instance.environment",
            "payload": {
                "values": {"temperature_c": 31.2, "ambient_lux": 45},
                "ttl": 10,
            },
            "delivery": "state",
            "replay_safe": True,
            "dedupe_key": "sample-2026-08-16T12:00:00Z",
        }
    )
    completed = client.wait_stimulus(stimulus_id, timeout=5)
    assert completed.state == "completed"
```

A process using `PivotClient.open` owns the runtime and lease. Do not open another client for the same instance from a separate process.

## D-Bus adapter

For independent device processes, run pivot with `--dbus-only` and call `org.pivot.Control1.Inject` with an envelope JSON string. Subscribe to `OutputAvailable` for low latency, but always recover with `ListOutputs(after_sequence, limit)`.

The essential `dbus-next` call is:

```python
import json

from dbus_next.aio import MessageBus


async def inject_transcript(text: str) -> str:
    bus = await MessageBus().connect()
    introspection = await bus.introspect("org.pivot.Control", "/org/pivot/Control")
    proxy = bus.get_proxy_object("org.pivot.Control", "/org/pivot/Control", introspection)
    control = proxy.get_interface("org.pivot.Control1")
    return await control.call_inject(
        json.dumps(
            {
                "kind": "command",
                "source": "instance.voice",
                "payload": {"content": text},
                "correlation_id": "voice-turn-42",
                "dedupe_key": "asr-segment-42",
            }
        )
    )
```

Persist the highest output sequence only after the device has handled it successfully. Signals are notifications, not a replacement for the durable cursor.

Use a stable `source` per adapter and a source-local `dedupe_key` for retryable messages. Preserve a `correlation_id` across ASR, pivot, and TTS or display output. Use `causation_id` to connect a derived stimulus to the upstream event or job that caused it.

## Attention policy

Do not activate the LLM for every sensor sample. A practical pipeline is:

```text
sensor -> local filtering -> state observation
                         -> activating observation only on attention decision
```

Use Event-to-Stimulus bridges for declarative threshold conditions. Use adapter logic for policies involving signal processing, rate aggregation, wake words, or device-specific state machines.

## Safety boundaries

- Validate all model arguments again in capability and dependency code.
- Make physical actions bounded and idempotent where possible.
- Use `replay_safe=false` when repeating an interrupted action could be harmful.
- Keep API keys out of `config.toml`, source files, logs, and stimulus payloads.
- Treat the shell executor as process control, not a security sandbox.
- Add OS users, containers, namespaces, seccomp, or MAC policy when commands are not fully trusted.
- Add a D-Bus policy before exporting pivot or a dependency on the system bus.

## Operations

Recommended startup command:

```bash
PIVOT_INSTANCE_PATH=/opt/pivot/device uv run pivot --dbus-only
```

For service management, set the instance path explicitly, give the process access to its D-Bus bus and hardware devices, and use normal SIGTERM shutdown. Do not start multiple service units for one instance.

Inspect:

- `logs/pivot.log` for structured runtime diagnostics;
- `logs/dependencies/<id>.log` for dependency process output;
- `GetRuntime` for main-Agent, worker, capability, event, bridge, dependency, and queue summaries;
- `ListStimuli` and `ListOutputs` for durable request/output state.

`RequestReload` validates changed configuration and asks the pivot host to rebuild the runtime. Changes to dependencies, environments, credentials, capabilities, or event scripts should be applied through an orderly reload or restart.

An orderly stop marks `runtime.lock` clean. On the next clean start, pivot cancels queued work and replay-safe processing work; replay-unsafe processing work becomes failed. Adapters should treat an orderly restart as a new submission boundary. After a process crash or power loss, queued work is retained, processing work is replayed only when `replay_safe=true`, and unsafe processing work becomes failed. Always inspect durable stimulus state rather than assuming a restart retried a request.

Framework logs are JSON Lines in `logs/pivot.log`. Operational observations use logger `pivot.observe` with a stable event name, optional numeric value, and scalar dimensions. Framework extensions should not put prompts, media, credentials, or nested sensitive objects in observation fields. See [Runtime and extension boundaries](../framework/runtime.md#operational-observations) for the framework-side contract.

## Deployment checklist

- The instance path is explicit and stored on persistent writable media.
- `credentials.toml` has mode `0600`.
- Every environment and dependency has reproducible dependency declarations and preferably a lockfile.
- Capabilities and event scripts pass their protocol commands independently.
- Dependency health interfaces report the manifest id and correct state.
- Activating observations have an intentional attention and replay policy.
- Adapter output cursors survive process restart.
- Logs rotate and do not contain secrets or raw sensitive media.
- Shutdown releases dependencies, SQLite, and `runtime.lock` cleanly.
- Restart handling matches each adapter's replay and resubmission policy.
- Physical actions have an external fail-safe beyond model behavior.
