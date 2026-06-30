"""Team CCM sharing API — Admin shares Projects/Tasks to users."""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database import get_db
from backend.models.team_share import TeamProjectShare, TeamTaskShare
from backend.models.task import Task
from backend.api.deps import get_current_user_id, get_current_user_role

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/team", tags=["team-sharing"])


class ShareBody(BaseModel):
    target_type: str = "user"  # 'user' | 'group'
    target_id: int
    permission: str = "chat"  # for task shares: 'chat' only


class UnshareBody(BaseModel):
    target_type: str = "user"
    target_id: int


# --- Project sharing ---

@router.post("/projects/{project_id}/share")
async def share_project(project_id: int, body: ShareBody, request: Request, db: AsyncSession = Depends(get_db)):
    if get_current_user_role(request) != "admin":
        raise HTTPException(403, "Only admin can share projects")
    existing = await db.execute(
        select(TeamProjectShare).where(
            TeamProjectShare.project_id == project_id,
            TeamProjectShare.target_type == body.target_type,
            TeamProjectShare.target_id == body.target_id,
        )
    )
    if existing.scalar_one_or_none():
        return {"ok": True, "message": "Already shared"}
    db.add(TeamProjectShare(
        project_id=project_id,
        target_type=body.target_type,
        target_id=body.target_id,
        shared_by=get_current_user_id(request) or 0,
    ))
    await db.commit()
    return {"ok": True}


@router.delete("/projects/{project_id}/share")
async def unshare_project(project_id: int, body: UnshareBody, request: Request, db: AsyncSession = Depends(get_db)):
    if get_current_user_role(request) != "admin":
        raise HTTPException(403, "Only admin can manage sharing")
    await db.execute(
        delete(TeamProjectShare).where(
            TeamProjectShare.project_id == project_id,
            TeamProjectShare.target_type == body.target_type,
            TeamProjectShare.target_id == body.target_id,
        )
    )
    await db.commit()
    return {"ok": True}


@router.get("/projects/{project_id}/shares")
async def list_project_shares(project_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    if get_current_user_role(request) != "admin":
        raise HTTPException(403, "Only admin can view shares")
    result = await db.execute(
        select(TeamProjectShare).where(TeamProjectShare.project_id == project_id)
    )
    shares = result.scalars().all()
    return [{"id": s.id, "target_type": s.target_type, "target_id": s.target_id,
             "shared_by": s.shared_by, "created_at": s.created_at.isoformat()} for s in shares]


# --- Task sharing ---

@router.post("/tasks/{task_id}/share")
async def share_task(task_id: int, body: ShareBody, request: Request, db: AsyncSession = Depends(get_db)):
    if get_current_user_role(request) != "admin":
        raise HTTPException(403, "Only admin can share tasks")
    # Verify the user has Project access
    task = await db.get(Task, task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    if task.project_id:
        proj_share = await db.execute(
            select(TeamProjectShare).where(
                TeamProjectShare.project_id == task.project_id,
                TeamProjectShare.target_type == body.target_type,
                TeamProjectShare.target_id == body.target_id,
            )
        )
        if not proj_share.scalar_one_or_none():
            raise HTTPException(400, "User does not have Project access. Share the Project first.")
    existing = await db.execute(
        select(TeamTaskShare).where(
            TeamTaskShare.task_id == task_id,
            TeamTaskShare.target_type == body.target_type,
            TeamTaskShare.target_id == body.target_id,
        )
    )
    if existing.scalar_one_or_none():
        return {"ok": True, "message": "Already shared"}
    db.add(TeamTaskShare(
        task_id=task_id,
        target_type=body.target_type,
        target_id=body.target_id,
        permission=body.permission,
        shared_by=get_current_user_id(request) or 0,
    ))
    await db.commit()
    return {"ok": True}


@router.delete("/tasks/{task_id}/share")
async def unshare_task(task_id: int, body: UnshareBody, request: Request, db: AsyncSession = Depends(get_db)):
    if get_current_user_role(request) != "admin":
        raise HTTPException(403, "Only admin can manage sharing")
    await db.execute(
        delete(TeamTaskShare).where(
            TeamTaskShare.task_id == task_id,
            TeamTaskShare.target_type == body.target_type,
            TeamTaskShare.target_id == body.target_id,
        )
    )
    await db.commit()
    return {"ok": True}


@router.get("/tasks/{task_id}/shares")
async def list_task_shares(task_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    if get_current_user_role(request) != "admin":
        raise HTTPException(403, "Only admin can view shares")
    result = await db.execute(
        select(TeamTaskShare).where(TeamTaskShare.task_id == task_id)
    )
    shares = result.scalars().all()
    return [{"id": s.id, "target_type": s.target_type, "target_id": s.target_id,
             "permission": s.permission, "shared_by": s.shared_by,
             "created_at": s.created_at.isoformat()} for s in shares]


# --- Users list (for admin share UI) ---

@router.get("/users")
async def list_users(request: Request, db: AsyncSession = Depends(get_db)):
    if get_current_user_role(request) != "admin":
        raise HTTPException(403, "Only admin can list users")
    from backend.models.user import User
    result = await db.execute(select(User).where(User.is_active == True).order_by(User.id))
    users = result.scalars().all()
    return [{"id": u.id, "email": u.email, "name": u.name, "role": u.role,
             "avatar_url": u.avatar_url} for u in users]
