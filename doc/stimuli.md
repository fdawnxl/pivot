# Stimulus and output protocol

Pivot is a continuously running main-agent reactor. An instance adapter does not call an Agent method directly. It validates its local data, then injects one JSON `StimulusEnvelope` into the instance control transport.

The framework does not implement wake-word detection, speech recognition, audio capture, or text-to-speech. Those are instance concerns. A voice adapter can turn a final transcript into a `command` stimulus and consume a `response` output; a sensor adapter can publish an `observation` stimulus and consume later outputs. The core only defines the transport-neutral contract.

## Stimulus envelope

```json
{
  "kind": "command | observation | worker_report | timer | system",
  "source": "instance.temperature-monitor",
  "payload": {},
  "priority": 80,
  "correlation_id": "optional-id",
  "causation_id": "optional-parent-id",
  "dedupe_key": "optional-source-local-id"
}
```

`target_agent_id` is assigned by pivot and cannot be supplied by an adapter. The only external target is the durable main Agent. `command` payloads require `content`; other kinds carry a source-specific JSON object. Pivot assigns a UUID and timestamp, validates size and types, and records the source and causal identifiers.

Stimuli are persisted in `memory/pivot.db`. The reactor claims the highest-priority queued item and preserves FIFO order among equal priorities. A unique `(source, dedupe_key)` pair makes retries idempotent. Items claimed when a process stops are returned to `queued` on the next runtime start.

The lifecycle is `queued`, `processing`, then `completed`, `failed`, or `cancelled`. Every activation is bounded by the normal model round limit. A sensor adapter should perform debouncing, hysteresis, cooldown, and threshold evaluation in the instance/event layer before publishing repeated observations.

## Output envelope

Successful main-agent results are persisted and emitted as:

```json
{
  "output_id": "uuid",
  "stimulus_id": "uuid",
  "agent_id": "uuid",
  "kind": "response",
  "payload": {
    "content": "...",
    "stimulus_kind": "command",
    "source": "instance.voice"
  },
  "correlation_id": "optional-id"
}
```

Adapters choose whether an output is relevant to them. A voice adapter may speak command responses; a logging or device adapter may consume observation outcomes. Pivot does not assume a presentation channel.

Worker completion is represented as a `worker_report` stimulus with the worker snapshot in its payload. The main Agent therefore receives user commands, observations, system notices, and worker results through one scheduling and memory path.
