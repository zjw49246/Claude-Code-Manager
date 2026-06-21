"""Feishu DM notifications — send share/revoke messages to users."""

import json
import logging

import httpx

from backend.config import settings

logger = logging.getLogger(__name__)

_FEISHU_BASE = "https://open.feishu.cn/open-apis"


async def _get_app_access_token() -> str:
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


async def _send_dm(recipient_open_id: str, text: str) -> bool:
    """Send a text DM to a Feishu user."""
    if not settings.feishu_app_id or not settings.feishu_app_secret:
        return False
    try:
        token = await _get_app_access_token()
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{_FEISHU_BASE}/im/v1/messages",
                params={"receive_id_type": "open_id"},
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "receive_id": recipient_open_id,
                    "msg_type": "text",
                    "content": json.dumps({"text": text}),
                },
            )
            data = resp.json()
            if data.get("code") != 0:
                logger.warning("Feishu send_message error: %s", data)
                return False
        return True
    except Exception:
        logger.exception("Failed to send Feishu DM to %s", recipient_open_id)
        return False


async def send_share_notification(
    recipient_open_id: str,
    sharer_name: str,
    task_title: str,
    ccm_url: str,
) -> bool:
    return await _send_dm(
        recipient_open_id,
        f"{sharer_name} shared a task with you: \"{task_title}\"\nView at: {ccm_url}/#/team",
    )


async def send_revoke_notification(
    recipient_open_id: str,
    sharer_name: str,
    task_title: str,
) -> bool:
    return await _send_dm(
        recipient_open_id,
        f"{sharer_name} revoked sharing of task: \"{task_title}\"",
    )


async def send_status_notification(
    recipient_open_id: str,
    sharer_name: str,
    task_title: str,
    new_status: str,
) -> bool:
    return await _send_dm(
        recipient_open_id,
        f"Task \"{task_title}\" (shared by {sharer_name}) is now {new_status}",
    )
