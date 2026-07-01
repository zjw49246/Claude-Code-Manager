"""Feishu OAuth endpoints — per-user Feishu binding for Team CCM."""

import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import RedirectResponse

from backend.config import settings
from backend.database import get_db
from backend.models.user import User
from backend.services import feishu_auth
from backend.api.deps import get_current_user_id

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/feishu", tags=["feishu"])


@router.get("/auth-url")
async def get_feishu_auth_url(request: Request):
    """Return Feishu OAuth URL with user_id in state param."""
    if not settings.feishu_app_id:
        raise HTTPException(400, "Feishu app not configured")
    user_id = get_current_user_id(request)
    redirect_uri = settings.public_base_url + "/api/feishu/callback"
    state = f"uid:{user_id}" if user_id else ""
    url = await feishu_auth.get_auth_url(redirect_uri, state=state)
    return {"url": url}


@router.get("/callback")
async def feishu_callback(code: str, state: str = "", db: AsyncSession = Depends(get_db)):
    """Handle Feishu OAuth callback — bind to the user identified by state."""
    try:
        token_data = await feishu_auth.exchange_code(code)
        access_token = token_data["access_token"]

        user_info = await feishu_auth.get_user_info(access_token)
        open_id = user_info["open_id"]
        name = user_info.get("name", "")
        avatar_url = user_info.get("avatar_url", "")

        # Parse user_id from state
        user_id = None
        if state.startswith("uid:"):
            try:
                user_id = int(state.split(":")[1])
            except (ValueError, IndexError):
                pass

        if user_id:
            # Per-user binding: write to User table
            user = await db.get(User, user_id)
            if user:
                user.feishu_open_id = open_id
                user.feishu_name = name
                if avatar_url:
                    user.avatar_url = avatar_url
                await db.commit()
                logger.info("Feishu bound for user %s: %s (%s)", user_id, name, open_id)
        else:
            # Legacy fallback: bind to first admin user
            result = await db.execute(
                select(User).where(User.role.in_(["admin", "super_admin"])).order_by(User.id).limit(1)
            )
            user = result.scalar_one_or_none()
            if user:
                user.feishu_open_id = open_id
                user.feishu_name = name
                if avatar_url:
                    user.avatar_url = avatar_url
                await db.commit()

    except Exception:
        logger.exception("Feishu callback failed")
        return RedirectResponse("/#/team?feishu_error=1")

    return RedirectResponse("/#/team?feishu_bound=1")


@router.get("/status")
async def get_feishu_status(request: Request, db: AsyncSession = Depends(get_db)):
    """Return current user's Feishu binding status."""
    user_id = get_current_user_id(request)
    if not user_id:
        return {"bound": False}

    user = await db.get(User, user_id)
    if not user or not user.feishu_open_id:
        return {"bound": False, "name": None, "open_id": None, "avatar_url": None}

    return {
        "bound": True,
        "name": user.feishu_name,
        "open_id": user.feishu_open_id,
        "avatar_url": user.avatar_url,
    }


@router.delete("/unbind")
async def unbind_feishu(request: Request, db: AsyncSession = Depends(get_db)):
    """Unbind current user's Feishu account."""
    user_id = get_current_user_id(request)
    if not user_id:
        raise HTTPException(401, "Not authenticated")

    user = await db.get(User, user_id)
    if not user or not user.feishu_open_id:
        raise HTTPException(404, "No Feishu binding found")

    user.feishu_open_id = ""
    user.feishu_name = ""
    await db.commit()
    return {"ok": True}
