from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database import get_db
from backend.models.task import Task
from backend.models.instance import Instance
from backend.schemas.task import TaskCreate, TaskUpdate, TaskResponse
from backend.services.task_queue import TaskQueue

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


def _get_queue(db: AsyncSession = Depends(get_db)) -> TaskQueue:
    return TaskQueue(db)


@router.get("/count")
async def count_tasks(
    status: str | None = None,
    include_archived: bool = False,
    project_id: int | None = None,
    starred: bool | None = None,
    queue: TaskQueue = Depends(_get_queue),
):
    total = await queue.count_tasks(
        status=status, include_archived=include_archived,
        project_id=project_id, starred=starred,
    )
    return {"total": total}


@router.get("", response_model=list[TaskResponse])
async def list_tasks(
    status: str | None = None,
    include_archived: bool = False,
    project_id: int | None = None,
    starred: bool | None = None,
    limit: int = 50,
    offset: int = 0,
    queue: TaskQueue = Depends(_get_queue),
):
    return await queue.list_tasks(
        status=status, include_archived=include_archived,
        project_id=project_id, starred=starred,
        limit=limit, offset=offset,
    )


@router.post("", response_model=TaskResponse, status_code=201)
async def create_task(body: TaskCreate, queue: TaskQueue = Depends(_get_queue)):
    data = body.model_dump()
    image_paths = data.pop("image_paths", None)
    secret_ids = data.pop("secret_ids", None)
    meta = data.get("metadata_") or {}
    if image_paths:
        meta["image_paths"] = image_paths
    if secret_ids:
        meta["secret_ids"] = secret_ids
    if meta:
        data["metadata_"] = meta
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


@router.delete("/{task_id}")
async def delete_task(task_id: int, queue: TaskQueue = Depends(_get_queue)):
    ok = await queue.delete(task_id)
    if not ok:
        raise HTTPException(400, "Cannot delete task (not found or not in deletable state)")
    return {"ok": True}


@router.post("/{task_id}/stop-session")
async def stop_task_session(task_id: int, db: AsyncSession = Depends(get_db)):
    """Stop the running Claude Code session for a task."""
    from backend.main import instance_manager
    task = await db.get(Task, task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    # Find the instance running this task by checking instance_manager processes
    stopped = False
    for inst_id, proc in list(instance_manager.processes.items()):
        if proc.returncode is not None:
            continue
        inst = await db.get(Instance, inst_id)
        if inst and inst.current_task_id == task_id:
            await instance_manager.stop(inst_id)
            stopped = True
            break
    # Fallback: try task.instance_id
    if not stopped and task.instance_id:
        stopped = await instance_manager.stop(task.instance_id)
    if not stopped:
        raise HTTPException(400, "No running session found for this task")
    return {"ok": True}


@router.post("/{task_id}/cancel", response_model=TaskResponse)
async def cancel_task(task_id: int, queue: TaskQueue = Depends(_get_queue)):
    task = await queue.cancel(task_id)
    if not task:
        raise HTTPException(400, "Cannot cancel task")
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
