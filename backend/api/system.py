from fastapi import APIRouter, Depends
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import settings
from backend.database import get_db
from backend.models.task import Task
from backend.models.instance import Instance

router = APIRouter(prefix="/api/system", tags=["system"])

_GIT_COMMIT: str | None = None


def _git_commit() -> str:
    """本服务运行代码的 commit（缓存）。Manager/Worker 版本锁定校验用。"""
    global _GIT_COMMIT
    if _GIT_COMMIT is None:
        import subprocess
        try:
            r = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                capture_output=True, text=True, timeout=10,
            )
            _GIT_COMMIT = r.stdout.strip() if r.returncode == 0 else ""
        except Exception:
            _GIT_COMMIT = ""
    return _GIT_COMMIT


@router.get("/health")
async def health():
    return {"status": "ok", "commit": _git_commit()}


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
        "default_provider": settings.default_provider,
        "provider_options": [p.strip() for p in settings.provider_options.split(",") if p.strip()],
        "default_codex_model": settings.default_codex_model,
        "codex_model_options": [m.strip() for m in settings.codex_model_options.split(",") if m.strip()],
        "default_effort": settings.default_effort,
        "effort_options": [e.strip() for e in settings.effort_options.split(",") if e.strip()],
        "codex_effort_options": [e.strip() for e in settings.codex_effort_options.split(",") if e.strip()],
    }
