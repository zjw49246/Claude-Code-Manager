"""Feishu OAuth helpers — authorization, token exchange, user info, org registry."""

import logging
import secrets

import httpx

from backend.config import settings

logger = logging.getLogger(__name__)

_FEISHU_BASE = "https://open.feishu.cn/open-apis"


async def get_auth_url(redirect_uri: str) -> str:
    """Build Feishu OAuth authorization URL."""
    state = secrets.token_urlsafe(16)
    return (
        f"{_FEISHU_BASE}/authen/v1/authorize"
        f"?app_id={settings.feishu_app_id}"
        f"&redirect_uri={redirect_uri}"
        f"&state={state}"
    )


async def _get_app_access_token() -> str:
    """Get an app_access_token (internal app type)."""
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            f"{_FEISHU_BASE}/auth/v3/app_access_token/internal",
            json={
                "app_id": settings.feishu_app_id,
                "app_secret": settings.feishu_app_secret,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"Feishu app_access_token error: {data}")
        return data["app_access_token"]


async def exchange_code(code: str) -> dict:
    """Exchange authorization code for user_access_token.

    Returns dict with access_token, expires_in, open_id, etc.
    """
    app_token = await _get_app_access_token()
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            f"{_FEISHU_BASE}/authen/v1/oidc/access_token",
            headers={"Authorization": f"Bearer {app_token}"},
            json={
                "grant_type": "authorization_code",
                "code": code,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"Feishu exchange_code error: {data}")
        return data["data"]


async def get_user_info(access_token: str) -> dict:
    """Get user info from Feishu using the user access token.

    Returns dict with open_id, name, avatar_url, email, etc.
    """
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(
            f"{_FEISHU_BASE}/authen/v1/user_info",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"Feishu get_user_info error: {data}")
        return data["data"]


async def register_with_org_registry(open_id: str, name: str, avatar_url: str) -> bool:
    """Register this CCM with the org registry after Feishu binding."""
    payload = {
        "open_id": open_id,
        "name": name,
        "ccm_url": settings.public_base_url,
        "avatar_url": avatar_url or "",
    }

    if settings.org_registry_enabled:
        # This CCM *is* the registry — write directly to local DB.
        from backend.database import async_session
        from backend.models.org import OrgMember
        from sqlalchemy import select
        from datetime import datetime

        try:
            async with async_session() as db:
                result = await db.execute(
                    select(OrgMember).where(OrgMember.feishu_open_id == open_id)
                )
                member = result.scalar_one_or_none()
                if member:
                    member.name = name
                    member.ccm_url = settings.public_base_url
                    member.avatar_url = avatar_url
                    member.last_seen_at = datetime.utcnow()
                else:
                    db.add(OrgMember(**payload))
                await db.commit()
            return True
        except Exception:
            logger.exception("Failed to register locally in org registry")
            return False

    if settings.org_registry_url:
        # Remote registry — POST to it.
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    f"{settings.org_registry_url}/api/org/register",
                    json=payload,
                )
                resp.raise_for_status()
            return True
        except Exception:
            logger.exception("Failed to register with remote org registry")
            return False

    # No registry configured — nothing to do.
    return True


async def unregister_from_org_registry(open_id: str) -> bool:
    """Unregister from org registry on unbind."""
    if settings.org_registry_enabled:
        from backend.database import async_session
        from backend.models.org import OrgMember
        from sqlalchemy import select

        try:
            async with async_session() as db:
                result = await db.execute(
                    select(OrgMember).where(OrgMember.feishu_open_id == open_id)
                )
                member = result.scalar_one_or_none()
                if member:
                    await db.delete(member)
                    await db.commit()
            return True
        except Exception:
            logger.exception("Failed to unregister locally from org registry")
            return False

    if settings.org_registry_url:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.delete(
                    f"{settings.org_registry_url}/api/org/members/{open_id}",
                )
                resp.raise_for_status()
            return True
        except Exception:
            logger.exception("Failed to unregister from remote org registry")
            return False

    return True
