"""CCM Skills MCP Server — 给 Task 的 Claude 主 session 注入工具能力。

Usage:
    python -m backend.mcp.ccm_skills_server --task-id 123 --api-base http://localhost:8000
"""
import argparse
import json
import logging
import sys

import httpx
from mcp.server.fastmcp import FastMCP

logger = logging.getLogger(__name__)

mcp = FastMCP("ccm-skills", instructions="CCM task skill tools")

_TASK_ID: int = 0
_API_BASE: str = "http://localhost:8000"


def _api_url(path: str) -> str:
    return f"{_API_BASE}/api/tasks/{_TASK_ID}{path}"


@mcp.tool()
async def create_monitor(
    description: str,
    context: str = "",
    interval: int = 120,
    max_checks: int = 50,
) -> str:
    """启动一个后台监控子 session。不阻塞当前对话。

    子 session 是只读的（不能 Edit/Write），会定期检查进程状态和日志，
    将摘要报告写入数据库。你可以随时用 check_monitors() 查看最新状态。

    Args:
        description: 监控什么（如"编译进度"、"测试运行"、"后台训练"）
        context: 额外上下文（如日志路径、进程名、PID、如何判断完成）
        interval: 检查间隔秒数（默认 120）
        max_checks: 最大检查次数（默认 50，达到后自动停止）
    """
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                _api_url("/monitor-sessions"),
                json={
                    "description": description,
                    "monitor_context": context,
                    "interval": interval,
                    "max_checks": max_checks,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            return json.dumps({
                "success": True,
                "monitor_id": data["id"],
                "status": "created",
                "message": f"Monitor #{data['id']} 已启动，每 {interval} 秒检查一次，最多 {max_checks} 次。",
            }, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)


@mcp.tool()
async def check_monitors() -> str:
    """查询当前 task 下所有活跃的 monitor 子 session 的最新状态。

    返回每个 monitor 的: id, description, status, checks_done, last_summary。
    当用户询问监控情况、或你需要了解后台任务进展时调用此工具。
    """
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(_api_url("/monitor-sessions"))
            resp.raise_for_status()
            sessions = resp.json()
            if not sessions:
                return json.dumps({"success": True, "monitors": [], "message": "当前没有活跃的监控。"}, ensure_ascii=False)
            summary = []
            for s in sessions:
                summary.append({
                    "monitor_id": s["id"],
                    "description": s["description"],
                    "status": s["status"],
                    "checks_done": s["checks_done"],
                    "max_checks": s["max_checks"],
                    "last_summary": s.get("last_summary"),
                })
            return json.dumps({"success": True, "monitors": summary}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)


@mcp.tool()
async def stop_monitor(monitor_id: int) -> str:
    """停止指定的 monitor 子 session。

    当后台任务已完成或不再需要监控时调用。

    Args:
        monitor_id: 要停止的 monitor ID（从 create_monitor 或 check_monitors 获取）
    """
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.delete(
                _api_url(f"/monitor-sessions/{monitor_id}")
            )
            resp.raise_for_status()
            return json.dumps({
                "success": True,
                "status": "cancelled",
                "message": f"Monitor #{monitor_id} 已停止。",
            }, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="CCM Skills MCP Server")
    parser.add_argument("--task-id", type=int, required=True)
    parser.add_argument("--api-base", default="http://localhost:8000")
    args = parser.parse_args()

    _TASK_ID = args.task_id
    _API_BASE = args.api_base

    mcp.run(transport="stdio")
