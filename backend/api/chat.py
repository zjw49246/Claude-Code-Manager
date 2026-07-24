import logging
import os
import json

from fastapi import APIRouter, Depends, HTTPException, Request
from backend.api.deps import require_task_access
from pydantic import BaseModel
from sqlalchemy import and_, not_, select, func, update as sa_update  # still used by chat history
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database import get_db
from backend.models.task import Task
from backend.models.log_entry import LogEntry
from backend.models.user_skill import UserSkill
from backend.services.task_queue import task_is_pr_review_superseded
from backend.services.worker_proxy import get_task_operation_lock
from backend.services.worker_relay import (
    worker_task_generation,
    worker_task_generation_predicates,
)

logger = logging.getLogger(__name__)

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
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Send a follow-up message on a task, resuming its previous session."""
    task = await db.get(Task, task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    await require_task_access(request, task, db)
    if task_is_pr_review_superseded(task):
        raise HTTPException(
            409,
            "This PR review task was superseded by a newer push",
        )
    if task.shared_from_id is not None:
        return await _send_shared_chat(task, body, db)
    if task.worker_id is not None:
        return await _send_worker_chat(task, body, db, request)
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

    # Keep sender identity presentation-only.  The raw text is what the model
    # receives; the prefixed form is only stored/broadcast for the chat UI.
    user_id = getattr(request.state, "user_id", None)
    model_message = body.message
    display_content = model_message
    sender_display_name = None
    if user_id:
        from backend.models.user import User
        sender = await db.get(User, user_id)
        if sender:
            sender_display_name = sender.name
            display_content = f"[{sender.name}] {model_message}"

    # Build prompt — append secrets, skill instructions, and image paths
    prompt_parts = [model_message]
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
    log_metadata: dict = {"raw_content": model_message}
    if attachments:
        log_metadata["attachments"] = attachments
    if sender_display_name:
        # Model-facing history rebuilds must use this exact original text,
        # never guess by regex (the user's real message may start with [BUG]).
        log_metadata["sender_name"] = sender_display_name
    user_log = LogEntry(
        instance_id=1,
        task_id=task_id,
        event_type="user_message",
        role="user",
        content=display_content,
        raw_json=json.dumps(log_metadata) if log_metadata else None,
        is_error=False,
    )
    db.add(user_log)
    await db.commit()

    # Broadcast user message to task channel
    from backend.main import broadcaster
    image_urls = [a["url"] for a in attachments if a.get("is_image")]
    broadcast_data = {
        "event_type": "user_message",
        "role": "user",
        "content": display_content,
        "raw_content": model_message,
        "image_urls": image_urls,
        "attachments": attachments,
    }
    if sender_display_name:
        broadcast_data["sender_name"] = sender_display_name
    await broadcaster.broadcast(f"task:{task_id}", broadcast_data)

    # Enqueue for serial processing (replaces direct launch)
    from backend.main import dispatcher
    from backend.services.dispatcher import PRIORITY_USER, TaskStartPausedError
    try:
        await dispatcher.enqueue_message(
            task_id=task_id,
            prompt=prompt,
            priority=PRIORITY_USER,
            source="user",
            command_skills=command_skills,
            model_override=body.model,
        )
    except TaskStartPausedError as exc:
        raise HTTPException(
            status_code=409,
            detail="服务即将重启，消息未进入执行队列，请重连后重试",
        ) from exc

    return {"ok": True, "queued": True, "session_id": task.session_id}


async def _send_shared_chat(task: Task, body: ChatMessage, db: AsyncSession):
    """Shared (shadow) task: store locally, broadcast, proxy to sharer CCM."""
    from backend.main import broadcaster
    from backend.models.task_share import SharedTaskReceived
    from backend.services.shared_proxy import proxy_chat

    # Find the shared record
    result = await db.execute(
        select(SharedTaskReceived).where(SharedTaskReceived.id == task.shared_from_id)
    )
    shared = result.scalar_one_or_none()
    if not shared:
        raise HTTPException(400, "Shared task record not found")

    # Get sender name for prefix
    from backend.models.feishu_binding import FeishuUserBinding
    binding_result = await db.execute(select(FeishuUserBinding).limit(1))
    binding = binding_result.scalar_one_or_none()
    sender_name = binding.feishu_name if binding else None
    prefixed = f"[{sender_name}] {body.message}" if sender_name else body.message

    log_metadata: dict = {"raw_content": body.message}
    if sender_name:
        log_metadata["sender_name"] = sender_name

    # Store user message locally WITH prefix (same as what sharer sees)
    user_log = LogEntry(
        instance_id=None,
        task_id=task.id,
        event_type="user_message",
        role="user",
        content=prefixed,
        raw_json=json.dumps(log_metadata),
        is_error=False,
    )
    db.add(user_log)
    await db.commit()

    # Broadcast to local frontend WITH prefix
    await broadcaster.broadcast(f"task:{task.id}", {
        "event_type": "user_message",
        "role": "user",
        "content": prefixed,
        "raw_content": body.message,
        "sender_name": sender_name,
    })

    # Proxy to sharer
    try:
        await proxy_chat(
            shared.owner_ccm_url, shared.remote_task_id, shared.share_token,
            message=body.message, sender_name=sender_name,
        )
    except Exception as e:
        raise HTTPException(502, f"Cannot reach sharer CCM: {e}")

    return {"ok": True, "queued": True}


async def _send_worker_chat(task: Task, body: ChatMessage, db: AsyncSession, request: Request | None = None):
    """Worker task 的 chat 代理。"""
    from backend.main import broadcaster, worker_proxy
    if worker_proxy is None:
        raise HTTPException(503, "Worker 功能未启用")
    if body.secret_ids:
        raise HTTPException(400, "Worker task 暂不支持引用 Secrets（Phase 3）")

    # Drop the route's read snapshot before waiting for the process-wide lock.
    # TaskMigrator holds the same lock for its complete copy/rebind workflow.
    task_id = task.id
    await db.rollback()
    async with get_task_operation_lock(task_id):
        db.expire_all()
        current = await db.get(Task, task_id)
        observed = (
            worker_task_generation(current)
            if current is not None
            else None
        )
        if observed is None:
            raise HTTPException(
                409,
                "Task moved away from its Worker before chat could be sent",
            )
        if task_is_pr_review_superseded(current):
            raise HTTPException(
                409,
                "This PR review task was superseded by a newer push",
            )

        # Preserve the sender prefix for the Manager UI, but forward only the
        # raw user text so it never becomes part of the model prompt.
        model_message = body.message
        display_content = model_message
        sender_display_name = None
        if request:
            uid = getattr(request.state, "user_id", None)
            if uid:
                from backend.models.user import User
                sender = await db.get(User, uid)
                if sender:
                    sender_display_name = sender.name
                    display_content = f"[{sender.name}] {model_message}"

        worker = await worker_proxy.require_ready_worker(observed.worker_id)

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

        # 1. Persist the display copy only if the exact pre-network Worker
        # generation is still current.
        guarded = await db.execute(
            sa_update(Task)
            .where(*worker_task_generation_predicates(observed))
            .values(status=observed.status)
        )
        if guarded.rowcount != 1:
            await db.rollback()
            raise HTTPException(
                409,
                "Task Worker generation changed before chat could be sent",
            )
        log_metadata: dict = {"raw_content": model_message}
        if attachments:
            log_metadata["attachments"] = attachments
        if sender_display_name:
            log_metadata["sender_name"] = sender_display_name
        db.add(LogEntry(
            instance_id=None,
            task_id=current.id,
            event_type="user_message",
            role="user",
            content=display_content,
            raw_json=json.dumps(log_metadata) if log_metadata else None,
            is_error=False,
        ))
        await db.commit()

        # 2. Broadcast to the Manager frontend.
        broadcast_data = {
            "event_type": "user_message",
            "role": "user",
            "content": display_content,
            "raw_content": model_message,
            "image_paths": body.image_paths or [],
        }
        if sender_display_name:
            broadcast_data["sender_name"] = sender_display_name
        await broadcaster.broadcast(f"task:{current.id}", broadcast_data)

        # 3. Push attachments to the same Worker path.
        if all_paths:
            try:
                await worker_proxy.push_files(worker, all_paths)
            except Exception as e:
                raise HTTPException(503, f"附件同步到 Worker 失败: {e}")

        # 4. Ensure relay subscription before the remote turn can emit events.
        await worker_proxy.relay.subscribe_task(worker, current.id)

        # 5. The common operation lock is already held; asking WorkerProxy to
        # acquire it again would deadlock.
        result = await worker_proxy.proxy_to_worker(
            current,
            "POST",
            f"/api/tasks/{current.id}/chat",
            body={
                "message": model_message,
                "image_paths": body.image_paths,
                "file_paths": body.file_paths,
                "model": body.model,
            },
            operation_lock_held=True,
        )

        # 6. A delayed response can only update the generation that issued the
        # request.  Even responses without a session id perform a no-op CAS so
        # reassignment/retry during the network await is reported as conflict.
        values = {"status": observed.status}
        if isinstance(result, dict) and result.get("session_id"):
            values["session_id"] = result["session_id"]
        changed = await db.execute(
            sa_update(Task)
            .where(*worker_task_generation_predicates(observed))
            .values(**values)
        )
        if changed.rowcount != 1:
            await db.rollback()
            raise HTTPException(
                409,
                "Task Worker assignment or generation changed while chat was in flight",
            )
        await db.commit()

        if isinstance(result, dict):
            result["instance_id"] = None  # Worker instance ids are not Manager ids.
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
    task_id: int, request: Request,
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
    from backend.models.task import Task as _T2
    _task_check = await db.get(_T2, task_id)
    if _task_check:
        await require_task_access(request, _task_check, db)

    task = await db.get(Task, task_id)
    if not task:
        raise HTTPException(404, "Task not found")

    if touch:
        from datetime import datetime as _dt
        task.last_accessed_at = _dt.utcnow()
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
        # Over-fetch to compensate for Python-level filtering (message+user
        # rows are skipped below). Without this, a page of exactly `limit`
        # rows can shrink below `limit` after filtering, and the client
        # interprets that as "no more history" — hiding the Load More button.
        stmt = (
            select(*cols)
            .where(*conditions)
            .order_by(LogEntry.id.desc())
            .limit(limit + 20)
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
        raw_content = None
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
                    if isinstance(raw.get("raw_content"), str):
                        raw_content = raw["raw_content"]
            except (json.JSONDecodeError, TypeError):
                pass

        if row.event_type in ("user_message", "system_event") and source:
            current_source = source
        elif row.event_type == "user_message":
            current_source = None
        msg_source = current_source

        # event_type=message with role=user are CC internal messages (compact
        # summaries, task-notifications, local-command caveats) — not real user
        # input (which uses event_type=user_message). Hide them from chat.
        if row.event_type == "message" and row.role == "user":
            continue

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
            "raw_content": raw_content,
        })

    # Trim back to requested limit (we over-fetched to compensate for
    # Python-level filtering). Keep the newest messages (end of list).
    if limit > 0 and len(messages) > limit:
        messages = messages[-limit:]
    return messages


@router.get("/{task_id}/chat/{message_id}/detail")
async def get_message_detail(
    task_id: int,
    message_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    _t = await db.get(Task, task_id)
    if _t:
        await require_task_access(request, _t, db)
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
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Inject a message into the task's currently running turn."""
    task = await db.get(Task, task_id)
    if task:
        await require_task_access(request, task, db)
    if not task:
        raise HTTPException(404, "Task not found")

    from backend.main import instance_manager, broadcaster
    if not task.session_id:
        raise HTTPException(400, "Task has no session yet")
    if task.worker_id is not None or task.shared_from_id is not None:
        raise HTTPException(400, "远程 Worker / shared task 暂不支持执行中注入")

    provider = (task.provider or "claude").lower()
    if provider == "codex":
        from backend.config import settings
        if not settings.codex_app_server_enabled:
            raise HTTPException(
                400,
                "Codex app-server 未开启，当前 exec 链路不支持执行中注入",
            )
        ok = await instance_manager.inject_codex_message(
            task.session_id, body.message
        )
        unavailable_detail = (
            "注入失败：当前 Codex turn 已结束、暂不可 steer，或正在使用 "
            "exec fallback；空闲时请关闭注入模式直接发普通消息"
        )
    elif provider == "claude":
        if not instance_manager.pty_mode_enabled:
            raise HTTPException(
                400,
                "PTY 模式未开启，Claude 注入仅在 PTY 模式下可用",
            )
        ok = await instance_manager.inject_pty_message(
            task.session_id, body.message
        )
        unavailable_detail = (
            "注入失败：没有正在运行的 turn。注入仅在任务执行中可用"
            "（用于中途补充指令）；空闲时请关闭注入模式直接发普通消息"
        )
    else:
        raise HTTPException(400, f"Provider {provider} 不支持执行中注入")

    if not ok:
        raise HTTPException(409, unavailable_detail)

    # Record + broadcast so the injected text shows up in the chat thread
    db.add(LogEntry(
        instance_id=task.instance_id or 1,
        task_id=task_id,
        event_type="user_message",
        role="user",
        content=body.message,
        raw_json=json.dumps({"source": "inject", "raw_content": body.message}),
        is_error=False,
    ))
    await db.commit()
    await broadcaster.broadcast(f"task:{task_id}", {
        "event_type": "user_message",
        "role": "user",
        "content": body.message,
        "source": "inject",
        "raw_content": body.message,
    })
    return {"ok": True, "injected": True}


class PermissionDecision(BaseModel):
    behavior: str  # "allow" | "deny"


@router.post("/{task_id}/permissions/{request_id}")
async def resolve_permission(
    task_id: int,
    request_id: str,
    body: PermissionDecision,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    if body.behavior not in ("allow", "deny"):
        raise HTTPException(400, "behavior must be 'allow' or 'deny'")

    task = await db.get(Task, task_id)
    if task:
        await require_task_access(request, task, db)
    if not task:
        raise HTTPException(404, "Task not found")

    from backend.main import instance_manager
    ok = await instance_manager.resolve_pty_permission(request_id, body.behavior)
    if not ok:
        raise HTTPException(410, "权限请求已过期或不存在（CC 侧可能已超时默认拒绝）")
    return {"ok": True, "behavior": body.behavior}


# ---------------------------------------------------------------------------
# Task Distill — extract reusable skill from conversation history
# ---------------------------------------------------------------------------

async def _collect_conversation_for_distill(task_id: int, db: AsyncSession) -> str:
    """Collect conversation history for task skill distillation."""
    from backend.services.skill_distill import TASK_DISTILL_MAX_CHARS

    result = await db.execute(
        select(
            LogEntry.event_type,
            LogEntry.role,
            LogEntry.content,
            LogEntry.tool_name,
            LogEntry.is_error,
            LogEntry.raw_json,
        )
        .where(
            LogEntry.task_id == task_id,
            LogEntry.event_type.in_(["user_message", "message", "tool_use", "tool_result"]),
        )
        .order_by(LogEntry.id.asc())
    )
    rows = result.all()

    parts: list[str] = []
    total = 0
    for row in rows:
        event_type, role, content, tool_name, is_error, raw_json = row
        if not content:
            continue

        if event_type == "user_message":
            model_content = content
            if raw_json:
                try:
                    raw = json.loads(raw_json)
                    if isinstance(raw, dict) and isinstance(raw.get("raw_content"), str):
                        model_content = raw["raw_content"]
                except (json.JSONDecodeError, TypeError):
                    pass
            line = f"[User]: {model_content[:2000]}"
        elif event_type == "message" and role == "assistant":
            line = f"[Assistant]: {content[:2000]}"
        elif event_type == "tool_use" and tool_name:
            line = f"[Tool: {tool_name}]: {content[:500]}"
        elif event_type == "tool_result":
            prefix = "[Error]" if is_error else "[Result]"
            line = f"{prefix}: {content[:500]}"
        else:
            continue

        total += len(line)
        if total > TASK_DISTILL_MAX_CHARS:
            parts.append("... (conversation truncated)")
            break
        parts.append(line)

    return "\n".join(parts)


class DistillRequest(BaseModel):
    custom_instruction: str | None = None


class DistillSaveRequest(BaseModel):
    name: str
    description: str = ""
    content: str


@router.post("/{task_id}/distill")
async def distill_task(
    task_id: int,
    request: Request,
    body: DistillRequest = DistillRequest(),
    db: AsyncSession = Depends(get_db),
):
    """Distill a task's conversation into a reusable skill (markdown).

    Uses the task's provider and returns a card for user preview/editing.
    """
    task = await db.get(Task, task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    await require_task_access(request, task, db)

    conversation = await _collect_conversation_for_distill(task_id, db)
    if not conversation.strip():
        raise HTTPException(400, "No conversation history to distill")

    from backend.main import codex_pool, instance_manager
    from backend.services.skill_distill import (
        CodexDistillAccountUnavailableError,
        TaskDistillError,
        TaskDistillTimeoutError,
        distill_task_conversation,
    )
    title = (
        task.title
        or (task.description[:100] if task.description else "")
        or "Untitled"
    )
    try:
        result = await distill_task_conversation(
            title=title,
            conversation=conversation,
            provider=task.provider or "claude",
            custom_instruction=body.custom_instruction,
            codex_pool=codex_pool,
            codex_account_id=(task.metadata_ or {}).get("codex_account_id"),
            instance_manager=instance_manager,
        )
    except TaskDistillTimeoutError as exc:
        raise HTTPException(504, str(exc)) from exc
    except CodexDistillAccountUnavailableError as exc:
        raise HTTPException(503, str(exc)) from exc
    except TaskDistillError as exc:
        detail = (exc.stderr or exc.stdout).strip()[:500]
        logger.error(
            "distill: %s failed. stdout=%s stderr=%s",
            exc.provider,
            exc.stdout[:500],
            exc.stderr[:500],
        )
        message = str(exc)
        if detail:
            message = f"{message}: {detail}"
        raise HTTPException(502, message) from exc

    suggested_name = (task.title or task.description or "untitled")[:50].strip()

    return {
        "task_id": task_id,
        "suggested_name": suggested_name,
        "content": result["content"],
        "provider": result["provider"],
        "model": result["model"],
    }


@router.post("/{task_id}/distill/save")
async def save_distilled_skill(
    task_id: int, request: Request,
    body: DistillSaveRequest,
    db: AsyncSession = Depends(get_db),
):
    """Save a distilled skill as a UserSkill."""
    task = await db.get(Task, task_id)
    if not task:
        raise HTTPException(404, "Task not found")

    existing = await db.execute(
        select(UserSkill).where(UserSkill.name == body.name)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(409, f"Skill with name '{body.name}' already exists")

    skill = UserSkill(
        name=body.name,
        description=body.description or f"Distilled from task #{task_id}",
        content=body.content,
    )
    db.add(skill)
    await db.commit()
    await db.refresh(skill)

    return {
        "id": skill.id,
        "name": skill.name,
        "description": skill.description,
        "content": skill.content,
    }
