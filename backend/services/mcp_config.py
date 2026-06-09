"""MCP config 动态生成 — 根据 task 的 enabled_skills 生成 MCP server 配置。"""
import json
import sys
import tempfile
from pathlib import Path


_CCM_ROOT = str(Path(__file__).resolve().parent.parent.parent)


def generate_mcp_config(task_id: int, enabled_skills: dict, api_base: str | None = None) -> Path | None:
    """为指定 task 生成 MCP config JSON 文件。

    Returns:
        临时文件路径，进程结束后由调用方清理。
        如果没有需要注入的 skill，返回 None。
    """
    if not enabled_skills:
        return None

    from backend.config import settings

    if api_base is None:
        host = settings.host if settings.host != "0.0.0.0" else "127.0.0.1"
        api_base = f"http://{host}:{settings.port}"

    auth_token = getattr(settings, "auth_token", "") or ""

    servers = {}

    if enabled_skills.get("monitor"):
        args = [
            "-m", "backend.mcp.ccm_skills_server",
            "--task-id", str(task_id),
            "--api-base", api_base,
        ]
        if auth_token:
            args.extend(["--auth-token", auth_token])
        servers["ccm_skills"] = {
            "command": sys.executable,
            "args": args,
            "cwd": _CCM_ROOT,
        }

    if not servers:
        return None

    config = {"mcpServers": servers}
    config_path = Path(tempfile.gettempdir()) / f"ccm_mcp_{task_id}.json"
    config_path.write_text(json.dumps(config, indent=2))
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
                "command": sys.executable,
                "args": args,
                "cwd": _CCM_ROOT,
            }
        }
    }

    config_path = Path(tempfile.gettempdir()) / f"ccm_monitor_agent_{monitor_session_id}.json"
    config_path.write_text(json.dumps(config, indent=2))
    return config_path


def cleanup_monitor_agent_mcp_config(monitor_session_id: int):
    """清理 monitor 子 agent 的 MCP config 文件。"""
    config_path = Path(tempfile.gettempdir()) / f"ccm_monitor_agent_{monitor_session_id}.json"
    config_path.unlink(missing_ok=True)
