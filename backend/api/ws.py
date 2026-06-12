import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from backend.config import settings

router = APIRouter()


def _ws_token_ok(ws: WebSocket) -> bool:
    """WS 认证：AUTH_TOKEN 配置时校验。浏览器原生 WebSocket 设不了 header，
    支持 ?token= 查询参数；WorkerRelay（服务端连接）用 Authorization header。"""
    if not settings.auth_token:
        return True
    auth = ws.headers.get("authorization", "")
    if auth.startswith("Bearer ") and auth[7:] == settings.auth_token:
        return True
    return ws.query_params.get("token") == settings.auth_token


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
