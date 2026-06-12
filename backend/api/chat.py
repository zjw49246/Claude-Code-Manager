import os
import json

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import and_, not_, select  # still used by chat history
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database import get_db
from backend.models.task import Task
from backend.models.log_entry import LogEntry

router = APIRouter(prefix="/api/tasks", tags=["chat"])


class ChatMessage(BaseModel):
    message: str
    image_paths: list[str] | None = None  # kept for backwards compatibility
    file_paths: list[str] | None = None
    secret_ids: list[int] | None = None
    # One-shot model override for this message (does not change task.model)
    model: str | None = None


@router.post("/{task_id}/chat")
async def send_chat_message(
    task_id: int,
    body: ChatMessage,
    db: AsyncSession = Depends(get_db),
):
    """Send a follow-up message on a task, resuming its previous session."""
    task = await db.get(Task, task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    if task.worker_id is not None:
        # Worker task：代理到 Worker CCM（session 在 worker 上，由 worker 校验）
        return await _send_worker_chat(task, body, db)
    if not task.session_id:
        raise HTTPException(400, "No previous session on this task. Run the task first.")

    # Parse $command syntax
    from backend.services.command_registry import parse_command, COMMAND_REGISTRY
    command, command_args = parse_command(body.message)
    command_skills: dict | None = None

    # Check for unknown $command
    stripped = body.message.strip()
    if stripped.startswith("$") and command is None:
        unknown_cmd = stripped.split(None, 1)[0]
        raise HTTPException(400, f"未知命令 {unknown_cmd}，输入 $help 查看可用命令")

    # Build prompt — append secrets, skill instructions, and image paths
    prompt_parts = [body.message]
    if command:
        # $command detected: inject command prompt and set temporary skills
        prompt_parts.append(command.prompt_template)
        if command_args:
            prompt_parts[0] = command_args
        command_skills = command.required_skills or None
    else:
        # Normal message: inject prompts for permanently enabled skills
        if task.enabled_skills:
            for skill_name, enabled in task.enabled_skills.items():
                if enabled and skill_name in COMMAND_REGISTRY:
                    cmd = COMMAND_REGISTRY[skill_name]
                    if not cmd.always_available:
                        prompt_parts.append(cmd.prompt_template)
    if body.secret_ids:
        from backend.services.dispatcher import _build_secrets_block
        from backend.database import async_session
        secrets_block = await _build_secrets_block(async_session, body.secret_ids)
        if secrets_block:
            prompt_parts.append(secrets_block)
    all_paths = body.file_paths or body.image_paths or []
    if all_paths:
        file_list = "\n".join(f"- {p}" for p in all_paths)
        prompt_parts.append(f"请用 Read 工具查看以下文件：\n{file_list}")
    prompt = "\n\n".join(prompt_parts)

    # Build file attachment metadata for storage and display
    _IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
    attachments: list[dict] = []
    for p in all_paths:
        filename = os.path.basename(p)
        ext = os.path.splitext(filename)[1].lower()
        attachments.append({
            "url": f"/api/uploads/{filename}",
            "name": filename,
            "is_image": ext in _IMAGE_EXTS,
        })

    # Store user message as a log entry (use instance_id=1 as placeholder)
    user_log = LogEntry(
        instance_id=1,
        task_id=task_id,
        event_type="user_message",
        role="user",
        content=body.message,
        raw_json=json.dumps({"attachments": attachments}) if attachments else None,
        is_error=False,
    )
    db.add(user_log)
    await db.commit()

    # Broadcast user message to task channel
    from backend.main import broadcaster
    await broadcaster.broadcast(f"task:{task_id}", {
        "event_type": "user_message",
        "role": "user",
        "content": body.message,
        "image_paths": body.image_paths or [],
    })

    # Enqueue for serial processing (replaces direct launch)
    from backend.main import dispatcher
    from backend.services.dispatcher import PRIORITY_USER
    await dispatcher.enqueue_message(
        task_id=task_id,
        prompt=prompt,
        priority=PRIORITY_USER,
        source="user",
        command_skills=command_skills,
        model_override=body.model,
    )

    return {"ok": True, "queued": True, "session_id": task.session_id}


async def _send_worker_chat(task: Task, body: ChatMessage, db: AsyncSession):
    """Worker task 的 chat 代理（elastic-worker 设计 §6.3）。

    顺序很重要：存 user_message → 广播 → 推附件 → 订阅 relay → 转发 →
    同步 session_id（worker 广播前会 pop session_id，只有 chat 响应里有）。
    """
    from backend.main import broadcaster, worker_proxy
    if worker_proxy is None:
        raise HTTPException(503, "Worker 功能未启用")
    if body.secret_ids:
        # secrets 存在 Manager DB，worker 解析不了 manager 的 secret id
        raise HTTPException(400, "Worker task 暂不支持引用 Secrets（Phase 3）")

    worker = await worker_proxy.require_ready_worker(task.worker_id)

    all_paths = body.file_paths or body.image_paths or []
    _IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
    attachments = [
        {
            "url": f"/api/uploads/{os.path.basename(p)}",
            "name": os.path.basename(p),
            "is_error": False,
            "is_image": os.path.splitext(p)[1].lower() in _IMAGE_EXTS,
        }
        for p in all_paths
    ]

    # 1. Manager DB 存 user_message（日志完整性；relay 会跳过 worker 回传的同条）
    db.add(LogEntry(
        instance_id=None,
        task_id=task.id,
        event_type="user_message",
        role="user",
        content=body.message,
        raw_json=json.dumps({"attachments": attachments}) if attachments else None,
        is_error=False,
    ))
    await db.commit()

    # 2. 广播到 Manager 前端
    await broadcaster.broadcast(f"task:{task.id}", {
        "event_type": "user_message",
        "role": "user",
        "content": body.message,
        "image_paths": body.image_paths or [],
    })

    # 3. 附件推到 worker 同一路径（worker 上 Claude 用 Read 读）
    if all_paths:
        try:
            await worker_proxy.push_files(worker, all_paths)
        except Exception as e:
            raise HTTPException(503, f"附件同步到 Worker 失败: {e}")

    # 4. 确保 relay 订阅（幂等；Manager 重启后已完成 task 不在恢复列表里，
    #    此时 chat 若不补订阅，worker 的响应事件全丢）
    await worker_proxy.relay.subscribe_task(worker, task.id)

    # 5. 转发到 Worker CCM（worker 自己做 $command/skill 展开）
    result = await worker_proxy.proxy_to_worker(
        task, "POST", f"/api/tasks/{task.id}/chat",
        body={
            "message": body.message,
            "image_paths": body.image_paths,
            "file_paths": body.file_paths,
            "model": body.model,
        },
    )

    # 6. 同步 session_id（worker instance_manager 广播前 pop 掉了，relay 收不到）
    if isinstance(result, dict) and result.get("session_id"):
        task.session_id = result["session_id"]
        await db.commit()

    if isinstance(result, dict):
        result["instance_id"] = None  # worker 的 instance_id 对 Manager 无意义
    return result


def _tool_summary(tool_input: str | None) -> str:
    """Extract a short one-line summary from tool_input JSON."""
    if not tool_input:
        return ""
    try:
        parsed = json.loads(tool_input)
        if isinstance(parsed, dict):
            if cmd := parsed.get("command"):
                return cmd[:120] + "..." if len(cmd) > 120 else cmd
            if fp := parsed.get("file_path"):
                return fp
            if pat := parsed.get("pattern"):
                path = parsed.get("path", "")
                return f"{pat} in {path}" if path else pat
            if q := parsed.get("query"):
                return q[:120] + "..." if len(q) > 120 else q
    except (json.JSONDecodeError, TypeError):
        pass
    return ""


@router.get("/{task_id}/chat/history")
async def get_chat_history(
    task_id: int,
    limit: int = 0,
    before_id: int = 0,
    compact: bool = True,
    touch: bool = False,
    db: AsyncSession = Depends(get_db),
):
    """Get chat-formatted history for a task.

    compact=True (default): tool_input/tool_output replaced with short summary.
    compact=False: full tool_input/tool_output included (truncated at 20k chars).
    before_id: only return messages with id < before_id (for pagination).
    touch=True: count this fetch as a user access (move-to-front). Only the
    frontend's initial page load sends it — pagination, background polling and
    stale old-version clients must NOT reorder tasks (prod task 68 实录：
    一个旧版前端残留标签页每隔十几分钟轮询一次，任务在列表里来回跳).
    """
    task = await db.get(Task, task_id)
    if not task:
        raise HTTPException(404, "Task not found")

    # 访问 = 移到同组（starred/非 starred）第一位：写 sort_order 为组内
    # 最大键 +60，其余任务相对顺序不变、整体后移一位（位置链表语义）
    if touch:
        from datetime import datetime as _dt
        from sqlalchemy import Float as _Float, func as _func
        # 先取旧键再 touch——若先写 last_accessed_at=now，_own 永远大于
        # 组内最大键，sort_order 写入分支对无 sort_order 的任务永远不触发
        _own = task.sort_order if task.sort_order is not None else (
            task.last_accessed_at.timestamp() if task.last_accessed_at
            else (task.created_at.timestamp() if task.created_at else 0)
        )
        task.last_accessed_at = _dt.utcnow()
        _eff = _func.coalesce(
            Task.sort_order,
            _func.cast(_func.strftime("%s", _func.coalesce(Task.last_accessed_at, Task.created_at)), _Float),
        )
        _max_key = (
            await db.execute(
                select(_func.max(_eff)).where(
                    Task.archived == False,  # noqa: E712
                    Task.starred == task.starred,
                    Task.id != task_id,
                )
            )
        ).scalar()
        if _max_key is not None and _own <= _max_key:
            task.sort_order = _max_key + 60
        await db.commit()

    allowed = ["user_message", "message", "result", "tool_use", "tool_result", "system_init", "system_event", "thinking", "process_exit"]
    # Noisy telemetry must be excluded in SQL, before LIMIT applies. Filtering
    # after the query made pages come back short (< limit), which the client
    # reads as "history exhausted" — older messages became unreachable.
    noisy_system = ["task_progress", "thinking_tokens", "token_usage", "api_request", "api_response"]
    cols = [
        LogEntry.id, LogEntry.role, LogEntry.event_type, LogEntry.content,
        LogEntry.tool_name, LogEntry.tool_input, LogEntry.tool_output,
        LogEntry.is_error, LogEntry.loop_iteration, LogEntry.timestamp,
        LogEntry.raw_json,
    ]
    conditions = [
        LogEntry.task_id == task_id,
        LogEntry.event_type.in_(allowed),
        not_(and_(
            LogEntry.event_type == "system_event",
            LogEntry.content.in_(noisy_system),
        )),
    ]
    if before_id > 0:
        conditions.append(LogEntry.id < before_id)

    if limit > 0:
        stmt = (
            select(*cols)
            .where(*conditions)
            .order_by(LogEntry.id.desc())
            .limit(limit)
        )
        result = await db.execute(stmt)
        rows = list(reversed(result.all()))
    else:
        stmt = (
            select(*cols)
            .where(*conditions)
            .order_by(LogEntry.id.asc())
        )
        result = await db.execute(stmt)
        rows = result.all()

    _TRUNCATE = 20_000  # chars; tool outputs can be huge (file reads, bash output)

    messages = []
    current_source = None  # track monitor context
    for row in rows:
        tool_input = row.tool_input
        tool_output = row.tool_output

        if compact and row.event_type in ("tool_use", "tool_result"):
            summary = _tool_summary(tool_input) if row.event_type == "tool_use" else None
            tool_input = summary or None
            tool_output = None
        else:
            if tool_input and len(tool_input) > _TRUNCATE:
                tool_input = tool_input[:_TRUNCATE] + "\n…(truncated)"
            if tool_output and len(tool_output) > _TRUNCATE:
                tool_output = tool_output[:_TRUNCATE] + "\n…(truncated)"

        attachments = None
        image_urls = None
        source = None
        if row.raw_json:
            try:
                raw = json.loads(row.raw_json)
                if isinstance(raw, dict):
                    if raw.get("attachments"):
                        attachments = raw["attachments"]
                        image_urls = [a["url"] for a in attachments if a.get("is_image")]
                    elif raw.get("image_urls"):
                        image_urls = raw["image_urls"]
                        attachments = [{"url": u, "name": u.split("/")[-1], "is_image": True} for u in image_urls]
                    if raw.get("source"):
                        source = raw["source"]
            except (json.JSONDecodeError, TypeError):
                pass

        if row.event_type in ("user_message", "system_event") and source:
            current_source = source
        elif row.event_type == "user_message":
            current_source = None
        msg_source = current_source

        messages.append({
            "id": row.id,
            "role": row.role or ("assistant" if row.event_type in ("message", "result") else "system"),
            "event_type": row.event_type,
            "content": row.content,
            "tool_name": row.tool_name,
            "tool_input": tool_input,
            "tool_output": tool_output,
            "is_error": row.is_error,
            "loop_iteration": row.loop_iteration,
            "timestamp": (row.timestamp.isoformat() + "Z") if row.timestamp else None,
            "image_urls": image_urls or None,
            "attachments": attachments,
            "source": msg_source,
        })

    return messages


@router.get("/{task_id}/chat/{message_id}/detail")
async def get_message_detail(
    task_id: int,
    message_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Get full tool_input/tool_output for a single message (lazy-load on expand)."""
    _TRUNCATE = 20_000

    stmt = (
        select(LogEntry.id, LogEntry.tool_input, LogEntry.tool_output, LogEntry.content)
        .where(LogEntry.id == message_id, LogEntry.task_id == task_id)
    )
    result = await db.execute(stmt)
    row = result.one_or_none()
    if not row:
        raise HTTPException(404, "Message not found")

    tool_input = row.tool_input
    tool_output = row.tool_output
    if tool_input and len(tool_input) > _TRUNCATE:
        tool_input = tool_input[:_TRUNCATE] + "\n…(truncated)"
    if tool_output and len(tool_output) > _TRUNCATE:
        tool_output = tool_output[:_TRUNCATE] + "\n…(truncated)"

    return {
        "id": row.id,
        "tool_input": tool_input,
        "tool_output": tool_output,
        "content": row.content,
    }


class InjectMessage(BaseModel):
    message: str


@router.post("/{task_id}/inject")
async def inject_message(
    task_id: int,
    body: InjectMessage,
    db: AsyncSession = Depends(get_db),
):
    """Inject a message into the RUNNING turn of a PTY session (PTY-only).

    Unlike /chat (which queues a new turn), this delivers the text into CC's
    context mid-execution via the channel bridge — CC sees it at the next
    tool-call boundary. Fails when PTY mode is off or no live session exists.
    """
    task = await db.get(Task, task_id)
    if not task:
        raise HTTPException(404, "Task not found")

    from backend.main import instance_manager, broadcaster
    if not instance_manager.pty_mode_enabled:
        raise HTTPException(400, "PTY 模式未开启，注入功能仅在 PTY 模式下可用")
    if not task.session_id:
        raise HTTPException(400, "Task has no session yet")

    ok = await instance_manager.inject_pty_message(task.session_id, body.message)
    if not ok:
        raise HTTPException(
            409,
            "注入失败：没有正在运行的 turn。注入仅在任务执行中可用"
            "（用于中途补充指令）；空闲时请关闭注入模式直接发普通消息",
        )

    # Record + broadcast so the injected text shows up in the chat thread
    db.add(LogEntry(
        instance_id=task.instance_id or 1,
        task_id=task_id,
        event_type="user_message",
        role="user",
        content=body.message,
        raw_json=json.dumps({"source": "inject"}),
        is_error=False,
    ))
    await db.commit()
    await broadcaster.broadcast(f"task:{task_id}", {
        "event_type": "user_message",
        "role": "user",
        "content": body.message,
        "source": "inject",
    })
    return {"ok": True, "injected": True}


class PermissionDecision(BaseModel):
    behavior: str  # "allow" | "deny"


@router.post("/{task_id}/permissions/{request_id}")
async def resolve_permission(
    task_id: int,
    request_id: str,
    body: PermissionDecision,
    db: AsyncSession = Depends(get_db),
):
    """权限透传回包：前端卡片的 允许/拒绝 → BridgeHub → CC（PTY-only）。

    CC 侧 channel server 最多等 120s，超时默认 deny——过期请求返回 410。
    """
    if body.behavior not in ("allow", "deny"):
        raise HTTPException(400, "behavior must be 'allow' or 'deny'")

    task = await db.get(Task, task_id)
    if not task:
        raise HTTPException(404, "Task not found")

    from backend.main import instance_manager
    ok = await instance_manager.resolve_pty_permission(request_id, body.behavior)
    if not ok:
        raise HTTPException(410, "权限请求已过期或不存在（CC 侧可能已超时默认拒绝）")
    return {"ok": True, "behavior": body.behavior}
