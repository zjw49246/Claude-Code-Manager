import asyncio
import logging
import os
import json

from fastapi import APIRouter, Depends, HTTPException, Request
from backend.api.deps import require_task_access
from pydantic import BaseModel
from sqlalchemy import and_, not_, select, func  # still used by chat history
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database import get_db
from backend.models.task import Task
from backend.models.log_entry import LogEntry
from backend.models.user_skill import UserSkill

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

    # Add user identifier prefix to all messages
    user_id = getattr(request.state, "user_id", None)
    message_text = body.message
    if user_id:
        from backend.models.user import User
        sender = await db.get(User, user_id)
        if sender:
            message_text = f"[{sender.name}] {body.message}"

    # Build prompt — append secrets, skill instructions, and image paths
    prompt_parts = [message_text]
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

    # Always show sender name in display for multi-user context
    display_content = message_text  # message_text already has [username] prefix for non-creators
    user_id_for_display = getattr(request.state, "user_id", None)
    sender_display_name = None
    if user_id_for_display:
        from backend.models.user import User as _User
        _sender = await db.get(_User, user_id_for_display)
        if _sender:
            sender_display_name = _sender.name

    # Store user message as a log entry (use instance_id=1 as placeholder)
    user_log = LogEntry(
        instance_id=1,
        task_id=task_id,
        event_type="user_message",
        role="user",
        content=display_content,
        raw_json=json.dumps({"attachments": attachments}) if attachments else None,
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
        "image_urls": image_urls,
        "attachments": attachments,
    }
    if sender_display_name:
        broadcast_data["sender_name"] = sender_display_name
    await broadcaster.broadcast(f"task:{task_id}", broadcast_data)

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

    # Store user message locally WITH prefix (same as what sharer sees)
    user_log = LogEntry(
        instance_id=None,
        task_id=task.id,
        event_type="user_message",
        role="user",
        content=prefixed,
        is_error=False,
    )
    db.add(user_log)
    await db.commit()

    # Broadcast to local frontend WITH prefix
    await broadcaster.broadcast(f"task:{task.id}", {
        "event_type": "user_message",
        "role": "user",
        "content": prefixed,
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

    # Add user prefix to all messages
    if request:
        uid = getattr(request.state, "user_id", None)
        if uid:
            from backend.models.user import User
            sender = await db.get(User, uid)
            if sender:
                body.message = f"[{sender.name}] {body.message}"

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
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Inject a message into the RUNNING turn of a PTY session (PTY-only)."""
    task = await db.get(Task, task_id)
    if task:
        await require_task_access(request, task, db)
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

_DISTILL_MAX_CHARS = 30_000
_DISTILL_MODEL = "claude-opus-4-6"


async def _collect_conversation_for_distill(task_id: int, db: AsyncSession) -> str:
    """Collect conversation history for distillation, capped at _DISTILL_MAX_CHARS."""
    result = await db.execute(
        select(LogEntry.event_type, LogEntry.role, LogEntry.content, LogEntry.tool_name, LogEntry.is_error)
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
        event_type, role, content, tool_name, is_error = row
        if not content:
            continue

        if event_type == "user_message":
            line = f"[User]: {content[:2000]}"
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
        if total > _DISTILL_MAX_CHARS:
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
    body: DistillRequest = DistillRequest(),
    db: AsyncSession = Depends(get_db),
):
    """Distill a task's conversation into a reusable skill (markdown).

    Reads the full conversation history, sends it to Opus for extraction,
    returns the generated skill card for user preview/editing.
    """
    task = await db.get(Task, task_id)
    if not task:
        raise HTTPException(404, "Task not found")

    conversation = await _collect_conversation_for_distill(task_id, db)
    if not conversation.strip():
        raise HTTPException(400, "No conversation history to distill")

    custom = ""
    if body.custom_instruction:
        custom = f"\n\n用户补充说明：{body.custom_instruction}"

    prompt = (
        "你是一个经验提取专家。下面是一个编程任务的完整对话记录。\n"
        "请从中提取可复用的经验，生成一份结构化的 Skill 卡片（Markdown 格式）。\n\n"
        "Skill 卡片应包含：\n"
        "1. **意图**：这类任务要解决什么问题\n"
        "2. **关键步骤**：做这类任务的推荐流程\n"
        "3. **踩坑点**：容易犯的错误和注意事项\n"
        "4. **验证方法**：怎么确认做对了\n"
        "5. **适用场景**：什么情况下这个 skill 有用\n\n"
        "要求：\n"
        "- 只保留可迁移的过程性知识，去掉具体的文件路径、变量名等细节\n"
        "- 用中文输出\n"
        "- 简洁实用，不要废话\n"
        f"{custom}\n\n"
        f"--- 任务标题 ---\n{task.title or task.description[:100] if task.description else 'Untitled'}\n\n"
        f"--- 对话记录 ---\n{conversation}"
    )

    import tempfile
    from backend.config import settings
    env = {
        k: v for k, v in os.environ.items()
        if k.upper() not in ("CLAUDECODE", "CLAUDE_CODE")
    }
    if "CLAUDE_CONFIG_DIR" not in env:
        try:
            from backend.services.claude_pool import pool
            if pool:
                acct = pool.select(validate=False)
                if acct:
                    env["CLAUDE_CONFIG_DIR"] = acct.config_dir
        except Exception:
            pass
        if "CLAUDE_CONFIG_DIR" not in env:
            for candidate in ["/home/ubuntu/.claude-account-2", "/home/ubuntu/.claude"]:
                if os.path.isdir(candidate):
                    env["CLAUDE_CONFIG_DIR"] = candidate
                    break

    cmd = [
        settings.claude_binary,
        "-p", "-",
        "--dangerously-skip-permissions",
        "--output-format", "json",
        "--model", _DISTILL_MODEL,
        "--max-turns", "1",
    ]

    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        stdout, stderr = await asyncio.wait_for(
            process.communicate(input=prompt.encode("utf-8")),
            timeout=300,
        )
    except asyncio.TimeoutError:
        raise HTTPException(504, "Distillation timed out (5min)")
    except Exception as e:
        logger.exception("distill: subprocess failed")
        raise HTTPException(500, f"Distillation failed: {e}")

    raw = stdout.decode("utf-8", errors="replace")
    if process.returncode != 0:
        err = stderr.decode("utf-8", errors="replace")[:500]
        logger.error("distill: claude failed. stdout=%s stderr=%s", raw[:500], err)
        raise HTTPException(502, f"Claude process failed (exit {process.returncode}): {err}")

    skill_content = ""
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            if obj.get("type") == "result":
                skill_content = obj.get("result", "")
                break
        except json.JSONDecodeError:
            continue

    if not skill_content:
        skill_content = raw.strip()

    suggested_name = (task.title or task.description or "untitled")[:50].strip()

    return {
        "task_id": task_id,
        "suggested_name": suggested_name,
        "content": skill_content,
    }


@router.post("/{task_id}/distill/save")
async def save_distilled_skill(
    task_id: int,
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
