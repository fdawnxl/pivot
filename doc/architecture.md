# pivot architecture

The runtime follows a one-way dependency flow from models and configuration through adapters into activation and orchestration. Provider responses are converted at the LLM boundary, so the Agent core never imports LiteLLM types. `ActionDetector` normalizes native tool calls and fixed-format `<pivot-action>{JSON}</pivot-action>` output into capability, event, control, executor, or memory actions. Instance scripts are optional runtime extensions: one broken script is logged and skipped while the rest of the instance remains usable.

## Runtime ownership and identity

Runtime assembly first acquires an exclusive process lease in the resolved instance. Only one process may own dependency lifecycle, the main Agent, and writable runtime state for an instance at a time; a competing process fails before it starts dependencies. The lease is released during orderly shutdown and failed assembly.

The memory database owns one stable main-Agent UUID, enforced by a unique SQLite index. The identity is generated once rather than derived from a filesystem path. User-facing clients always submit to this Agent. Worker Agents are durable internal identities created and scheduled by the main Agent, but they are not user-selectable endpoints.

Every external stimulus is normalized into a `StimulusEnvelope` and appended to a SQLite-backed main-Agent inbox. Commands, device observations, timer notices, system notices, and worker reports share the same target, validation, priority, deduplication, lifecycle, and recovery rules. The reactor claims the highest-priority queued item while preserving FIFO order within a priority. The main Agent is therefore a continuously running consumer rather than a session created by a client request.

## Persistent Agents and finite activations

`PersistentAgent` separates enduring identity from finite work. Each stimulus creates one activation with a bounded model/action loop and the states `running`, `completed`, `failed`, or `cancelled`. The Agent itself exposes the runtime states `ready`, `running`, and `pending`. Failed and cancelled activation data remains auditable in SQLite but is excluded from later model context.

Before every model round, `ContextBuilder` constructs a fresh prompt from:

- current Agent role and resource scope;
- current capability, event, executor, control, and memory descriptions;
- a bounded recent-message window from completed and current activations;
- relevant sourced long-term memories;
- non-expired world-state observations.

Static runtime context is never saved as prompt history. This prevents stale capability or device state from being replayed and prevents lifetime history from growing every request. See [memory](memory.md) for the persistence model.

## Worker scheduling and event waits

`AgentControl` lets the main Agent create a worker, assign explicit capability/event allowlists, submit a task, observe progress, and receive a structured report. Worker completion, failure, or cancellation becomes a typed `worker_report` stimulus. It is never serialized into a special user-visible message or delivered through a remote Agent control method.

The event runtime separates generic sources from per-request conditions. An instance event adapter can publish an `observation` stimulus after applying threshold, debounce, hysteresis, and cooldown rules. The native `pivot_wait_event` tool remains available to workers for compatibility, while normal device monitoring is push-based and does not occupy a main-Agent activation. Waiting conditions and outcomes are recorded as durable continuations.

## Capabilities, executors, and dependencies

Capabilities describe or provide domain work, while executors perform concrete machine actions. The initial `shell` executor uses a fixed instance cwd, a restricted inherited environment, a maximum timeout, and bounded output. This boundary is injectable and observable, but it remains process control rather than an operating-system sandbox.

Think, measure, and work scripts are discovered through `-l` in dedicated uv projects, so instance Python is never imported into the framework interpreter. Think scripts return their body through `-r` only after the model selects the summary. Measure scripts read one feature through `-r <feature>`. Work scripts accept JSON arguments on stdin through `-x` and emit a JSON result. Subprocess timeouts, output limits, fixed working directories, and restricted environments provide a lightweight process boundary.

External dependencies are standalone uv projects under `instance/dependencies`. Runtime assembly starts valid dependencies before discovering capabilities and events. The dependency manager performs one initial `uv sync`, records success with an atomic `.pivot-installed` marker, and uses `uv run --no-sync` thereafter. Every dependency implements `org.pivot.Dependency1` at `/org/pivot/Dependency`; heartbeat and structured status checks establish readiness without coupling pivot to application data protocols.

## Control and presentation

`pivot.runtime` exposes a persistent reactor and provider-neutral stimulus/output methods independently from presentation. `PivotClient.run_main` is only a synchronous convenience wrapper around command injection. The Textual application uses the same envelope path as an instance adapter and shows the main timeline plus a read-only Agent lifecycle sidebar.

`pivot.control` is the shared framework surface. Local clients and `pivot.dbus_control` both inject envelopes, inspect durable stimulus/output state, interrupt active work, and request reload or shutdown. Agent-level operations remain inside model activations and cannot be invoked as remote control shortcuts.
