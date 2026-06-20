"""Feishu OAuth endpoints — bind/unbind Feishu identity to this CCM."""

import logging
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import RedirectResponse

from backend.config import settings
from backend.database import get_db
from backend.models.feishu_binding import FeishuUserBinding
from backend.services import feishu_auth

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/feishu", tags=["feishu"])


@router.get("/auth-url")
async def get_feishu_auth_url():
    """Return the Feishu OAuth authorization URL."""
    if not settings.feishu_app_id:
        raise HTTPException(400, "Feishu app not configured")
    redirect_uri = settings.public_base_url + "/api/feishu/callback"
    url = await feishu_auth.get_auth_url(redirect_uri)
    return {"url": url}


@router.get("/callback")
async def feishu_callback(code: str, state: str = "", db: AsyncSession = Depends(get_db)):
    """Handle Feishu OAuth callback — exchange code, bind user, register with org."""
    try:
        token_data = await feishu_auth.exchange_code(code)
        access_token = token_data["access_token"]
        expires_in = token_data.get("expires_in", 7200)

        user_info = await feishu_auth.get_user_info(access_token)
        open_id = user_info["open_id"]
        name = user_info.get("name", "")
        avatar_url = user_info.get("avatar_url", "")

        # Upsert FeishuUserBinding
        result = await db.execute(
            select(FeishuUserBinding).where(FeishuUserBinding.feishu_open_id == open_id)
        )
        binding = result.scalar_one_or_none()
        if binding:
            binding.feishu_name = name
            binding.avatar_url = avatar_url
            binding.access_token = access_token
            binding.token_expires_at = datetime.utcnow() + timedelta(seconds=expires_in)
        else:
            binding = FeishuUserBinding(
                feishu_open_id=open_id,
                feishu_name=name,
                avatar_url=avatar_url,
                access_token=access_token,
                token_expires_at=datetime.utcnow() + timedelta(seconds=expires_in),
            )
            db.add(binding)
        await db.commit()

        # Register with org registry (best-effort)
        await feishu_auth.register_with_org_registry(open_id, name, avatar_url)

    except Exception:
        logger.exception("Feishu callback failed")
        return RedirectResponse("/#/team?feishu_error=1")

    return RedirectResponse("/#/team?feishu_bound=1")


@router.get("/status")
async def get_feishu_status(db: AsyncSession = Depends(get_db)):
    """Return current Feishu binding status."""
    result = await db.execute(select(FeishuUserBinding).limit(1))
    binding = result.scalar_one_or_none()
    if not binding:
        return {"bound": False, "name": None, "open_id": None, "avatar_url": None, "is_registry": settings.org_registry_enabled}
    return {
        "bound": True,
        "name": binding.feishu_name,
        "open_id": binding.feishu_open_id,
        "avatar_url": binding.avatar_url,
        "is_registry": settings.org_registry_enabled,
    }


@router.delete("/unbind")
async def unbind_feishu(db: AsyncSession = Depends(get_db)):
    """Unbind Feishu account."""
    result = await db.execute(select(FeishuUserBinding).limit(1))
    binding = result.scalar_one_or_none()
    if not binding:
        raise HTTPException(404, "No Feishu binding found")

    # Unregister from org registry (best-effort)
    await feishu_auth.unregister_from_org_registry(binding.feishu_open_id)

    await db.delete(binding)
    await db.commit()
    return {"ok": True}
