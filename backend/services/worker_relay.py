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

import httpx
import websockets
from sqlalchemy import func, select

from backend.models.log_entry import LogEntry
from backend.models.monitor_session import MonitorCheck, MonitorSession
from backend.models.task import Task
from backend.models.worker import Worker

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
        return f"ws://{worker.private_ip}:{worker.ccm_port}/ws?token={worker.auth_token}"

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
        for attempt in range(10):
            if worker.id in self._closing:
                return
            await asyncio.sleep(min(2 ** attempt, 60))
            try:
                await self.ensure_connection(worker)
                for tid in task_ids:
                    await self.subscribe_task(worker, tid)
                await self._backfill_missing_logs(worker, task_ids)
                logger.info("worker %s relay reconnected", worker.id)
                return
            except Exception:
                continue
        # 重连失败 → 活跃 task 标 failed（worker 状态交给健康检查处理）
        logger.error("worker %s relay reconnect exhausted", worker.id)
        async with self.db_factory() as db:
            for tid in task_ids:
                t = await db.get(Task, tid)
                if t and t.status in ("executing", "in_progress"):
                    t.status = "failed"
                    t.error_message = f"Worker {worker.name} 断连且无法重连"
            await db.commit()

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

        # 2) chat 事件双写 LogEntry（instance_id=None；广播 payload 无 raw_json，存 None）
        if event_type in CHAT_EVENT_TYPES:
            async with self.db_factory() as db:
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
            # 助手产出 → 未读标记（与本地 _process_event 行为一致）
            if data.get("role") == "assistant" and event_type in ("message", "result"):
                async with self.db_factory() as db:
                    t = await db.get(Task, task_id)
                    if t:
                        t.has_unread = True
                        await db.commit()

        # 3) 字段同步
        if event_type == "status_change":
            new_status = data.get("new_status")
            if new_status:
                async with self.db_factory() as db:
                    t = await db.get(Task, task_id)
                    if t:
                        t.status = new_status
                        if data.get("error_message"):
                            t.error_message = data["error_message"]
                        await db.commit()

        elif event_type == "context_usage":
            async with self.db_factory() as db:
                t = await db.get(Task, task_id)
                if t:
                    t.context_window_usage = {
                        k: v for k, v in data.items()
                        if k not in ("event_type", "task_id")
                    }
                    await db.commit()

        elif event_type == "plan_ready":
            # plan_ready 广播不含 plan_content，从 worker API 拉
            plan_content = await self._fetch_task_field(worker, task_id, "plan_content")
            async with self.db_factory() as db:
                t = await db.get(Task, task_id)
                if t:
                    t.plan_content = plan_content
                    t.status = "plan_review"
                    await db.commit()

        elif event_type == "loop_iteration_end":
            async with self.db_factory() as db:
                t = await db.get(Task, task_id)
                if t:
                    t.loop_progress = data.get("progress") or t.loop_progress
                    await db.commit()

        elif event_type == "goal_evaluation":
            async with self.db_factory() as db:
                t = await db.get(Task, task_id)
                if t:
                    t.goal_turns_used = data.get("turn", t.goal_turns_used)
                    if data.get("reason"):
                        t.goal_last_reason = data["reason"]
                    await db.commit()

        elif event_type == "monitor_session_created":
            async with self.db_factory() as db:
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

    async def _fetch_task_field(self, worker: Worker, task_id: int, field: str):
        try:
            async with httpx.AsyncClient(timeout=15) as c:
                r = await c.get(
                    self._api(worker, f"/api/tasks/{task_id}"),
                    headers=self._headers(worker),
                )
                if r.status_code == 200:
                    return r.json().get(field)
        except Exception:
            logger.warning("fetch %s for task %s from worker %s failed", field, task_id, worker.id)
        return None

    async def _backfill_missing_logs(self, worker: Worker, task_ids: set[int]):
        """断连/重启后补日志。用「非 user_message 条数」对比（user_message 由
        chat 代理直接入 Manager DB，不经 relay，按总条数比会错位重复）。"""
        async with httpx.AsyncClient(timeout=30) as client:
            for tid in task_ids:
                try:
                    async with self.db_factory() as db:
                        local_count = (await db.execute(
                            select(func.count()).select_from(LogEntry).where(
                                LogEntry.task_id == tid,
                                LogEntry.event_type != "user_message",
                            )
                        )).scalar() or 0
                    r = await client.get(
                        self._api(worker, f"/api/tasks/{tid}/chat/history?compact=false"),
                        headers=self._headers(worker),
                    )
                    if r.status_code != 200:
                        continue
                    remote = r.json()
                    if isinstance(remote, dict):
                        remote = remote.get("messages", [])
                    remote_non_user = [m for m in remote if m.get("event_type") != "user_message"]
                    missing = remote_non_user[local_count:]
                    if not missing:
                        continue
                    async with self.db_factory() as db:
                        for m in missing:
                            db.add(LogEntry(
                                instance_id=None,
                                task_id=tid,
                                event_type=m.get("event_type") or "message",
                                role=m.get("role"),
                                content=m.get("content"),
                                tool_name=m.get("tool_name"),
                                tool_input=m.get("tool_input"),
                                tool_output=m.get("tool_output"),
                                raw_json=m.get("raw_json"),
                                is_error=m.get("is_error", False),
                                loop_iteration=m.get("loop_iteration"),
                            ))
                        await db.commit()
                    logger.info("backfilled %d log entries for task %s", len(missing), tid)
                except Exception:
                    logger.exception("backfill task %s from worker %s failed", tid, worker.id)
