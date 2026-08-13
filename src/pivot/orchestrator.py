"""Lightweight multi-agent orchestration built on isolated conversation sessions."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
import logging
from uuid import uuid4
from typing import Any

from .session import ConversationSession
from .logging import log_context

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AgentResult:
    agent_id: str
    response: str | None = None
    error: str | None = None


class AgentOrchestrator:
    """Run independent agents concurrently and collect every result."""

    def __init__(self, agents: dict[str, ConversationSession], *, max_workers: int | None = None) -> None:
        if not agents:
            raise ValueError("At least one agent is required")
        self.agents = dict(agents)
        self.max_workers = max_workers or len(agents)

    def run(self, user_input: Any) -> tuple[AgentResult, ...]:
        results: list[AgentResult] = []
        LOGGER.info("Agent orchestration started agents=%d", len(self.agents))
        with ThreadPoolExecutor(max_workers=self.max_workers, thread_name_prefix="pivot-agent") as pool:
            futures = {
                pool.submit(self._run_agent, agent_id, agent, user_input): agent_id
                for agent_id, agent in self.agents.items()
            }
            for future in as_completed(futures):
                agent_id = futures[future]
                try:
                    results.append(AgentResult(agent_id=agent_id, response=future.result()))
                except Exception as exc:
                    LOGGER.error("Agent execution failed agent_id=%s error_type=%s", agent_id, type(exc).__name__)
                    results.append(AgentResult(agent_id=agent_id, error=f"{type(exc).__name__}: {exc}"))
        ordered = tuple(sorted(results, key=lambda item: item.agent_id))
        LOGGER.info("Agent orchestration completed agents=%d failures=%d", len(ordered), sum(item.error is not None for item in ordered))
        return ordered

    @staticmethod
    def _run_agent(agent_id: str, agent: ConversationSession, user_input: Any) -> str:
        with log_context(correlation_id=str(uuid4()), agent_id=agent_id, session_id=agent.session_id):
            return agent.run(user_input)
