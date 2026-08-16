# Event runtime and stimulus bridges

Pivot keeps task event waits separate from main-Agent stimulus delivery. Event sources expose observable fields; waits and bridge rules apply conditions to those fields. A bridge is the only framework path that converts an autonomous event condition into a main-Agent stimulus.

Event source scripts live directly under `instance/events` and run through `instance/environment/event`. They support:

```text
python event.py -l   # list event descriptors
python event.py -p   # return the current JSON field snapshot
```

An event descriptor provides a stable name, description, field, supported comparison operators, optional result templates, and its source script. `EventSupervisor` serializes source polling and sends each successful report to both pending Agent waits and autonomous bridge subscribers.

## Agent waits

Workers use `pivot_wait_event` with an event name, operator, expected value, and timeout. A wait is one-shot: it completes on the first matching report, source error, timeout, or cancellation. Long waits belong to workers so that the main Agent remains available to consume its durable inbox.

## Event-to-stimulus bridges

Autonomous monitoring rules are optional instance data in `events/bridges.toml`:

```toml
[[bridge]]
id = "temperature-alert"
event = "monitor_temperature"
operator = ">"
expected = 80
delivery = "activate"
priority = 80
replay_safe = false
cooldown = 30
```

Each rule references a discovered event and one of that event's supported operators. Invalid rules are logged and skipped independently. `delivery`, `priority`, and `replay_safe` have the same meaning as fields in `StimulusEnvelope`. Activating bridges default to unsafe replay; state-only bridges default to safe replay.

Bridge conditions use persistent rising-edge semantics:

1. A false condition updates bridge state without producing a stimulus.
2. The first false-to-true transition creates one `EventOccurrence` and one observation stimulus.
3. Further matching samples do not produce more stimuli while the condition remains true.
4. A false sample rearms the rule.
5. `cooldown` can suppress a new rising edge that occurs too soon after the previous one.

The persisted edge state includes a signature of the event, operator, and expected value. Changing a rule condition under the same id therefore starts with fresh condition state after reload.

The generated observation stimulus has this shape:

```json
{
  "kind": "observation",
  "source": "event-bridge:temperature-alert",
  "payload": {
    "values": {"temperature": 83.5},
    "event": "monitor_temperature",
    "bridge_id": "temperature-alert",
    "occurrence_id": "uuid",
    "condition": {"field": "temperature", "operator": ">", "expected": 80},
    "observation": {"temperature": 83.5, "unit": "celsius"},
    "observed_at": 1786850000.0
  },
  "delivery": "activate",
  "causation_id": "the-occurrence-uuid",
  "dedupe_key": "the-occurrence-uuid"
}
```

The occurrence, queued stimulus, and fired edge state are inserted in one SQLite transaction. The causal path can then be followed from `EventOccurrence` through stimulus, activation, delegated task, and worker report.

Bridge rules define attention policy but do not implement device protocols. Audio capture, wake words, speech recognition, text-to-speech, sensor drivers, and device-specific acquisition remain instance responsibilities. Continuous telemetry that only refreshes world state should be injected as state-only observations rather than modeled as repeated event edges.
