"""CCM MCP server specs and provider-specific config generation.

The server description is provider-neutral.  Existing callers still receive a
Claude-compatible JSON file; future providers can render the same specs without
duplicating task/session context construction.
"""

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Mapping, Sequence


_CCM_ROOT = str(Path(__file__).resolve().parent.parent.parent)
_VENV_PYTHON = str(Path(_CCM_ROOT) / ".venv" / "bin" / "python3")
_MCP_STARTUP_TIMEOUT_SEC = 10.0
_MCP_TOOL_TIMEOUT_SEC = 60.0

CCM_SKILLS_TOOLS = (
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
CCM_MONITOR_AGENT_TOOLS = (
    "report_status",
    "mark_complete",
    "get_context",
)
CCM_SUB_AGENT_TOOLS = (
    "report_progress",
    "submit_result",
    "get_context",
)


@dataclass(frozen=True, slots=True)
class McpServerSpec:
    """Provider-neutral description of one stdio MCP server."""

    name: str
    command: str
    args: tuple[str, ...] = ()
    cwd: str | None = None
    env: Mapping[str, str] = field(default_factory=dict)
    required: bool = False
    enabled_tools: tuple[str, ...] = ()
    startup_timeout_sec: float | None = None
    tool_timeout_sec: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "args", tuple(self.args))
        object.__setattr__(self, "env", MappingProxyType(dict(self.env)))
        object.__setattr__(self, "enabled_tools", tuple(self.enabled_tools))


def _api_base_and_auth_token(api_base: str | None) -> tuple[str, str]:
    from backend.config import settings

    if api_base is None:
        host = settings.host if settings.host != "0.0.0.0" else "127.0.0.1"
        api_base = f"http://{host}:{settings.port}"
    return api_base, getattr(settings, "auth_token", "") or ""


def _ccm_server_spec(
    *,
    name: str,
    module: str,
    context_args: Sequence[str],
    enabled_tools: tuple[str, ...],
    api_base: str | None,
) -> McpServerSpec:
    resolved_api_base, auth_token = _api_base_and_auth_token(api_base)
    args = [
        "-m",
        module,
        *context_args,
        "--api-base",
        resolved_api_base,
    ]
    if auth_token:
        args.extend(["--auth-token", auth_token])

    return McpServerSpec(
        name=name,
        command=_VENV_PYTHON,
        args=tuple(args),
        cwd=_CCM_ROOT,
        required=True,
        enabled_tools=enabled_tools,
        startup_timeout_sec=_MCP_STARTUP_TIMEOUT_SEC,
        tool_timeout_sec=_MCP_TOOL_TIMEOUT_SEC,
    )


def build_mcp_server_specs(
    task_id: int,
    enabled_skills: dict | None = None,
    api_base: str | None = None,
) -> tuple[McpServerSpec, ...]:
    """Build the main task's CCM MCP server specs.

    ``enabled_skills`` remains part of the public API for compatibility.  The
    unified ``ccm_skills`` server is always present and decides tool behaviour
    from task state at call time.
    """

    return (
        _ccm_server_spec(
            name="ccm_skills",
            module="backend.mcp.ccm_skills_server",
            context_args=("--task-id", str(task_id)),
            enabled_tools=CCM_SKILLS_TOOLS,
            api_base=api_base,
        ),
    )


def build_monitor_agent_mcp_server_specs(
    monitor_session_id: int,
    task_id: int,
    api_base: str | None = None,
) -> tuple[McpServerSpec, ...]:
    """Build MCP callback specs for one monitor agent."""

    return (
        _ccm_server_spec(
            name="ccm_monitor_agent",
            module="backend.mcp.ccm_monitor_agent_server",
            context_args=(
                "--monitor-session-id",
                str(monitor_session_id),
                "--task-id",
                str(task_id),
            ),
            enabled_tools=CCM_MONITOR_AGENT_TOOLS,
            api_base=api_base,
        ),
    )


def build_sub_agent_mcp_server_specs(
    session_id: int,
    task_id: int,
    api_base: str | None = None,
) -> tuple[McpServerSpec, ...]:
    """Build MCP callback specs for one sub-agent."""

    return (
        _ccm_server_spec(
            name="ccm_sub_agent",
            module="backend.mcp.ccm_sub_agent_server",
            context_args=(
                "--sub-agent-session-id",
                str(session_id),
                "--task-id",
                str(task_id),
            ),
            enabled_tools=CCM_SUB_AGENT_TOOLS,
            api_base=api_base,
        ),
    )


def render_claude_mcp_config(specs: Sequence[McpServerSpec]) -> dict[str, object]:
    """Render specs in Claude Code's existing ``mcpServers`` JSON shape."""

    servers: dict[str, dict[str, object]] = {}
    for spec in specs:
        if spec.name in servers:
            raise ValueError(f"Duplicate MCP server name: {spec.name}")

        server: dict[str, object] = {
            "command": spec.command,
            "args": list(spec.args),
        }
        if spec.cwd is not None:
            server["cwd"] = spec.cwd
        if spec.env:
            server["env"] = dict(spec.env)
        servers[spec.name] = server

    return {"mcpServers": servers}


def _write_claude_mcp_config(
    specs: Sequence[McpServerSpec],
    config_path: Path,
) -> Path:
    config = render_claude_mcp_config(specs)
    fd = os.open(str(config_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(config, f, indent=2)
    return config_path


def generate_mcp_config(
    task_id: int,
    enabled_skills: dict | None = None,
    api_base: str | None = None,
) -> Path:
    """为指定 task 生成 MCP config JSON 文件。

    ccm_skills server 始终包含（提供 $help 等默认命令）。
    Returns: 临时文件路径，进程结束后由调用方清理。
    """
    config_path = Path(tempfile.gettempdir()) / f"ccm_mcp_{task_id}.json"
    specs = build_mcp_server_specs(task_id, enabled_skills, api_base)
    return _write_claude_mcp_config(specs, config_path)


def cleanup_mcp_config(task_id: int):
    """清理临时 MCP config 文件。"""
    config_path = Path(tempfile.gettempdir()) / f"ccm_mcp_{task_id}.json"
    config_path.unlink(missing_ok=True)


def generate_monitor_agent_mcp_config(
    monitor_session_id: int, task_id: int, api_base: str | None = None
) -> Path:
    """为 monitor 子 agent 生成专用 MCP config。

    Returns:
        配置文件路径，调用方负责清理。
    """
    config_path = Path(tempfile.gettempdir()) / f"ccm_monitor_agent_{monitor_session_id}.json"
    specs = build_monitor_agent_mcp_server_specs(
        monitor_session_id,
        task_id,
        api_base,
    )
    return _write_claude_mcp_config(specs, config_path)


def cleanup_monitor_agent_mcp_config(monitor_session_id: int):
    """清理 monitor 子 agent 的 MCP config 文件。"""
    config_path = Path(tempfile.gettempdir()) / f"ccm_monitor_agent_{monitor_session_id}.json"
    config_path.unlink(missing_ok=True)


def generate_sub_agent_mcp_config(
    session_id: int, task_id: int, api_base: str | None = None
) -> Path:
    """为 sub-agent 子进程生成专用 MCP config。

    Returns:
        配置文件路径，调用方负责清理。
    """
    config_path = Path(tempfile.gettempdir()) / f"ccm_sub_agent_{session_id}.json"
    specs = build_sub_agent_mcp_server_specs(session_id, task_id, api_base)
    return _write_claude_mcp_config(specs, config_path)


def cleanup_sub_agent_mcp_config(session_id: int):
    """清理 sub-agent 的 MCP config 文件。"""
    config_path = Path(tempfile.gettempdir()) / f"ccm_sub_agent_{session_id}.json"
    config_path.unlink(missing_ok=True)
