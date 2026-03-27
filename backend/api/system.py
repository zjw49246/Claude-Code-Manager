from fastapi import APIRouter, Depends
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import settings
from backend.database import get_db
from backend.models.task import Task
from backend.models.instance import Instance

router = APIRouter(prefix="/api/system", tags=["system"])


@router.get("/health")
async def health():
    return {"status": "ok"}


@router.get("/stats")
async def stats(db: AsyncSession = Depends(get_db)):
    task_counts = {}
    for status in ("pending", "in_progress", "executing", "completed", "failed"):
        result = await db.execute(
            select(func.count()).select_from(Task).where(Task.status == status)
        )
        task_counts[status] = result.scalar()

    result = await db.execute(
        select(func.count()).select_from(Instance).where(Instance.status == "running")
    )
    running_instances = result.scalar()

    return {
        "tasks": task_counts,
        "running_instances": running_instances,
    }


@router.get("/config")
async def get_config():
    return {
        "default_model": settings.default_model,
        "model_options": [m.strip() for m in settings.model_options.split(",") if m.strip()],
    }
