# Agent control, actions, and executors

## Main-agent ownership

Each pivot instance has one stable main-agent UUID derived from its resolved instance path. CLI, TUI, `PivotClient.run_main`, and the compatibility session methods all route user messages to this agent. Delegated workers are internal agents with separate histories under `memory/agents/<worker-uuid>/history.jsonl`; they are observable but never selectable as user conversations.

The main agent can solve a request directly or invoke `agent.delegate`. A delegate request contains a task and explicit capability/event allowlists:

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

Omitted allowlists are empty. An unknown capability or event rejects worker creation. Workers cannot create other workers. They can invoke `agent.report` with any JSON-serializable result, after which the main agent receives the report as its control action result and remains responsible for the user-facing answer.

## Unified model action protocol

The preferred provider tool is `pivot_action`. Every request has exactly three fields:

```json
{
  "kind": "capability | event | control | executor",
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

The detector removes the envelope from user-facing text, synthesizes an ordinary tool call when needed, and routes all four action kinds through the same session path. Existing native capability tools, `pivot_wait_event`, and `pivot_execute_shell` remain compatibility aliases and are normalized before execution.

## Internal control operations

The agent-facing control surface provides:

```text
agent.list
agent.get       {agent_id}
agent.create    {name?, capabilities?, events?}
agent.assign    {agent_id, task}
agent.delegate  {task, name?, capabilities?, events?}
agent.report    {result}                              # worker only
```

`agent.delegate` is the normal main-agent operation. It combines create, assign, synchronous bounded execution, and report delivery. The same create/assign/list/get operations are exported through `PivotControl` and therefore through generic D-Bus `Invoke` calls.

Main-turn cancellation is passed into the active worker. Model and subprocess requests remain cooperative: cancellation takes effect at their next safe return boundary, while event polling checks it directly.

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
