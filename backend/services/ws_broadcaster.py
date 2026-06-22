import asyncio
import json
import logging
from collections import defaultdict

from fastapi import WebSocket

logger = logging.getLogger(__name__)


class WebSocketBroadcaster:
    """Central hub for broadcasting real-time events to WebSocket clients."""

    def __init__(self):
        self.subscriptions: dict[str, set[WebSocket]] = defaultdict(set)
        self._lock = asyncio.Lock()
        self.db_factory = None

    async def subscribe(self, ws: WebSocket, channels: list[str]):
        async with self._lock:
            for ch in channels:
                self.subscriptions[ch].add(ws)

    async def unsubscribe(self, ws: WebSocket):
        async with self._lock:
            for ch in list(self.subscriptions):
                self.subscriptions[ch].discard(ws)
                if not self.subscriptions[ch]:
                    del self.subscriptions[ch]

    async def broadcast(self, channel: str, data: dict):
        message = json.dumps({"channel": channel, "data": data})
        subs = self.subscriptions.get(channel, set())
        dead = []
        for ws in subs:
            try:
                await ws.send_text(message)
            except Exception as e:
                logger.debug("broadcast send failed on %s: %s", channel, e)
                dead.append(ws)
        for ws in dead:
            await self.unsubscribe(ws)

        # Mirror status_change to per-task channel so /ws/shared subscribers see it
        if channel == "tasks" and data.get("event") == "status_change" and data.get("task_id"):
            task_channel = f"task:{data['task_id']}"
            task_msg = json.dumps({"channel": task_channel, "data": data})
            task_dead = []
            for ws in self.subscriptions.get(task_channel, set()):
                try:
                    await ws.send_text(task_msg)
                except Exception:
                    task_dead.append(ws)
            for ws in task_dead:
                await self.unsubscribe(ws)

        # Fire-and-forget share notifications on terminal status changes
        if (
            channel == "tasks"
            and data.get("event") == "status_change"
            and data.get("new_status") in ("completed", "failed", "cancelled")
            and self.db_factory
        ):
            asyncio.create_task(self._notify_shared_status(data))

    async def _notify_shared_status(self, data: dict):
        try:
            from backend.services.share_notifier import notify_shared_users_on_status_change
            await notify_shared_users_on_status_change(
                self.db_factory, data["task_id"], data["new_status"],
            )
        except Exception:
            logger.debug("share status notification failed for task %s", data.get("task_id"))
