# Agents and activations

pivot has exactly one externally addressable main Agent. Workers are internal, scoped collaborators created by that main Agent.

## Main Agent

The main Agent owns the user-facing timeline and consumes every activating stimulus. Its durable UUID is created once per instance. CLI, TUI, Python, and D-Bus clients all address it indirectly by inserting an envelope into the main inbox.

The main Agent may use capabilities and executors directly, or delegate work. Event waits longer than one second must be delegated so the main reactor remains available for new commands and observations.

## Workers

`AgentControl` creates workers with explicit capability and event allowlists. An empty allowlist grants nothing. Workers cannot create other workers and are never external request targets.

The model-facing operations are:

```text
agent.list
agent.get       {agent_id}
agent.create    {name?, capabilities?, events?, one_shot?}
agent.assign    {agent_id, task, recurrence?}
agent.delegate  {task, name?, capabilities?, events?, recurrence?, one_shot?}
agent.report    {result}  # worker only
```

`agent.delegate` combines creation and asynchronous assignment. Finite delegated work defaults to `one_shot=true`; recurring event workers must be reusable. Terminal one-shot workers are reclaimed after the configured retention period, but only after no queued or processing worker report still references them.

## Explicit reports

`agent.report` publishes a structured `worker_report` stimulus immediately. Reporting does not itself finish the worker. This allows a recurring monitor to notify the main Agent, re-arm an event wait, and remain pending.

Each report has a monotonically increasing revision within the task. The reactor deduplicates the matching terminal notification when the latest explicit report already carried the result.

## Event recurrence

An event assignment with an event scope requires one recurrence policy:

- `once`: report the first matched occurrence and finish;
- `rising`: repeatedly report false-to-true transitions;
- `falling`: repeatedly report true-to-false transitions.

The activation loop prevents a worker from starting another wait while a matched occurrence remains unreported. A recurring worker must use the assigned edge trigger on every wait. After an accepted report, the per-occurrence model-round budget resets.

## Activation loop

For each activation, `PersistentAgent`:

1. creates a durable activation and appends the input message;
2. builds current context and model tools;
3. calls the LLM and parses its response;
4. normalizes native or textual actions;
5. dispatches each action and appends tool results;
6. repeats until the model returns no action or the safety budget is exhausted;
7. persists completion, failure, or cancellation.

The preferred model action is:

```json
{
  "kind": "capability | event | control | executor | memory",
  "name": "operation-name",
  "arguments": {}
}
```

The same payload can be emitted through the native `pivot_action` tool or a `<pivot-action>...</pivot-action>` textual fallback. Provider-native capability and event tool calls pass through the same `ActionDetector` and router.

## State and progress

Persistent Agent state is `ready`, `running`, or `pending`. Worker task state is `created`, `running`, `completed`, `failed`, or `cancelled`. Activations emit structured progress events such as `llm_waiting`, `capability_started`, `event_wait_started`, `agent_progress`, and `activation_completed` without exposing hidden chain-of-thought.

## Cancellation and restart

Cancellation is cooperative. Event waits check it during short completion waits; synchronous LLM, capability, and executor calls stop at their next safe boundary. Runtime shutdown cancels workers and waits for active threads before closing persistence.

Workers found in `running` state after restart are marked failed because cross-process activation recovery is not implemented. Their history remains auditable.
