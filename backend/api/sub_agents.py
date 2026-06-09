from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, func, case
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database import get_db
from backend.models.task import Task
from backend.models.monitor_session import MonitorSession

router = APIRouter(prefix="/api/tasks/{task_id}/sub-agents", tags=["sub-agents"])


@router.get("/summary")
async def get_sub_agent_summary(
    task_id: int,
    db: AsyncSession = Depends(get_db),
):
    task = await db.get(Task, task_id)
    if not task:
        raise HTTPException(404, "Task not found")

    row = (
        await db.execute(
            select(
                func.count()
                .filter(MonitorSession.status == "running")
                .label("running"),
                func.count()
                .filter(MonitorSession.status == "completed")
                .label("completed"),
            ).where(MonitorSession.task_id == task_id)
        )
    ).one()

    by_type: dict = {}
    if row.running or row.completed:
        by_type["monitor"] = {
            "running": row.running,
            "completed": row.completed,
        }

    return {"by_type": by_type}
