import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query
from sqlalchemy import select

from backend.config import settings

logger = logging.getLogger(__name__)

router = APIRouter()


def _ws_token_ok(ws: WebSocket) -> bool:
    """WS 认证：AUTH_TOKEN 或 JWT 都接受。"""
    if not settings.auth_token:
        return True
    # Legacy admin token (header or query param)
    auth = ws.headers.get("authorization", "")
    if auth.startswith("Bearer ") and auth[7:] == settings.auth_token:
        return True
    token = ws.query_params.get("token", "")
    if token == settings.auth_token:
        return True
    # JWT token (query param — browser WS can't set headers)
    if token:
        from backend.api.auth import decode_jwt
        if decode_jwt(token) is not None:
            return True
    if auth.startswith("Bearer "):
        from backend.api.auth import decode_jwt
        if decode_jwt(auth[7:]) is not None:
            return True
    return False


@router.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    from backend.main import broadcaster

    if not _ws_token_ok(ws):
        await ws.close(code=4401)
        return
    await ws.accept()
    try:
        while True:
            text = await ws.receive_text()
            try:
                msg = json.loads(text)
            except json.JSONDecodeError:
                continue

            action = msg.get("action")
            channels = msg.get("channels", [])

            if action == "subscribe" and channels:
                await broadcaster.subscribe(ws, channels)
                await ws.send_text(json.dumps({"action": "subscribed", "channels": channels}))
            elif action == "unsubscribe" and channels:
                await broadcaster.unsubscribe(ws)
                await ws.send_text(json.dumps({"action": "unsubscribed"}))
    except WebSocketDisconnect:
        await broadcaster.unsubscribe(ws)


@router.websocket("/ws/shared")
async def shared_websocket_endpoint(ws: WebSocket):
    """Shared task WebSocket — authenticated via share_token, auto-subscribes to the task channel."""
    from backend.main import broadcaster
    from backend.database import async_session
    from backend.models.task_share import TaskShare

    token = ws.query_params.get("token", "")
    task_id_str = ws.query_params.get("task_id", "")
    if not token or not task_id_str:
        await ws.close(code=4400)
        return

    try:
        task_id = int(task_id_str)
    except ValueError:
        await ws.close(code=4400)
        return

    # Validate share_token
    async with async_session() as db:
        result = await db.execute(
            select(TaskShare).where(
                TaskShare.task_id == task_id,
                TaskShare.share_token == token,
                TaskShare.status == "active",
            )
        )
        share = result.scalar_one_or_none()
        if not share:
            await ws.close(code=4403)
            return

    await ws.accept()
    channel = f"task:{task_id}"
    await broadcaster.subscribe(ws, [channel])
    await ws.send_text(json.dumps({"action": "subscribed", "channels": [channel]}))

    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        await broadcaster.unsubscribe(ws)
