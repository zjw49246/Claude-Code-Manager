"""Feishu DM notifications — send share/revoke messages to users."""

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


async def send_share_notification(
    recipient_open_id: str,
    sharer_name: str,
    task_title: str,
    ccm_url: str,
) -> bool:
    """Send a Feishu DM to notify someone that a task was shared with them."""
    if not settings.feishu_app_id or not settings.feishu_app_secret:
        return False

    try:
        token = await _get_app_access_token()
        content = {
            "text": f"{sharer_name} shared a task with you: \"{task_title}\"\nView at: {ccm_url}/#/shares"
        }
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{_FEISHU_BASE}/im/v1/messages",
                params={"receive_id_type": "open_id"},
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "receive_id": recipient_open_id,
                    "msg_type": "text",
                    "content": str(content).replace("'", '"'),
                },
            )
            data = resp.json()
            if data.get("code") != 0:
                logger.warning("Feishu send_message error: %s", data)
                return False
        return True
    except Exception:
        logger.exception("Failed to send Feishu notification to %s", recipient_open_id)
        return False


async def send_revoke_notification(
    recipient_open_id: str,
    sharer_name: str,
    task_title: str,
) -> bool:
    """Send a Feishu DM to notify someone that a shared task was revoked."""
    if not settings.feishu_app_id or not settings.feishu_app_secret:
        return False

    try:
        token = await _get_app_access_token()
        content = {
            "text": f"{sharer_name} revoked sharing of task: \"{task_title}\""
        }
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{_FEISHU_BASE}/im/v1/messages",
                params={"receive_id_type": "open_id"},
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "receive_id": recipient_open_id,
                    "msg_type": "text",
                    "content": str(content).replace("'", '"'),
                },
            )
            data = resp.json()
            if data.get("code") != 0:
                logger.warning("Feishu send_message error: %s", data)
                return False
        return True
    except Exception:
        logger.exception("Failed to send Feishu revoke notification to %s", recipient_open_id)
        return False


async def send_status_notification(
    recipient_open_id: str,
    sharer_name: str,
    task_title: str,
    new_status: str,
) -> bool:
    """Send a Feishu DM when a shared task reaches a terminal status."""
    if not settings.feishu_app_id or not settings.feishu_app_secret:
        return False

    try:
        token = await _get_app_access_token()
        content = {
            "text": f"Task \"{task_title}\" (shared by {sharer_name}) is now {new_status}"
        }
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{_FEISHU_BASE}/im/v1/messages",
                params={"receive_id_type": "open_id"},
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "receive_id": recipient_open_id,
                    "msg_type": "text",
                    "content": str(content).replace("'", '"'),
                },
            )
            data = resp.json()
            if data.get("code") != 0:
                logger.warning("Feishu send_message error: %s", data)
                return False
        return True
    except Exception:
        logger.exception("Failed to send Feishu status notification to %s", recipient_open_id)
        return False
