import asyncio
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
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


@router.post("", response_model=MonitorSessionResponse)
async def create_monitor_session(
    task_id: int,
    body: MonitorSessionCreate,
    db: AsyncSession = Depends(get_db),
):
    task = await db.get(Task, task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    if task.mode != "auto":
        raise HTTPException(403, "Manual monitor sessions are only supported for auto mode tasks")
    if task.status not in ("in_progress", "executing"):
        raise HTTPException(400, "Cannot create monitor for a task that is not active")

    ms = MonitorSession(
        task_id=task_id,
        description=body.description,
        monitor_context=body.monitor_context,
        interval=body.interval,
        max_checks=body.max_checks,
        model=body.model,
        source="manual",
    )
    db.add(ms)
    await db.commit()
    await db.refresh(ms)

    from backend.main import dispatcher
    asyncio.create_task(dispatcher._run_monitor_session_background(ms, task_id))

    return ms


@router.delete("/{session_id}")
async def delete_monitor_session(
    task_id: int,
    session_id: int,
    db: AsyncSession = Depends(get_db),
):
    ms = await db.get(MonitorSession, session_id)
    if not ms or ms.task_id != task_id:
        raise HTTPException(404, "Monitor session not found")
    if ms.source != "manual":
        raise HTTPException(403, "Cannot delete system monitor sessions")

    ms.status = "cancelled"
    ms.completed_at = datetime.utcnow()
    await db.commit()

    from backend.main import dispatcher
    atask = dispatcher._monitor_tasks.get(session_id)
    if atask and not atask.done():
        atask.cancel()

    return {"status": "cancelled"}


@router.get("", response_model=list[MonitorSessionResponse])
async def list_monitor_sessions(
    task_id: int,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(MonitorSession).where(MonitorSession.task_id == task_id)
    )
    return list(result.scalars().all())


@router.get("/{session_id}/checks", response_model=list[MonitorCheckResponse])
async def get_monitor_checks(
    task_id: int,
    session_id: int,
    db: AsyncSession = Depends(get_db),
):
    ms = await db.get(MonitorSession, session_id)
    if not ms or ms.task_id != task_id:
        raise HTTPException(404, "Monitor session not found")

    result = await db.execute(
        select(MonitorCheck).where(MonitorCheck.monitor_session_id == session_id)
    )
    return list(result.scalars().all())
