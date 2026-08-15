# Agent control, actions, and executors

## Main-Agent ownership

Each pivot instance has one stable main-Agent UUID stored in `memory/pivot.db`. CLI, TUI, `PivotClient.run_main`, and D-Bus `SendMessage` all route user input to this Agent. Delegated workers have separate identities and activation histories in the same database; they are observable but never selectable as user endpoints.

The main Agent can solve a request directly or invoke asynchronous `agent.delegate`:

```json
{
  "kind": "control",
  "name": "agent.delegate",
  "arguments": {
    "name": "sensor-inspector",
    "task": "Inspect the temperature evidence and report anomalies.",
    "capabilities": ["read_temperature", "analyze_samples"],
    "events": ["temperature_changed"]
  }
}
```

Omitted allowlists are empty. An unknown capability or event rejects worker creation. Workers cannot create other workers. They can invoke `agent.report` with any JSON-serializable result. Delegation returns an accepted worker snapshot immediately; after the worker reaches a terminal state, its report or error is submitted as an internal main-Agent mailbox item. The main Agent remains responsible for later user-facing updates.

## Unified model action protocol

The preferred provider tool is `pivot_action`. Every request has exactly three fields:

```json
{
  "kind": "capability | event | control | executor | memory",
  "name": "operation-name",
  "arguments": {}
}
```

For models without native tool calling, the detector accepts the same JSON in either fixed form:

```text
<pivot-action>{"kind":"executor","name":"shell","arguments":{"command":"pwd"}}</pivot-action>
```

```pivot-action
{"kind":"event","name":"wait","arguments":{"event":"ready","operator":"==","expected":true,"timeout":30}}
```

The detector removes the envelope from user-facing text, synthesizes an ordinary tool call when needed, and routes all action kinds through the activation core. Direct capability tools, `pivot_wait_event`, and executor tools are normalized into the same route.

## Agent operations

The Agent-facing control surface provides:

```text
agent.list
agent.get       {agent_id}
agent.create    {name?, capabilities?, events?}
agent.assign    {agent_id, task}
agent.delegate  {task, name?, capabilities?, events?}
agent.report    {result}                              # worker only
```

`agent.delegate` combines create and asynchronous assignment; report delivery is a later mailbox activation. Main activation completion does not wait for the worker. Long event waits are therefore assigned to workers, while the main Agent remains available for queued user input.

Cancellation is cooperative. Model and subprocess requests stop at their next safe return boundary, while event polling checks cancellation directly. Runtime shutdown cancels active work and waits for Agent cleanup before closing durable memory.

## Memory operations

The same action protocol exposes:

```text
memory.remember {kind, content, confidence?, valid_for?, supersedes?}
memory.recall   {query, limit?}
memory.forget   {memory_id}
```

These operations target the calling Agent's namespace. Stored records keep their source, confidence, validity, sensitivity, and supersession metadata. Full details are in [memory](memory.md).

## Executors

Executors are concrete machine-action backends, separate from capabilities that describe domain methods or expose sensors. The initial executor is `shell`:

```json
{
  "kind": "executor",
  "name": "shell",
  "arguments": {
    "command": "command text",
    "timeout": 10
  }
}
```

The command runs as `/bin/sh -c` with `shell=False` at the subprocess API boundary. Its cwd is the resolved instance, its environment is allowlisted, its timeout cannot exceed `executor_timeout`, and stdout/stderr are bounded by `executor_max_output_bytes`. The result contains `exit_code`, `stdout`, `stderr`, and `truncated`.

This boundary prevents accidental coupling to the pivot interpreter but is not an operating-system sandbox. Deployments that process hostile prompts or commands must add a container, namespace, seccomp, or equivalent policy around executor processes.
