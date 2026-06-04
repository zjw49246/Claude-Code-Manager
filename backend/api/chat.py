import json
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
    image_paths: list[str] | None = None  # kept for backwards compatibility
    file_paths: list[str] | None = None
    secret_ids: list[int] | None = None


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

    # Store user message as a log entry
    user_log = LogEntry(
        instance_id=inst.id,
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

    # Determine cwd: Claude Code launches in repo root, session binds there
    cwd = task.last_cwd or task.target_repo
    if not cwd or not os.path.isdir(cwd):
        raise HTTPException(400, "Task working directory not found.")

    # Build git env (SSH key, HTTPS token, author info) same as dispatcher
    from backend.services.dispatcher import _build_git_env
    from backend.services.git_config import merge_git_config, settings_to_dict
    from backend.models.project import Project
    from backend.models.global_settings import GlobalSettings
    merged: dict = {}
    if task.project_id:
        project = await db.get(Project, task.project_id)
        global_cfg = await db.get(GlobalSettings, 1)
        if project:
            merged = merge_git_config(settings_to_dict(project), settings_to_dict(global_cfg))
    git_env = _build_git_env(merged)

    # Resolve effort: task.effort_level → instance.effort_level → settings.default_effort
    from backend.config import settings as app_settings
    effort_level = task.effort_level or inst.effort_level or app_settings.default_effort

    # Launch with --resume, using the task's cwd
    # Use task.model (the model that created the session) to maintain consistency,
    # falling back to instance model only if task has no model set.
    pid = await instance_manager.launch(
        instance_id=inst.id,
        prompt=prompt,
        task_id=task_id,
        cwd=cwd,
        model=task.model or inst.model,
        resume_session_id=task.session_id,
        git_env=git_env,
        thinking_budget=inst.thinking_budget,
        effort_level=effort_level,
        chat_initiated=True,
    )

    task.status = "executing"
    await db.commit()
    await broadcaster.broadcast("tasks", {
        "event": "status_change",
        "task_id": task_id,
        "new_status": "executing",
        "instance_id": inst.id,
    })

    return {"ok": True, "pid": pid, "instance_id": inst.id, "session_id": task.session_id}


@router.get("/{task_id}/chat/history")
async def get_chat_history(
    task_id: int,
    limit: int = 0,
    db: AsyncSession = Depends(get_db),
):
    """Get chat-formatted history for a task (user messages + assistant responses)."""
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
    for row in rows:
        # Skip noisy system events (heartbeats, telemetry subtypes)
        if row.event_type == "system_event" and row.content in ("task_progress", "thinking_tokens", "token_usage", "api_request", "api_response"):
            continue
        tool_input = row.tool_input
        tool_output = row.tool_output
        if tool_input and len(tool_input) > _TRUNCATE:
            tool_input = tool_input[:_TRUNCATE] + "\n…(truncated)"
        if tool_output and len(tool_output) > _TRUNCATE:
            tool_output = tool_output[:_TRUNCATE] + "\n…(truncated)"
        attachments = None
        image_urls = None
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
            except (json.JSONDecodeError, TypeError):
                pass
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
        })

    return messages
