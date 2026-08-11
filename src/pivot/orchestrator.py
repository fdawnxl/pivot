"""Lightweight multi-agent orchestration built on isolated conversation sessions."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any

from .session import ConversationSession


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

    def run(self, user_input: str) -> tuple[AgentResult, ...]:
        results: list[AgentResult] = []
        with ThreadPoolExecutor(max_workers=self.max_workers, thread_name_prefix="pivot-agent") as pool:
            futures = {pool.submit(agent.run, user_input): agent_id for agent_id, agent in self.agents.items()}
            for future in as_completed(futures):
                agent_id = futures[future]
                try:
                    results.append(AgentResult(agent_id=agent_id, response=future.result()))
                except Exception as exc:
                    results.append(AgentResult(agent_id=agent_id, error=f"{type(exc).__name__}: {exc}"))
        return tuple(sorted(results, key=lambda item: item.agent_id))
