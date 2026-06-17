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
_AUTH_TOKEN: str = ""


def _api_url(path: str) -> str:
    return f"{_API_BASE}/api/tasks/{_TASK_ID}{path}"


def _headers() -> dict[str, str]:
    if _AUTH_TOKEN:
        return {"Authorization": f"Bearer {_AUTH_TOKEN}"}
    return {}


@mcp.tool()
async def ccm_command_help() -> str:
    """列出所有可用的 CCM 命令和技能。

    返回：
    - 内置命令列表（$help 等）
    - 可用技能列表（从 SKILL.md 文件加载）及启用状态
    用户可以通过 $command_name 语法使用命令，或通过 ccm_read_skill 读取技能详情。
    """
    try:
        from backend.services.command_registry import COMMAND_REGISTRY
        from backend.services.skill_loader import discover_skills
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(_api_url(""), headers=_headers())
            resp.raise_for_status()
            task_data = resp.json()
            enabled_skills = task_data.get("enabled_skills") or {}

        # Built-in commands
        commands = []
        for cmd in COMMAND_REGISTRY.values():
            commands.append({
                "command": f"${cmd.name}",
                "description": cmd.description,
                "type": "command",
            })

        # Skills from SKILL.md files
        skills = discover_skills()
        skill_list = []
        for name, skill in skills.items():
            skill_list.append({
                "name": name,
                "description": skill.description.strip()[:150],
                "enabled": enabled_skills.get(name, False),
                "commands": [c["name"] for c in skill.ccm.commands],
                "type": "skill",
            })

        return json.dumps({
            "success": True,
            "commands": commands,
            "skills": skill_list,
            "usage": "用 $命令名 触发命令，用 ccm_read_skill(name) 读取技能详情，用 ccm_enable_skill(name) 启用技能。",
        }, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)


@mcp.tool()
async def ccm_read_skill(skill_name: str) -> str:
    """读取一个技能的完整内容。

    技能目录（name + description）在 system prompt 中可见。
    当需要某个技能的详细指南时，调用此工具获取全文。

    Args:
        skill_name: 技能名称（如 monitor, code-review）
    """
    try:
        from backend.services.skill_loader import discover_skills
        skills = discover_skills()
        skill = skills.get(skill_name)
        if not skill:
            available = ", ".join(skills.keys())
            return json.dumps({
                "success": False,
                "error": f"技能 '{skill_name}' 不存在。可用技能: {available}",
            }, ensure_ascii=False)
        # Merge DB lessons into skill body
        body_with_lessons = skill.body
        try:
            from backend.services.skill_evolution import get_lessons_for_skill
            from backend.database import async_session
            async with async_session() as db:
                lessons = await get_lessons_for_skill(skill_name, db)
                if lessons:
                    body_with_lessons += "\n\n## Learned Lessons\n"
                    body_with_lessons += "\n".join(f"- {l}" for l in lessons)
        except Exception:
            pass

        return json.dumps({
            "success": True,
            "name": skill.name,
            "description": skill.description,
            "body": body_with_lessons,
            "commands": skill.ccm.commands,
            "tags": skill.ccm.tags,
        }, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)


@mcp.tool()
async def ccm_enable_skill(skill_name: str) -> str:
    """为当前 task 启用一个工具/技能（持久生效）。

    Args:
        skill_name: 要启用的工具名称（如 monitor, help）
    """
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(_api_url(""), headers=_headers())
            resp.raise_for_status()
            task_data = resp.json()
            skills = task_data.get("enabled_skills") or {}
            if skills.get(skill_name):
                return json.dumps({"success": True, "message": f"{skill_name} 已经是启用状态"}, ensure_ascii=False)
            skills[skill_name] = True
            resp = await client.put(_api_url(""), headers=_headers(), json={"enabled_skills": skills})
            resp.raise_for_status()
            return json.dumps({"success": True, "message": f"已启用 {skill_name}"}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)


@mcp.tool()
async def ccm_disable_skill(skill_name: str) -> str:
    """为当前 task 禁用一个工具/技能（持久生效）。

    Args:
        skill_name: 要禁用的工具名称（如 monitor）。help 不可禁用。
    """
    try:
        from backend.services.command_registry import COMMAND_REGISTRY
        cmd = COMMAND_REGISTRY.get(skill_name)
        if cmd and cmd.always_available:
            return json.dumps({"success": False, "error": f"{skill_name} 是内置命令，不可禁用"}, ensure_ascii=False)
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(_api_url(""), headers=_headers())
            resp.raise_for_status()
            task_data = resp.json()
            skills = task_data.get("enabled_skills") or {}
            if not skills.get(skill_name):
                return json.dumps({"success": True, "message": f"{skill_name} 已经是禁用状态"}, ensure_ascii=False)
            skills.pop(skill_name, None)
            resp = await client.put(_api_url(""), headers=_headers(), json={"enabled_skills": skills})
            resp.raise_for_status()
            return json.dumps({"success": True, "message": f"已禁用 {skill_name}"}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)


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
                headers=_headers(),
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
            resp = await client.get(_api_url("/monitor-sessions"), headers=_headers())
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
                _api_url(f"/monitor-sessions/{monitor_id}"),
                headers=_headers(),
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
    parser.add_argument("--auth-token", default="")
    args = parser.parse_args()

    _TASK_ID = args.task_id
    _API_BASE = args.api_base
    _AUTH_TOKEN = args.auth_token

    mcp.run(transport="stdio")
