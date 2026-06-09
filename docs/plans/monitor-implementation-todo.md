# Monitor Session v2 — MCP 驱动的 Agent 监控

> **设计理念**: 从"CCM 从外部控制监控"变为"给 Agent 注入 MCP 工具，Agent 自主决定何时监控"。
> Monitor 是 Agent 的第一个 Skill，为未来多 Agent 协作架构奠基。
>
> **分支**: `feature/monitor-session`
> **范围**: 先做 Auto 模式，Loop 模式后续扩展
> **约束**: 只修改本地代码，不影响已部署服务，不 push 到 main
>
> **参考项目**:
> - [oh-my-codex](https://github.com/Yeachan-Heo/oh-my-codex) — MCP server 架构、Plugin 模式、Bootstrap 生命周期
> - [agent-ml-research](https://github.com/caoxiaoyuyuyuyuyu/agent-ml-research) — 角色工具注册、Worker spawn、子进程生命周期、FastMCP tool 定义模式

---

## 架构概览

```
┌─────────────────────────────────────────────────────────────────┐
│  CCM 后端 (FastAPI)                                             │
│                                                                 │
│  ┌───────────────────────────────────────────────────────────┐  │
│  │  Task (enabled_skills: {"monitor": true})                 │  │
│  │                                                           │  │
│  │  主 Session (Claude CLI subprocess)                       │  │
│  │    │                                                      │  │
│  │    │ --mcp-config /tmp/ccm_mcp_{task_id}.json             │  │
│  │    │                                                      │  │
│  │    ├── MCP Tool: create_monitor(desc, context, interval)  │  │
│  │    │     → CCM 后端启动子 session（独立只读 Claude 进程）  │  │
│  │    │     → 立即返回 monitor_id（不阻塞主 session）         │  │
│  │    │                                                      │  │
│  │    ├── MCP Tool: check_monitors()                         │  │
│  │    │     → 查询所有子 session 最新状态                    │  │
│  │    │     → 返回摘要列表                                   │  │
│  │    │                                                      │  │
│  │    └── MCP Tool: stop_monitor(monitor_id)                 │  │
│  │          → 取消指定子 session                             │  │
│  │                                                           │  │
│  │  子 Session 1 (只读 Claude，监控后台任务 A)               │  │
│  │  子 Session 2 (只读 Claude，监控后台任务 B)               │  │
│  └───────────────────────────────────────────────────────────┘  │
│                                                                 │
│  WebSocket → 前端（monitor 消息带 is_monitor 标记）             │
└─────────────────────────────────────────────────────────────────┘
```

### 核心流程

1. 用户创建 Task 时勾选 Skills（当前只有 Monitor）
2. CCM 启动 Claude 子进程时，根据 `enabled_skills` 动态生成 MCP config，通过 `--mcp-config` 注入
3. 用户对话："帮我监控一下这个后台进程的编译"
4. 主 session 调用 MCP tool `create_monitor` → MCP server 通过 HTTP 调用 CCM API → 后端创建 MonitorSession + 启动只读子 Claude 进程
5. MCP tool 立即返回 `{"monitor_id": 1, "status": "created"}` → 主 session 不阻塞
6. 子 session 定期运行检查，每次结果写入 DB + 广播 WebSocket 事件
7. 信息回流两条路径:
   - **被动查询**: 用户问"监控情况怎么样？" → 主 session 调 `check_monitors()` → 返回摘要
   - **主动通知**: 子 session 完成检查 → MCP server 通过 notification 通知主 session → 主 session 发消息给用户（带 `[Monitor]` 标记）
8. 前端收到带 `is_monitor: true` 标记的消息 → 渲染为 monitor 区域

### 两个参考项目的借鉴点

**agent-ml-research（重点参考，Python + FastMCP，架构最接近 CCM）:**

| 借鉴模式 | agent-ml-research 实现 | CCM 实现 |
|----------|----------------------|---------|
| MCP Server | `FastMCP("agent-ml-research")` + `@mcp.tool()` 装饰器，stdio 传输 | 相同模式: `FastMCP("ccm-skills")` + `@mcp.tool()` |
| 工具返回值 | 所有 tool 返回 `ToolResult(success, data, error)` 的 JSON，never raise | 相同: 返回 JSON dict，never raise |
| 子进程启动 | `claude_client.build_command()`: `["claude", "--print", "--output-format", "stream-json", "--dangerously-skip-permissions", "--mcp-config", path, "-p", prompt]` | 扩展 `_build_command()` 加入 `--mcp-config` 参数 |
| Worker spawn | MCP tool `spawn_worker()` → 注册到 registry → `subprocess.Popen(start_new_session=True)` → 后台 `_monitor_worker()` 等待完成 | monitor 子 session 采用相同生命周期模式 |
| 并发限制 | `MAX_CONCURRENT_WORKERS = 6`，超出返回错误 | `MAX_CONCURRENT_MONITORS = 5` per task |
| 上下文注入 | `env["AGENT_ML_SESSION_PROJECT"]` + `env["AGENT_ML_SESSION_CHAT_ID"]` | `--task-id` + `--api-base` 命令行参数 |
| 子进程异常恢复 | Worker 失败 → registry 标记 failed → 通知 Agent | Monitor check 失败 → 记录 failed check → 继续下一轮 |
| 结果回调 | `dispatch_worker_done()` → 注入消息到主 session queue | WebSocket 广播 `monitor_check` 事件 + MCP notification |
| Prompt 设计 | "目标+约束，不教步骤。Claude Opus 足够聪明" | 相同原则 |

**oh-my-codex（概念参考，TypeScript，MCP 架构理念）:**

| 借鉴模式 | oh-my-codex 实现 | CCM 实现 |
|----------|-----------------|---------|
| MCP 配置 | `.mcp.json` plugin 格式: `{"mcpServers": {"omx_state": {"command": "omx", "args": [...]}}}` | 动态生成 `/tmp/ccm_mcp_{task_id}.json`，相同格式 |
| 多 Server 架构 | 6 个独立 MCP server（state/memory/trace/wiki/hermes/code-intel） | 先做 1 个 `ccm_skills` server，未来扩展 skill 时按需拆分 |
| Bootstrap 生命周期 | 680 行 `bootstrap.ts`: parent watchdog、sibling dedup、graceful shutdown | 简化: FastMCP stdio transport 自动处理 EOF/退出 |
| Hermes 通信桥 | agent 间 mailbox + dispatch mechanism | 未来多 agent 通信参考，本次不实现 |

---

## Phase 1 — 数据层

### 1.1 Task 表扩展: `enabled_skills` 字段

> 用 JSON 字段代替 boolean，支持未来多 skill 扩展。
> 当前只有 `{"monitor": true}`，以后可以加 `{"monitor": true, "worker": true, "research": true}`。

**修改文件**: `backend/models/task.py`

- [ ] 新增字段（搜索 `enable_workflows` 找到插入位置，在其下方添加）:
  ```python
  enabled_skills: Mapped[dict | None] = mapped_column(JSON, nullable=True)
  ```

**修改文件**: `backend/schemas/task.py`

- [ ] `TaskCreate`（搜索 `enable_workflows`，在其下方添加）:
  ```python
  enabled_skills: dict | None = None  # e.g. {"monitor": true}
  ```
- [ ] `TaskUpdate`（搜索 `enable_workflows`，在其下方添加）:
  ```python
  enabled_skills: dict | None = None
  ```
- [ ] `TaskResponse`（搜索 `enable_workflows`，在其下方添加）:
  ```python
  enabled_skills: dict | None = None
  ```

**Migration**:
- [ ] `alembic revision --autogenerate -m "add_enabled_skills_to_tasks"`
- [ ] `alembic upgrade head`

### 1.2 MonitorSession Model

> **参考**: `backend/models/task.py` 的 `Mapped[]` + `mapped_column()` 模式。
> **约定**: 无 FK，所有关联字段纯 Integer + index（项目不使用 SQLAlchemy ForeignKey）。

**新建文件**: `backend/models/monitor_session.py`

- [ ] 创建以下内容:

```python
from datetime import datetime
from sqlalchemy import Integer, String, Text, DateTime, JSON
from sqlalchemy.orm import Mapped, mapped_column
from backend.database import Base


class MonitorSession(Base):
    __tablename__ = "monitor_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(Integer, index=True)           # 逻辑关联 tasks.id（无 FK）
    description: Mapped[str] = mapped_column(String(500), nullable=False)
    monitor_context: Mapped[str | None] = mapped_column(Text, nullable=True)
    interval: Mapped[int] = mapped_column(Integer, default=120)         # 检查间隔秒数
    max_checks: Mapped[int] = mapped_column(Integer, default=50)        # 最大检查次数
    model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="running")  # running/completed/failed/cancelled
    checks_done: Mapped[int] = mapped_column(Integer, default=0)
    last_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class MonitorCheck(Base):
    __tablename__ = "monitor_checks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    monitor_session_id: Mapped[int] = mapped_column(Integer, index=True)  # 逻辑关联 monitor_sessions.id（无 FK）
    check_number: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20))                       # success/failed
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    full_output: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
```

### 1.3 注册 Model（用于 Alembic autogenerate）

**修改文件**: `alembic/env.py`

- [ ] 在现有 import 区域添加（搜索其他 model import 找到位置）:
  ```python
  from backend.models.monitor_session import MonitorSession, MonitorCheck
  ```

### 1.4 创建 Migration

- [ ] `alembic revision --autogenerate -m "add_monitor_sessions_and_checks_tables"`
- [ ] 检查生成的 migration 文件，确认两个表结构正确
- [ ] `alembic upgrade head` 验证迁移成功

### 1.5 Schema

**新建文件**: `backend/schemas/monitor_session.py`

- [ ] 创建:

```python
from datetime import datetime
from pydantic import BaseModel


class MonitorSessionCreate(BaseModel):
    description: str
    monitor_context: str | None = None
    interval: int = 120
    max_checks: int = 50
    model: str | None = None


class MonitorSessionResponse(BaseModel):
    id: int
    task_id: int
    description: str
    monitor_context: str | None
    interval: int
    max_checks: int
    model: str | None
    status: str
    checks_done: int
    last_summary: str | None
    created_at: datetime
    completed_at: datetime | None

    model_config = {"from_attributes": True}


class MonitorCheckResponse(BaseModel):
    id: int
    monitor_session_id: int
    check_number: int
    status: str
    summary: str | None
    full_output: str | None
    created_at: datetime

    model_config = {"from_attributes": True}
```

---

## Phase 2 — MCP Server

### 2.1 安装依赖

- [ ] 在 `pyproject.toml` 的 `dependencies` 中添加:
  ```
  "mcp>=1.0.0",
  ```
- [ ] `uv sync`（或 `pip install mcp`）

### 2.2 创建 MCP Server

> **参考**: `agent-ml-research/core/mcp_server.py` 的 `FastMCP` + `@mcp.tool()` 模式。
> MCP server 是独立进程，通过 stdio 与 Claude CLI 通信，通过 HTTP 调用 CCM 后端 API。
> 不直接访问 DB，保持进程隔离。

**新建文件**: `backend/mcp/__init__.py`（空文件）

**新建文件**: `backend/mcp/ccm_skills_server.py`

- [ ] 完整实现:

```python
"""CCM Skills MCP Server — 给 Task 的 Claude 主 session 注入工具能力。

Usage:
    python -m backend.mcp.ccm_skills_server --task-id 123 --api-base http://localhost:8002

参考: agent-ml-research/core/mcp_server.py 的 FastMCP 模式。
MCP server 通过 HTTP 调用 CCM 后端 API（不直接访问 DB，保持进程隔离）。
"""
import argparse
import json
import logging
import sys

import httpx
from mcp.server.fastmcp import FastMCP

logger = logging.getLogger(__name__)

mcp = FastMCP("ccm-skills", instructions="CCM task skill tools")

# 模块级变量，由 __main__ 设置
_TASK_ID: int = 0
_API_BASE: str = "http://localhost:8002"


def _api_url(path: str) -> str:
    return f"{_API_BASE}/api/tasks/{_TASK_ID}{path}"


# ---------------------------------------------------------------------------
# Monitor Tools
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="CCM Skills MCP Server")
    parser.add_argument("--task-id", type=int, required=True)
    parser.add_argument("--api-base", default="http://localhost:8002")
    args = parser.parse_args()

    _TASK_ID = args.task_id
    _API_BASE = args.api_base

    mcp.run(transport="stdio")
```

### 2.3 MCP Config 动态生成

> **参考**: oh-my-codex 的 `.mcp.json` 格式。
> **参考**: agent-ml-research 的 `_build_worker_mcp_config(tools)` — 按需生成配置。
> 根据 task 的 `enabled_skills` 决定注入哪些 tool。当前只有 monitor skill。

**新建文件**: `backend/services/mcp_config.py`

- [ ] 创建:

```python
"""MCP config 动态生成 — 根据 task 的 enabled_skills 生成 MCP server 配置。

参考:
- oh-my-codex 的 .mcp.json plugin 格式
- agent-ml-research 的 _build_worker_mcp_config(tools) 动态生成
"""
import json
import sys
import tempfile
from pathlib import Path


# CCM 项目根目录（mcp server 需要在此 cwd 下运行才能 import backend 模块）
_CCM_ROOT = str(Path(__file__).resolve().parent.parent.parent)


def generate_mcp_config(task_id: int, enabled_skills: dict, api_base: str = "http://localhost:8002") -> Path | None:
    """为指定 task 生成 MCP config JSON 文件。

    根据 enabled_skills dict 决定注入哪些 MCP server。
    当前只有 monitor skill，未来新增 skill 时在此函数中添加对应 server。

    Returns:
        临时文件路径，进程结束后由调用方清理。
        如果没有需要注入的 skill，返回 None。
    """
    if not enabled_skills:
        return None

    servers = {}

    # Monitor skill → ccm_skills MCP server（包含 create_monitor / check_monitors / stop_monitor）
    if enabled_skills.get("monitor"):
        servers["ccm_skills"] = {
            "command": sys.executable,
            "args": [
                "-m", "backend.mcp.ccm_skills_server",
                "--task-id", str(task_id),
                "--api-base", api_base,
            ],
            "cwd": _CCM_ROOT,
        }

    # 未来扩展: 新 skill 在这里添加对应 server
    # if enabled_skills.get("worker"):
    #     servers["ccm_worker"] = { ... }

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
```

---

## Phase 3 — Claude CLI 注入 MCP

### 3.1 扩展 `_build_command()`

> **参考**: agent-ml-research 的 `claude_client.build_base_args()`:
> ```python
> if mcp_config:
>     p = str(mcp_config)
>     if Path(p).exists():
>         args += ["--mcp-config", p]
> ```

**修改文件**: `backend/services/instance_manager.py`

- [ ] `_build_command()` 方法（搜索 `def _build_command`，当前在 line 128）新增参数 `mcp_config_path`:

  ```python
  def _build_command(
      self,
      provider: str,
      prompt: str,
      model: str | None,
      resume_session_id: str | None,
      effort_level: str | None,
      enable_workflows: bool = False,
      mcp_config_path: str | None = None,   # ← 新增
  ) -> list[str]:
  ```

  在 Claude provider 分支中（搜索 `if provider == "claude":`），在 `return cmd` 前添加:
  ```python
  if mcp_config_path and Path(mcp_config_path).exists():
      cmd.extend(["--mcp-config", mcp_config_path])
  ```

  需要在文件头添加 `from pathlib import Path`（如果还没有的话）。

### 3.2 扩展 `launch()`

**修改文件**: `backend/services/instance_manager.py`

- [ ] `launch()` 方法签名（搜索 `async def launch`，当前在 line 37）新增参数:
  ```python
  enabled_skills: dict | None = None,  # ← 新增
  ```

- [ ] 在 `launch()` 方法体中，`cmd = self._build_command(...)` 调用前，生成 MCP config:
  ```python
  # Generate MCP config for enabled skills
  mcp_config_path = None
  if enabled_skills and provider == "claude":
      from backend.services.mcp_config import generate_mcp_config
      mcp_config_path = generate_mcp_config(task_id, enabled_skills)
  ```

- [ ] 将 `mcp_config_path` 传入 `_build_command()`:
  ```python
  cmd = self._build_command(
      provider=provider,
      prompt=prompt,
      model=model,
      resume_session_id=resume_session_id,
      effort_level=effort_level,
      enable_workflows=enable_workflows,
      mcp_config_path=str(mcp_config_path) if mcp_config_path else None,  # ← 新增
  )
  ```

- [ ] 在 `_launch_params` 存储中（搜索 `self._launch_params[instance_id]`）添加:
  ```python
  "enabled_skills": enabled_skills,
  ```

- [ ] MCP config 清理: 在 `_consume_output()` 方法结束时（搜索 `async def _consume_output`），或在 dispatcher 的进程等待完成后，调用:
  ```python
  from backend.services.mcp_config import cleanup_mcp_config
  if task_id:
      cleanup_mcp_config(task_id)
  ```

### 3.3 Dispatcher 传递 `enabled_skills`

**修改文件**: `backend/services/dispatcher.py`

- [ ] 在所有 `self.instance_manager.launch(...)` 调用中，添加 `enabled_skills=task.enabled_skills`。

  共 8 个调用点（搜索 `enable_workflows=task.enable_workflows` 找到每个位置，在其下方添加）:
  - Auto 模式首次启动（约 line 522）
  - Chat rotation resume（约 line 697）
  - Chat rotation fresh（约 line 725）
  - Loop 模式（约 line 881）
  - Goal 模式第一轮（约 line 1062）
  - Goal 模式后续轮（约 line 1078）
  - Loop signal fix（约 line 1440）
  - Plan 模式（约 line 1470）

  每个位置添加:
  ```python
  enabled_skills=task.enabled_skills,
  ```

---

## Phase 4 — 后端 API + 子 Session 管理

### 4.1 Monitor API 端点

> **参考**: `backend/api/tasks.py` 的路由模式。
> **参考**: agent-ml-research 的 `spawn_worker()` — 校验 → 注册 → 启动 → 返回 ID。

**新建文件**: `backend/api/monitor.py`

- [ ] 创建以下端点:

```python
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database import get_db
from backend.models.task import Task
from backend.models.monitor_session import MonitorSession, MonitorCheck
from backend.schemas.monitor_session import (
    MonitorSessionCreate,
    MonitorSessionResponse,
    MonitorCheckResponse,
)

router = APIRouter(prefix="/api/tasks/{task_id}/monitor-sessions", tags=["monitor"])

MAX_CONCURRENT_MONITORS = 5  # 参考 agent-ml-research 的 MAX_CONCURRENT_WORKERS


@router.post("", response_model=MonitorSessionResponse)
async def create_monitor_session(
    task_id: int,
    body: MonitorSessionCreate,
    db: AsyncSession = Depends(get_db),
):
    """创建 monitor session — MCP server 或用户通过 API 调用。"""
    task = await db.get(Task, task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    skills = task.enabled_skills or {}
    if not skills.get("monitor"):
        raise HTTPException(403, "Monitor skill not enabled for this task")
    if task.status not in ("in_progress", "executing"):
        raise HTTPException(400, "Cannot create monitor for inactive task")

    # 并发限制
    active_count = await db.scalar(
        select(func.count(MonitorSession.id))
        .where(MonitorSession.task_id == task_id, MonitorSession.status == "running")
    )
    if active_count >= MAX_CONCURRENT_MONITORS:
        raise HTTPException(
            429,
            f"Too many active monitors ({active_count}/{MAX_CONCURRENT_MONITORS}). "
            "Stop an existing monitor first.",
        )

    ms = MonitorSession(
        task_id=task_id,
        description=body.description,
        monitor_context=body.monitor_context,
        interval=body.interval,
        max_checks=body.max_checks,
        model=body.model,
    )
    db.add(ms)
    await db.commit()
    await db.refresh(ms)

    # 启动子 session 后台循环
    from backend.main import dispatcher
    dispatcher.start_monitor_session(ms)

    # 广播 WebSocket 事件
    await dispatcher.broadcaster.broadcast(
        f"task:{task_id}",
        {"event": "monitor_session_created", "monitor_session_id": ms.id, "description": ms.description},
    )

    return ms


@router.get("", response_model=list[MonitorSessionResponse])
async def list_monitor_sessions(task_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(MonitorSession)
        .where(MonitorSession.task_id == task_id)
        .order_by(MonitorSession.created_at.desc())
    )
    return list(result.scalars().all())


@router.get("/{session_id}", response_model=MonitorSessionResponse)
async def get_monitor_session(
    task_id: int, session_id: int, db: AsyncSession = Depends(get_db),
):
    ms = await db.get(MonitorSession, session_id)
    if not ms or ms.task_id != task_id:
        raise HTTPException(404, "Monitor session not found")
    return ms


@router.delete("/{session_id}")
async def delete_monitor_session(
    task_id: int, session_id: int, db: AsyncSession = Depends(get_db),
):
    """停止并删除 monitor session。"""
    ms = await db.get(MonitorSession, session_id)
    if not ms or ms.task_id != task_id:
        raise HTTPException(404, "Monitor session not found")

    # 更新 DB 状态（先于 cancel，因为 cancel 时 CancelledError handler 依赖 DB 状态）
    if ms.status == "running":
        ms.status = "cancelled"
        ms.completed_at = func.now()
        await db.commit()

    # Cancel 后台 asyncio task + kill 子进程
    from backend.main import dispatcher
    atask = dispatcher._monitor_tasks.get(session_id)
    if atask and not atask.done():
        atask.cancel()
    proc = dispatcher._monitor_processes.get(session_id)
    if proc and proc.returncode is None:
        proc.kill()

    # 广播状态变更
    await dispatcher.broadcaster.broadcast(
        f"task:{task_id}",
        {"event": "monitor_session_status", "monitor_session_id": session_id, "status": "cancelled"},
    )

    return {"ok": True}


@router.get("/{session_id}/checks", response_model=list[MonitorCheckResponse])
async def get_monitor_checks(
    task_id: int, session_id: int, db: AsyncSession = Depends(get_db),
):
    ms = await db.get(MonitorSession, session_id)
    if not ms or ms.task_id != task_id:
        raise HTTPException(404, "Monitor session not found")
    result = await db.execute(
        select(MonitorCheck)
        .where(MonitorCheck.monitor_session_id == session_id)
        .order_by(MonitorCheck.created_at.desc())
    )
    return list(result.scalars().all())
```

### 4.2 注册路由

**修改文件**: `backend/main.py`

- [ ] 在现有 router import 区域（搜索 `from backend.api.`）添加:
  ```python
  from backend.api.monitor import router as monitor_router
  ```
- [ ] 在现有 `app.include_router(...)` 区域添加:
  ```python
  app.include_router(monitor_router)
  ```

### 4.3 Dispatcher 扩展 — 子 Session 生命周期

> **参考**: agent-ml-research 的 Worker 生命周期:
> `spawn_worker()` → `_reg.register()` → `_launch_worker_process()` →
> `subprocess.Popen(start_new_session=True)` → `asyncio.create_task(_monitor_worker())`

**修改文件**: `backend/services/dispatcher.py`

- [ ] 在 `__init__` 中（搜索 `self._running_tasks`，在其附近添加）:
  ```python
  self._monitor_tasks: dict[int, asyncio.Task] = {}           # monitor_session_id -> asyncio task
  self._monitor_processes: dict[int, asyncio.subprocess.Process] = {}  # monitor_session_id -> subprocess
  ```

- [ ] 新增 `start_monitor_session()`:
  ```python
  def start_monitor_session(self, monitor_session):
      """启动 monitor session 的后台循环。由 API 层调用。"""
      task = asyncio.create_task(
          self._monitor_session_lifecycle(monitor_session.id)
      )
      self._monitor_tasks[monitor_session.id] = task
  ```

- [ ] 新增 `_monitor_session_lifecycle()` — 核心循环:

  ```python
  async def _monitor_session_lifecycle(self, monitor_session_id: int):
      """Monitor session 的完整生命周期。

      参考 agent-ml-research 的 _monitor_worker():
      - 循环: sleep → 检查状态 → 启动子进程 → 解析输出 → 记录结果 → 广播
      - 子进程失败不终止整个 session（记录 failed check，继续下一轮）
      - CancelledError 时 kill 子进程
      """
      try:
          async with self.db_factory() as db:
              ms = await db.get(MonitorSession, monitor_session_id)
              task = await db.get(Task, ms.task_id)
              if not ms or not task:
                  return

          while True:
              # 1. 检查 monitor 是否被外部 cancel（DELETE API 会提前更新 DB 状态）
              async with self.db_factory() as db:
                  ms = await db.get(MonitorSession, monitor_session_id)
                  if not ms or ms.status != "running":
                      break

              # 2. 检查 task 是否已结束
              async with self.db_factory() as db:
                  task = await db.get(Task, ms.task_id)
                  if task.status in ("completed", "failed", "cancelled"):
                      final_status = "completed" if task.status == "completed" else "cancelled"
                      ms = await db.get(MonitorSession, monitor_session_id)
                      ms.status = final_status
                      ms.completed_at = datetime.utcnow()
                      await db.commit()
                      await self.broadcaster.broadcast(
                          f"task:{ms.task_id}",
                          {"event": "monitor_session_status", "monitor_session_id": ms.id, "status": final_status},
                      )
                      break

              # 3. 检查 max_checks 限制
              if ms.checks_done >= ms.max_checks:
                  async with self.db_factory() as db:
                      ms = await db.get(MonitorSession, monitor_session_id)
                      ms.status = "completed"
                      ms.completed_at = datetime.utcnow()
                      await db.commit()
                  await self.broadcaster.broadcast(
                      f"task:{ms.task_id}",
                      {"event": "monitor_session_status", "monitor_session_id": ms.id, "status": "completed"},
                  )
                  break

              # 4. 构建子 session prompt
              prompt = self._build_monitor_prompt(ms, task)

              # 5. 启动只读 Claude 子进程 + 解析输出
              check_status = "success"
              summary = ""
              full_output = ""
              is_done = False

              try:
                  full_output = await self._run_monitor_subprocess(
                      prompt=prompt,
                      cwd=task.last_cwd or task.target_repo or os.getcwd(),
                      model=ms.model,
                  )
                  # 解析输出: 找 STATUS: 和 SUMMARY: 行
                  for line in full_output.splitlines():
                      line_stripped = line.strip()
                      if line_stripped.startswith("STATUS:"):
                          status_val = line_stripped[7:].strip().lower()
                          if status_val == "done":
                              is_done = True
                          elif status_val == "error":
                              check_status = "failed"
                      elif line_stripped.startswith("SUMMARY:"):
                          summary = line_stripped[8:].strip()
              except asyncio.TimeoutError:
                  check_status = "failed"
                  summary = "Monitor check timed out"
              except asyncio.CancelledError:
                  raise  # 向上传播，不吞掉
              except Exception as e:
                  check_status = "failed"
                  summary = f"Monitor check error: {e}"

              # 6. 写入 MonitorCheck 到 DB
              async with self.db_factory() as db:
                  ms = await db.get(MonitorSession, monitor_session_id)
                  ms.checks_done += 1
                  ms.last_summary = summary
                  check = MonitorCheck(
                      monitor_session_id=monitor_session_id,
                      check_number=ms.checks_done,
                      status=check_status,
                      summary=summary,
                      full_output=full_output[:10000] if full_output else None,  # 截断防 DB 爆
                  )
                  db.add(check)

                  if is_done:
                      ms.status = "completed"
                      ms.completed_at = datetime.utcnow()

                  await db.commit()

              # 7. 广播 WebSocket 事件
              await self.broadcaster.broadcast(
                  f"task:{ms.task_id}",
                  {
                      "event": "monitor_check",
                      "monitor_session_id": ms.id,
                      "check_number": ms.checks_done,
                      "status": check_status,
                      "summary": summary,
                      "is_monitor": True,  # 前端用此标记区分 monitor 消息
                  },
              )

              if is_done:
                  await self.broadcaster.broadcast(
                      f"task:{ms.task_id}",
                      {"event": "monitor_session_status", "monitor_session_id": ms.id, "status": "completed"},
                  )
                  break

              # 8. Sleep 等待下一轮
              await asyncio.sleep(ms.interval)

      except asyncio.CancelledError:
          # DELETE API 取消 — kill 子进程（参考 agent-ml-research: CancelledError → process.kill()）
          proc = self._monitor_processes.get(monitor_session_id)
          if proc and proc.returncode is None:
              proc.kill()
              await proc.wait()
          # DB 状态已被 DELETE API 提前更新，不需要再改
      except Exception:
          # 未预期异常 → 标记 failed（防止状态卡在 running）
          logger.exception(f"Monitor session {monitor_session_id} failed unexpectedly")
          try:
              async with self.db_factory() as db:
                  ms = await db.get(MonitorSession, monitor_session_id)
                  if ms and ms.status == "running":
                      ms.status = "failed"
                      ms.completed_at = datetime.utcnow()
                      await db.commit()
                      await self.broadcaster.broadcast(
                          f"task:{ms.task_id}",
                          {"event": "monitor_session_status", "monitor_session_id": ms.id, "status": "failed"},
                      )
          except Exception:
              pass
      finally:
          self._monitor_tasks.pop(monitor_session_id, None)
          self._monitor_processes.pop(monitor_session_id, None)
  ```

- [ ] 新增 `_run_monitor_subprocess()` — 启动只读 Claude 子进程:

  > **参考**: agent-ml-research 的 `claude_client.build_command()`:
  > `[binary, "--print", "--dangerously-skip-permissions", "--output-format", "stream-json", "-p", prompt]`

  ```python
  async def _run_monitor_subprocess(self, prompt: str, cwd: str, model: str | None) -> str:
      """运行一次只读 Claude 检查子进程，返回文本输出。"""
      cmd = [
          settings.claude_binary,
          "-p", prompt,
          "--output-format", "stream-json",
          "--dangerously-skip-permissions",
          "--disallowedTools", "Edit,Write,NotebookEdit",
      ]
      if model:
          cmd.extend(["--model", model])
      elif settings.default_model:
          cmd.extend(["--model", settings.default_model])

      env = {k: v for k, v in os.environ.items()
             if k.upper() not in ("CLAUDECODE", "CLAUDE_CODE")}

      process = await asyncio.create_subprocess_exec(
          *cmd,
          stdout=asyncio.subprocess.PIPE,
          stderr=asyncio.subprocess.PIPE,
          cwd=cwd,
          env=env,
          limit=10 * 1024 * 1024,
      )
      # 注册进程以便 cancel 时 kill
      # （调用方通过 monitor_session_id 传入，但这里简化：存到临时变量，由调用方管理）
      # 实际实现中 monitor_session_id 需要传入本方法，这里简化

      try:
          stdout, stderr = await asyncio.wait_for(
              process.communicate(),
              timeout=300,
          )
      except asyncio.TimeoutError:
          process.kill()
          await process.wait()
          raise

      # 使用 StreamParser 解析 stream-json 输出，提取最终文本
      # 参考现有的 _consume_output() 中的解析逻辑
      text_parts = []
      for line in stdout.decode(errors="replace").splitlines():
          line = line.strip()
          if not line:
              continue
          try:
              event = json.loads(line)
              if event.get("type") == "assistant" and "message" in event:
                  for block in event["message"].get("content", []):
                      if block.get("type") == "text":
                          text_parts.append(block["text"])
              elif event.get("type") == "result":
                  if "result" in event:
                      text_parts.append(event["result"])
          except json.JSONDecodeError:
              continue

      return "\n".join(text_parts)
  ```

  > **注意**: 上面的 stream-json 解析逻辑需要适配 Claude CLI 的实际输出格式。
  > 应复用现有的 `StreamParser`（`backend/services/stream_parser.py`），而不是重新实现。
  > 实现时搜索 `class StreamParser` 了解接口，然后在此方法中使用。

- [ ] 新增 `_build_monitor_prompt()`:

  > **参考**: agent-ml-research 的 prompt 设计原则:
  > "目标+约束，不教步骤。Claude Opus 足够聪明。"

  ```python
  def _build_monitor_prompt(self, monitor_session, task) -> str:
      parts = [
          f"你是一个后台监控进程，这是第 {monitor_session.checks_done + 1} 次检查。",
          f"监控目标: {monitor_session.description}",
      ]
      if monitor_session.monitor_context:
          parts.append(f"上下文: {monitor_session.monitor_context}")
      parts.append(
          "\n检查并报告当前状态。使用 Bash 工具执行 ps aux、tail 日志等命令。"
          "\n\n最后两行必须严格遵循以下格式:"
          "\nSUMMARY: <一句话概括当前状态>"
          "\nSTATUS: running|done|error"
      )
      return "\n".join(parts)
  ```

### 4.4 MCP Notification（主动通知机制）

> **参考**: oh-my-codex 的 Hermes 通信桥 — agent 间消息传递。
> **参考**: MCP 协议的 server-to-client notification。

- [ ] 在 `ccm_skills_server.py` 中实现后台轮询 + notification:

  ```python
  # 后台任务: 每 30 秒检查是否有新的 MonitorCheck
  # 如果有，通过 MCP notification 通知主 session
  
  _last_check_ids: dict[int, int] = {}  # monitor_id -> last_seen_check_id
  
  async def _poll_monitor_updates():
      """后台轮询 monitor 更新，有新结果时发 MCP notification。"""
      while True:
          await asyncio.sleep(30)
          try:
              async with httpx.AsyncClient(timeout=5) as client:
                  resp = await client.get(_api_url("/monitor-sessions"))
                  sessions = resp.json()
                  for s in sessions:
                      if s["status"] != "running":
                          continue
                      # 检查是否有新的 check
                      if s.get("last_summary") and s["checks_done"] > _last_check_ids.get(s["id"], 0):
                          _last_check_ids[s["id"]] = s["checks_done"]
                          # 发送 MCP notification
                          # 注意: 需要验证 FastMCP 是否支持 send_notification
                          # 如果不支持，此功能跳过，依赖被动查询
          except Exception:
              pass
  ```

  > **重要**: 需要先验证 Claude CLI 是否处理 MCP server notification。
  > 如果不支持，此功能跳过。被动查询（`check_monitors()`）作为主要信息流。

### 4.5 任务取消/删除时清理 Monitor

**修改文件**: `backend/services/task_queue.py`

- [ ] `cancel()` 方法（搜索 `async def cancel`，line 175）末尾添加:
  ```python
  # 批量取消该 task 下所有 running 的 MonitorSession
  from backend.models.monitor_session import MonitorSession
  await self.db.execute(
      update(MonitorSession)
      .where(MonitorSession.task_id == task_id, MonitorSession.status == "running")
      .values(status="cancelled", completed_at=datetime.utcnow())
  )
  ```

- [ ] `delete()` 方法（搜索 `async def delete`，line 101）在 `await self.db.delete(task)` 前添加:
  ```python
  # 清理 MonitorCheck 和 MonitorSession（无 FK CASCADE，必须手动清理）
  from backend.models.monitor_session import MonitorSession, MonitorCheck
  ms_ids = (await self.db.execute(
      select(MonitorSession.id).where(MonitorSession.task_id == task_id)
  )).scalars().all()
  if ms_ids:
      await self.db.execute(
          sa_delete(MonitorCheck).where(MonitorCheck.monitor_session_id.in_(ms_ids))
      )
      await self.db.execute(
          sa_delete(MonitorSession).where(MonitorSession.task_id == task_id)
      )
  ```

**修改文件**: `backend/api/tasks.py`

- [ ] `cancel_task` endpoint（搜索 `async def cancel_task`，line 167）中，`await _stop_task_process(task_id, db)` 后添加:
  ```python
  # Cancel 所有该 task 的 monitor asyncio tasks + kill 子进程
  from backend.main import dispatcher
  from backend.models.monitor_session import MonitorSession
  result = await db.execute(
      select(MonitorSession.id)
      .where(MonitorSession.task_id == task_id, MonitorSession.status.in_(["running"]))
  )
  for (ms_id,) in result.all():
      atask = dispatcher._monitor_tasks.get(ms_id)
      if atask and not atask.done():
          atask.cancel()
      proc = dispatcher._monitor_processes.get(ms_id)
      if proc and proc.returncode is None:
          proc.kill()
  ```

**修改文件**: `backend/services/dispatcher.py`

- [ ] `_cleanup_stale_state()` 末尾（搜索 `await db.commit()` in `_cleanup_stale_state`）前添加:
  ```python
  # 清理重启前遗留的 running monitor sessions
  from backend.models.monitor_session import MonitorSession
  result = await db.execute(
      select(MonitorSession).where(MonitorSession.status == "running")
  )
  for ms in result.scalars().all():
      logger.warning(f"Cleaning up stale monitor session {ms.id}")
      ms.status = "failed"
      ms.completed_at = datetime.utcnow()
  ```

### 4.6 Prompt 注入

> 当 task 有 monitor skill 时，在主 session prompt 中添加工具使用引导。

**修改文件**: `backend/services/dispatcher.py`

- [ ] 在 Auto 模式的 prompt 构建中（搜索 `parts.append(f"任务:\n{task.description}")` 在 `_run_task_lifecycle` 中，约 line 504），在其前面添加:
  ```python
  # 注入 skill 引导
  if task.enabled_skills and task.enabled_skills.get("monitor"):
      parts.append(
          "你拥有后台监控能力（通过 ccm-skills MCP 工具）。"
          "当用户要求监控后台进程或长时间运行的任务时，"
          "调用 create_monitor 启动后台只读监控。"
          "可用工具: create_monitor / check_monitors / stop_monitor。"
      )
  ```

  > **注意**: chat 模式也有类似的 prompt 构建（搜索第二个 `parts.append(f"任务:\n{task.description}")`，约 line 711），同样需要添加。

---

## Phase 5 — 前端

### 5.1 Task 创建表单

**修改文件**: `frontend/src/components/Tasks/TaskForm.tsx`

- [ ] 新增 state（搜索 `enableWorkflows` 的 useState，在其附近添加）:
  ```tsx
  const [enableMonitor, setEnableMonitor] = useState(false);
  ```

- [ ] 在 submit handler 中（搜索 `enable_workflows: enableWorkflows`），添加:
  ```tsx
  enabled_skills: enableMonitor ? { monitor: true } : undefined,
  ```

- [ ] 在 Workflows checkbox 后面（搜索 `Workflows</label>`）添加:
  ```tsx
  <label className="flex items-center gap-1 text-sm text-gray-400 whitespace-nowrap cursor-pointer" title="Enable Monitor skill - lets the agent create background monitoring sessions">
    <input
      type="checkbox"
      checked={enableMonitor}
      onChange={(e) => setEnableMonitor(e.target.checked)}
      className="accent-indigo-500"
    />
    Monitor
  </label>
  ```

### 5.2 API Client 更新

**修改文件**: `frontend/src/api/client.ts`

- [ ] `TaskResponse` 接口中（搜索 `enable_workflows`）添加:
  ```tsx
  enabled_skills: Record<string, boolean> | null;
  ```

- [ ] `createTask` 方法参数中（搜索 `enable_workflows?:` ）添加:
  ```tsx
  enabled_skills?: Record<string, boolean>;
  ```

- [ ] 添加 Monitor API 方法:
  ```tsx
  // Monitor Sessions
  listMonitorSessions: (taskId: number) =>
    api.get(`/api/tasks/${taskId}/monitor-sessions`).then(r => r.data),
  getMonitorChecks: (taskId: number, sessionId: number) =>
    api.get(`/api/tasks/${taskId}/monitor-sessions/${sessionId}/checks`).then(r => r.data),
  deleteMonitorSession: (taskId: number, sessionId: number) =>
    api.delete(`/api/tasks/${taskId}/monitor-sessions/${sessionId}`).then(r => r.data),
  ```

- [ ] TypeScript 接口:
  ```tsx
  export interface MonitorSession {
    id: number;
    task_id: number;
    description: string;
    monitor_context: string | null;
    interval: number;
    max_checks: number;
    status: string;
    checks_done: number;
    last_summary: string | null;
    created_at: string;
    completed_at: string | null;
  }
  export interface MonitorCheck {
    id: number;
    monitor_session_id: number;
    check_number: number;
    status: string;
    summary: string | null;
    full_output: string | null;
    created_at: string;
  }
  ```

### 5.3 Monitor 面板组件

**新建文件**: `frontend/src/components/Chat/MonitorPanel.tsx`

- [ ] 在 Task 详情页（ChatView）中，当 `task.enabled_skills?.monitor` 为 true 时显示 Monitor 面板:
  - 活跃 monitor 列表，每条显示: 描述、状态、`3/50 次`、最新摘要
  - 停止按钮 → 调用 deleteMonitorSession
  - 点击展开历史 checks 列表

### 5.4 Monitor 消息渲染

- [ ] Chat 消息流中，WebSocket 收到 `monitor_check` 事件时:
  - 在消息流中插入一条特殊样式的 monitor 更新消息
  - 不同背景色/图标，与普通对话区分
  - 显示来源 monitor 描述 + 摘要

### 5.5 WebSocket 事件处理

- [ ] 在 ChatView 中监听（使用现有的 `useWebSocket` hook 的 `onMessage` 回调）:
  - `monitor_session_created` → 面板添加新条目
  - `monitor_check` → 更新摘要 + 消息流插入
  - `monitor_session_status` → 更新状态标签

---

## Phase 6 — 测试

### 6.1 MCP Server 测试

- [ ] MCP server 启动、tool 注册正确
- [ ] `create_monitor` → HTTP POST 到 CCM API
- [ ] `check_monitors` → HTTP GET 返回正确状态
- [ ] `stop_monitor` → HTTP DELETE 取消子 session
- [ ] API 不可达时返回 `{"success": false, "error": "..."}`（never raise）

### 6.2 数据层测试

- [ ] MonitorSession / MonitorCheck CRUD
- [ ] `enabled_skills` JSON 字段读写
- [ ] Migration 正确创建所有表和字段

### 6.3 Dispatcher 测试

- [ ] 子 session 生命周期: 启动 → 检查 → 完成
- [ ] `max_checks` 耗尽 → status 变为 completed
- [ ] task 结束 → monitor 联动结束
- [ ] 子进程超时 → 记录 failed check → 继续下一轮
- [ ] 子进程崩溃 → 记录 failed check → 继续下一轮
- [ ] CancelledError → kill 子进程 → 正常退出
- [ ] 并发限制: 第 6 个 monitor 返回 429

### 6.4 API 测试

- [ ] POST: 正常创建
- [ ] POST: `enabled_skills` 无 monitor 时 → 403
- [ ] POST: task 不存在 → 404
- [ ] POST: task 已完成 → 400
- [ ] POST: 超过并发限制 → 429
- [ ] DELETE: 正常停止
- [ ] GET: 列表和详情
- [ ] task 删除 → MonitorCheck 和 MonitorSession 全部清理，无孤儿数据
- [ ] task 取消 → 所有 running monitor 变为 cancelled

### 6.5 MCP Config 测试

- [ ] `generate_mcp_config` 生成正确的 JSON
- [ ] `enabled_skills` 为 None 时返回 None
- [ ] `enabled_skills` 为 `{"monitor": true}` 时生成包含 ccm_skills server 的配置
- [ ] `cleanup_mcp_config` 正确清理临时文件

### 6.6 集成测试

- [ ] 端到端: 创建 task(enabled_skills) → Claude CLI 启动带 `--mcp-config` → MCP server 响应 tool 调用

---

## Phase 7 — 文档

- [ ] 更新 CLAUDE.md（MCP 架构、enabled_skills 字段）
- [ ] 更新 README.md（Monitor 功能说明、创建 task 时勾选 Skills）
- [ ] 更新 TEST.md（新增测试用例）

---

## 实现顺序建议

```
Phase 1 (数据层)      → 可独立完成，无依赖
Phase 2 (MCP Server)  → 可独立完成，无依赖
Phase 3 (CLI 注入)    → 依赖 Phase 1 (enabled_skills) + Phase 2 (MCP config)
Phase 4 (API + 子 Session) → 依赖 Phase 1 (model) + Phase 2 (MCP server)
Phase 5 (前端)        → 依赖 Phase 1 (schema) + Phase 4 (API)
Phase 6 (测试)        → 每个 Phase 完成后立即补测试
Phase 7 (文档)        → 最后
```

建议分两批:
1. **后端核心**: Phase 1 → 2 → 3 → 4（可以用 loop 模式跑完）
2. **前端 + 测试**: Phase 5 → 6 → 7

---

## 未来扩展（本次不实现）

### Loop 模式集成
- Loop 的 Claude 在迭代间调用 `create_monitor` → CCM 阻塞到 monitor 完成 → 继续下一轮
- 需要扩展 MCP tool: `create_gate_monitor()` — 同步阻塞版本

### 更多 MCP Skills
| Skill | 对应 agent-ml-research | 说明 |
|-------|------------------------|------|
| `spawn_worker` | `spawn_worker()` | 启动独立工作 agent |
| `query_state` | `read_registry()` | 读取/写入持久化状态 |
| `send_message` | `main_agent_post_to_director()` | agent 间通信 |

### 角色工具注册（参考 agent-ml-research 的 `tool_registry.py`）
- 当有 2-3 个 skill 时，提取 `backend/services/tool_registry.py` 统一管理
- `enabled_skills` JSON 天然支持多 skill，数据模型不用改
- 每个 skill 对应 MCP server 中的一组 tools
- 按角色定义工具白名单（`RoleDef(name, mode, tools)`）

### Task → Agent 概念升级
- Task 演变为 Agent: 有身份、有工具、有记忆、能自主协作
- 参考 agent-ml-research 的三角架构: Director（决策）+ Agent（执行）+ Reviewer（审查）
