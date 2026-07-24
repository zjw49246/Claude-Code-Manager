import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import settings
from backend.database import get_db
from backend.models.task import Task
from backend.models.instance import Instance

from backend.services.codex_models import CODEX_MODEL_EFFORTS
from backend.services.git_info import git_head_commit

router = APIRouter(prefix="/api/system", tags=["system"])

# import 时一次性求值（~10ms）：health 端点保持零阻塞；cwd 固定仓库根
_GIT_COMMIT: str = git_head_commit()


@router.get("/health")
async def health():
    return {"status": "ok", "commit": _GIT_COMMIT}


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
        # GPT-5.6 系列按模型区分档位（sol/terra 到 ultra，luna 到 max）；未列出的模型用 codex_effort_options
        "codex_model_efforts": CODEX_MODEL_EFFORTS,
    }


@router.get("/skills/usage")
async def skill_usage_report(db: AsyncSession = Depends(get_db)):
    """Get skill usage statistics."""
    from backend.services.skill_curator import get_usage_report
    return await get_usage_report(db)


@router.post("/skills/curator")
async def run_skill_curator(db: AsyncSession = Depends(get_db)):
    """Manually trigger curator lifecycle management."""
    from backend.services.skill_curator import run_curator
    return await run_curator(db)


@router.post("/skills/distill")
async def distill_skills(db: AsyncSession = Depends(get_db)):
    """Analyze conversation history and propose new skill candidates."""
    from backend.services.skill_distill import analyze_patterns
    return await analyze_patterns(db)


_BRANCH_RE = re.compile(r'^[a-zA-Z0-9._/\-]+$')


class UpdateRequest(BaseModel):
    skip_frontend_build: bool = False
    dry_run: bool = False
    force: bool = False
    branch: str | None = None


def _get_update_service():
    from backend.main import update_service
    if update_service is None:
        raise HTTPException(status_code=503, detail="UpdateService not initialized")
    return update_service


@router.post("/update")
async def start_update(req: UpdateRequest):
    if req.branch and not _BRANCH_RE.match(req.branch):
        raise HTTPException(status_code=400, detail="Invalid branch name")
    svc = _get_update_service()
    if req.dry_run:
        return await svc.dry_run(branch=req.branch, force=req.force)
    result = await svc.start_update(
        skip_frontend_build=req.skip_frontend_build,
        force=req.force,
        branch=req.branch,
    )
    if "error" in result:
        raise HTTPException(status_code=409, detail=result["error"])
    return result


@router.get("/update/status")
async def update_status():
    svc = _get_update_service()
    return await svc.get_status()


@router.post("/update/rollback")
async def rollback_update():
    svc = _get_update_service()
    result = await svc.rollback()
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@router.get("/skills")
async def list_skills():
    """List all available skills (from SKILL.md files)."""
    from backend.services.skill_loader import discover_skills
    skills = discover_skills()
    return [
        {
            "key": name,
            "label": skill.name,
            "description": skill.description,
            "always": skill.ccm.always,
            "priority": skill.ccm.priority,
            "version": skill.ccm.version,
            "tags": skill.ccm.tags,
            "commands": skill.ccm.commands,
            "scope": skill.scope,
            "heavy": skill.ccm.heavy,
        }
        for name, skill in sorted(skills.items(), key=lambda x: x[1].ccm.priority, reverse=True)
    ]
