# Event system

The event subsystem separates data acquisition, condition evaluation, Agent waiting, and autonomous attention policy.

## Event sources

An instance event script describes one or more observable fields with `-l` and returns a current snapshot with `-p`. pivot does not import the script. `EventScriptRunner` executes it through `environment/event` with a timeout, controlled environment, fixed instance cwd, and JSON output validation.

An `EventDescriptor` contains a stable name, field, supported comparison operators, result templates, timeout and error templates, and source script.

## Single polling owner

`EventSupervisor` is the only background owner of source polling. It computes the union of sources required by active waits and bridge subscribers, polls each source once per cycle, and distributes the same snapshot to both consumers.

This avoids duplicate hardware reads and timing races between Agent waits and autonomous monitoring.

## Agent waits

`EventService` exposes `pivot_wait_event` with:

```json
{
  "event": "temperature",
  "operator": ">",
  "expected": 80,
  "timeout": 300,
  "trigger": "level | rising | falling"
}
```

`level` completes whenever the current condition is true. `rising` first observes false, then completes on false-to-true. `falling` first observes true, then completes on true-to-false. Each wait is one-shot; a recurring worker creates a new edge wait after reporting an occurrence.

The wait completes with `matched`, `timeout`, or `error`. Cancellation removes it safely. Continuation state is persisted for audit, but active waits are not resumed across process restart.

## Event-to-Stimulus bridge

Bridge rules under `events/bridges.toml` turn selected source conditions into durable observation stimuli without putting device logic in pivot:

```toml
[[bridge]]
id = "temperature-alert"
event = "temperature"
operator = ">"
expected = 80
delivery = "activate"
priority = 80
replay_safe = false
cooldown = 30
```

Bridge conditions always use persistent rising-edge semantics:

1. false updates state and arms the rule;
2. false-to-true creates one occurrence and stimulus;
3. further true samples are suppressed;
4. false rearms the rule;
5. cooldown may suppress an edge that occurs too soon.

The rule signature includes event, operator, and expected value. Changing a condition under the same bridge id resets its edge state. Occurrence, stimulus, and fired state are inserted atomically.

## Choosing waits or bridges

Use a worker wait when the condition belongs to a user task and the worker needs to continue reasoning after a match. Use a bridge when the device must autonomously attract main-Agent attention even without an active user task. Use a state-only observation for continuous telemetry that should refresh context without causing model work.
