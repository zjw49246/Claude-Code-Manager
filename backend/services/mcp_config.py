"""MCP config 动态生成 — 根据 task 的 enabled_skills 生成 MCP server 配置。"""
import json
import os
import sys
import tempfile
from pathlib import Path


_CCM_ROOT = str(Path(__file__).resolve().parent.parent.parent)
_VENV_PYTHON = str(Path(_CCM_ROOT) / ".venv" / "bin" / "python3")


def generate_mcp_config(task_id: int, enabled_skills: dict | None = None, api_base: str | None = None) -> Path:
    """为指定 task 生成 MCP config JSON 文件。

    ccm_skills server 始终包含（提供 $help 等默认命令）。
    Returns: 临时文件路径，进程结束后由调用方清理。
    """
    from backend.config import settings

    if api_base is None:
        host = settings.host if settings.host != "0.0.0.0" else "127.0.0.1"
        api_base = f"http://{host}:{settings.port}"

    auth_token = getattr(settings, "auth_token", "") or ""

    args = [
        "-m", "backend.mcp.ccm_skills_server",
        "--task-id", str(task_id),
        "--api-base", api_base,
    ]
    if auth_token:
        args.extend(["--auth-token", auth_token])

    servers = {
        "ccm_skills": {
            "command": _VENV_PYTHON,
            "args": args,
            "cwd": _CCM_ROOT,
        }
    }

    config = {"mcpServers": servers}
    config_path = Path(tempfile.gettempdir()) / f"ccm_mcp_{task_id}.json"
    fd = os.open(str(config_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(config, f, indent=2)
    return config_path


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
    from backend.config import settings

    if api_base is None:
        host = settings.host if settings.host != "0.0.0.0" else "127.0.0.1"
        api_base = f"http://{host}:{settings.port}"

    auth_token = getattr(settings, "auth_token", "") or ""

    args = [
        "-m", "backend.mcp.ccm_monitor_agent_server",
        "--monitor-session-id", str(monitor_session_id),
        "--task-id", str(task_id),
        "--api-base", api_base,
    ]
    if auth_token:
        args.extend(["--auth-token", auth_token])

    config = {
        "mcpServers": {
            "ccm_monitor_agent": {
                "command": _VENV_PYTHON,
                "args": args,
                "cwd": _CCM_ROOT,
            }
        }
    }

    config_path = Path(tempfile.gettempdir()) / f"ccm_monitor_agent_{monitor_session_id}.json"
    fd = os.open(str(config_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(config, f, indent=2)
    return config_path


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
    from backend.config import settings

    if api_base is None:
        host = settings.host if settings.host != "0.0.0.0" else "127.0.0.1"
        api_base = f"http://{host}:{settings.port}"

    auth_token = getattr(settings, "auth_token", "") or ""

    args = [
        "-m", "backend.mcp.ccm_sub_agent_server",
        "--sub-agent-session-id", str(session_id),
        "--task-id", str(task_id),
        "--api-base", api_base,
    ]
    if auth_token:
        args.extend(["--auth-token", auth_token])

    config = {
        "mcpServers": {
            "ccm_sub_agent": {
                "command": _VENV_PYTHON,
                "args": args,
                "cwd": _CCM_ROOT,
            }
        }
    }

    config_path = Path(tempfile.gettempdir()) / f"ccm_sub_agent_{session_id}.json"
    fd = os.open(str(config_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(config, f, indent=2)
    return config_path


def cleanup_sub_agent_mcp_config(session_id: int):
    """清理 sub-agent 的 MCP config 文件。"""
    config_path = Path(tempfile.gettempdir()) / f"ccm_sub_agent_{session_id}.json"
    config_path.unlink(missing_ok=True)
