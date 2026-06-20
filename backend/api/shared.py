"""Shared tasks API — receiver-side endpoints for tasks shared TO this CCM."""

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database import get_db
from backend.models.task_share import SharedTaskReceived

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/shared", tags=["shared"])


class ReceiveSharePayload(BaseModel):
    owner_ccm_url: str
    owner_name: str | None = None
    owner_feishu_open_id: str | None = None
    remote_task_id: int
    share_token: str
    task_title: str | None = None
    task_description: str | None = None
    project_name: str | None = None


class RevokeSharePayload(BaseModel):
    owner_ccm_url: str
    remote_task_id: int


@router.post("/receive")
async def receive_share(payload: ReceiveSharePayload, db: AsyncSession = Depends(get_db)):
    """Called by the sharer's CCM to push a share notification."""
    # Upsert
    result = await db.execute(
        select(SharedTaskReceived).where(
            SharedTaskReceived.owner_ccm_url == payload.owner_ccm_url,
            SharedTaskReceived.remote_task_id == payload.remote_task_id,
        )
    )
    existing = result.scalar_one_or_none()
    if existing:
        existing.share_token = payload.share_token
        existing.task_title = payload.task_title
        existing.task_description = payload.task_description
        existing.project_name = payload.project_name
        existing.owner_name = payload.owner_name
        existing.owner_feishu_open_id = payload.owner_feishu_open_id
        existing.status = "active"
    else:
        db.add(SharedTaskReceived(**payload.model_dump()))
    await db.commit()
    return {"ok": True}


@router.post("/revoke")
async def receive_revoke(payload: RevokeSharePayload, db: AsyncSession = Depends(get_db)):
    """Called by the sharer's CCM to revoke a share."""
    result = await db.execute(
        select(SharedTaskReceived).where(
            SharedTaskReceived.owner_ccm_url == payload.owner_ccm_url,
            SharedTaskReceived.remote_task_id == payload.remote_task_id,
        )
    )
    record = result.scalar_one_or_none()
    if record:
        await db.delete(record)
        await db.commit()
    return {"ok": True}


@router.get("/tasks")
async def list_shared_tasks(db: AsyncSession = Depends(get_db)):
    """List all tasks shared to this CCM."""
    result = await db.execute(
        select(SharedTaskReceived).where(
            SharedTaskReceived.status == "active"
        ).order_by(SharedTaskReceived.received_at.desc())
    )
    tasks = result.scalars().all()
    return {
        "tasks": [
            {
                "id": t.id,
                "owner_ccm_url": t.owner_ccm_url,
                "owner_name": t.owner_name,
                "remote_task_id": t.remote_task_id,
                "task_title": t.task_title,
                "task_description": t.task_description,
                "project_name": t.project_name,
                "received_at": t.received_at.isoformat() if t.received_at else None,
            }
            for t in tasks
        ]
    }


@router.delete("/{shared_id}")
async def leave_shared_task(shared_id: int, db: AsyncSession = Depends(get_db)):
    """Voluntarily leave a shared task."""
    record = await db.get(SharedTaskReceived, shared_id)
    if not record:
        raise HTTPException(404, "Shared task not found")
    await db.delete(record)
    await db.commit()
    return {"ok": True}
