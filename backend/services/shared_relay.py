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
        self._my_name: str | None = None

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
        """Restart relays for all active shared tasks (called on startup).

        Also backfills shadow tasks for legacy shared records that predate
        the relay feature (local_task_id is NULL).
        """
        # Load my feishu name for self-message dedup
        try:
            from backend.models.feishu_binding import FeishuUserBinding
            async with self.db_factory() as db:
                binding = (await db.execute(select(FeishuUserBinding).limit(1))).scalar_one_or_none()
                if binding:
                    self._my_name = binding.feishu_name
        except Exception:
            pass

        async with self.db_factory() as db:
            result = await db.execute(
                select(SharedTaskReceived).where(
                    SharedTaskReceived.status == "active",
                )
            )
            all_active = result.scalars().all()

        for shared in all_active:
            # Backfill shadow task for legacy records
            if not shared.local_task_id:
                try:
                    await self._create_shadow_task(shared)
                except Exception:
                    logger.debug("failed to create shadow task for shared %d", shared.id)
                    continue

            try:
                await self.start_relay(shared)
            except Exception:
                logger.debug("recover relay for shared %d failed", shared.id)

    async def _create_shadow_task(self, shared: SharedTaskReceived):
        """Create a local shadow task for a shared record that doesn't have one."""
        from backend.models.task import Task
        async with self.db_factory() as db:
            shadow = Task(
                title=shared.task_title or "",
                description=shared.task_description,
                status="pending",
                shared_from_id=shared.id,
            )
            db.add(shadow)
            await db.flush()
            shared_record = await db.get(SharedTaskReceived, shared.id)
            if shared_record:
                shared_record.local_task_id = shadow.id
            await db.commit()
            shared.local_task_id = shadow.id
            logger.info("created shadow task %d for shared %d", shadow.id, shared.id)

        # Fetch live config and backfill
        try:
            from backend.services.shared_proxy import proxy_config
            config = await proxy_config(shared.owner_ccm_url, shared.remote_task_id, shared.share_token)
            async with self.db_factory() as db:
                shadow = await db.get(Task, shared.local_task_id)
                if shadow and config:
                    shadow.status = config.get("status", "pending")
                    shadow.title = config.get("title") or shadow.title
                    shadow.description = config.get("description") or shadow.description
                    shadow.model = config.get("model")
                    shadow.provider = config.get("provider", "claude")
                    shadow.session_id = config.get("session_id") or shadow.session_id
                    shadow.target_repo = config.get("target_repo")
                    shadow.error_message = config.get("error_message")
                    await db.commit()
        except Exception:
            logger.debug("failed to fetch config for shadow task shared=%d", shared.id)

        try:
            await self.backfill_history(shared)
        except Exception:
            logger.debug("backfill failed for shared %d", shared.id)

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

        # user_message: skip self-sent (already stored locally with prefix by _send_shared_chat).
        # Relay messages from sharer or other shared users (different prefix or no prefix).
        if event_type == "user_message":
            content = data.get("content") or ""
            if self._my_name and content.startswith(f"[{self._my_name}]"):
                return  # self-sent, already stored locally

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
