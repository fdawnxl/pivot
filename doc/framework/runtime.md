# Runtime and extension boundaries

`pivot.runtime.build_runtime` is the composition root. It turns a validated `PivotConfig` into a started `Runtime` without depending on the CLI or TUI.

## Assembly order

Runtime construction deliberately follows ownership dependencies:

1. Acquire the instance lease.
2. Discover, synchronize, and start dependency projects.
3. Discover capability and event scripts.
4. Build the event pool, polling supervisor, and event service.
5. Create the provider-neutral LLM client and executor registry.
6. Open `RuntimeStore` and obtain the stable main-Agent identity.
7. Build the main Agent and `AgentControl`.
8. Start the main reactor, event supervisor, and bridge subscribers.

If assembly fails, already-created resources are closed in reverse ownership order.

## LLM boundary

`LiteLLMClient` is the only module that imports LiteLLM, and it does so lazily. It accepts pivot `Message` objects and returns an untrusted provider response. `parse_response` extracts user-facing content and normalized `ToolCall` values from dict-like or object-like OpenAI-compatible responses.

Media content is preserved as provider-compatible parts. Because OpenAI-compatible APIs do not consistently accept media in a `tool` role, media returned by a capability is kept with the tool result for persistence and moved to a synthetic following `user` message at the provider boundary.

## Capability boundary

`CapabilityRegistry` owns stable descriptors and executable handlers. Instance scripts are discovered without importing their modules into pivot:

```text
think:   script.py -l        descriptor
         script.py -r        JSON string with the full method

measure: script.py -l        descriptor
         script.py -r NAME   JSON measurement

work:    script.py -l        descriptor
         script.py -x        JSON stdin -> JSON result
```

Think capabilities are prompt methods and have no execution side effect. Their full text is lazy-loaded through `pivot_read_think`. Measure and work capabilities are model-callable tools. All results must be JSON serializable; a result may contain `{"content": [...]}` to return multimodal evidence.

Capability subprocesses use `environment/<kind>` as their uv project, the instance as cwd, a restricted inherited environment, a timeout, and an output limit. One invalid script is skipped without preventing other capabilities from loading.

## Executor boundary

Executors are runtime-owned mechanisms for concrete machine actions. They are separate from capabilities, which describe domain methods or expose device-specific work.

The built-in `shell` executor invokes `/bin/sh -c` with:

- the instance as fixed cwd;
- an environment allowlist;
- a configurable maximum timeout;
- bounded stdout and stderr;
- structured `exit_code`, `stdout`, `stderr`, and `truncated` output.

It does not provide namespaces, seccomp, filesystem isolation, or command authorization.

## Dependency boundary

Dependencies are long-running services under `instance/dependencies/<project>`. Each first-level project has its own `pyproject.toml`, optional `uv.lock`, and `dependency.toml`.

`DependencyManager` runs `uv sync` once, records success with `.pivot-installed`, launches with `uv run --no-sync`, and observes readiness through the common D-Bus interface. Dependency state is one of `starting`, `ready`, `degraded`, `stopping`, `stopped`, or `error`.

Dependencies own their device or network protocol. pivot only manages their lifecycle and common health status.

## Shutdown

`Runtime.close` is idempotent and closes bridge subscriptions, the main reactor, worker control, the main Agent, event polling, dependencies, the inbox, the database, and finally the lease. `PivotClient.close` first stops its optional D-Bus service, then closes the control facade and runtime.

Cleanup intentionally continues after ordinary component errors and raises an `ExceptionGroup` after all release steps have been attempted.
