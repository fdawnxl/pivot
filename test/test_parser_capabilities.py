from pathlib import Path

import pytest

from pivot.capabilities import CapabilityError, CapabilityRegistry, THINK_READER_NAME
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
        environment = workspace / "environment" / kind
        environment.mkdir(parents=True)
        (environment / "pyproject.toml").write_text(
            f'[project]\nname="test-{kind}"\nversion="0.1.0"\nrequires-python=">=3.11"\ndependencies=[]\n',
            encoding="utf-8",
        )
    (workspace / "capabilities" / "think" / "a.py").write_text(
        'import json,sys\nBODY="""Full private planning method."""\n'
        'D={"name":"t","description":"planning summary","parameters":{}}\n'
        'print(json.dumps(D if "-l" in sys.argv else BODY))\n', encoding="utf-8"
    )
    (workspace / "capabilities" / "work" / "b.py").write_text(
        'import json,sys\nD={"name":"w","description":"work","parameters":{"type":"object"}}\n'
        'print(json.dumps(D if "-l" in sys.argv else {"received":json.load(sys.stdin)}))\n', encoding="utf-8"
    )
    (workspace / "capabilities" / "measure" / "c.py").write_text(
        'import json,sys\nD={"name":"m","description":"measure","parameters":{"type":"object"}}\n'
        'print(json.dumps(D if "-l" in sys.argv else {"feature":sys.argv[-1]}))\n', encoding="utf-8"
    )
    registry = CapabilityRegistry()
    register_workspace_capabilities(workspace, registry, workspace / "environment")
    assert {item.kind for item in registry.descriptors()} == {"think", "measure", "work"}
    assert "Full private planning method" not in str(registry.prompt_context())
    assert registry.execute(ToolCall(THINK_READER_NAME, {"name": "t"}))["content"] == "Full private planning method."
    assert registry.execute(ToolCall("m", {"feature": "temperature"})) == {"feature": "temperature"}
    assert registry.execute(ToolCall("w", {"value": 3})) == {"received": {"value": 3}}
    assert registry.execute(ToolCall("w", {"_script": "/tmp/override"})) == {"received": {"_script": "/tmp/override"}}


def test_registry_rejects_non_json_work_results() -> None:
    registry = CapabilityRegistry()
    registry.register(CapabilityDescriptor("bad", "work", "bad"), lambda: object())
    with pytest.raises(CapabilityError, match="non-JSON"):
        registry.execute(ToolCall("bad"))


def test_capability_runner_preserves_dbus_addresses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from pivot.capabilities.registry import CapabilityScriptRunner

    environment = tmp_path / "environment"
    environment.mkdir()
    (environment / "pyproject.toml").write_text(
        '[project]\nname="dbus-capability"\nversion="0.1.0"\nrequires-python=">=3.11"\ndependencies=[]\n',
        encoding="utf-8",
    )
    script = tmp_path / "read_env.py"
    script.write_text(
        "import json,os\nprint(json.dumps(os.environ.get('DBUS_SESSION_BUS_ADDRESS')))\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/tmp/pivot-test-bus")

    assert CapabilityScriptRunner(environment, workspace=tmp_path)._run(script, ["-l"]) == (
        "unix:path=/tmp/pivot-test-bus"
    )
