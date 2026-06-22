"""SharedRelay — real-time event relay from sharer CCMs for shared tasks.

Each active shared task gets a persistent WebSocket connection to the
sharer's /ws/shared endpoint. Events are written to local log_entries
and broadcast to the local frontend, making shadow tasks behave like
local tasks in the UI.
"""

import asyncio
import json
import logging

import httpx
import websockets
from sqlalchemy import select

from backend.models.log_entry import LogEntry
from backend.models.task import Task
from backend.models.task_share import SharedTaskReceived

logger = logging.getLogger(__name__)

CHAT_EVENT_TYPES = {
    "user_message", "message", "result", "tool_use", "tool_result",
    "system_init", "system_event", "thinking", "process_exit",
}


class SharedRelay:
    def __init__(self, db_factory, broadcaster):
        self.db_factory = db_factory
        self.broadcaster = broadcaster
        self._connections: dict[int, object] = {}  # shared_received.id -> ws
        self._loops: dict[int, asyncio.Task] = {}  # shared_received.id -> relay task
        self._closing: set[int] = set()

    async def start_relay(self, shared: SharedTaskReceived):
        """Start relay for a shared task. Idempotent."""
        if shared.id in self._connections or not shared.local_task_id:
            return
        self._closing.discard(shared.id)
        loop_task = asyncio.create_task(self._connect_and_relay(shared))
        self._loops[shared.id] = loop_task

    async def stop_relay(self, shared_id: int):
        """Stop relay for a shared task."""
        self._closing.add(shared_id)
        ws = self._connections.pop(shared_id, None)
        loop_task = self._loops.pop(shared_id, None)
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass
        if loop_task is not None:
            loop_task.cancel()

    async def recover_all(self):
        """Restart relays for all active shared tasks (called on startup)."""
        async with self.db_factory() as db:
            result = await db.execute(
                select(SharedTaskReceived).where(
                    SharedTaskReceived.status == "active",
                    SharedTaskReceived.local_task_id.isnot(None),
                )
            )
            active = result.scalars().all()
        for shared in active:
            try:
                await self.start_relay(shared)
            except Exception:
                logger.debug("recover relay for shared %d failed", shared.id)

    async def _connect_and_relay(self, shared: SharedTaskReceived):
        """Connect to sharer's WS and relay events. Auto-reconnects."""
        ws_url = (
            shared.owner_ccm_url.replace("https://", "wss://").replace("http://", "ws://")
            + f"/ws/shared?token={shared.share_token}&task_id={shared.remote_task_id}"
        )
        for attempt in range(100):
            if shared.id in self._closing:
                return
            try:
                async with websockets.connect(ws_url, open_timeout=15) as ws:
                    self._connections[shared.id] = ws
                    logger.info("shared relay connected: shared=%d remote_task=%d", shared.id, shared.remote_task_id)
                    try:
                        async for raw in ws:
                            try:
                                await self._handle(json.loads(raw), shared)
                            except Exception:
                                logger.debug("shared relay handle error shared=%d", shared.id)
                    except (websockets.ConnectionClosed, OSError):
                        pass
            except asyncio.CancelledError:
                return
            except Exception:
                pass
            finally:
                self._connections.pop(shared.id, None)

            if shared.id in self._closing:
                return
            delay = min(2 ** attempt, 60)
            logger.debug("shared relay reconnecting shared=%d in %ds", shared.id, delay)
            await asyncio.sleep(delay)

        logger.warning("shared relay gave up reconnecting shared=%d", shared.id)

    async def _handle(self, msg: dict, shared: SharedTaskReceived):
        """Process one WS message from the sharer."""
        data = msg.get("data", msg)
        if not isinstance(data, dict):
            return
        if data.get("action") == "subscribed":
            return

        event_type = data.get("event_type") or data.get("event")
        if not event_type:
            return

        local_task_id = shared.local_task_id
        if not local_task_id:
            return

        # Skip user_message — the chat proxy already stored it locally
        if event_type == "user_message":
            return

        # Write chat events to local log_entries
        if event_type in CHAT_EVENT_TYPES:
            async with self.db_factory() as db:
                db.add(LogEntry(
                    instance_id=None,
                    task_id=local_task_id,
                    event_type=event_type,
                    role=data.get("role"),
                    content=data.get("content"),
                    tool_name=data.get("tool_name"),
                    tool_input=data.get("tool_input"),
                    tool_output=data.get("tool_output"),
                    raw_json=data.get("raw_json"),
                    is_error=data.get("is_error", False),
                ))
                await db.commit()

            if data.get("role") == "assistant" and event_type in ("message", "result"):
                async with self.db_factory() as db:
                    t = await db.get(Task, local_task_id)
                    if t:
                        t.has_unread = True
                        await db.commit()

        # Sync status changes
        if event_type == "status_change":
            new_status = data.get("new_status")
            if new_status:
                async with self.db_factory() as db:
                    t = await db.get(Task, local_task_id)
                    if t:
                        t.status = new_status
                        if data.get("error_message"):
                            t.error_message = data["error_message"]
                        await db.commit()

        # Broadcast to local frontend (mirror the event on local task channel)
        await self.broadcaster.broadcast(f"task:{local_task_id}", data)

    async def backfill_history(self, shared: SharedTaskReceived):
        """Pull full chat history from sharer and store locally."""
        if not shared.local_task_id:
            return
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(
                    f"{shared.owner_ccm_url}/api/shared-access/{shared.remote_task_id}/history",
                    params={"token": shared.share_token},
                )
                resp.raise_for_status()
                messages = resp.json()

            async with self.db_factory() as db:
                # Check if we already have entries
                existing = await db.execute(
                    select(LogEntry.id).where(LogEntry.task_id == shared.local_task_id).limit(1)
                )
                if existing.scalar_one_or_none():
                    return  # already backfilled

                for msg in messages:
                    db.add(LogEntry(
                        instance_id=None,
                        task_id=shared.local_task_id,
                        event_type=msg.get("event_type", "message"),
                        role=msg.get("role"),
                        content=msg.get("content"),
                        tool_name=msg.get("tool_name"),
                        tool_input=msg.get("tool_input"),
                        tool_output=msg.get("tool_output"),
                        is_error=msg.get("is_error", False),
                    ))
                await db.commit()
                logger.info("backfilled %d entries for shared task %d", len(messages), shared.local_task_id)
        except Exception:
            logger.debug("backfill failed for shared %d", shared.id)
