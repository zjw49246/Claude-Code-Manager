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
                "share_token": t.share_token,
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


# ---------- Proxy endpoints: forward requests to the sharer's CCM ----------

async def _get_shared_record(shared_id: int, db: AsyncSession) -> SharedTaskReceived:
    record = await db.get(SharedTaskReceived, shared_id)
    if not record or record.status != "active":
        raise HTTPException(404, "Shared task not found")
    return record


@router.get("/{shared_id}/history")
async def proxy_history(
    shared_id: int,
    limit: int = 0,
    before_id: int = 0,
    compact: bool = True,
    db: AsyncSession = Depends(get_db),
):
    """Proxy chat history from the sharer's CCM."""
    record = await _get_shared_record(shared_id, db)
    from backend.services.shared_proxy import proxy_history as _proxy
    try:
        return await _proxy(
            record.owner_ccm_url, record.remote_task_id, record.share_token,
            limit=limit, before_id=before_id, compact=compact,
        )
    except Exception as e:
        logger.warning("proxy_history failed for shared %d: %s", shared_id, e)
        raise HTTPException(502, f"Cannot reach sharer CCM: {e}")


class SharedChatBody(BaseModel):
    message: str


@router.post("/{shared_id}/chat")
async def proxy_chat(
    shared_id: int,
    body: SharedChatBody,
    db: AsyncSession = Depends(get_db),
):
    """Proxy a chat message to the sharer's CCM."""
    record = await _get_shared_record(shared_id, db)

    # Get my feishu name for the [sender] prefix
    from backend.models.feishu_binding import FeishuUserBinding
    binding_result = await db.execute(select(FeishuUserBinding).limit(1))
    binding = binding_result.scalar_one_or_none()
    sender_name = binding.feishu_name if binding else None

    from backend.services.shared_proxy import proxy_chat as _proxy
    try:
        return await _proxy(
            record.owner_ccm_url, record.remote_task_id, record.share_token,
            message=body.message, sender_name=sender_name,
        )
    except Exception as e:
        logger.warning("proxy_chat failed for shared %d: %s", shared_id, e)
        raise HTTPException(502, f"Cannot reach sharer CCM: {e}")


@router.get("/{shared_id}/config")
async def proxy_config(
    shared_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Proxy task config from the sharer's CCM."""
    record = await _get_shared_record(shared_id, db)
    from backend.services.shared_proxy import proxy_config as _proxy
    try:
        return await _proxy(
            record.owner_ccm_url, record.remote_task_id, record.share_token,
        )
    except Exception as e:
        logger.warning("proxy_config failed for shared %d: %s", shared_id, e)
        raise HTTPException(502, f"Cannot reach sharer CCM: {e}")


@router.get("/{shared_id}/ping")
async def ping_sharer(shared_id: int, db: AsyncSession = Depends(get_db)):
    """Check if the sharer's CCM is online."""
    record = await _get_shared_record(shared_id, db)
    import httpx
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"{record.owner_ccm_url}/api/system/health")
            resp.raise_for_status()
        return {"online": True}
    except Exception:
        return {"online": False}
