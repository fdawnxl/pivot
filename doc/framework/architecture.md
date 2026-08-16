# Architecture

pivot is a layered Agent runtime for edge devices, industrial control, and embodied systems. Its central constraint is that model reasoning, device integration, persistence, and process lifecycle cooperate through explicit protocols instead of sharing implementation details.

## Dependency direction

The intended dependency flow is:

```text
config / logging / models
          |
          v
llm / parser / capabilities / memory / events / executors
          |
          v
activation / agents / stimuli
          |
          v
runtime / control
          |
          v
cli / tui / dbus_control
```

Lower layers never import presentation code or a concrete device implementation. LiteLLM types stop at `pivot.llm` and `pivot.parser`; instance scripts run out of process; external clients use stimulus envelopes instead of mutable Agent objects.

## Runtime ownership

One process owns an instance at a time. Runtime assembly acquires `runtime.lock` before starting dependencies or opening writable runtime state. The lock prevents two local processes from controlling the same hardware, main Agent, and SQLite database concurrently.

`RuntimeStore` owns the only SQLite connection shared by memory, agents, stimuli, outputs, events, and tasks. One stable main-Agent UUID is stored in the database and enforced by a unique index. Worker UUIDs are durable internal identities, but external clients cannot select them as targets.

## Input-to-output flow

```text
device adapter / CLI / local client
              |
              v
      StimulusEnvelope validation
              |
              v
       SQLite durable inbox
              |
              v
       MainAgentReactor claim
              |
              v
  bounded PersistentAgent activation
              |
       +------+-------+---------+---------+
       |              |         |         |
  capability       event    executor   worker
       |              |         |         |
       +--------------+---------+---------+
              |
              v
       durable OutputEnvelope
```

Every external input becomes a `StimulusEnvelope`. The reactor claims one queued envelope, binds it to an activation, and either updates world state or runs the main Agent. A successful activating stimulus creates a response output with a monotonic sequence. Worker reports return through the same inbox instead of calling the main Agent directly.

## Persistent identity, finite work

An Agent is persistent; an activation is finite. Each activation has a bounded model/action loop and ends as completed, failed, or cancelled. The Agent returns to `ready` after finite work or becomes `pending` while waiting on an event.

This split keeps identity and memory stable without allowing one prompt or task to grow forever. Recurring event workers are the deliberate exception at the scheduling level: each reported occurrence resets their per-occurrence round budget, while cancellation still ends the activation cooperatively.

## Dynamic context

Before every LLM call, `ContextBuilder` rebuilds context from:

- the current Agent role and resource scope;
- current capability, event, executor, control, and memory descriptions;
- a bounded recent-message window;
- relevant non-expired long-term memories;
- current non-expired world state.

Runtime descriptions are not persisted as transcript messages. This prevents stale device state and outdated tool lists from becoming permanent prompt history.

## Isolation model

pivot uses several isolation boundaries:

- provider-neutral models isolate the Agent core from LiteLLM response types;
- capability and event scripts run in dedicated uv environments;
- long-running dependencies are separate uv projects and processes;
- executor requests have a fixed instance cwd, environment allowlist, timeout, and output cap;
- D-Bus exposes lifecycle and stimulus operations, not direct capability or worker control.

These are process and protocol boundaries, not a hostile-code sandbox. Deployments executing untrusted commands still need operating-system isolation.

## Failure policy

A malformed capability, event, dependency, or bridge is logged and skipped independently where possible. An activation failure is persisted and excluded from later prompt context. Shutdown attempts every cleanup step and reports ordinary failures together, so one failed component does not prevent database closure or lease release.
