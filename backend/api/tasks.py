import os
import shutil
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database import get_db
from backend.models.task import Task
from backend.models.instance import Instance
from backend.schemas.task import TaskCreate, TaskUpdate, TaskResponse
from backend.services.task_queue import TaskQueue

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


async def _clone_session(source_task_id: int, db: AsyncSession) -> dict | None:
    """Clone a Claude Code session file from a source task, returning new session_id and last_cwd."""
    source = await db.get(Task, source_task_id)
    if not source or not source.session_id or not source.last_cwd:
        return None

    config_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR", os.path.expanduser("~/.claude")))
    encoded_cwd = source.last_cwd.replace("/", "-")
    source_jsonl = config_dir / "projects" / encoded_cwd / f"{source.session_id}.jsonl"

    if not source_jsonl.exists():
        return None

    new_session_id = str(uuid.uuid4())
    dest_jsonl = source_jsonl.parent / f"{new_session_id}.jsonl"
    shutil.copy2(source_jsonl, dest_jsonl)

    return {"session_id": new_session_id, "last_cwd": source.last_cwd}


def _get_queue(db: AsyncSession = Depends(get_db)) -> TaskQueue:
    return TaskQueue(db)


@router.get("/count")
async def count_tasks(
    status: str | None = None,
    include_archived: bool = False,
    archived_only: bool = False,
    project_id: int | None = None,
    starred: bool | None = None,
    has_unread: bool | None = None,
    queue: TaskQueue = Depends(_get_queue),
):
    total = await queue.count_tasks(
        status=status, include_archived=include_archived,
        archived_only=archived_only,
        project_id=project_id, starred=starred,
        has_unread=has_unread,
    )
    return {"total": total}


@router.get("", response_model=list[TaskResponse])
async def list_tasks(
    status: str | None = None,
    include_archived: bool = False,
    archived_only: bool = False,
    project_id: int | None = None,
    starred: bool | None = None,
    has_unread: bool | None = None,
    limit: int = 50,
    offset: int = 0,
    queue: TaskQueue = Depends(_get_queue),
):
    return await queue.list_tasks(
        status=status, include_archived=include_archived,
        archived_only=archived_only,
        project_id=project_id, starred=starred,
        has_unread=has_unread,
        limit=limit, offset=offset,
    )


@router.post("", response_model=TaskResponse, status_code=201)
async def create_task(body: TaskCreate, queue: TaskQueue = Depends(_get_queue), db: AsyncSession = Depends(get_db)):
    data = body.model_dump()
    image_paths = data.pop("image_paths", None)
    file_paths = data.pop("file_paths", None)
    attachments = data.pop("attachments", None)
    secret_ids = data.pop("secret_ids", None)
    clone_from_task_id = data.pop("clone_from_task_id", None)
    meta = data.get("metadata_") or {}
    all_paths = file_paths or image_paths
    if all_paths:
        meta["image_paths"] = all_paths
    if attachments:
        meta["attachments"] = attachments
    if secret_ids:
        meta["secret_ids"] = secret_ids
    if meta:
        data["metadata_"] = meta

    if clone_from_task_id:
        cloned = await _clone_session(clone_from_task_id, db)
        if cloned:
            data["session_id"] = cloned["session_id"]
            data["last_cwd"] = cloned["last_cwd"]

    # 设置归 Task：创建时填入全局默认值，后续不再依赖 instance fallback
    from backend.config import settings as app_settings
    if not data.get("model"):
        data["model"] = (
            app_settings.default_codex_model
            if data.get("provider") == "codex"
            else app_settings.default_model
        )
    if not data.get("effort_level"):
        data["effort_level"] = app_settings.default_effort

    return await queue.create(**data)


@router.get("/{task_id}", response_model=TaskResponse)
async def get_task(task_id: int, queue: TaskQueue = Depends(_get_queue)):
    task = await queue.get(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    return task


@router.put("/{task_id}", response_model=TaskResponse)
async def update_task(
    task_id: int, body: TaskUpdate, queue: TaskQueue = Depends(_get_queue)
):
    task = await queue.update_task(task_id, **body.model_dump(exclude_unset=True))
    if not task:
        raise HTTPException(404, "Task not found")
    return task


async def _stop_task_process(task_id: int, db: AsyncSession) -> bool:
    """Stop the running Claude Code process for a task, if any. Returns True if stopped."""
    from backend.main import instance_manager
    for inst_id, proc in list(instance_manager.processes.items()):
        if proc.returncode is not None:
            continue
        inst = await db.get(Instance, inst_id)
        if inst and inst.current_task_id == task_id:
            await instance_manager.stop(inst_id)
            return True
    task = await db.get(Task, task_id)
    if task and task.instance_id:
        return await instance_manager.stop(task.instance_id)
    return False


@router.delete("/{task_id}")
async def delete_task(task_id: int, queue: TaskQueue = Depends(_get_queue)):
    ok = await queue.delete(task_id)
    if not ok:
        raise HTTPException(400, "Cannot delete task (not found or not in deletable state)")
    return {"ok": True}


@router.post("/{task_id}/stop-session")
async def stop_task_session(task_id: int, db: AsyncSession = Depends(get_db)):
    """Stop the running Claude Code session for a task.

    Clears pending queued chat messages first so the queue consumer does not
    immediately relaunch after the current process is stopped.
    """
    from backend.main import dispatcher
    cleared = dispatcher.clear_task_queue(task_id)
    stopped = await _stop_task_process(task_id, db)
    if not stopped:
        task = await db.get(Task, task_id)
        if task and task.status in ("executing", "in_progress"):
            task.status = "completed"
            await db.commit()
            return {
                "ok": True,
                "stopped": False,
                "cleared_messages": cleared,
                "note": "No running process found, task marked as completed",
            }
        if cleared:
            return {"ok": True, "stopped": False, "cleared_messages": cleared}
        raise HTTPException(400, "No running session found for this task")
    return {"ok": True, "stopped": True, "cleared_messages": cleared}


@router.post("/{task_id}/cancel", response_model=TaskResponse)
async def cancel_task(task_id: int, queue: TaskQueue = Depends(_get_queue), db: AsyncSession = Depends(get_db)):
    task = await queue.cancel(task_id)
    if not task:
        raise HTTPException(400, "Cannot cancel task")
    await _stop_task_process(task_id, db)

    from backend.main import dispatcher
    from backend.models.monitor_session import MonitorSession
    from sqlalchemy import select as sa_select
    result = await db.execute(
        sa_select(MonitorSession.id)
        .where(MonitorSession.task_id == task_id, MonitorSession.status.in_(["running"]))
    )
    for (ms_id,) in result.all():
        proc = dispatcher._monitor_processes.get(ms_id)
        if proc and proc.returncode is None:
            proc.kill()
            await proc.wait()
        atask = dispatcher._monitor_tasks.get(ms_id)
        if atask and not atask.done():
            atask.cancel()

    return task


@router.post("/{task_id}/retry", response_model=TaskResponse)
async def retry_task(task_id: int, queue: TaskQueue = Depends(_get_queue)):
    task = await queue.retry(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    return task


@router.post("/{task_id}/star", response_model=TaskResponse)
async def star_task(task_id: int, queue: TaskQueue = Depends(_get_queue)):
    task = await queue.star(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    return task


@router.post("/{task_id}/read", response_model=TaskResponse)
async def mark_task_read(task_id: int, queue: TaskQueue = Depends(_get_queue)):
    task = await queue.update_task(task_id, has_unread=False)
    if not task:
        raise HTTPException(404, "Task not found")
    return task


@router.post("/{task_id}/unread", response_model=TaskResponse)
async def mark_task_unread(task_id: int, queue: TaskQueue = Depends(_get_queue)):
    task = await queue.update_task(task_id, has_unread=True)
    if not task:
        raise HTTPException(404, "Task not found")
    return task


@router.post("/{task_id}/archive", response_model=TaskResponse)
async def archive_task(task_id: int, queue: TaskQueue = Depends(_get_queue)):
    task = await queue.archive(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    return task


@router.get("/queue/next", response_model=list[TaskResponse])
async def get_queue(queue: TaskQueue = Depends(_get_queue)):
    return await queue.list_tasks(status="pending")


@router.post("/{task_id}/plan/approve", response_model=TaskResponse)
async def approve_plan(task_id: int, queue: TaskQueue = Depends(_get_queue)):
    """Approve a plan-mode task's plan and queue it for execution."""
    task = await queue.get(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    if task.mode != "plan" or task.status != "plan_review":
        raise HTTPException(400, "Task is not in plan review state")
    task = await queue.update_task(task_id, plan_approved=True, status="pending")
    return task


@router.post("/{task_id}/plan/reject", response_model=TaskResponse)
async def reject_plan(task_id: int, queue: TaskQueue = Depends(_get_queue)):
    """Reject a plan-mode task's plan."""
    task = await queue.get(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    if task.mode != "plan" or task.status != "plan_review":
        raise HTTPException(400, "Task is not in plan review state")
    task = await queue.update_task(task_id, plan_approved=False, status="cancelled")
    return task
