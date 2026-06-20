"""SharedProxy — proxies requests from the receiver's frontend to the sharer's CCM."""

import logging

import httpx

logger = logging.getLogger(__name__)


async def proxy_history(
    owner_ccm_url: str,
    remote_task_id: int,
    share_token: str,
    limit: int = 0,
    before_id: int = 0,
    compact: bool = True,
) -> list[dict]:
    params: dict = {"token": share_token, "compact": str(compact).lower()}
    if limit > 0:
        params["limit"] = str(limit)
    if before_id > 0:
        params["before_id"] = str(before_id)

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{owner_ccm_url}/api/shared-access/{remote_task_id}/history",
            params=params,
        )
        resp.raise_for_status()
        return resp.json()


async def proxy_chat(
    owner_ccm_url: str,
    remote_task_id: int,
    share_token: str,
    message: str,
    sender_name: str | None = None,
) -> dict:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{owner_ccm_url}/api/shared-access/{remote_task_id}/chat",
            params={"token": share_token},
            json={"message": message, "sender_name": sender_name},
        )
        resp.raise_for_status()
        return resp.json()


async def proxy_config(
    owner_ccm_url: str,
    remote_task_id: int,
    share_token: str,
) -> dict:
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{owner_ccm_url}/api/shared-access/{remote_task_id}/config",
            params={"token": share_token},
        )
        resp.raise_for_status()
        return resp.json()
