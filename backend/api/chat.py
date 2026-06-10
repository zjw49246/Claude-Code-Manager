import os
import json

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select  # still used by chat history
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
    )

    return {"ok": True, "queued": True, "session_id": task.session_id}


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
    compact: bool = True,
    db: AsyncSession = Depends(get_db),
):
    """Get chat-formatted history for a task.

    compact=True (default): tool_input/tool_output replaced with short summary.
    compact=False: full tool_input/tool_output included (truncated at 20k chars).
    """
    task = await db.get(Task, task_id)
    if not task:
        raise HTTPException(404, "Task not found")

    allowed = ["user_message", "message", "result", "tool_use", "tool_result", "system_init", "system_event", "thinking", "process_exit"]
    cols = [
        LogEntry.id, LogEntry.role, LogEntry.event_type, LogEntry.content,
        LogEntry.tool_name, LogEntry.tool_input, LogEntry.tool_output,
        LogEntry.is_error, LogEntry.loop_iteration, LogEntry.timestamp,
        LogEntry.raw_json,
    ]
    if limit > 0:
        stmt = (
            select(*cols)
            .where(LogEntry.task_id == task_id, LogEntry.event_type.in_(allowed))
            .order_by(LogEntry.id.desc())
            .limit(limit)
        )
        result = await db.execute(stmt)
        rows = list(reversed(result.all()))
    else:
        stmt = (
            select(*cols)
            .where(LogEntry.task_id == task_id, LogEntry.event_type.in_(allowed))
            .order_by(LogEntry.id.asc())
        )
        result = await db.execute(stmt)
        rows = result.all()

    _TRUNCATE = 20_000  # chars; tool outputs can be huge (file reads, bash output)

    messages = []
    current_source = None  # track monitor context
    for row in rows:
        # Skip noisy system events (heartbeats, telemetry subtypes)
        if row.event_type == "system_event" and row.content in ("task_progress", "thinking_tokens", "token_usage", "api_request", "api_response"):
            continue

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
            "timestamp": row.timestamp.isoformat() if row.timestamp else None,
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
