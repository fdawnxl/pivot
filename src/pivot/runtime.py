"""Runtime assembly independent from any terminal client."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any
from .agents import AgentControl
from .activation import CancellationToken, PersistentAgent, ProgressCallback
from .capabilities import CapabilityRegistry
from .capabilities.discovery import register_instance_capabilities
from .config import PivotConfig
from .dependencies import DependencyManager
from .events import EventPool, EventScriptRunner, EventService, EventSupervisor, load_event_scripts_isolated
from .executors import ExecutorRegistry, ShellExecutor
from .llm import LiteLLMClient
from .lease import RuntimeLease
from .memory import ContextBuilder, MemoryService, MemoryStore

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Runtime:
    """Fully assembled pivot services, usable without the CLI."""

    config: PivotConfig
    registry: CapabilityRegistry
    events: EventPool
    event_service: EventService
    memory: MemoryStore
    main_agent: PersistentAgent
    dependencies: DependencyManager | None = None
    executors: ExecutorRegistry | None = None
    agents: AgentControl | None = None
    lease: RuntimeLease | None = None

    def close(self) -> None:
        """Release runtime-owned external processes."""

        if self.agents is not None:
            self.agents.close()
        self.main_agent.wait_until_idle()
        if self.dependencies is not None:
            self.dependencies.close()
        self.memory.close()
        if self.lease is not None:
            self.lease.release()


def build_runtime(config: PivotConfig) -> Runtime:
    """Build capability, event, LLM, memory, and agent services."""

    lease = RuntimeLease(config.instance_path)
    lease.acquire()
    dependencies = DependencyManager(
        config.instance_path,
        install_timeout=config.dependency_install_timeout,
        start_timeout=config.dependency_start_timeout,
        dbus_timeout=config.dependency_dbus_timeout,
        stop_timeout=config.dependency_stop_timeout,
    )
    memory: MemoryStore | None = None
    try:
        dependencies.start_all()
        registry = CapabilityRegistry()
        environment_root = config.instance_path / "environment"
        register_instance_capabilities(
            config.instance_path,
            registry,
            environment_root,
            timeout=config.capability_timeout,
        )
        event_pool = EventPool()
        event_runner = EventScriptRunner(
            environment_root / "event",
            instance=config.instance_path,
            timeout=config.capability_timeout,
        )
        for event in load_event_scripts_isolated(str(config.instance_path / "events"), event_runner):
            try:
                event_pool.register(event)
            except Exception as exc:
                LOGGER.warning("Unable to register instance event %s: %s", event.name, exc)
        event_service = EventService(
            event_pool,
            EventSupervisor(event_pool, event_runner),
            poll_interval=config.event_poll_interval,
            max_wait=config.event_max_wait,
        )
        llm = LiteLLMClient(
            config.provider.model,
            api_base=config.provider.api_base,
            api_key=config.provider.api_key,
            timeout=config.llm_timeout,
        )
        executors = ExecutorRegistry()
        executors.register(
            ShellExecutor(
                config.instance_path,
                timeout=config.executor_timeout,
                max_output_bytes=config.executor_max_output_bytes,
            )
        )
        memory = MemoryStore(config.instance_path / "memory")
        memory_service = MemoryService(memory)
        main_agent = PersistentAgent(
            memory.main_agent_id(),
            llm=llm,
            capabilities=registry,
            memory=memory,
            events=event_pool,
            event_service=event_service,
            executors=executors,
            memory_service=memory_service,
            context_builder=ContextBuilder(memory),
            max_rounds=config.max_rounds,
        )
        agents = AgentControl(
            main_agent,
            llm=llm,
            capabilities=registry,
            memory=memory,
            events=event_pool,
            event_service=event_service,
            executors=executors,
            max_rounds=config.max_rounds,
        )
        LOGGER.info(
            "Runtime assembly completed dependencies=%d capabilities=%d events=%d executors=%d",
            len(dependencies.descriptors()),
            len(registry.descriptors()),
            len(event_pool.descriptors()),
            len(executors.descriptors()),
        )
        return Runtime(
            config,
            registry,
            event_pool,
            event_service,
            memory,
            main_agent,
            dependencies,
            executors,
            agents,
            lease,
        )
    except BaseException:
        dependencies.close()
        if memory is not None:
            memory.close()
        lease.release()
        raise


class PivotClient:
    """Provider-neutral application facade for non-CLI integrations."""

    def __init__(self, runtime: Runtime) -> None:
        from .control import PivotControl

        self.runtime = runtime
        self.control = PivotControl(runtime)
        self._dbus_service: object | None = None

    @classmethod
    def open(cls, *, instance_path: str | None = None) -> "PivotClient":
        """Load configuration and create a reusable pivot client."""

        return cls(build_runtime(PivotConfig.load(instance_path=instance_path)))

    def main_agent(self) -> PersistentAgent:
        """Return the sole user-facing main agent."""

        return self.runtime.main_agent

    def run_main(
        self,
        user_input: Any,
        *,
        progress: ProgressCallback | None = None,
        cancellation: CancellationToken | None = None,
    ) -> str:
        """Run one turn through the main agent."""

        return self.control.run_main(user_input, progress=progress, cancellation=cancellation)

    def start_dbus(
        self,
        *,
        bus: str = "session",
        service_name: str = "org.pivot.Control",
        bus_address: str | None = None,
        start_timeout: float = 5.0,
    ) -> object:
        """Export the shared control surface through D-Bus."""

        from .dbus_control import ControlDBusService

        if self._dbus_service is None:
            service = ControlDBusService(
                self.control,
                bus=bus,
                service_name=service_name,
                bus_address=bus_address,
                start_timeout=start_timeout,
            )
            service.start()
            self._dbus_service = service
        return self._dbus_service

    def close(self) -> None:
        """Release dependency processes owned by this client runtime."""

        if self._dbus_service is not None:
            self._dbus_service.stop()
            self._dbus_service = None
        self.control.close()
        self.runtime.close()

    def __enter__(self) -> "PivotClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


__all__ = ["PivotClient", "Runtime", "build_runtime"]
