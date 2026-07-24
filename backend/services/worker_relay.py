"""WorkerRelay — Worker CCM 事件中继（elastic-worker 设计 §6/§7/§11）。

每个 Worker 一条 WS 连接，订阅 `tasks` 全局 channel + 各活跃 task 的
`task:{id}` channel。收到事件后：
1. chat 类事件双写 Manager DB（LogEntry，instance_id=None）——历史永远查本地，
   Worker 离线/销毁后日志依然完整
2. 同步 task 状态/cost/plan/loop/goal/monitor 到 Manager DB
3. 镜像广播到 Manager 前端的同名 channel（前端零改动）

已知陷阱（实现处有注释）：worker 的 instance_manager 广播前会 pop session_id
（relay 永远收不到，由 chat 代理从响应同步）；广播 payload 不含 raw_json；
status_change 用 "new_status" 键；monitor 事件用 "event" 而非 "event_type" 键；
worker 的 MonitorSession.id 与本地自增会碰撞（用 remote_id 列翻译）。
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx
import websockets
from sqlalchemy import func, select, update

from backend.models.log_entry import LogEntry
from backend.models.monitor_session import MonitorCheck, MonitorSession
from backend.models.task import Task
from backend.models.worker import Worker
from backend.services.task_queue import PR_REVIEW_SUPERSEDED_METADATA_KEY

_TASK_STATUSES = frozenset(
    {
        "pending",
        "in_progress",
        "executing",
        "plan_review",
        "merging",
        "migrating",
        "completed",
        "failed",
        "cancelled",
        "conflict",
    }
)
_TERMINAL_TASK_STATUSES = frozenset(
    {"completed", "failed", "cancelled", "conflict"}
)
_FP_PREFIX = 1000  # chars; compare only a prefix so the chat/history endpoint's
                   # 20k truncation of tool_input/tool_output can't cause a false
                   # "missing" (which would re-insert an already-present entry).


@dataclass(frozen=True)
class WorkerTaskGeneration:
    """Exact Manager-side mirror generation owned by one Worker.

    ``worker_id`` is part of the generation, not merely routing metadata.  A
    delayed response/event from Worker A must not be able to update the same
    task id after it has moved local, moved to Worker B, or been retried on A.
    """

    task_id: int
    worker_id: int
    status: str
    retry_count: int
    instance_id: int | None
    started_at: datetime | None
    completed_at: datetime | None


def worker_task_generation(
    task: Task,
    *,
    expected_worker_id: int | None = None,
) -> WorkerTaskGeneration | None:
    worker_id = task.worker_id
    if (
        type(worker_id) is not int
        or task.shared_from_id is not None
        or (
            expected_worker_id is not None
            and worker_id != expected_worker_id
        )
    ):
        return None
    return WorkerTaskGeneration(
        task_id=task.id,
        worker_id=worker_id,
        status=task.status,
        retry_count=task.retry_count,
        instance_id=task.instance_id,
        started_at=task.started_at,
        completed_at=task.completed_at,
    )


def _nullable_eq(column, value):
    return column.is_(None) if value is None else column == value


def worker_task_generation_predicates(
    generation: WorkerTaskGeneration,
) -> tuple:
    return (
        Task.id == generation.task_id,
        Task.worker_id == generation.worker_id,
        Task.shared_from_id.is_(None),
        Task.status == generation.status,
        Task.retry_count == generation.retry_count,
        _nullable_eq(Task.instance_id, generation.instance_id),
        _nullable_eq(Task.started_at, generation.started_at),
        _nullable_eq(Task.completed_at, generation.completed_at),
    )


async def read_worker_task_generation(
    db,
    task_id: int,
    worker_id: int,
) -> WorkerTaskGeneration | None:
    """Read DB-normalized generation fields for one exact Worker assignment."""

    row = (
        await db.execute(
            select(
                Task.id,
                Task.worker_id,
                Task.status,
                Task.retry_count,
                Task.instance_id,
                Task.started_at,
                Task.completed_at,
            ).where(
                Task.id == task_id,
                Task.worker_id == worker_id,
                Task.shared_from_id.is_(None),
            )
        )
    ).one_or_none()
    if row is None:
        return None
    return WorkerTaskGeneration(
        task_id=row.id,
        worker_id=row.worker_id,
        status=row.status,
        retry_count=row.retry_count,
        instance_id=row.instance_id,
        started_at=row.started_at,
        completed_at=row.completed_at,
    )


def _remote_datetime(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def authoritative_worker_task_values(
    remote_task: dict,
    *,
    task_id: int,
) -> dict | None:
    """Validate a Worker task snapshot and return mirror-safe fields.

    ``retry_count`` is mandatory.  Status events do not currently carry a
    remote generation, so callers must use the authoritative Worker GET
    response.  Accepting a status-only payload would let a delayed event from a
    prior retry overwrite a newer retry on the same Worker.
    """

    if (
        not isinstance(remote_task, dict)
        or type(remote_task.get("id")) is not int
        or remote_task["id"] != task_id
        or remote_task.get("status") not in _TASK_STATUSES
        or type(remote_task.get("retry_count")) is not int
        or remote_task["retry_count"] < 0
    ):
        return None

    status = remote_task["status"]
    values: dict = {
        "status": status,
        "retry_count": remote_task["retry_count"],
    }
    for field in (
        "plan_approved",
        "error_message",
        "loop_progress",
        "session_id",
        "plan_content",
        "goal_turns_used",
        "goal_last_reason",
    ):
        if field in remote_task:
            values[field] = remote_task[field]

    if "started_at" in remote_task:
        started_at = _remote_datetime(remote_task["started_at"])
        if remote_task["started_at"] is None or started_at is not None:
            values["started_at"] = started_at

    if status in _TERMINAL_TASK_STATUSES:
        completed_at = _remote_datetime(remote_task.get("completed_at"))
        values["completed_at"] = (
            completed_at
            if completed_at is not None
            else datetime.utcnow()
        )
        if (
            status in ("failed", "conflict")
            and not remote_task.get("error_message")
        ):
            values["error_message"] = (
                "Worker task failed without an error message"
                if status == "failed"
                else "Worker task ended with an unresolved conflict"
            )
        elif status not in ("failed", "conflict"):
            values["error_message"] = remote_task.get("error_message")
    elif "completed_at" in remote_task:
        completed_at = _remote_datetime(remote_task["completed_at"])
        if remote_task["completed_at"] is None or completed_at is not None:
            values["completed_at"] = completed_at

    return values


async def apply_authoritative_worker_task(
    db,
    observed: WorkerTaskGeneration,
    remote_task: dict,
    *,
    metadata_updates: dict | None = None,
) -> WorkerTaskGeneration | None:
    """CAS an authoritative Worker snapshot onto its exact observed mirror."""

    values = authoritative_worker_task_values(
        remote_task,
        task_id=observed.task_id,
    )
    if (
        values is None
        or values["retry_count"] < observed.retry_count
    ):
        return None
    merged_metadata_updates = dict(metadata_updates or {})
    remote_metadata = remote_task.get("metadata_") or {}
    if (
        isinstance(remote_metadata, dict)
        and remote_metadata.get(PR_REVIEW_SUPERSEDED_METADATA_KEY) is True
    ):
        # This reserved lifecycle marker must survive every authoritative
        # Worker→Manager path, including a normal relay GET after the hidden
        # termination response was lost.
        merged_metadata_updates[PR_REVIEW_SUPERSEDED_METADATA_KEY] = True
    if merged_metadata_updates:
        # Lock the exact mirror before merging JSON in Python. PostgreSQL JSON
        # has no equality operator, so comparing the whole document in the CAS
        # is not portable; the row lock protects unrelated Manager metadata
        # such as ``pr_review_id`` from being overwritten by the Worker marker.
        locked = (
            await db.execute(
                select(Task)
                .where(*worker_task_generation_predicates(observed))
                .with_for_update()
            )
        ).scalar_one_or_none()
        if locked is None:
            await db.rollback()
            return None
        metadata = dict(locked.metadata_ or {})
        metadata.update(merged_metadata_updates)
        values["metadata_"] = metadata
    changed = await db.execute(
        update(Task)
        .where(*worker_task_generation_predicates(observed))
        .values(**values)
    )
    if changed.rowcount != 1:
        await db.rollback()
        return None
    resulting = await read_worker_task_generation(
        db,
        observed.task_id,
        observed.worker_id,
    )
    if resulting is None:
        await db.rollback()
        return None
    await db.commit()
    return resulting


def _entry_fingerprint(e: dict) -> tuple:
    """Stable identity for a relayed log entry, comparable between the local DB
    copy and the remote chat/history payload. Uses only fields that survive the
    history serialization unchanged, prefix-capped to dodge truncation."""
    def p(s):
        return (s or "")[:_FP_PREFIX]
    return (
        e.get("event_type") or "",
        e.get("role") or "",
        p(e.get("content")),
        e.get("tool_name") or "",
        p(e.get("tool_input")),
        p(e.get("tool_output")),
        e.get("loop_iteration"),
    )


def _missing_by_fingerprint(local_entries: list[dict], remote_entries: list[dict]) -> list[dict]:
    """Remote entries not already present locally, matched by fingerprint multiset.

    Order- and race-tolerant: unlike count-based tail slicing
    (``remote[local_count:]``), a mid-stream gap or a concurrent live-relay insert
    cannot make an already-present entry be re-inserted — the duplicate-message-
    on-reconnect bug.
    """
    have = Counter(_entry_fingerprint(e) for e in local_entries)
    missing: list[dict] = []
    for r in remote_entries:
        fp = _entry_fingerprint(r)
        if have.get(fp, 0) > 0:
            have[fp] -= 1
        else:
            missing.append(r)
    return missing

logger = logging.getLogger(__name__)

# 与 worker instance_manager 实际入库/广播的 chat 事件对齐
CHAT_EVENT_TYPES = {
    "user_message", "message", "result", "tool_use", "tool_result",
    "system_init", "system_event", "thinking", "process_exit",
}


class WorkerRelay:
    def __init__(self, db_factory, broadcaster):
        self.db_factory = db_factory
        self.broadcaster = broadcaster
        self._ws: dict[int, object] = {}            # worker_id -> ws connection
        self._tasks: dict[int, set[int]] = {}       # worker_id -> relayed task ids
        self._loops: dict[int, asyncio.Task] = {}    # worker_id -> relay loop（强引用）
        self._closing: set[int] = set()

    # ------------------------------------------------------------------
    # 连接管理
    # ------------------------------------------------------------------

    @staticmethod
    def _ws_url(worker: Worker) -> str:
        return f"ws://{worker.private_ip}:{worker.ccm_port}/ws"

    @staticmethod
    def _api(worker: Worker, path: str) -> str:
        return f"http://{worker.private_ip}:{worker.ccm_port}{path}"

    def _headers(self, worker: Worker) -> dict:
        return {"Authorization": f"Bearer {worker.auth_token}"}

    async def ensure_connection(self, worker: Worker):
        if worker.id in self._ws:
            return
        ws = await websockets.connect(
            self._ws_url(worker),
            additional_headers=self._headers(worker),
            open_timeout=15,
        )
        await ws.send(json.dumps({"action": "subscribe", "channels": ["tasks"]}))
        self._ws[worker.id] = ws
        self._tasks.setdefault(worker.id, set())
        self._closing.discard(worker.id)
        loop_task = asyncio.create_task(self._relay_loop(ws, worker))
        self._loops[worker.id] = loop_task
        logger.info("worker relay connected: worker %s (%s)", worker.id, worker.private_ip)

    async def subscribe_task(self, worker: Worker, task_id: int):
        """幂等订阅某 task 的事件中继。必须在向 worker 创建/操作 task 之前调用，
        否则初始事件会丢。"""
        await self.ensure_connection(worker)
        if task_id in self._tasks.get(worker.id, set()):
            return
        ws = self._ws[worker.id]
        await ws.send(json.dumps({"action": "subscribe", "channels": [f"task:{task_id}"]}))
        self._tasks[worker.id].add(task_id)

    def unsubscribe_task(self, worker_id: int, task_id: int):
        """迁移后停止中继该 task（_handle 按 self._tasks 过滤，移除即生效）。"""
        self._tasks.get(worker_id, set()).discard(task_id)

    async def stop_worker(self, worker_id: int):
        """断开并停止重连（worker 关机/销毁前必须调，否则重连风暴）。"""
        self._closing.add(worker_id)
        ws = self._ws.pop(worker_id, None)
        self._tasks.pop(worker_id, None)
        loop_task = self._loops.pop(worker_id, None)
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass
        if loop_task is not None:
            loop_task.cancel()

    async def recover(self, worker: Worker):
        """worker 恢复（开机/健康自动恢复/Manager 重启）后重建中继 + 补日志。"""
        async with self.db_factory() as db:
            result = await db.execute(
                select(Task).where(
                    Task.worker_id == worker.id,
                    Task.status.in_(["executing", "in_progress", "plan_review"]),
                )
            )
            active = result.scalars().all()
        for t in active:
            try:
                await self.subscribe_task(worker, t.id)
            except Exception:
                logger.exception("recover: subscribe task %s on worker %s failed", t.id, worker.id)
                return
        if active:
            await self._backfill_missing_logs(worker, {t.id for t in active})

    async def _observe_task_generation(
        self,
        worker_id: int,
        task_id: int,
    ) -> WorkerTaskGeneration | None:
        async with self.db_factory() as db:
            return await read_worker_task_generation(db, task_id, worker_id)

    async def _fetch_task_snapshot(
        self,
        worker: Worker,
        task_id: int,
        *,
        client=None,
    ) -> dict | None:
        async def fetch(http_client):
            response = await http_client.get(
                self._api(worker, f"/api/tasks/{task_id}"),
                headers=self._headers(worker),
            )
            if response.status_code != 200:
                return None
            payload = response.json()
            return payload if isinstance(payload, dict) else None

        try:
            if client is not None:
                return await fetch(client)
            async with httpx.AsyncClient(timeout=15) as http_client:
                return await fetch(http_client)
        except Exception:
            logger.warning(
                "fetch task %s from worker %s failed",
                task_id,
                worker.id,
            )
            return None

    async def _publish_status_generation(
        self,
        generation: WorkerTaskGeneration,
        payload: dict | None = None,
    ) -> bool:
        """Publish while holding a no-op write lock on the exact result row."""

        async with self.db_factory() as db:
            guarded = await db.execute(
                update(Task)
                .where(*worker_task_generation_predicates(generation))
                .values(status=generation.status)
            )
            if guarded.rowcount != 1:
                await db.rollback()
                return False
            event = {
                "event": "status_change",
                "task_id": generation.task_id,
                "new_status": generation.status,
            }
            if payload:
                event.update(
                    {
                        key: value
                        for key, value in payload.items()
                        if key not in ("instance_id", "worker_id")
                    }
                )
                event["event"] = "status_change"
                event["task_id"] = generation.task_id
                event["new_status"] = generation.status
            try:
                await self.broadcaster.broadcast("tasks", event)
            except Exception:
                logger.exception(
                    "failed to publish Worker status for task %s",
                    generation.task_id,
                )
            await db.commit()
            return True

    # ------------------------------------------------------------------
    # 事件中继主循环
    # ------------------------------------------------------------------

    async def _relay_loop(self, ws, worker: Worker):
        try:
            async for raw in ws:
                try:
                    await self._handle(json.loads(raw), worker)
                except Exception:
                    logger.exception("relay handle error (worker %s)", worker.id)
        except (websockets.ConnectionClosed, OSError):
            pass
        except asyncio.CancelledError:
            return
        if worker.id not in self._closing:
            logger.warning("worker %s relay disconnected, reconnecting", worker.id)
            self._ws.pop(worker.id, None)
            asyncio.create_task(self._reconnect(worker))

    async def _reconnect(self, worker: Worker):
        task_ids = self._tasks.pop(worker.id, set())
        worker_id = worker.id
        # Capture the generations owned by this disconnected relay before any
        # backoff/network await.  Reconnect exhaustion belongs only to these
        # generations; a retry on the same Worker is a distinct generation.
        disconnected_generations: dict[int, WorkerTaskGeneration] = {}
        async with self.db_factory() as db:
            for task_id in task_ids:
                generation = await read_worker_task_generation(
                    db,
                    task_id,
                    worker_id,
                )
                if (
                    generation is not None
                    and generation.status in ("executing", "in_progress")
                ):
                    disconnected_generations[task_id] = generation
        for attempt in range(10):
            if worker_id in self._closing:
                return
            await asyncio.sleep(min(2 ** attempt, 60))
            try:
                # Re-fetch worker from DB to get latest IP/token after stop/start
                async with self.db_factory() as db:
                    fresh = await db.get(Worker, worker_id)
                    if not fresh or fresh.status in ("terminated", "destroying"):
                        return
                await self.ensure_connection(fresh)
                current_task_ids: set[int] = set()
                for tid in task_ids:
                    if (
                        await self._observe_task_generation(worker_id, tid)
                        is None
                    ):
                        continue
                    await self.subscribe_task(fresh, tid)
                    current_task_ids.add(tid)
                await self._backfill_missing_logs(fresh, current_task_ids)
                logger.info("worker %s relay reconnected", worker_id)
                return
            except Exception:
                continue
        # 重连失败 → 活跃 task 标 failed（worker 状态交给健康检查处理）
        logger.error("worker %s relay reconnect exhausted", worker.id)
        failed_generations: list[WorkerTaskGeneration] = []
        for tid, observed in disconnected_generations.items():
            async with self.db_factory() as db:
                failed = await db.execute(
                    update(Task)
                    .where(*worker_task_generation_predicates(observed))
                    .values(
                        status="failed",
                        completed_at=datetime.utcnow(),
                        error_message=(
                            f"Worker {worker.name} 断连且无法重连"
                        ),
                    )
                )
                if failed.rowcount != 1:
                    await db.rollback()
                    continue
                resulting = await read_worker_task_generation(
                    db,
                    tid,
                    worker_id,
                )
                if resulting is None:
                    await db.rollback()
                    continue
                await db.commit()
                failed_generations.append(resulting)
        for generation in failed_generations:
            await self._publish_status_generation(generation)

    async def _handle(self, msg: dict, worker: Worker):
        channel = msg.get("channel", "")
        data = msg.get("data", msg)
        if not isinstance(data, dict):
            return
        # monitor 事件用 "event" 键，chat 事件用 "event_type"，status_change 用 "event"
        event_type = data.get("event_type") or data.get("event")

        # task_id：data 里有就用，没有从 channel 名解析（task:{id} 的 chat 事件不带）
        task_id = data.get("task_id")
        if not task_id and channel.startswith("task:"):
            try:
                task_id = int(channel.split(":", 1)[1])
            except (ValueError, IndexError):
                return
        if not task_id or task_id not in self._tasks.get(worker.id, set()):
            return

        # 1) user_message 跳过：chat 代理已在转发前存 Manager DB 并广播，防双写
        if event_type == "user_message":
            return

        observed = await self._observe_task_generation(worker.id, task_id)
        if observed is None:
            # Subscription state is only a routing hint.  The durable worker_id
            # assignment is the authority after migrations.
            return

        # 2) chat 事件双写 LogEntry（instance_id=None；广播 payload 无 raw_json，存 None）
        if event_type in CHAT_EVENT_TYPES:
            async with self.db_factory() as db:
                guard_values = {"status": observed.status}
                if (
                    data.get("role") == "assistant"
                    and event_type in ("message", "result")
                ):
                    guard_values["has_unread"] = True
                guarded = await db.execute(
                    update(Task)
                    .where(*worker_task_generation_predicates(observed))
                    .values(**guard_values)
                )
                if guarded.rowcount != 1:
                    await db.rollback()
                    return
                db.add(LogEntry(
                    instance_id=None,
                    task_id=task_id,
                    event_type=event_type,
                    role=data.get("role"),
                    content=data.get("content"),
                    tool_name=data.get("tool_name"),
                    tool_input=data.get("tool_input"),
                    tool_output=data.get("tool_output"),
                    raw_json=data.get("raw_json"),
                    is_error=data.get("is_error", False),
                    loop_iteration=data.get("loop_iteration"),
                ))
                await db.commit()
            # session_id 同步：worker 广播前 pop 了 session_id，首条事件到达时从 Worker 拉取
            if event_type == "system_init":
                session_observed = await self._observe_task_generation(
                    worker.id,
                    task_id,
                )
                if session_observed is not None:
                    remote_task = await self._fetch_task_snapshot(worker, task_id)
                    remote_values = (
                        authoritative_worker_task_values(
                            remote_task,
                            task_id=task_id,
                        )
                        if remote_task is not None
                        else None
                    )
                    if (
                        remote_values is not None
                        and remote_values["retry_count"]
                        == session_observed.retry_count
                        and remote_values.get("session_id")
                    ):
                        async with self.db_factory() as db:
                            session_synced = await db.execute(
                                update(Task)
                                .where(
                                    *worker_task_generation_predicates(
                                        session_observed
                                    ),
                                    Task.session_id.is_(None),
                                )
                                .values(
                                    session_id=remote_values["session_id"]
                                )
                            )
                            if session_synced.rowcount == 1:
                                await db.commit()
                            else:
                                await db.rollback()

        # 2b) Skill evolution from Worker tool failures
        if (
            event_type == "tool_result"
            and data.get("is_error")
            and data.get("tool_name")
        ):
            try:
                from backend.services.skill_evolution import evolve_on_failure
                async with self.db_factory() as db:
                    await evolve_on_failure(
                        tool_name=data["tool_name"],
                        error=str(data.get("tool_output", ""))[:500],
                        context=str(data.get("tool_input", ""))[:300],
                        db=db,
                        worker_id=worker.id,
                    )
            except Exception:
                logger.debug("worker skill evolution failed", exc_info=True)

        # 3) 字段同步
        if event_type == "status_change":
            new_status = data.get("new_status")
            if not isinstance(new_status, str):
                return
            # status_change itself carries no remote retry generation.  Resolve
            # it against the authoritative Worker task before touching the
            # Manager mirror; a mismatching status means this queued event is
            # stale and must be dropped.
            remote_task = await self._fetch_task_snapshot(worker, task_id)
            if (
                remote_task is None
                or remote_task.get("status") != new_status
            ):
                return
            async with self.db_factory() as db:
                resulting = await apply_authoritative_worker_task(
                    db,
                    observed,
                    remote_task,
                )
            if resulting is not None:
                await self._publish_status_generation(resulting, data)
            return

        elif event_type == "context_usage":
            async with self.db_factory() as db:
                changed = await db.execute(
                    update(Task)
                    .where(*worker_task_generation_predicates(observed))
                    .values(
                        context_window_usage={
                        k: v for k, v in data.items()
                        if k not in ("event_type", "task_id")
                        }
                    )
                )
                if changed.rowcount == 1:
                    await db.commit()
                else:
                    await db.rollback()
                    return

        elif event_type == "plan_ready":
            # plan_ready carries neither plan_content nor a remote generation.
            # Resolve both from one authoritative snapshot.
            remote_task = await self._fetch_task_snapshot(worker, task_id)
            if (
                remote_task is None
                or remote_task.get("status") != "plan_review"
            ):
                return
            async with self.db_factory() as db:
                resulting = await apply_authoritative_worker_task(
                    db,
                    observed,
                    remote_task,
                )
            if resulting is None:
                return

        elif event_type == "loop_iteration_end":
            async with self.db_factory() as db:
                values = {"status": observed.status}
                if data.get("progress"):
                    values["loop_progress"] = data["progress"]
                changed = await db.execute(
                    update(Task)
                    .where(*worker_task_generation_predicates(observed))
                    .values(**values)
                )
                if changed.rowcount == 1:
                    await db.commit()
                else:
                    await db.rollback()
                    return

        elif event_type == "goal_evaluation":
            async with self.db_factory() as db:
                values = {"status": observed.status}
                if data.get("turn") is not None:
                    values["goal_turns_used"] = data["turn"]
                if data.get("reason"):
                    values["goal_last_reason"] = data["reason"]
                changed = await db.execute(
                    update(Task)
                    .where(*worker_task_generation_predicates(observed))
                    .values(**values)
                )
                if changed.rowcount == 1:
                    await db.commit()
                else:
                    await db.rollback()
                    return

        elif event_type == "monitor_session_created":
            async with self.db_factory() as db:
                guarded = await db.execute(
                    update(Task)
                    .where(*worker_task_generation_predicates(observed))
                    .values(status=observed.status)
                )
                if guarded.rowcount != 1:
                    await db.rollback()
                    return
                remote_id = data.get("monitor_session_id")
                existing = (await db.execute(
                    select(MonitorSession).where(
                        MonitorSession.task_id == task_id,
                        MonitorSession.remote_id == remote_id,
                    )
                )).scalar_one_or_none()
                if existing is None and remote_id is not None:
                    db.add(MonitorSession(
                        remote_id=remote_id,
                        task_id=task_id,
                        description=data.get("description") or "",
                        status="running",
                    ))
                await db.commit()

        elif event_type == "monitor_check":
            async with self.db_factory() as db:
                guarded = await db.execute(
                    update(Task)
                    .where(*worker_task_generation_predicates(observed))
                    .values(status=observed.status)
                )
                if guarded.rowcount != 1:
                    await db.rollback()
                    return
                ms = await self._local_monitor(db, task_id, data.get("monitor_session_id"))
                if ms:
                    db.add(MonitorCheck(
                        monitor_session_id=ms.id,
                        check_number=data.get("check_number") or 0,
                        status=data.get("status") or "",
                        summary=data.get("summary"),
                        full_output=data.get("full_output"),
                    ))
                    ms.checks_done = data.get("check_number", ms.checks_done)
                    ms.last_summary = data.get("summary")
                await db.commit()

        elif event_type == "monitor_session_status":
            async with self.db_factory() as db:
                guarded = await db.execute(
                    update(Task)
                    .where(*worker_task_generation_predicates(observed))
                    .values(status=observed.status)
                )
                if guarded.rowcount != 1:
                    await db.rollback()
                    return
                ms = await self._local_monitor(db, task_id, data.get("monitor_session_id"))
                if ms:
                    ms.status = data.get("status") or ms.status
                    if ms.status in ("completed", "failed", "cancelled"):
                        ms.completed_at = func.now()
                await db.commit()

        # 4) 镜像广播到来源同名 channel（剥 worker 的 instance_id，对 Manager 无意义）
        forward = {k: v for k, v in data.items() if k != "instance_id"}
        if channel.startswith("task:"):
            await self.broadcaster.broadcast(f"task:{task_id}", forward)
        elif channel == "tasks":
            await self.broadcaster.broadcast("tasks", forward)

    @staticmethod
    async def _local_monitor(db, task_id: int, remote_id) -> MonitorSession | None:
        if remote_id is None:
            return None
        return (await db.execute(
            select(MonitorSession).where(
                MonitorSession.task_id == task_id,
                MonitorSession.remote_id == remote_id,
            )
        )).scalar_one_or_none()

    # ------------------------------------------------------------------
    # Worker API 辅助
    # ------------------------------------------------------------------

    async def _backfill_missing_logs(self, worker: Worker, task_ids: set[int]):
        """断连/重启后补日志。用「非 user_message 条数」对比（user_message 由
        chat 代理直接入 Manager DB，不经 relay，按总条数比会错位重复）。"""
        async with httpx.AsyncClient(timeout=30) as client:
            for tid in task_ids:
                try:
                    history_observed = await self._observe_task_generation(
                        worker.id,
                        tid,
                    )
                    if history_observed is None:
                        continue
                    history_response = await client.get(
                        self._api(
                            worker,
                            f"/api/tasks/{tid}/chat/history?compact=false",
                        ),
                        headers=self._headers(worker),
                    )
                    if history_response.status_code == 200:
                        remote = history_response.json()
                        if isinstance(remote, dict):
                            remote = remote.get("messages", [])
                        if not isinstance(remote, list):
                            remote = []
                        remote_non_user = [
                            message
                            for message in remote
                            if isinstance(message, dict)
                            and message.get("event_type") != "user_message"
                        ]
                        async with self.db_factory() as db:
                            guarded = await db.execute(
                                update(Task)
                                .where(
                                    *worker_task_generation_predicates(
                                        history_observed
                                    )
                                )
                                .values(status=history_observed.status)
                            )
                            if guarded.rowcount != 1:
                                await db.rollback()
                            else:
                                # Re-read after acquiring the Task generation
                                # lock so a live relay insert which won the race
                                # is included in fingerprint deduplication.
                                local_rows = (
                                    await db.execute(
                                        select(
                                            LogEntry.event_type,
                                            LogEntry.role,
                                            LogEntry.content,
                                            LogEntry.tool_name,
                                            LogEntry.tool_input,
                                            LogEntry.tool_output,
                                            LogEntry.loop_iteration,
                                        ).where(
                                            LogEntry.task_id == tid,
                                            LogEntry.event_type
                                            != "user_message",
                                        )
                                    )
                                ).all()
                                local_entries = [
                                    dict(row._mapping)
                                    for row in local_rows
                                ]
                                missing = _missing_by_fingerprint(
                                    local_entries,
                                    remote_non_user,
                                )
                                for message in missing:
                                    db.add(
                                        LogEntry(
                                            instance_id=None,
                                            task_id=tid,
                                            event_type=(
                                                message.get("event_type")
                                                or "message"
                                            ),
                                            role=message.get("role"),
                                            content=message.get("content"),
                                            tool_name=message.get("tool_name"),
                                            tool_input=message.get("tool_input"),
                                            tool_output=message.get("tool_output"),
                                            raw_json=message.get("raw_json"),
                                            is_error=message.get(
                                                "is_error",
                                                False,
                                            ),
                                            loop_iteration=message.get(
                                                "loop_iteration"
                                            ),
                                        )
                                    )
                                await db.commit()
                                if missing:
                                    logger.info(
                                        "backfilled %d log entries for task %s",
                                        len(missing),
                                        tid,
                                    )

                    # The status request gets its own pre-request observation.
                    # Never re-read the current Task only after the network
                    # response: that would let an old response borrow a newer
                    # local/Worker assignment.
                    status_observed = await self._observe_task_generation(
                        worker.id,
                        tid,
                    )
                    if status_observed is None:
                        continue
                    remote_task = await self._fetch_task_snapshot(
                        worker,
                        tid,
                        client=client,
                    )
                    if remote_task is None:
                        continue
                    async with self.db_factory() as db:
                        resulting = await apply_authoritative_worker_task(
                            db,
                            status_observed,
                            remote_task,
                        )
                    if (
                        resulting is not None
                        and resulting.status != status_observed.status
                    ):
                        await self._publish_status_generation(resulting)
                except Exception:
                    logger.exception("backfill task %s from worker %s failed", tid, worker.id)
