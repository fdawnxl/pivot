# Stimulus and output protocol

Pivot is a continuously running main-agent reactor. An instance adapter does not call an Agent method directly. It validates its local data, then injects one JSON `StimulusEnvelope` into the instance control transport.

The framework does not implement wake-word detection, speech recognition, audio capture, or text-to-speech. Those are instance concerns. A voice adapter can turn a final transcript into a `command` stimulus and consume a `response` output; a sensor adapter can publish an `observation` stimulus and consume later outputs. The core only defines the transport-neutral contract.

## Stimulus envelope

```json
{
  "kind": "command | observation | worker_report | timer | system",
  "source": "instance.temperature-monitor",
  "payload": {"values": {"temperature": 83.5}, "ttl": 30},
  "priority": 80,
  "delivery": "state | activate",
  "replay_safe": true,
  "correlation_id": "optional-id",
  "causation_id": "optional-parent-id",
  "dedupe_key": "optional-source-local-id"
}
```

`target_agent_id` is assigned by pivot and cannot be supplied by an adapter. The only external target is the durable main Agent. `command` payloads require `content`. An observation with `delivery: "state"` requires a non-empty `payload.values` object and accepts an optional positive `ttl` in seconds.

`observation` defaults to `state`: pivot updates world state and completes the stimulus without invoking an LLM. An adapter emits `delivery: "activate"` after its local threshold, hysteresis, or other attention policy decides that the main Agent must act. All other kinds default to `activate`.

Stimuli are persisted in `memory/pivot.db`. Priority aging prevents a continuous urgent source from starving older work, and FIFO order is preserved when effective priorities match. The pending queue is bounded and old terminal records are removed according to instance configuration. A unique `(source, dedupe_key)` pair makes adapter retries idempotent.

State-only observations default to `replay_safe: true`; activating stimuli default to `false`. After an unclean stop, only explicitly safe processing stimuli return to the queue. Unsafe stimuli become failed with a diagnostic error because a capability or executor side effect may already have happened.

The lifecycle is `queued`, `processing`, then `completed`, `failed`, or `cancelled`. Every activation is bounded by the normal model round limit. A sensor adapter should perform debouncing, hysteresis, cooldown, and threshold evaluation in the instance/event layer before publishing repeated observations.

## Output envelope

Successful main-agent results are persisted and emitted as:

```json
{
  "output_id": "uuid",
  "sequence": 42,
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

`sequence` is a monotonically increasing instance-local cursor. Consumers resume with `ListOutputs(after_sequence, limit)`, persist the highest processed sequence, and can therefore recover after missing a transient D-Bus signal. Adapters choose whether an output is relevant to them. A voice adapter may speak command responses; a logging or device adapter may consume `state_updated` outputs. Pivot does not assume a presentation channel.

Worker completion is represented as a `worker_report` stimulus with the worker snapshot in its payload. Stimuli persist their activation id; delegated tasks persist their originating activation id; worker reports use the task id as their cause. This provides a durable command-to-report causal chain.
