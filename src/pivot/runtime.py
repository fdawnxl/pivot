"""Runtime assembly independent from any terminal client."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from .agents import AgentControl
from .capabilities import CapabilityRegistry
from .capabilities.discovery import register_instance_capabilities
from .config import PivotConfig
from .dependencies import DependencyManager
from .events import EventPool, EventScriptRunner, EventService, EventSupervisor, load_event_scripts_isolated
from .executors import ExecutorRegistry, ShellExecutor
from .llm import LiteLLMClient
from .memory import TextMemory
from .session import CancellationToken, ConversationSession, ProgressCallback, SessionManager

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Runtime:
    """Fully assembled pivot services, usable without the CLI."""

    config: PivotConfig
    registry: CapabilityRegistry
    events: EventPool
    event_service: EventService
    sessions: SessionManager
    dependencies: DependencyManager | None = None
    executors: ExecutorRegistry | None = None
    agents: AgentControl | None = None

    def close(self) -> None:
        """Release runtime-owned external processes."""

        if self.dependencies is not None:
            self.dependencies.close()


def build_runtime(config: PivotConfig) -> Runtime:
    """Build capability, event, LLM, memory, and session services."""

    dependencies = DependencyManager(
        config.instance_path,
        install_timeout=config.dependency_install_timeout,
        start_timeout=config.dependency_start_timeout,
        dbus_timeout=config.dependency_dbus_timeout,
        stop_timeout=config.dependency_stop_timeout,
    )
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
        manager = SessionManager(
            llm=llm,
            capabilities=registry,
            memory=TextMemory(config.instance_path / "memory"),
            events=event_pool,
            event_service=event_service,
            executors=executors,
            max_rounds=config.max_rounds,
        )
        main_agent_id = str(uuid5(NAMESPACE_URL, f"pivot-main-agent:{config.instance_path}"))
        main_agent = manager.get(main_agent_id)
        agents = AgentControl(
            main_agent,
            llm=llm,
            capabilities=registry,
            child_memory=TextMemory(config.instance_path / "memory" / "agents"),
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
        return Runtime(config, registry, event_pool, event_service, manager, dependencies, executors, agents)
    except BaseException:
        dependencies.close()
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

    def create_session(self) -> ConversationSession:
        """Return the sole user-facing main agent conversation."""

        return self.control.create_session()

    def main_agent(self) -> ConversationSession:
        """Return the sole user-facing main agent."""

        if self.runtime.agents is not None:
            return self.runtime.agents.main_agent
        sessions = self.control.sessions()
        return sessions[0] if sessions else self.control.create_session()

    def get_session(self, session_id: str) -> ConversationSession:
        """Load or create a canonical UUID conversation."""

        return self.control.get_session(session_id)

    def select_session(self, session_id: str) -> ConversationSession:
        """Select the conversation used by implicit control operations."""

        return self.control.select_session(session_id)

    def run(
        self,
        session_id: str,
        user_input: Any,
        *,
        progress: ProgressCallback | None = None,
        cancellation: CancellationToken | None = None,
    ) -> str:
        """Run one conversation turn without involving terminal UI code."""

        return self.control.run(session_id, user_input, progress=progress, cancellation=cancellation)

    def run_main(
        self,
        user_input: Any,
        *,
        progress: ProgressCallback | None = None,
        cancellation: CancellationToken | None = None,
    ) -> str:
        """Run one turn through the main agent."""

        return self.control.run(self.main_agent().session_id, user_input, progress=progress, cancellation=cancellation)

    def sessions(self) -> tuple[ConversationSession, ...]:
        """List sessions currently managed by this client."""

        return self.control.sessions()

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
