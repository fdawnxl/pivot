from pathlib import Path

import pytest

from pivot.capabilities import CapabilityError, CapabilityRegistry
from pivot.capabilities.discovery import register_workspace_capabilities
from pivot.models import CapabilityDescriptor, ToolCall
from pivot.parser import ResponseParseError, parse_response


def test_parse_openai_dict_response() -> None:
    parsed = parse_response({"choices": [{"message": {"content": "done", "tool_calls": [{"id": "1", "function": {"name": "read_temp", "arguments": '{"unit":"C"}'}}]}}]})
    assert parsed.text == "done"
    assert parsed.tool_calls[0].arguments == {"unit": "C"}


def test_parse_rejects_bad_arguments() -> None:
    with pytest.raises(ResponseParseError):
        parse_response({"choices": [{"message": {"tool_calls": [{"function": {"name": "x", "arguments": "oops"}}]}}]})


def test_registry_dispatch_and_tools() -> None:
    registry = CapabilityRegistry()
    registry.register(CapabilityDescriptor("add", "work", "Add numbers", {"type": "object"}), lambda a, b: a + b)
    assert registry.execute(ToolCall("add", {"a": 2, "b": 3})) == 5
    assert registry.llm_tools()[0]["function"]["name"] == "add"
    with pytest.raises(CapabilityError):
        registry.execute(ToolCall("missing"))


def test_measure_runner_rejects_missing_script(tmp_path: Path) -> None:
    from pivot.capabilities.registry import MeasureRunner

    with pytest.raises(CapabilityError):
        MeasureRunner(tmp_path).list_features(tmp_path / "missing.py")


def test_workspace_capability_discovery_loads_all_kinds(tmp_path: Path) -> None:
    workspace = tmp_path
    for kind in ("think", "measure", "work"):
        (workspace / "capabilities" / kind).mkdir(parents=True)
    (workspace / "capabilities" / "think" / "a.py").write_text(
        "from pivot.models import CapabilityDescriptor\nDESCRIPTOR = CapabilityDescriptor('t', 'think', 't')\n", encoding="utf-8"
    )
    (workspace / "capabilities" / "work" / "b.py").write_text(
        "from pivot.models import CapabilityDescriptor\nDESCRIPTOR = CapabilityDescriptor('w', 'work', 'w')\ndef handle(): return 'ok'\n", encoding="utf-8"
    )
    (workspace / "capabilities" / "measure" / "c.py").write_text(
        "DESCRIPTOR = {'name': 'm', 'description': 'm', 'parameters': {}}\n", encoding="utf-8"
    )
    registry = CapabilityRegistry()
    register_workspace_capabilities(workspace, registry, workspace / "measure-env")
    assert {item.kind for item in registry.descriptors()} == {"think", "measure", "work"}
