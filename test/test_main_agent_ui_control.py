from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from pivot.control import ControlError
from pivot.runtime import PivotClient
from pivot.tui import AgentItem, PivotApp
from test_agents_actions_executors import _agent_runtime


def test_client_exposes_only_main_agent_to_user(tmp_path: Path) -> None:
    client = PivotClient(_agent_runtime(tmp_path))
    try:
        main = client.main_agent()
        assert client.create_session() is main
        assert client.create_session() is main
        assert client.sessions() == (main,)
        with pytest.raises(ControlError, match="only target the main agent"):
            client.get_session(str(uuid4()))
        operation_names = {item.name for item in client.control.operations()}
        assert {"agent.create", "agent.assign", "agent.delegate", "executor.execute"} <= operation_names
    finally:
        client.close()


@pytest.mark.asyncio
async def test_tui_shows_main_agent_and_workers_without_session_controls(tmp_path: Path) -> None:
    runtime = _agent_runtime(tmp_path)
    assert runtime.agents is not None
    client = PivotClient(runtime)
    app = PivotApp(client, runtime.agents.main_agent)

    async with app.run_test(size=(120, 36)):
        assert str(app.query_one("#conversation-title").render()) == "Main Agent"
        assert [item.record.role for item in app.query(AgentItem)] == ["main"]
        assert not app.query("#new-session")
        assert not app.query("#shortcut-new")
        assert str(app.query_one("#shortcut-sessions").label) == "Agents (Ctrl+B)"

    client.close()
