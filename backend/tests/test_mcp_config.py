"""Tests for MCP config generation and cleanup."""
import json
import tempfile
from pathlib import Path

import pytest

from backend.config import settings
from backend.mcp import (
    ccm_monitor_agent_server,
    ccm_skills_server,
    ccm_sub_agent_server,
)
from backend.services import mcp_config
from backend.services.mcp_config import (
    CCM_MONITOR_AGENT_TOOLS,
    CCM_SKILLS_TOOLS,
    CCM_SUB_AGENT_TOOLS,
    McpServerSpec,
    build_mcp_server_specs,
    build_monitor_agent_mcp_server_specs,
    build_sub_agent_mcp_server_specs,
    cleanup_mcp_config,
    cleanup_monitor_agent_mcp_config,
    cleanup_sub_agent_mcp_config,
    generate_mcp_config,
    generate_monitor_agent_mcp_config,
    generate_sub_agent_mcp_config,
    render_claude_mcp_config,
)


EXPECTED_MAIN_TOOLS = (
    "ccm_command_help",
    "ccm_read_skill",
    "ccm_read_user_skill",
    "ccm_create_skill",
    "ccm_distill",
    "ccm_enable_skill",
    "ccm_disable_skill",
    "create_monitor",
    "check_monitors",
    "stop_monitor",
    "create_sub_agent",
    "check_sub_agents",
    "stop_sub_agent",
)
EXPECTED_MONITOR_TOOLS = (
    "report_status",
    "mark_complete",
    "get_context",
)
EXPECTED_SUB_AGENT_TOOLS = (
    "report_progress",
    "submit_result",
    "get_context",
)


def _assert_ccm_skills_config(path, task_id: int, api_base: str):
    """ccm_skills server 始终包含，参数齐全。"""
    assert path is not None
    assert path.exists()

    config = json.loads(path.read_text())
    assert "mcpServers" in config
    assert "ccm_skills" in config["mcpServers"]

    server = config["mcpServers"]["ccm_skills"]
    assert server["command"].endswith("python3")
    assert "--task-id" in server["args"]
    assert str(task_id) in server["args"]
    assert "--api-base" in server["args"]
    assert api_base in server["args"]
    assert "-m" in server["args"]
    assert "backend.mcp.ccm_skills_server" in server["args"]


def test_generate_mcp_config_none_skills_still_includes_ccm_skills():
    """skills=None 时也返回配置：ccm_skills 提供 $help 等默认命令。"""
    path = generate_mcp_config(1, None, api_base="http://localhost:8000")
    _assert_ccm_skills_config(path, 1, "http://localhost:8000")
    path.unlink(missing_ok=True)


def test_generate_mcp_config_empty_skills_still_includes_ccm_skills():
    """skills={} 时也返回配置（ccm_skills 始终包含）。"""
    path = generate_mcp_config(1, {}, api_base="http://localhost:8000")
    _assert_ccm_skills_config(path, 1, "http://localhost:8000")
    path.unlink(missing_ok=True)


def test_generate_mcp_config_skills_do_not_add_extra_servers():
    """启用任意 skill 不再产生独立的 per-skill server，只有 ccm_skills 一个入口。"""
    path = generate_mcp_config(1, {"worker": True, "monitor": True}, api_base="http://localhost:8000")
    config = json.loads(path.read_text())
    assert set(config["mcpServers"].keys()) == {"ccm_skills"}
    path.unlink(missing_ok=True)


def test_generate_mcp_config_monitor_enabled():
    path = generate_mcp_config(99, {"monitor": True}, api_base="http://test:8000")
    _assert_ccm_skills_config(path, 99, "http://test:8000")
    path.unlink(missing_ok=True)


def test_generate_mcp_config_file_path():
    path = generate_mcp_config(42, {"monitor": True}, api_base="http://localhost:8000")
    expected = Path(tempfile.gettempdir()) / "ccm_mcp_42.json"
    assert path == expected
    path.unlink(missing_ok=True)


def test_cleanup_mcp_config():
    path = generate_mcp_config(77, {"monitor": True}, api_base="http://localhost:8000")
    assert path.exists()
    cleanup_mcp_config(77)
    assert not path.exists()


def test_cleanup_mcp_config_missing_file():
    cleanup_mcp_config(999999)


def _set_spec_snapshot_runtime(monkeypatch):
    monkeypatch.setattr(mcp_config, "_CCM_ROOT", "/srv/ccm")
    monkeypatch.setattr(
        mcp_config,
        "_VENV_PYTHON",
        "/srv/ccm/.venv/bin/python3",
    )
    monkeypatch.setattr(settings, "auth_token", "secret-token")


def test_main_mcp_server_spec_snapshot(monkeypatch):
    _set_spec_snapshot_runtime(monkeypatch)

    assert build_mcp_server_specs(
        42,
        {"monitor": True},
        api_base="http://manager:8321",
    ) == (
        McpServerSpec(
            name="ccm_skills",
            command="/srv/ccm/.venv/bin/python3",
            args=(
                "-m",
                "backend.mcp.ccm_skills_server",
                "--task-id",
                "42",
                "--api-base",
                "http://manager:8321",
                "--auth-token",
                "secret-token",
            ),
            cwd="/srv/ccm",
            required=True,
            enabled_tools=EXPECTED_MAIN_TOOLS,
            startup_timeout_sec=10.0,
            tool_timeout_sec=60.0,
        ),
    )
    assert CCM_SKILLS_TOOLS == EXPECTED_MAIN_TOOLS


def test_monitor_agent_mcp_server_spec_snapshot(monkeypatch):
    _set_spec_snapshot_runtime(monkeypatch)

    assert build_monitor_agent_mcp_server_specs(
        7,
        42,
        api_base="http://manager:8321",
    ) == (
        McpServerSpec(
            name="ccm_monitor_agent",
            command="/srv/ccm/.venv/bin/python3",
            args=(
                "-m",
                "backend.mcp.ccm_monitor_agent_server",
                "--monitor-session-id",
                "7",
                "--task-id",
                "42",
                "--api-base",
                "http://manager:8321",
                "--auth-token",
                "secret-token",
            ),
            cwd="/srv/ccm",
            required=True,
            enabled_tools=EXPECTED_MONITOR_TOOLS,
            startup_timeout_sec=10.0,
            tool_timeout_sec=60.0,
        ),
    )
    assert CCM_MONITOR_AGENT_TOOLS == EXPECTED_MONITOR_TOOLS


def test_sub_agent_mcp_server_spec_snapshot(monkeypatch):
    _set_spec_snapshot_runtime(monkeypatch)

    assert build_sub_agent_mcp_server_specs(
        9,
        42,
        api_base="http://manager:8321",
    ) == (
        McpServerSpec(
            name="ccm_sub_agent",
            command="/srv/ccm/.venv/bin/python3",
            args=(
                "-m",
                "backend.mcp.ccm_sub_agent_server",
                "--sub-agent-session-id",
                "9",
                "--task-id",
                "42",
                "--api-base",
                "http://manager:8321",
                "--auth-token",
                "secret-token",
            ),
            cwd="/srv/ccm",
            required=True,
            enabled_tools=EXPECTED_SUB_AGENT_TOOLS,
            startup_timeout_sec=10.0,
            tool_timeout_sec=60.0,
        ),
    )
    assert CCM_SUB_AGENT_TOOLS == EXPECTED_SUB_AGENT_TOOLS


@pytest.mark.parametrize(
    ("server_module", "enabled_tools"),
    [
        (ccm_skills_server, CCM_SKILLS_TOOLS),
        (ccm_monitor_agent_server, CCM_MONITOR_AGENT_TOOLS),
        (ccm_sub_agent_server, CCM_SUB_AGENT_TOOLS),
    ],
)
def test_spec_enabled_tools_match_registered_server_tools(
    server_module,
    enabled_tools,
):
    assert set(enabled_tools) == set(server_module.mcp._tool_manager._tools)


@pytest.mark.parametrize(
    ("generator", "generator_args", "cleanup", "expected_name", "expected_args"),
    [
        (
            generate_mcp_config,
            (42, {"monitor": True}),
            lambda: cleanup_mcp_config(42),
            "ccm_skills",
            [
                "-m",
                "backend.mcp.ccm_skills_server",
                "--task-id",
                "42",
            ],
        ),
        (
            generate_monitor_agent_mcp_config,
            (7, 42),
            lambda: cleanup_monitor_agent_mcp_config(7),
            "ccm_monitor_agent",
            [
                "-m",
                "backend.mcp.ccm_monitor_agent_server",
                "--monitor-session-id",
                "7",
                "--task-id",
                "42",
            ],
        ),
        (
            generate_sub_agent_mcp_config,
            (9, 42),
            lambda: cleanup_sub_agent_mcp_config(9),
            "ccm_sub_agent",
            [
                "-m",
                "backend.mcp.ccm_sub_agent_server",
                "--sub-agent-session-id",
                "9",
                "--task-id",
                "42",
            ],
        ),
    ],
)
def test_claude_json_output_remains_compatible(
    monkeypatch,
    generator,
    generator_args,
    cleanup,
    expected_name,
    expected_args,
):
    _set_spec_snapshot_runtime(monkeypatch)
    api_base = "http://manager:8321"

    path = generator(*generator_args, api_base=api_base)
    try:
        assert json.loads(path.read_text()) == {
            "mcpServers": {
                expected_name: {
                    "command": "/srv/ccm/.venv/bin/python3",
                    "args": [
                        *expected_args,
                        "--api-base",
                        api_base,
                        "--auth-token",
                        "secret-token",
                    ],
                    "cwd": "/srv/ccm",
                }
            }
        }
    finally:
        cleanup()


def test_default_api_base_and_empty_auth_token(monkeypatch):
    monkeypatch.setattr(settings, "host", "0.0.0.0")
    monkeypatch.setattr(settings, "port", 8321)
    monkeypatch.setattr(settings, "auth_token", "")

    (spec,) = build_mcp_server_specs(42)

    assert spec.args[-2:] == ("--api-base", "http://127.0.0.1:8321")
    assert "--auth-token" not in spec.args


@pytest.mark.parametrize(
    ("root", "python"),
    [
        ("/opt/Claude Code Manager", "/opt/Claude Code Manager/.venv/bin/python3"),
        (
            r"C:\CCM 工作区",
            r"C:\CCM 工作区\.venv\Scripts\python.exe",
        ),
    ],
)
def test_platform_paths_are_preserved(monkeypatch, root, python):
    monkeypatch.setattr(mcp_config, "_CCM_ROOT", root)
    monkeypatch.setattr(mcp_config, "_VENV_PYTHON", python)
    monkeypatch.setattr(settings, "auth_token", "")

    (spec,) = build_mcp_server_specs(
        42,
        api_base="http://127.0.0.1:8000",
    )
    rendered = render_claude_mcp_config((spec,))

    assert spec.command == python
    assert spec.cwd == root
    assert rendered["mcpServers"]["ccm_skills"]["command"] == python
    assert rendered["mcpServers"]["ccm_skills"]["cwd"] == root


def test_claude_renderer_includes_env_but_not_provider_metadata():
    spec = McpServerSpec(
        name="example",
        command="python",
        args=("-m", "example"),
        cwd="/workspace",
        env={"LANG": "zh_CN.UTF-8"},
        required=True,
        enabled_tools=("example_tool",),
        startup_timeout_sec=15,
        tool_timeout_sec=90,
    )

    assert render_claude_mcp_config((spec,)) == {
        "mcpServers": {
            "example": {
                "command": "python",
                "args": ["-m", "example"],
                "cwd": "/workspace",
                "env": {"LANG": "zh_CN.UTF-8"},
            }
        }
    }


def test_mcp_server_spec_collections_are_immutable():
    args = ["-m", "example"]
    env = {"TOKEN": "secret"}
    tools = ["example_tool"]

    spec = McpServerSpec(
        name="example",
        command="python",
        args=args,
        env=env,
        enabled_tools=tools,
    )
    args.append("--changed")
    env["TOKEN"] = "changed"
    tools.append("changed_tool")

    assert spec.args == ("-m", "example")
    assert dict(spec.env) == {"TOKEN": "secret"}
    assert spec.enabled_tools == ("example_tool",)
    with pytest.raises(TypeError):
        spec.env["TOKEN"] = "cannot-change"


def test_claude_renderer_rejects_duplicate_server_names():
    spec = McpServerSpec(name="duplicate", command="python")

    with pytest.raises(ValueError, match="Duplicate MCP server name: duplicate"):
        render_claude_mcp_config((spec, spec))
