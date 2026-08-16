# pivot technical documentation

This documentation is organized around two different jobs. Framework contributors need to understand why pivot is structured as it is and where runtime responsibilities live. Device developers need a practical contract for building an instance without importing pivot internals.

## Framework design and implementation

Read this section when changing pivot itself:

1. [Architecture](framework/architecture.md) explains the dependency direction, ownership model, and end-to-end request flow.
2. [Runtime and extension boundaries](framework/runtime.md) covers assembly, capabilities, executors, dependencies, and shutdown.
3. [Agents and activations](framework/agents.md) describes the persistent main Agent, workers, action routing, reporting, and cancellation.
4. [Persistence and stimuli](framework/persistence.md) documents SQLite ownership, context construction, the durable inbox, outputs, and recovery.
5. [Event system](framework/events.md) explains source polling, waits, edge semantics, and Event-to-Stimulus bridges.
6. [Control and presentation](framework/control.md) defines `PivotClient`, D-Bus, the CLI, and the TUI boundary.

## Instance development

Read this section when adapting pivot to an intelligent terminal or device:

1. [Getting started](instance/getting-started.md) creates and runs a minimal instance.
2. [Configuration](instance/configuration.md) lists provider, runtime, queue, logging, and lifecycle settings.
3. [Capabilities](instance/capabilities.md) shows how to implement `think`, `measure`, and `work` scripts.
4. [Events and dependencies](instance/events-and-dependencies.md) covers monitored fields, bridge rules, and long-running D-Bus services.
5. [Adapters and deployment](instance/adapters-and-deployment.md) connects device I/O to stimuli and outputs, then prepares the instance for operation.

## Terminology

- **instance**: all runtime data and device-specific integrations for one deployment.
- **main Agent**: the only Agent addressed by users and external adapters.
- **worker**: a scoped Agent created by the main Agent for finite work or long event waits.
- **activation**: one bounded model/action run for a persistent Agent.
- **capability**: a `think`, `measure`, or `work` extension supplied by an instance.
- **dependency**: a long-running external uv project managed by pivot.
- **adapter**: a device-facing process that injects stimuli or consumes outputs through the control interface.
