from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database import get_db
from backend.models.instance import Instance
from backend.models.log_entry import LogEntry
from backend.schemas.instance import InstanceCreate, InstanceResponse
from backend.schemas.log_entry import LogEntryResponse

router = APIRouter(prefix="/api/instances", tags=["instances"])
dispatcher_router = APIRouter(prefix="/api/dispatcher", tags=["dispatcher"])


@router.get("", response_model=list[InstanceResponse])
async def list_instances(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Instance).order_by(Instance.id))
    return list(result.scalars().all())


@router.post("", response_model=InstanceResponse, status_code=201)
async def create_instance(body: InstanceCreate, db: AsyncSession = Depends(get_db)):
    instance = Instance(
        name=body.name,
        config=body.config,
    )
    db.add(instance)
    await db.commit()
    await db.refresh(instance)
    return instance


@router.delete("/cleanup")
async def cleanup_instances(db: AsyncSession = Depends(get_db)):
    from backend.main import instance_manager
    result = await db.execute(
        select(Instance).where(Instance.status.in_(["error", "stopped"]))
    )
    targets = list(result.scalars().all())
    for inst in targets:
        if instance_manager.is_running(inst.id):
            await instance_manager.stop(inst.id)
        await db.delete(inst)
    await db.commit()
    return {"ok": True, "deleted": len(targets)}


@router.get("/{instance_id}", response_model=InstanceResponse)
async def get_instance(instance_id: int, db: AsyncSession = Depends(get_db)):
    instance = await db.get(Instance, instance_id)
    if not instance:
        raise HTTPException(404, "Instance not found")
    return instance


@router.delete("/{instance_id}")
async def delete_instance(instance_id: int, db: AsyncSession = Depends(get_db)):
    from backend.main import instance_manager
    instance = await db.get(Instance, instance_id)
    if not instance:
        raise HTTPException(404, "Instance not found")
    if instance_manager.is_running(instance_id):
        await instance_manager.stop(instance_id)
    await db.delete(instance)
    await db.commit()
    return {"ok": True}



@router.post("/{instance_id}/stop")
async def stop_instance(instance_id: int):
    from backend.main import instance_manager
    ok = await instance_manager.stop(instance_id)
    if not ok:
        raise HTTPException(400, "Instance is not running")
    return {"ok": True}


@router.post("/{instance_id}/run")
async def run_task_on_instance(
    instance_id: int,
    task_id: int | None = None,
    prompt: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    """Manually run a prompt or task on a specific instance."""
    from backend.main import instance_manager
    from backend.services.task_queue import TaskQueue

    instance = await db.get(Instance, instance_id)
    if not instance:
        raise HTTPException(404, "Instance not found")
    if instance_manager.is_running(instance_id):
        raise HTTPException(400, "Instance is already running")

    if task_id:
        queue = TaskQueue(db)
        task = await queue.get(task_id)
        if not task:
            raise HTTPException(404, "Task not found")
        actual_prompt = task.description
        cwd = task.target_repo
    elif prompt:
        actual_prompt = prompt
        cwd = None
    else:
        raise HTTPException(400, "Must provide task_id or prompt")

    from backend.config import settings as app_settings
    task_model = task.model if task_id and task else None
    task_provider = task.provider if task_id and task else "claude"
    task_effort = (task.effort_level if task_id and task else None) or app_settings.default_effort
    task_thinking = task.thinking_budget if task_id and task else None

    pid = await instance_manager.launch(
        instance_id=instance_id,
        prompt=actual_prompt,
        task_id=task_id,
        cwd=cwd,
        model=task_model,
        provider=task_provider,
        thinking_budget=task_thinking,
        effort_level=task_effort,
    )
    return {"ok": True, "pid": pid}


@router.get("/{instance_id}/logs", response_model=list[LogEntryResponse])
async def get_logs(
    instance_id: int,
    limit: int = 100,
    offset: int = 0,
    event_type: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    stmt = (
        select(LogEntry)
        .where(LogEntry.instance_id == instance_id)
        .order_by(LogEntry.id.desc())
    )
    if event_type:
        stmt = stmt.where(LogEntry.event_type == event_type)
    stmt = stmt.limit(limit).offset(offset)
    result = await db.execute(stmt)
    return list(result.scalars().all())


@router.post("/{instance_id}/ralph/start")
async def start_ralph_loop(instance_id: int, db: AsyncSession = Depends(get_db)):
    """Start the Ralph Loop for an instance (auto-fetch and run tasks)."""
    from backend.main import ralph_loop
    instance = await db.get(Instance, instance_id)
    if not instance:
        raise HTTPException(404, "Instance not found")
    await ralph_loop.start(instance_id)
    return {"ok": True, "message": f"Ralph loop started for instance {instance_id}"}


@router.post("/{instance_id}/ralph/stop")
async def stop_ralph_loop(instance_id: int):
    """Stop the Ralph Loop for an instance."""
    from backend.main import ralph_loop
    await ralph_loop.stop(instance_id)
    return {"ok": True}


@router.get("/{instance_id}/ralph/status")
async def ralph_loop_status(instance_id: int):
    from backend.main import ralph_loop
    return {"running": ralph_loop.is_running(instance_id)}


# ── Dispatcher endpoints ──

@dispatcher_router.get("/status")
async def dispatcher_status():
    from backend.main import dispatcher
    return dispatcher.status()


@dispatcher_router.post("/start")
async def start_dispatcher():
    from backend.main import dispatcher
    await dispatcher.start()
    return {"ok": True, "message": "Dispatcher started"}


@dispatcher_router.post("/stop")
async def stop_dispatcher():
    from backend.main import dispatcher
    await dispatcher.stop()
    return {"ok": True, "message": "Dispatcher stopped"}
