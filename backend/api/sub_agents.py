from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database import get_db
from backend.models.task import Task
from backend.models.sub_agent import SubAgentSession

router = APIRouter(prefix="/api/tasks/{task_id}/sub-agents", tags=["sub-agents"])


@router.get("/summary")
async def get_sub_agent_summary(
    task_id: int,
    db: AsyncSession = Depends(get_db),
):
    """按类别汇总该 task 的子 agent（monitor / native-agent / native-monitor / ...）。"""
    task = await db.get(Task, task_id)
    if not task:
        raise HTTPException(404, "Task not found")

    rows = (
        await db.execute(
            select(
                SubAgentSession.agent_type,
                SubAgentSession.status,
                func.count().label("n"),
            )
            .where(SubAgentSession.task_id == task_id)
            .group_by(SubAgentSession.agent_type, SubAgentSession.status)
        )
    ).all()

    by_type: dict = {}
    for agent_type, status, n in rows:
        # running/completed 恒存在（前端直接读这两个键），其余状态按实际值附加
        bucket = by_type.setdefault(agent_type, {"running": 0, "completed": 0})
        bucket[status] = bucket.get(status, 0) + n

    return {"by_type": by_type}
