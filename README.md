# pivot

**A layered Agent runtime for intelligent terminals, industrial controllers, and embodied systems.**

pivot gives one persistent main Agent a durable inbox, bounded memory, isolated device capabilities, event-driven workers, and a narrow control plane. Device code stays in an explicit **instance**, while the framework remains provider-neutral and replaceable.

```text
device I/O -> stimulus inbox -> main Agent -> capabilities / workers
                    |               |
                    +-> SQLite <-----+
                            |
                       durable outputs -> device I/O
```

## Why pivot

| Need | pivot's boundary |
| --- | --- |
| Connect sensors and actuators | Isolated `measure` and `work` capability scripts |
| Add domain reasoning | Lazy `think` capabilities |
| React to changing conditions | Shared event polling, edge waits, and durable bridges |
| Keep the device responsive | One main Agent with asynchronous scoped workers |
| Survive client disconnects | SQLite stimuli, outputs, task state, and sequence cursors |
| Integrate voice or device buses | Transport-neutral envelopes plus optional D-Bus control |
| Replace model providers | A provider-neutral LLM and response boundary over LiteLLM |

## Quick start

Requirements: Python 3.11+, [uv](https://docs.astral.sh/uv/), and an OpenAI-compatible or LiteLLM-supported model.

Install the project:

```bash
uv sync
```

Choose an instance directory. pivot creates its base structure without overwriting existing files:

```bash
export PIVOT_INSTANCE_PATH=/path/to/device-instance
uv run pivot --no-banner "hello"
```

The first run may stop after initialization because no provider is configured yet. Add `$PIVOT_INSTANCE_PATH/credentials.toml`:

```toml
[providers.local]
model = "openai/local-model"
api_base = "http://127.0.0.1:8000/v1"
api_key = "provider-secret"
```

Restrict it and select the provider:

```bash
chmod 600 "$PIVOT_INSTANCE_PATH/credentials.toml"
```

```toml
# config.toml
provider = "local"

[logging]
display_level = "info"
storage_level = "debug"
```

Start the interactive terminal:

```bash
uv run pivot
```

Or send one command:

```bash
uv run pivot "What can you observe?"
```

Use `--instance /path/to/instance` instead of `PIVOT_INSTANCE_PATH` when preferred. Run `uv run pivot --dbus-only` for a headless device service.

## The instance boundary

An instance contains everything specific to one deployment:

```text
instance/
├── capabilities/{think,measure,work}/   short isolated extensions
├── dependencies/<project>/              long-running device services
├── environment/{think,measure,work,event}/
├── events/                               monitored fields and bridge rules
├── memory/pivot.db                       Agents, stimuli, outputs, memory
├── logs/                                 runtime and dependency diagnostics
├── config.toml                           non-secret runtime settings
└── credentials.toml                      named providers, mode 0600
```

The framework does not implement wake words, ASR, TTS, camera capture, sensor drivers, or display rendering. Those belong to instance capabilities, dependencies, event sources, and adapters.

A typical intelligent terminal uses:

1. a dependency to own long-lived hardware connections;
2. measure capabilities for on-demand facts;
3. work capabilities for bounded physical actions;
4. event sources and bridges for autonomous attention;
5. an adapter that turns voice, buttons, or network messages into stimuli and consumes durable outputs.

## Core model

- The user and every external adapter address one stable main Agent.
- Every input enters a bounded, durable `StimulusEnvelope` queue.
- Each activation rebuilds current context from scoped tools, recent history, memory, and world state.
- Workers receive explicit capability and event allowlists.
- Worker reports return as stimuli, so the main Agent remains the only user-facing authority.
- State observations can refresh context without invoking an LLM.
- Outputs carry monotonic sequence numbers for disconnect recovery.

One process exclusively owns an instance. This protects dependency lifecycle, the main Agent, and writable state from concurrent local runtimes.

## Documentation

The [documentation index](doc/README.md) has two focused paths:

### Understand and extend pivot

- [Architecture](doc/framework/architecture.md)
- [Runtime and extension boundaries](doc/framework/runtime.md)
- [Agents and activations](doc/framework/agents.md)
- [Persistence and stimuli](doc/framework/persistence.md)
- [Event system](doc/framework/events.md)
- [Control and presentation](doc/framework/control.md)

### Build an intelligent-terminal instance

- [Build a minimal instance](doc/instance/getting-started.md)
- [Configure the instance](doc/instance/configuration.md)
- [Develop capabilities](doc/instance/capabilities.md)
- [Connect events and dependencies](doc/instance/events-and-dependencies.md)
- [Implement adapters and deploy](doc/instance/adapters-and-deployment.md)

## Development

```bash
uv sync --group dev
uv run pytest
uv run ruff check src test
uv run ruff format --check src test
```

The built-in shell executor has a fixed instance cwd, environment allowlist, timeout, and output cap, but it is not an operating-system sandbox. Add an OS-level isolation policy before executing commands that are not fully trusted.

Licensed under the [MIT License](LICENSE).
