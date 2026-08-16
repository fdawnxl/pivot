# Build a minimal instance

An instance is a self-contained deployment: provider selection, device capabilities, event sources, dependency services, memory, and logs. pivot never assumes a global instance path.

## Prerequisites

- Python 3.11 or newer;
- [uv](https://docs.astral.sh/uv/);
- an OpenAI-compatible model endpoint or another model supported by LiteLLM;
- a D-Bus session or system bus only when dependencies or remote control require it.

From the pivot repository, install the locked project and development tools:

```bash
uv sync --group dev
```

## Initialize the directory

Choose an explicit path and run pivot once:

```bash
export PIVOT_INSTANCE_PATH=/opt/pivot/demo
uv run pivot --no-banner "hello"
```

The first run creates the base layout before provider validation. It may then report that provider configuration is missing; that is expected for an empty instance.

```text
/opt/pivot/demo/
├── capabilities/
│   ├── think/
│   ├── measure/
│   └── work/
├── dependencies/
├── environment/
│   ├── think/
│   ├── measure/
│   ├── work/
│   └── event/
├── events/
├── logs/
├── memory/
├── config.toml
└── credentials.toml
```

Initialization is idempotent and does not overwrite existing files.

## Configure a provider

Create `credentials.toml` and restrict it to the current user:

```toml
[providers.local]
model = "openai/local-model"
api_base = "http://127.0.0.1:8000/v1"
api_key = "replace-with-provider-secret"
```

```bash
chmod 600 "$PIVOT_INSTANCE_PATH/credentials.toml"
```

Select the provider in `config.toml`:

```toml
provider = "local"

[logging]
display_level = "info"
storage_level = "debug"
```

Credentials contain the complete provider connection. `config.toml` only selects a named provider. pivot does not read Codex or other user-level credential files.

## Start pivot

Interactive terminal UI:

```bash
uv run pivot
```

One-shot command:

```bash
uv run pivot "Describe your current capabilities"
```

Headless D-Bus runtime:

```bash
uv run pivot --dbus-only
```

You can use `--instance /path/to/instance` instead of the environment variable. One running process exclusively owns an instance; a second process will fail before starting dependencies.

## Add device behavior

Build integrations in this order:

1. Add a `measure` capability for an on-demand read.
2. Add a `work` capability for a narrow device operation.
3. Move shared or long-running hardware access into a dependency service.
4. Add an event source for monitored fields.
5. Add a bridge only when a condition should autonomously activate the main Agent.
6. Add a device adapter for transport concerns such as wake word, ASR, TTS, displays, or upstream messaging.

The following guides define each protocol precisely:

- [Configuration](configuration.md)
- [Capabilities](capabilities.md)
- [Events and dependencies](events-and-dependencies.md)
- [Adapters and deployment](adapters-and-deployment.md)
