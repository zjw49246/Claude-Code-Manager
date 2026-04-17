import os

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database import get_db
from backend.models.instance import Instance
from backend.models.task import Task
from backend.models.log_entry import LogEntry

router = APIRouter(prefix="/api/tasks", tags=["chat"])


class ChatMessage(BaseModel):
    message: str
    image_paths: list[str] | None = None  # absolute paths of uploaded images
    secret_ids: list[int] | None = None  # IDs of secrets to inject into prompt


async def _find_idle_instance(db: AsyncSession) -> Instance | None:
    """Find an idle instance to run a chat message."""
    result = await db.execute(
        select(Instance).where(Instance.status == "idle").order_by(Instance.id).limit(1)
    )
    return result.scalar_one_or_none()


@router.post("/{task_id}/chat")
async def send_chat_message(
    task_id: int,
    body: ChatMessage,
    db: AsyncSession = Depends(get_db),
):
    """Send a follow-up message on a task, resuming its previous session."""
    from backend.main import instance_manager

    task = await db.get(Task, task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    if not task.session_id:
        raise HTTPException(400, "No previous session on this task. Run the task first.")

    # Check no instance is currently working on this task
    busy_instance_id = None
    for inst_id, proc in instance_manager.processes.items():
        if proc.returncode is None:
            inst = await db.get(Instance, inst_id)
            if inst and inst.current_task_id == task_id:
                busy_instance_id = inst_id
                break

    if busy_instance_id is not None:
        raise HTTPException(409, "Task is currently being processed. Use Interrupt to stop it first.")

    # Find an idle instance
    inst = await _find_idle_instance(db)
    if not inst:
        raise HTTPException(400, "No idle instance available. Create one or wait.")

    # Build prompt — append secrets and image paths if provided
    prompt_parts = [body.message]
    if body.secret_ids:
        from backend.services.dispatcher import _build_secrets_block
        from backend.database import async_session
        secrets_block = await _build_secrets_block(async_session, body.secret_ids)
        if secrets_block:
            prompt_parts.append(secrets_block)
    if body.image_paths:
        image_list = "\n".join(f"- {p}" for p in body.image_paths)
        prompt_parts.append(f"请用 Read 工具查看以下图片：\n{image_list}")
    prompt = "\n\n".join(prompt_parts)

    # Store user message as a log entry
    user_log = LogEntry(
        instance_id=inst.id,
        task_id=task_id,
        event_type="user_message",
        role="user",
        content=body.message,
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

    # Determine cwd: Claude Code launches in repo root, session binds there
    cwd = task.last_cwd or task.target_repo
    if not cwd or not os.path.isdir(cwd):
        raise HTTPException(400, "Task working directory not found.")

    # Launch with --resume, using the task's cwd
    pid = await instance_manager.launch(
        instance_id=inst.id,
        prompt=prompt,
        task_id=task_id,
        cwd=cwd,
        model=inst.model,
        resume_session_id=task.session_id,
        thinking_budget=inst.thinking_budget,
        effort_level=inst.effort_level,
    )
    return {"ok": True, "pid": pid, "instance_id": inst.id, "session_id": task.session_id}


@router.get("/{task_id}/chat/history")
async def get_chat_history(
    task_id: int,
    limit: int = 500,
    db: AsyncSession = Depends(get_db),
):
    """Get chat-formatted history for a task (user messages + assistant responses)."""
    task = await db.get(Task, task_id)
    if not task:
        raise HTTPException(404, "Task not found")

    # Fetch the most recent N messages (desc) then reverse to chronological order
    stmt = (
        select(
            LogEntry.id,
            LogEntry.role,
            LogEntry.event_type,
            LogEntry.content,
            LogEntry.tool_name,
            LogEntry.tool_input,
            LogEntry.tool_output,
            LogEntry.is_error,
            LogEntry.loop_iteration,
            LogEntry.timestamp,
        )
        .where(
            LogEntry.task_id == task_id,
            LogEntry.event_type.in_(["user_message", "message", "result", "tool_use", "tool_result", "system_init", "system_event", "thinking", "process_exit"]),
        )
        .order_by(LogEntry.id.desc())
        .limit(limit)
    )
    result = await db.execute(stmt)
    rows = list(reversed(result.all()))

    _TRUNCATE = 20_000  # chars; tool outputs can be huge (file reads, bash output)

    messages = []
    for row in rows:
        # Skip heartbeat events
        if row.event_type == "system_event" and row.content == "task_progress":
            continue
        tool_input = row.tool_input
        tool_output = row.tool_output
        if tool_input and len(tool_input) > _TRUNCATE:
            tool_input = tool_input[:_TRUNCATE] + "\n…(truncated)"
        if tool_output and len(tool_output) > _TRUNCATE:
            tool_output = tool_output[:_TRUNCATE] + "\n…(truncated)"
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
        })

    return messages
