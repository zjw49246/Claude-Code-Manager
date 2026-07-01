"""Shared FastAPI dependencies for user context."""

from fastapi import HTTPException, Request


def get_current_user_id(request: Request) -> int | None:
    return getattr(request.state, "user_id", None)


def get_current_user_role(request: Request) -> str:
    return getattr(request.state, "user_role", "member")


def is_admin(request: Request) -> bool:
    """Both admin and super_admin have admin-level permissions."""
    return get_current_user_role(request) in ("admin", "super_admin")


def is_super_admin(request: Request) -> bool:
    """Only super_admin can promote users to admin."""
    return get_current_user_role(request) == "super_admin"


def require_admin(request: Request):
    """Raise 403 if not admin/super_admin."""
    if not is_admin(request):
        raise HTTPException(403, "Admin only")


async def require_task_access(request: Request, task, db):
    """Raise 403 if user has no access to this task."""
    if is_admin(request):
        return
    user_id = get_current_user_id(request)
    if not user_id:
        raise HTTPException(403, "Not authenticated")
    if task.created_by == user_id:
        return
    if task.worker_id:
        from sqlalchemy import select
        from backend.models.worker import Worker
        w = await db.get(Worker, task.worker_id)
        if w and w.owner_user_id == user_id:
            return
    from sqlalchemy import select
    from backend.models.team_share import TeamTaskShare, TeamProjectShare
    shared = (await db.execute(
        select(TeamTaskShare.id).where(
            TeamTaskShare.task_id == task.id,
            TeamTaskShare.target_type == "user",
            TeamTaskShare.target_id == user_id,
        ).limit(1)
    )).scalar_one_or_none()
    if shared:
        return
    if task.project_id:
        proj_shared = (await db.execute(
            select(TeamProjectShare.id).where(
                TeamProjectShare.project_id == task.project_id,
                TeamProjectShare.target_type == "user",
                TeamProjectShare.target_id == user_id,
            ).limit(1)
        )).scalar_one_or_none()
        if proj_shared:
            return
    raise HTTPException(403, "No access to this task")


async def require_worker_access(request: Request, worker):
    """Raise 403 if user has no access to this worker."""
    if is_admin(request):
        return
    user_id = get_current_user_id(request)
    if worker.owner_user_id == user_id:
        return
    raise HTTPException(403, "No access to this worker")
