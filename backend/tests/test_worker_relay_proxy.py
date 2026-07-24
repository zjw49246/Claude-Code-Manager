"""Phase 2 测试：WorkerRelay 事件处理 / Dispatcher 双路径 / Chat 与操作代理。"""
import asyncio
import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import select, update

import backend.main as main_module
import backend.services.task_events as task_events_module
import backend.services.worker_proxy as worker_proxy_module
import backend.services.worker_relay as worker_relay_module
from backend.models.log_entry import LogEntry
from backend.models.monitor_session import MonitorCheck, MonitorSession
from backend.models.project import Project
from backend.models.task import Task
from backend.models.worker import Worker
from backend.schemas.task import TaskCreate
from backend.services.worker_proxy import WorkerProxy
from backend.services.worker_relay import WorkerRelay


class FakeBroadcaster:
    def __init__(self):
        self.sent: list[tuple[str, dict]] = []

    async def broadcast(self, channel, data):
        self.sent.append((channel, data))


@pytest.fixture
def broadcaster():
    return FakeBroadcaster()


@pytest.fixture
def relay(db_factory, broadcaster):
    r = WorkerRelay(db_factory=db_factory, broadcaster=broadcaster)
    return r


async def _mk_worker(session_factory, **fields) -> Worker:
    fields.setdefault("status", "ready")
    fields.setdefault("private_ip", "10.0.0.9")
    fields.setdefault("auth_token", "wtoken")
    async with session_factory() as db:
        w = Worker(name="w1", **fields)
        db.add(w)
        await db.commit()
        await db.refresh(w)
        return w


async def _mk_task(session_factory, **fields) -> Task:
    fields.setdefault("status", "in_progress")
    fields.setdefault("description", "d")
    async with session_factory() as db:
        t = Task(title="t", **fields)
        db.add(t)
        await db.commit()
        await db.refresh(t)
        return t


def _remote_task(task: Task, **overrides) -> dict:
    payload = {
        "id": task.id,
        "status": task.status,
        "retry_count": task.retry_count,
        "session_id": task.session_id,
        "started_at": (
            task.started_at.isoformat() if task.started_at else None
        ),
        "completed_at": (
            task.completed_at.isoformat() if task.completed_at else None
        ),
        "error_message": task.error_message,
    }
    payload.update(overrides)
    return payload


async def test_authoritative_worker_apply_preserves_supersede_marker(
    session_factory,
):
    """Normal relay/proxy convergence cannot drop a lost-response gate."""

    worker = await _mk_worker(session_factory)
    task = await _mk_task(
        session_factory,
        worker_id=worker.id,
        metadata_={"pr_review_id": 37},
    )
    observed = worker_relay_module.worker_task_generation(task)
    assert observed is not None

    async with session_factory() as db:
        resulting = await (
            worker_relay_module.apply_authoritative_worker_task(
                db,
                observed,
                _remote_task(
                    task,
                    status="completed",
                    metadata_={"pr_review_superseded": True},
                ),
            )
        )

    assert resulting is not None
    assert resulting.status == "completed"
    async with session_factory() as db:
        current = await db.get(Task, task.id)
        assert current.metadata_ == {
            "pr_review_id": 37,
            "pr_review_superseded": True,
        }


def test_worker_proxy_ssh_is_scoped_to_cloud_instance(monkeypatch):
    ssh_factory = Mock()
    monkeypatch.setattr(worker_proxy_module, "SSHExecutor", ssh_factory)
    monkeypatch.setattr(
        worker_proxy_module,
        "worker_known_hosts_path",
        Mock(return_value="/tmp/known-hosts/i-worker-proxy"),
    )
    proxy = WorkerProxy(None, relay=AsyncMock())
    worker = Worker(
        name="scoped-worker",
        private_ip="10.0.0.9",
        ssh_user="ubuntu",
        ssh_key_path="/tmp/worker-key",
        cloud_instance_id="i-worker-proxy",
    )

    proxy._ssh(worker)

    worker_proxy_module.worker_known_hosts_path.assert_called_once_with(
        "i-worker-proxy"
    )
    ssh_factory.assert_called_once_with(
        host="10.0.0.9",
        user="ubuntu",
        key_path="/tmp/worker-key",
        known_hosts_path="/tmp/known-hosts/i-worker-proxy",
    )


async def test_worker_task_operation_lock_blocks_concurrent_reforward():
    proxy = WorkerProxy(None, relay=AsyncMock())
    task = Task(id=321, title="remote", worker_id=7)
    forwarded = asyncio.Event()

    async def record_forward(_task):
        forwarded.set()

    proxy._forward_task_to_worker_locked = AsyncMock(
        side_effect=record_forward
    )
    operation_lock = proxy.task_operation_lock(task.id)
    await operation_lock.acquire()
    forward_task = asyncio.create_task(proxy.forward_task_to_worker(task))
    await asyncio.sleep(0)
    assert not forwarded.is_set()

    operation_lock.release()
    await forward_task
    assert forwarded.is_set()


async def test_worker_forward_preserves_pr_review_tag_through_task_create(
    monkeypatch,
):
    """The Worker copy retains the internal endpoint's routing fallback tag."""

    captured_payload = {}

    class Response:
        def raise_for_status(self):
            return None

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, _url, *, headers, json):
            captured_payload.update(json)
            return Response()

    monkeypatch.setattr(worker_proxy_module.httpx, "AsyncClient", Client)
    relay = AsyncMock()
    proxy = WorkerProxy(None, relay)
    worker = Worker(
        id=77,
        name="worker",
        status="ready",
        private_ip="10.0.0.77",
        auth_token="token",
    )
    task = Task(
        id=901,
        title="PR Review: owner/repo#1",
        description="review",
        worker_id=worker.id,
        project_id=12,
        priority=0,
        max_retries=2,
        mode="auto",
        max_iterations=50,
        must_complete=False,
        goal_max_turns=30,
        provider="codex",
        enable_workflows=False,
        tags=["pr-review"],
        metadata_={"pr_review_id": 123},
    )
    proxy.get_worker = AsyncMock(return_value=worker)
    proxy.ensure_worker_project = AsyncMock(return_value=34)

    await proxy._forward_task_to_worker_locked(task)

    parsed_on_worker = TaskCreate.model_validate(captured_payload)
    assert captured_payload["tags"] == ["pr-review"]
    assert parsed_on_worker.tags == ["pr-review"]
    # metadata_ is intentionally not a public TaskCreate field; the hidden
    # termination endpoint accepts the forwarded tag only for Worker copies.
    assert not hasattr(parsed_on_worker, "metadata_")


# === WorkerRelay._handle ===


async def test_relay_chat_event_stored_and_forwarded(relay, broadcaster, session_factory):
    w = await _mk_worker(session_factory)
    t = await _mk_task(session_factory, worker_id=w.id)
    relay._tasks[w.id] = {t.id}

    await relay._handle({
        "channel": f"task:{t.id}",
        "data": {"event_type": "message", "role": "assistant", "content": "hi",
                 "instance_id": 7},
    }, w)

    async with session_factory() as db:
        logs = (await db.execute(select(LogEntry).where(LogEntry.task_id == t.id))).scalars().all()
        task = await db.get(Task, t.id)
    assert len(logs) == 1
    assert logs[0].instance_id is None
    assert logs[0].content == "hi"
    assert task.has_unread is True
    # 镜像广播到同名 channel，且剥掉 worker 的 instance_id
    assert broadcaster.sent == [(f"task:{t.id}", {"event_type": "message", "role": "assistant", "content": "hi"})]


async def test_relay_skips_user_message_and_unsubscribed(relay, broadcaster, session_factory):
    w = await _mk_worker(session_factory)
    t = await _mk_task(session_factory, worker_id=w.id)
    relay._tasks[w.id] = {t.id}

    await relay._handle({"channel": f"task:{t.id}",
                         "data": {"event_type": "user_message", "content": "x"}}, w)
    await relay._handle({"channel": "task:99999",
                         "data": {"event_type": "message", "content": "x"}}, w)

    async with session_factory() as db:
        count = len((await db.execute(select(LogEntry))).scalars().all())
    assert count == 0
    assert broadcaster.sent == []


async def test_relay_status_change_syncs_task(relay, session_factory):
    w = await _mk_worker(session_factory)
    t = await _mk_task(session_factory, worker_id=w.id)
    relay._tasks[w.id] = {t.id}
    relay._fetch_task_snapshot = AsyncMock(
        return_value=_remote_task(
            t,
            status="completed",
            completed_at=None,
        )
    )

    await relay._handle({
        "channel": "tasks",
        "data": {"event": "status_change", "task_id": t.id,
                 "old_status": "in_progress", "new_status": "completed"},
    }, w)
    async with session_factory() as db:
        current = await db.get(Task, t.id)
        assert current.status == "completed"
        assert current.completed_at is not None
        assert current.error_message is None


async def test_relay_conflict_is_terminal_with_timestamp_and_error(
    relay,
    session_factory,
):
    worker = await _mk_worker(session_factory)
    task = await _mk_task(
        session_factory,
        worker_id=worker.id,
        status="in_progress",
    )
    relay._tasks[worker.id] = {task.id}
    relay._fetch_task_snapshot = AsyncMock(
        return_value=_remote_task(
            task,
            status="conflict",
            completed_at=None,
            error_message=None,
        )
    )

    await relay._handle(
        {
            "channel": "tasks",
            "data": {
                "event": "status_change",
                "task_id": task.id,
                "new_status": "conflict",
            },
        },
        worker,
    )

    async with session_factory() as db:
        current = await db.get(Task, task.id)
    assert current.status == "conflict"
    assert current.completed_at is not None
    assert "conflict" in current.error_message


async def test_relay_plan_ready_fetches_content(relay, session_factory):
    w = await _mk_worker(session_factory)
    t = await _mk_task(session_factory, worker_id=w.id, mode="plan")
    relay._tasks[w.id] = {t.id}
    relay._fetch_task_snapshot = AsyncMock(
        return_value=_remote_task(
            t,
            status="plan_review",
            plan_content="THE PLAN",
        )
    )

    await relay._handle({
        "channel": "tasks",
        "data": {"event": "plan_ready", "task_id": t.id},
    }, w)
    async with session_factory() as db:
        task = await db.get(Task, t.id)
    assert task.plan_content == "THE PLAN"
    assert task.status == "plan_review"


async def test_relay_monitor_events_with_remote_id(relay, session_factory):
    w = await _mk_worker(session_factory)
    t = await _mk_task(session_factory, worker_id=w.id)
    relay._tasks[w.id] = {t.id}

    await relay._handle({
        "channel": f"task:{t.id}",
        "data": {"event": "monitor_session_created", "monitor_session_id": 5,
                 "description": "watch"},
    }, w)
    await relay._handle({
        "channel": f"task:{t.id}",
        "data": {"event": "monitor_check", "monitor_session_id": 5,
                 "check_number": 1, "status": "ok", "summary": "fine"},
    }, w)
    await relay._handle({
        "channel": f"task:{t.id}",
        "data": {"event": "monitor_session_status", "monitor_session_id": 5,
                 "status": "completed"},
    }, w)

    async with session_factory() as db:
        ms = (await db.execute(select(MonitorSession))).scalars().one()
        checks = (await db.execute(select(MonitorCheck))).scalars().all()
    assert ms.remote_id == 5
    assert ms.task_id == t.id
    assert ms.status == "completed"
    assert ms.last_summary == "fine"
    assert len(checks) == 1
    assert checks[0].monitor_session_id == ms.id  # 本地 id，不是 remote 的 5


async def test_relay_context_usage_syncs(relay, session_factory):
    w = await _mk_worker(session_factory)
    t = await _mk_task(session_factory, worker_id=w.id)
    relay._tasks[w.id] = {t.id}
    await relay._handle({
        "channel": f"task:{t.id}",
        "data": {"event_type": "context_usage", "input_tokens": 100, "context_window": 200000},
    }, w)
    async with session_factory() as db:
        task = await db.get(Task, t.id)
    assert task.context_window_usage == {"input_tokens": 100, "context_window": 200000}


async def test_delayed_worker_message_cannot_mark_reassigned_local_task_unread(
    relay,
    broadcaster,
    session_factory,
):
    worker = await _mk_worker(session_factory)
    task = await _mk_task(
        session_factory,
        worker_id=worker.id,
        status="in_progress",
        has_unread=False,
    )
    relay._tasks[worker.id] = {task.id}
    async with session_factory() as db:
        await db.execute(
            update(Task)
            .where(Task.id == task.id)
            .values(
                worker_id=None,
                status="executing",
                retry_count=Task.retry_count + 1,
                session_id="local-session",
            )
        )
        await db.commit()

    await relay._handle(
        {
            "channel": f"task:{task.id}",
            "data": {
                "event_type": "message",
                "role": "assistant",
                "content": "late Worker output",
            },
        },
        worker,
    )

    async with session_factory() as db:
        current = await db.get(Task, task.id)
        logs = (
            await db.execute(
                select(LogEntry).where(LogEntry.task_id == task.id)
            )
        ).scalars().all()
    assert current.worker_id is None
    assert current.status == "executing"
    assert current.has_unread is False
    assert current.session_id == "local-session"
    assert logs == []
    assert broadcaster.sent == []


async def test_delayed_worker_status_cannot_overwrite_reassigned_local_generation(
    relay,
    broadcaster,
    session_factory,
):
    worker = await _mk_worker(session_factory)
    task = await _mk_task(
        session_factory,
        worker_id=worker.id,
        status="in_progress",
    )
    relay._tasks[worker.id] = {task.id}
    fetch_started = asyncio.Event()
    release_fetch = asyncio.Event()

    async def delayed_snapshot(*_args, **_kwargs):
        fetch_started.set()
        await release_fetch.wait()
        return _remote_task(
            task,
            status="completed",
            session_id="old-worker-session",
            completed_at=None,
        )

    relay._fetch_task_snapshot = AsyncMock(side_effect=delayed_snapshot)
    handling = asyncio.create_task(
        relay._handle(
            {
                "channel": "tasks",
                "data": {
                    "event": "status_change",
                    "task_id": task.id,
                    "new_status": "completed",
                },
            },
            worker,
        )
    )
    await fetch_started.wait()
    async with session_factory() as db:
        await db.execute(
            update(Task)
            .where(Task.id == task.id)
            .values(
                worker_id=None,
                status="executing",
                retry_count=Task.retry_count + 1,
                session_id="local-session",
            )
        )
        await db.commit()
    release_fetch.set()
    await handling

    async with session_factory() as db:
        current = await db.get(Task, task.id)
    assert current.worker_id is None
    assert current.status == "executing"
    assert current.retry_count == task.retry_count + 1
    assert current.session_id == "local-session"
    assert current.completed_at is None
    assert broadcaster.sent == []


async def test_delayed_worker_status_cannot_overwrite_same_worker_retry_aba(
    relay,
    broadcaster,
    session_factory,
):
    worker = await _mk_worker(session_factory)
    task = await _mk_task(
        session_factory,
        worker_id=worker.id,
        status="in_progress",
    )
    relay._tasks[worker.id] = {task.id}
    fetch_started = asyncio.Event()
    release_fetch = asyncio.Event()

    async def delayed_snapshot(*_args, **_kwargs):
        fetch_started.set()
        await release_fetch.wait()
        return _remote_task(
            task,
            status="completed",
            session_id="old-session",
            completed_at=None,
        )

    relay._fetch_task_snapshot = AsyncMock(side_effect=delayed_snapshot)
    handling = asyncio.create_task(
        relay._handle(
            {
                "channel": "tasks",
                "data": {
                    "event": "status_change",
                    "task_id": task.id,
                    "new_status": "completed",
                },
            },
            worker,
        )
    )
    await fetch_started.wait()
    async with session_factory() as db:
        await db.execute(
            update(Task)
            .where(Task.id == task.id)
            .values(
                status="executing",
                retry_count=Task.retry_count + 1,
                session_id="new-session",
            )
        )
        await db.commit()
    release_fetch.set()
    await handling

    async with session_factory() as db:
        current = await db.get(Task, task.id)
    assert current.worker_id == worker.id
    assert current.status == "executing"
    assert current.retry_count == task.retry_count + 1
    assert current.session_id == "new-session"
    assert current.completed_at is None
    assert broadcaster.sent == []


async def test_worker_status_publication_fence_drops_superseded_result(
    relay,
    broadcaster,
    session_factory,
):
    worker = await _mk_worker(session_factory)
    task = await _mk_task(
        session_factory,
        worker_id=worker.id,
        status="in_progress",
    )
    relay._tasks[worker.id] = {task.id}
    relay._fetch_task_snapshot = AsyncMock(
        return_value=_remote_task(
            task,
            status="completed",
            completed_at=None,
        )
    )
    real_publish = relay._publish_status_generation

    async def retry_before_publication(generation, payload=None):
        async with session_factory() as db:
            await db.execute(
                update(Task)
                .where(Task.id == task.id)
                .values(
                    status="executing",
                    retry_count=Task.retry_count + 1,
                    completed_at=None,
                )
            )
            await db.commit()
        return await real_publish(generation, payload)

    relay._publish_status_generation = AsyncMock(
        side_effect=retry_before_publication
    )
    await relay._handle(
        {
            "channel": "tasks",
            "data": {
                "event": "status_change",
                "task_id": task.id,
                "new_status": "completed",
            },
        },
        worker,
    )

    async with session_factory() as db:
        current = await db.get(Task, task.id)
    assert current.status == "executing"
    assert current.retry_count == task.retry_count + 1
    assert current.completed_at is None
    assert broadcaster.sent == []


async def test_reconnect_backfill_cannot_write_after_task_moves_local(
    relay,
    broadcaster,
    session_factory,
    monkeypatch,
):
    worker = await _mk_worker(session_factory)
    task = await _mk_task(
        session_factory,
        worker_id=worker.id,
        status="in_progress",
    )
    history_started = asyncio.Event()
    release_history = asyncio.Event()

    class Response:
        def __init__(self, payload):
            self.status_code = 200
            self._payload = payload

        def json(self):
            return self._payload

    class Client:
        def __init__(self, *_args, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def get(self, url, **_kwargs):
            if "/chat/history" in url:
                history_started.set()
                await release_history.wait()
                return Response(
                    [
                        {
                            "event_type": "message",
                            "role": "assistant",
                            "content": "late history",
                        }
                    ]
                )
            return Response(
                _remote_task(
                    task,
                    status="completed",
                    session_id="old-worker-session",
                    completed_at=None,
                )
            )

    monkeypatch.setattr(worker_relay_module.httpx, "AsyncClient", Client)
    backfill = asyncio.create_task(
        relay._backfill_missing_logs(worker, {task.id})
    )
    await history_started.wait()
    async with session_factory() as db:
        await db.execute(
            update(Task)
            .where(Task.id == task.id)
            .values(
                worker_id=None,
                status="executing",
                retry_count=Task.retry_count + 1,
                session_id="local-session",
            )
        )
        await db.commit()
    release_history.set()
    await backfill

    async with session_factory() as db:
        current = await db.get(Task, task.id)
        logs = (
            await db.execute(
                select(LogEntry).where(LogEntry.task_id == task.id)
            )
        ).scalars().all()
    assert current.worker_id is None
    assert current.status == "executing"
    assert current.session_id == "local-session"
    assert logs == []
    assert broadcaster.sent == []


async def test_reconnect_exhaustion_cannot_fail_same_worker_retry(
    relay,
    broadcaster,
    session_factory,
    monkeypatch,
):
    worker = await _mk_worker(session_factory)
    task = await _mk_task(
        session_factory,
        worker_id=worker.id,
        status="executing",
    )
    relay._tasks[worker.id] = {task.id}
    retried = False

    async def fail_after_retry(_worker):
        nonlocal retried
        if not retried:
            retried = True
            async with session_factory() as db:
                await db.execute(
                    update(Task)
                    .where(Task.id == task.id)
                    .values(
                        status="executing",
                        retry_count=Task.retry_count + 1,
                        session_id="new-session",
                    )
                )
                await db.commit()
        raise OSError("still disconnected")

    relay.ensure_connection = AsyncMock(side_effect=fail_after_retry)
    monkeypatch.setattr(
        worker_relay_module.asyncio,
        "sleep",
        AsyncMock(),
    )
    await relay._reconnect(worker)

    async with session_factory() as db:
        current = await db.get(Task, task.id)
    assert relay.ensure_connection.await_count == 10
    assert current.worker_id == worker.id
    assert current.status == "executing"
    assert current.retry_count == task.retry_count + 1
    assert current.session_id == "new-session"
    assert current.completed_at is None
    assert current.error_message is None
    assert broadcaster.sent == []


async def test_reconnect_exhaustion_fails_only_exact_generation_and_publishes(
    relay,
    broadcaster,
    session_factory,
    monkeypatch,
):
    worker = await _mk_worker(session_factory)
    task = await _mk_task(
        session_factory,
        worker_id=worker.id,
        status="executing",
    )
    relay._tasks[worker.id] = {task.id}
    relay.ensure_connection = AsyncMock(
        side_effect=OSError("still disconnected")
    )
    monkeypatch.setattr(
        worker_relay_module.asyncio,
        "sleep",
        AsyncMock(),
    )

    await relay._reconnect(worker)

    async with session_factory() as db:
        current = await db.get(Task, task.id)
    assert current.status == "failed"
    assert current.completed_at is not None
    assert "无法重连" in current.error_message
    assert broadcaster.sent == [
        (
            "tasks",
            {
                "event": "status_change",
                "task_id": task.id,
                "new_status": "failed",
            },
        )
    ]


# === Dispatcher 双路径 ===


async def test_dispatch_worker_tasks_forwards(db_factory, session_factory, broadcaster, monkeypatch):
    from backend.services.dispatcher import GlobalDispatcher
    w = await _mk_worker(session_factory)
    t = await _mk_task(session_factory, worker_id=w.id, status="pending")

    proxy = AsyncMock()
    monkeypatch.setattr(main_module, "worker_proxy", proxy)
    disp = GlobalDispatcher.__new__(GlobalDispatcher)
    disp.db_factory = db_factory
    disp.broadcaster = broadcaster
    disp._running_tasks = {}

    await disp._dispatch_worker_tasks()
    # 等 fire-and-forget 的 forward 跑完
    for _ in range(10):
        await asyncio.sleep(0)

    async with session_factory() as db:
        task = await db.get(Task, t.id)
    assert task.status == "in_progress"
    proxy.forward_task_to_worker.assert_called_once()
    assert any(c == "tasks" and d.get("new_status") == "in_progress" for c, d in broadcaster.sent)


async def test_dispatch_worker_tasks_skips_unready_worker(db_factory, session_factory, broadcaster, monkeypatch):
    from backend.services.dispatcher import GlobalDispatcher
    w = await _mk_worker(session_factory, status="stopped")
    t = await _mk_task(session_factory, worker_id=w.id, status="pending")
    monkeypatch.setattr(main_module, "worker_proxy", AsyncMock())

    disp = GlobalDispatcher.__new__(GlobalDispatcher)
    disp.db_factory = db_factory
    disp.broadcaster = broadcaster
    disp._running_tasks = {}
    await disp._dispatch_worker_tasks()

    async with session_factory() as db:
        assert (await db.get(Task, t.id)).status == "pending"  # 留队等 worker 就绪


async def test_dispatch_worker_claim_rejects_same_worker_pending_retry_aba(
    session_factory,
    broadcaster,
    monkeypatch,
):
    from backend.services.dispatcher import GlobalDispatcher

    worker = await _mk_worker(session_factory)
    task = await _mk_task(
        session_factory,
        worker_id=worker.id,
        status="pending",
    )
    proxy = AsyncMock()
    monkeypatch.setattr(main_module, "worker_proxy", proxy)
    factory_calls = 0

    @asynccontextmanager
    async def racing_db_factory():
        nonlocal factory_calls
        factory_calls += 1
        async with session_factory() as db:
            if factory_calls == 4:
                await db.execute(
                    update(Task)
                    .where(Task.id == task.id)
                    .values(
                        retry_count=Task.retry_count + 1,
                        title="new retry generation",
                    )
                )
                await db.commit()
            yield db

    dispatcher = GlobalDispatcher.__new__(GlobalDispatcher)
    dispatcher.db_factory = racing_db_factory
    dispatcher.broadcaster = broadcaster
    dispatcher._running_tasks = {}

    await dispatcher._dispatch_worker_tasks()
    await asyncio.sleep(0)

    async with session_factory() as db:
        current = await db.get(Task, task.id)
    assert current.status == "pending"
    assert current.retry_count == task.retry_count + 1
    assert current.title == "new retry generation"
    proxy.forward_task_to_worker.assert_not_awaited()
    assert broadcaster.sent == []


async def test_dispatch_worker_claim_rejects_new_shared_shadow(
    session_factory,
    broadcaster,
    monkeypatch,
):
    from backend.services.dispatcher import GlobalDispatcher

    worker = await _mk_worker(session_factory)
    task = await _mk_task(
        session_factory,
        worker_id=worker.id,
        status="pending",
    )
    proxy = AsyncMock()
    monkeypatch.setattr(main_module, "worker_proxy", proxy)
    factory_calls = 0

    @asynccontextmanager
    async def racing_db_factory():
        nonlocal factory_calls
        factory_calls += 1
        async with session_factory() as db:
            if factory_calls == 4:
                await db.execute(
                    update(Task)
                    .where(Task.id == task.id)
                    .values(shared_from_id=987654)
                )
                await db.commit()
            yield db

    dispatcher = GlobalDispatcher.__new__(GlobalDispatcher)
    dispatcher.db_factory = racing_db_factory
    dispatcher.broadcaster = broadcaster
    dispatcher._running_tasks = {}

    await dispatcher._dispatch_worker_tasks()
    await asyncio.sleep(0)

    async with session_factory() as db:
        current = await db.get(Task, task.id)
    assert current.status == "pending"
    assert current.shared_from_id == 987654
    proxy.forward_task_to_worker.assert_not_awaited()
    assert broadcaster.sent == []


async def test_dispatch_worker_forwards_refreshed_claimed_task(
    session_factory,
    broadcaster,
    monkeypatch,
):
    from backend.services.dispatcher import GlobalDispatcher

    worker = await _mk_worker(session_factory)
    task = await _mk_task(
        session_factory,
        worker_id=worker.id,
        status="pending",
    )
    proxy = AsyncMock()
    monkeypatch.setattr(main_module, "worker_proxy", proxy)
    factory_calls = 0

    @asynccontextmanager
    async def racing_db_factory():
        nonlocal factory_calls
        factory_calls += 1
        async with session_factory() as db:
            if factory_calls == 4:
                await db.execute(
                    update(Task)
                    .where(Task.id == task.id)
                    .values(title="current title")
                )
                await db.commit()
            yield db

    dispatcher = GlobalDispatcher.__new__(GlobalDispatcher)
    dispatcher.db_factory = racing_db_factory
    dispatcher.broadcaster = broadcaster
    dispatcher._running_tasks = {}

    await dispatcher._dispatch_worker_tasks()
    forward = dispatcher._running_tasks.get(f"worker-{task.id}")
    assert forward is not None
    await forward

    forwarded_task = proxy.forward_task_to_worker.await_args.args[0]
    assert forwarded_task.title == "current title"


async def test_dispatch_worker_target_repo_fill_preserves_concurrent_project_edit(
    session_factory,
    broadcaster,
    monkeypatch,
):
    from backend.services.dispatcher import GlobalDispatcher

    worker = await _mk_worker(session_factory)
    async with session_factory() as db:
        original_project = Project(
            name="worker-original-project",
            local_path="/workspace/original",
            status="ready",
        )
        replacement_project = Project(
            name="worker-replacement-project",
            local_path="/workspace/replacement",
            status="ready",
        )
        db.add_all([original_project, replacement_project])
        await db.commit()
        await db.refresh(original_project)
        await db.refresh(replacement_project)
        original_project_id = original_project.id
        replacement_project_id = replacement_project.id
    task = await _mk_task(
        session_factory,
        worker_id=worker.id,
        status="pending",
        project_id=original_project_id,
        target_repo="",
    )
    proxy = AsyncMock()
    monkeypatch.setattr(main_module, "worker_proxy", proxy)
    factory_calls = 0

    @asynccontextmanager
    async def racing_db_factory():
        nonlocal factory_calls
        factory_calls += 1
        async with session_factory() as db:
            if factory_calls == 4:
                await db.execute(
                    update(Task)
                    .where(Task.id == task.id)
                    .values(
                        project_id=replacement_project_id,
                        target_repo="/workspace/user-choice",
                    )
                )
                await db.commit()
            yield db

    dispatcher = GlobalDispatcher.__new__(GlobalDispatcher)
    dispatcher.db_factory = racing_db_factory
    dispatcher.broadcaster = broadcaster
    dispatcher._running_tasks = {}

    await dispatcher._dispatch_worker_tasks()
    await asyncio.sleep(0)

    async with session_factory() as db:
        current = await db.get(Task, task.id)
    assert current.status == "pending"
    assert current.project_id == replacement_project_id
    assert current.target_repo == "/workspace/user-choice"
    proxy.forward_task_to_worker.assert_not_awaited()
    assert broadcaster.sent == []


async def test_dispatch_forward_failure_marks_failed(db_factory, session_factory, broadcaster, monkeypatch):
    from backend.services.dispatcher import GlobalDispatcher
    w = await _mk_worker(session_factory)
    t = await _mk_task(session_factory, worker_id=w.id, status="pending")

    proxy = AsyncMock()
    proxy.forward_task_to_worker.side_effect = RuntimeError("boom")
    monkeypatch.setattr(main_module, "worker_proxy", proxy)
    disp = GlobalDispatcher.__new__(GlobalDispatcher)
    disp.db_factory = db_factory
    disp.broadcaster = broadcaster
    disp._running_tasks = {}

    await disp._dispatch_worker_tasks()
    # _safe_forward_to_worker 带 3 次指数退避重试（1s+2s），直接 await 转发
    # 任务跑完全部重试（done_callback 会 pop，所以先取引用）
    fwd = disp._running_tasks.get(f"worker-{t.id}")
    assert fwd is not None
    await fwd

    async with session_factory() as db:
        task = await db.get(Task, t.id)
    assert task.status == "failed"
    assert "转发到 Worker 失败" in task.error_message


async def test_old_worker_forward_failure_cannot_fail_reclaimed_generation(
    db_factory,
    session_factory,
    broadcaster,
    monkeypatch,
):
    """The async forwarder is fenced to the claim created for that request."""

    import backend.services.dispatcher as dispatcher_module
    from backend.services.dispatcher import GlobalDispatcher

    worker = await _mk_worker(session_factory)
    task = await _mk_task(
        session_factory,
        worker_id=worker.id,
        status="in_progress",
    )
    proxy = AsyncMock()
    proxy.forward_task_to_worker.side_effect = RuntimeError("boom")
    monkeypatch.setattr(main_module, "worker_proxy", proxy)
    monkeypatch.setattr(
        dispatcher_module.asyncio,
        "sleep",
        AsyncMock(),
    )

    dispatcher = GlobalDispatcher.__new__(GlobalDispatcher)
    dispatcher.db_factory = db_factory
    dispatcher.broadcaster = broadcaster
    async with db_factory() as db:
        claimed = await db.get(Task, task.id)
        old_generation = dispatcher._task_status_generation(claimed)
        await db.execute(
            update(Task)
            .where(Task.id == task.id)
            .values(retry_count=Task.retry_count + 1)
        )
        await db.commit()

    await dispatcher._safe_forward_to_worker(task, old_generation)

    async with db_factory() as db:
        current = await db.get(Task, task.id)
        assert current.status == "in_progress"
        assert current.retry_count == old_generation.retry_count + 1
    assert not any(
        channel == "tasks" and payload.get("new_status") == "failed"
        for channel, payload in broadcaster.sent
    )


# === API 代理 ===


class _ProxyResponse:
    def __init__(self, status_code: int, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


class _InvalidJSONProxyResponse(_ProxyResponse):
    def json(self):
        raise ValueError("invalid JSON")


def _install_proxy_transport(monkeypatch, outcome):
    requests = []

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            self.timeout = kwargs.get("timeout")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def request(self, method, url, **kwargs):
            requests.append((method, url, kwargs))
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

    monkeypatch.setattr(worker_proxy_module.httpx, "AsyncClient", FakeAsyncClient)
    return requests


@pytest.mark.parametrize("remote_status", [401, 403])
async def test_generic_worker_proxy_hides_internal_auth_failures(
    session_factory, monkeypatch, remote_status,
):
    worker = await _mk_worker(session_factory)
    task = await _mk_task(session_factory, worker_id=worker.id)
    requests = _install_proxy_transport(
        monkeypatch,
        _ProxyResponse(remote_status, {"detail": "secret Worker auth diagnostic"}),
    )
    proxy = WorkerProxy(session_factory, AsyncMock())

    with pytest.raises(HTTPException) as caught:
        await proxy.proxy_to_worker(task, "POST", f"/api/tasks/{task.id}/retry")

    assert caught.value.status_code == 502
    assert "内部 Worker 认证失败" in caught.value.detail
    assert str(remote_status) in caught.value.detail
    assert "secret Worker auth diagnostic" not in caught.value.detail
    assert requests[0][2]["headers"] == {"Authorization": "Bearer wtoken"}


async def test_generic_worker_proxy_can_require_json_confirmation(
    session_factory,
    monkeypatch,
):
    worker = await _mk_worker(session_factory)
    task = await _mk_task(session_factory, worker_id=worker.id)
    _install_proxy_transport(
        monkeypatch,
        _InvalidJSONProxyResponse(200, "not-json"),
    )
    proxy = WorkerProxy(session_factory, AsyncMock())

    with pytest.raises(HTTPException) as caught:
        await proxy.proxy_to_worker(
            task,
            "DELETE",
            f"/api/tasks/{task.id}",
            require_json=True,
        )

    assert caught.value.status_code == 502
    assert "invalid confirmation" in caught.value.detail


async def test_generic_worker_proxy_can_confirm_task_already_absent(
    session_factory,
    monkeypatch,
):
    worker = await _mk_worker(session_factory)
    task = await _mk_task(session_factory, worker_id=worker.id)
    _install_proxy_transport(
        monkeypatch,
        _ProxyResponse(404, {"detail": "Task not found"}),
    )
    proxy = WorkerProxy(session_factory, AsyncMock())

    result = await proxy.proxy_to_worker(
        task,
        "DELETE",
        f"/api/tasks/{task.id}",
        require_json=True,
        allow_task_absent=True,
    )

    assert result == {"ok": True, "already_deleted": True}


@pytest.mark.parametrize(
    ("transport_error", "expected_status", "expected_detail"),
    [
        (httpx.ConnectError("private address unreachable"), 502, "Worker 网关连接失败"),
        (httpx.ReadTimeout("Worker stalled"), 503, "Worker w1 请求超时"),
    ],
)
async def test_generic_worker_proxy_maps_transport_failures(
    session_factory,
    monkeypatch,
    transport_error,
    expected_status,
    expected_detail,
):
    worker = await _mk_worker(session_factory)
    task = await _mk_task(session_factory, worker_id=worker.id)
    _install_proxy_transport(monkeypatch, transport_error)
    proxy = WorkerProxy(session_factory, AsyncMock())

    with pytest.raises(HTTPException) as caught:
        await proxy.proxy_to_worker(task, "POST", f"/api/tasks/{task.id}/retry")

    assert caught.value.status_code == expected_status
    assert expected_detail in caught.value.detail
    assert str(transport_error) not in caught.value.detail


@pytest.mark.parametrize("remote_status", [302, 400, 404, 429, 500, 503])
async def test_generic_worker_proxy_hides_other_upstream_error_bodies(
    session_factory, monkeypatch, remote_status,
):
    worker = await _mk_worker(session_factory)
    task = await _mk_task(session_factory, worker_id=worker.id)
    _install_proxy_transport(
        monkeypatch,
        _ProxyResponse(remote_status, {"detail": "sensitive Worker traceback"}),
    )
    proxy = WorkerProxy(session_factory, AsyncMock())

    with pytest.raises(HTTPException) as caught:
        await proxy.proxy_to_worker(task, "POST", f"/api/tasks/{task.id}/retry")

    assert caught.value.status_code == 502
    assert f"远端 HTTP {remote_status}" in caught.value.detail
    assert "sensitive Worker traceback" not in caught.value.detail


async def test_create_task_with_worker_id_and_explicit_id(client, session_factory):
    resp = await client.post("/api/tasks", json={
        "id": 4242, "worker_id": 3, "title": "x", "description": "remote",
    })
    assert resp.status_code in (200, 201), resp.text
    data = resp.json()
    assert data["id"] == 4242
    assert data["worker_id"] == 3


async def test_proxy_terminal_response_commits_normalized_generation_then_publishes(
    client,
    session_factory,
    monkeypatch,
):
    worker = await _mk_worker(session_factory)
    task = await _mk_task(
        session_factory,
        worker_id=worker.id,
        status="in_progress",
        error_message="stale error",
    )
    proxy = AsyncMock()
    proxy.proxy_to_worker.return_value = _remote_task(
        task,
        status="completed",
        completed_at=None,
        error_message=None,
    )
    local_broadcaster = FakeBroadcaster()
    status_broadcast = AsyncMock()
    monkeypatch.setattr(main_module, "worker_proxy", proxy)
    monkeypatch.setattr(main_module, "broadcaster", local_broadcaster)
    monkeypatch.setattr(
        task_events_module,
        "broadcast_status_change",
        status_broadcast,
    )

    response = await client.post(f"/api/tasks/{task.id}/cancel")

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "completed"
    assert response.json()["completed_at"] is not None
    assert response.json()["error_message"] is None
    async with session_factory() as db:
        current = await db.get(Task, task.id)
    assert current.completed_at is not None
    assert current.error_message is None
    status_broadcast.assert_awaited_once_with(task.id, "completed")


async def test_proxy_response_cannot_overwrite_task_reassigned_during_request(
    client,
    session_factory,
    monkeypatch,
):
    worker = await _mk_worker(session_factory)
    task = await _mk_task(
        session_factory,
        worker_id=worker.id,
        status="in_progress",
    )

    async def move_local_before_response(*_args, **_kwargs):
        async with session_factory() as db:
            await db.execute(
                update(Task)
                .where(Task.id == task.id)
                .values(
                    worker_id=None,
                    status="executing",
                    retry_count=Task.retry_count + 1,
                    session_id="local-session",
                )
            )
            await db.commit()
        return _remote_task(
            task,
            status="cancelled",
            session_id="old-worker-session",
            completed_at=None,
        )

    proxy = AsyncMock()
    proxy.proxy_to_worker.side_effect = move_local_before_response
    local_broadcaster = FakeBroadcaster()
    status_broadcast = AsyncMock()
    monkeypatch.setattr(main_module, "worker_proxy", proxy)
    monkeypatch.setattr(main_module, "broadcaster", local_broadcaster)
    monkeypatch.setattr(
        task_events_module,
        "broadcast_status_change",
        status_broadcast,
    )

    response = await client.post(f"/api/tasks/{task.id}/cancel")

    assert response.status_code == 409, response.text
    async with session_factory() as db:
        current = await db.get(Task, task.id)
    assert current.worker_id is None
    assert current.status == "executing"
    assert current.retry_count == task.retry_count + 1
    assert current.session_id == "local-session"
    assert current.completed_at is None
    status_broadcast.assert_not_awaited()
    assert local_broadcaster.sent == []


async def test_proxy_response_cannot_overwrite_same_worker_retry_aba(
    client,
    session_factory,
    monkeypatch,
):
    worker = await _mk_worker(session_factory)
    task = await _mk_task(
        session_factory,
        worker_id=worker.id,
        status="failed",
        error_message="old failure",
    )

    async def retry_completes_before_old_response(*_args, **_kwargs):
        async with session_factory() as db:
            await db.execute(
                update(Task)
                .where(Task.id == task.id)
                .values(
                    status="executing",
                    retry_count=Task.retry_count + 1,
                    session_id="new-session",
                    error_message=None,
                    completed_at=None,
                )
            )
            await db.commit()
        return _remote_task(
            task,
            status="pending",
            retry_count=task.retry_count + 1,
            session_id=None,
            error_message=None,
            completed_at=None,
        )

    proxy = AsyncMock()
    proxy.proxy_to_worker.side_effect = retry_completes_before_old_response
    local_broadcaster = FakeBroadcaster()
    status_broadcast = AsyncMock()
    monkeypatch.setattr(main_module, "worker_proxy", proxy)
    monkeypatch.setattr(main_module, "broadcaster", local_broadcaster)
    monkeypatch.setattr(
        task_events_module,
        "broadcast_status_change",
        status_broadcast,
    )

    response = await client.post(f"/api/tasks/{task.id}/retry")

    assert response.status_code == 409, response.text
    async with session_factory() as db:
        current = await db.get(Task, task.id)
    assert current.worker_id == worker.id
    assert current.status == "executing"
    assert current.retry_count == task.retry_count + 1
    assert current.session_id == "new-session"
    assert current.error_message is None
    assert current.completed_at is None
    status_broadcast.assert_not_awaited()
    assert local_broadcaster.sent == []


async def test_proxy_status_publication_fence_miss_returns_conflict(
    client,
    session_factory,
    monkeypatch,
):
    worker = await _mk_worker(session_factory)
    task = await _mk_task(
        session_factory,
        worker_id=worker.id,
        status="in_progress",
    )
    proxy = AsyncMock()
    proxy.proxy_to_worker.return_value = _remote_task(
        task,
        status="completed",
        completed_at=None,
    )
    local_broadcaster = FakeBroadcaster()
    status_broadcast = AsyncMock()
    real_apply = worker_relay_module.apply_authoritative_worker_task

    async def replace_after_authoritative_commit(db, observed, result):
        resulting = await real_apply(db, observed, result)
        assert resulting is not None
        async with session_factory() as replacement_db:
            await replacement_db.execute(
                update(Task)
                .where(Task.id == task.id)
                .values(
                    status="executing",
                    retry_count=Task.retry_count + 1,
                    completed_at=None,
                )
            )
            await replacement_db.commit()
        return resulting

    monkeypatch.setattr(main_module, "worker_proxy", proxy)
    monkeypatch.setattr(main_module, "broadcaster", local_broadcaster)
    monkeypatch.setattr(
        "backend.api.tasks.apply_authoritative_worker_task",
        replace_after_authoritative_commit,
    )
    monkeypatch.setattr(
        task_events_module,
        "broadcast_status_change",
        status_broadcast,
    )

    response = await client.post(f"/api/tasks/{task.id}/cancel")

    assert response.status_code == 409, response.text
    async with session_factory() as db:
        current = await db.get(Task, task.id)
    assert current.status == "executing"
    assert current.retry_count == task.retry_count + 1
    assert current.completed_at is None
    status_broadcast.assert_not_awaited()
    assert local_broadcaster.sent == []


async def test_proxy_response_without_remote_generation_fails_closed(
    client,
    session_factory,
    monkeypatch,
):
    worker = await _mk_worker(session_factory)
    task = await _mk_task(
        session_factory,
        worker_id=worker.id,
        status="in_progress",
    )
    proxy = AsyncMock()
    proxy.proxy_to_worker.return_value = {
        "id": task.id,
        "status": "cancelled",
        # retry_count intentionally absent: this cannot identify a remote
        # generation on the same Worker.
    }
    local_broadcaster = FakeBroadcaster()
    status_broadcast = AsyncMock()
    monkeypatch.setattr(main_module, "worker_proxy", proxy)
    monkeypatch.setattr(main_module, "broadcaster", local_broadcaster)
    monkeypatch.setattr(
        task_events_module,
        "broadcast_status_change",
        status_broadcast,
    )

    response = await client.post(f"/api/tasks/{task.id}/cancel")

    assert response.status_code == 409, response.text
    async with session_factory() as db:
        current = await db.get(Task, task.id)
    assert current.status == "in_progress"
    assert current.completed_at is None
    status_broadcast.assert_not_awaited()
    assert local_broadcaster.sent == []


@pytest.mark.parametrize(
    "remote_overrides",
    [
        {"status": "not-a-task-status", "retry_count": 2},
        {"status": "cancelled", "retry_count": 1},
    ],
)
async def test_proxy_response_rejects_malformed_or_regressed_generation(
    client,
    session_factory,
    monkeypatch,
    remote_overrides,
):
    worker = await _mk_worker(session_factory)
    task = await _mk_task(
        session_factory,
        worker_id=worker.id,
        status="in_progress",
        retry_count=2,
    )
    proxy = AsyncMock()
    proxy.proxy_to_worker.return_value = _remote_task(
        task,
        completed_at=None,
        **remote_overrides,
    )
    status_broadcast = AsyncMock()
    monkeypatch.setattr(main_module, "worker_proxy", proxy)
    monkeypatch.setattr(main_module, "broadcaster", FakeBroadcaster())
    monkeypatch.setattr(
        task_events_module,
        "broadcast_status_change",
        status_broadcast,
    )

    response = await client.post(f"/api/tasks/{task.id}/cancel")

    assert response.status_code == 409, response.text
    async with session_factory() as db:
        current = await db.get(Task, task.id)
    assert current.status == "in_progress"
    assert current.retry_count == 2
    assert current.completed_at is None
    status_broadcast.assert_not_awaited()


async def test_proxy_response_cannot_overwrite_new_shared_shadow_authority(
    client,
    session_factory,
    monkeypatch,
):
    worker = await _mk_worker(session_factory)
    task = await _mk_task(
        session_factory,
        worker_id=worker.id,
        status="in_progress",
    )

    async def become_shared_before_response(*_args, **_kwargs):
        async with session_factory() as db:
            await db.execute(
                update(Task)
                .where(Task.id == task.id)
                .values(shared_from_id=987654)
            )
            await db.commit()
        return _remote_task(
            task,
            status="cancelled",
            completed_at=None,
        )

    proxy = AsyncMock()
    proxy.proxy_to_worker.side_effect = become_shared_before_response
    local_broadcaster = FakeBroadcaster()
    status_broadcast = AsyncMock()
    monkeypatch.setattr(main_module, "worker_proxy", proxy)
    monkeypatch.setattr(main_module, "broadcaster", local_broadcaster)
    monkeypatch.setattr(
        task_events_module,
        "broadcast_status_change",
        status_broadcast,
    )

    response = await client.post(f"/api/tasks/{task.id}/cancel")

    assert response.status_code == 409, response.text
    async with session_factory() as db:
        current = await db.get(Task, task.id)
    assert current.shared_from_id == 987654
    assert current.status == "in_progress"
    assert current.completed_at is None
    status_broadcast.assert_not_awaited()
    assert local_broadcaster.sent == []


async def test_local_dequeue_skips_worker_tasks(session_factory):
    from backend.services.task_queue import TaskQueue
    await _mk_task(session_factory, status="pending", worker_id=1)
    local = await _mk_task(session_factory, status="pending")
    async with session_factory() as db:
        q = TaskQueue(db)
        got = await q.dequeue()
    assert got is not None and got.id == local.id


async def test_chat_proxy_for_worker_task(client, session_factory, monkeypatch):
    w = await _mk_worker(session_factory)
    t = await _mk_task(session_factory, worker_id=w.id)

    proxy = AsyncMock()
    proxy.require_ready_worker.return_value = w
    proxy.relay = AsyncMock()
    proxy.proxy_to_worker.return_value = {"ok": True, "queued": True, "session_id": "sess-1"}
    monkeypatch.setattr(main_module, "worker_proxy", proxy)
    monkeypatch.setattr(main_module, "broadcaster", FakeBroadcaster())

    resp = await client.post(f"/api/tasks/{t.id}/chat", json={"message": "hello worker"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["session_id"] == "sess-1"
    assert body["instance_id"] is None

    async with session_factory() as db:
        logs = (await db.execute(
            select(LogEntry).where(LogEntry.task_id == t.id,
                                   LogEntry.event_type == "user_message")
        )).scalars().all()
        task = await db.get(Task, t.id)
    assert len(logs) == 1 and logs[0].instance_id is None
    assert task.session_id == "sess-1"
    proxy.proxy_to_worker.assert_called_once()
    assert (
        proxy.proxy_to_worker.call_args.kwargs["operation_lock_held"]
        is True
    )


async def test_worker_chat_response_cannot_overwrite_retry_aba(
    client,
    session_factory,
    monkeypatch,
):
    worker = await _mk_worker(session_factory)
    task = await _mk_task(
        session_factory,
        worker_id=worker.id,
        status="completed",
        session_id="old-session",
    )

    async def replace_generation_before_response(*_args, **_kwargs):
        async with session_factory() as db:
            await db.execute(
                update(Task)
                .where(Task.id == task.id)
                .values(
                    retry_count=Task.retry_count + 1,
                    session_id="new-session",
                )
            )
            await db.commit()
        return {
            "ok": True,
            "queued": True,
            "session_id": "stale-worker-session",
        }

    proxy = AsyncMock()
    proxy.require_ready_worker.return_value = worker
    proxy.relay = AsyncMock()
    proxy.proxy_to_worker.side_effect = replace_generation_before_response
    monkeypatch.setattr(main_module, "worker_proxy", proxy)
    monkeypatch.setattr(main_module, "broadcaster", FakeBroadcaster())

    response = await client.post(
        f"/api/tasks/{task.id}/chat",
        json={"message": "old generation chat"},
    )

    assert response.status_code == 409
    async with session_factory() as db:
        current = await db.get(Task, task.id)
    assert current.retry_count == task.retry_count + 1
    assert current.session_id == "new-session"
    assert (
        proxy.proxy_to_worker.call_args.kwargs["operation_lock_held"]
        is True
    )


async def test_worker_chat_sender_prefix_is_display_only(session_factory, monkeypatch):
    """Manager displays the sender, while the Worker receives raw model text."""
    import json
    from types import SimpleNamespace

    from backend.api.chat import ChatMessage, _send_worker_chat
    from backend.models.user import User

    w = await _mk_worker(session_factory)
    t = await _mk_task(session_factory, worker_id=w.id)
    async with session_factory() as db:
        sender = User(
            email="worker-prefix@test.local",
            name="Worker Alice",
            password_hash="unused",
            role="super_admin",
        )
        db.add(sender)
        await db.commit()
        await db.refresh(sender)

    proxy = AsyncMock()
    proxy.require_ready_worker.return_value = w
    proxy.relay = AsyncMock()
    proxy.proxy_to_worker.return_value = {
        "ok": True, "queued": True, "session_id": "worker-prefix-session",
    }
    broadcaster = FakeBroadcaster()
    monkeypatch.setattr(main_module, "worker_proxy", proxy)
    monkeypatch.setattr(main_module, "broadcaster", broadcaster)

    request = SimpleNamespace(
        state=SimpleNamespace(user_id=sender.id, user_role="super_admin")
    )
    async with session_factory() as db:
        task = await db.get(Task, t.id)
        await _send_worker_chat(
            task,
            ChatMessage(message="[FIX] preserve this tag"),
            db,
            request,
        )

    forwarded = proxy.proxy_to_worker.call_args.kwargs["body"]
    assert forwarded["message"] == "[FIX] preserve this tag"
    async with session_factory() as db:
        stored = (await db.execute(
            select(LogEntry).where(
                LogEntry.task_id == t.id,
                LogEntry.event_type == "user_message",
            )
        )).scalar_one()
    assert stored.content == "[Worker Alice] [FIX] preserve this tag"
    assert json.loads(stored.raw_json)["raw_content"] == "[FIX] preserve this tag"
    assert broadcaster.sent[0][1]["content"] == stored.content


async def test_chat_proxy_rejects_secrets(client, session_factory, monkeypatch):
    w = await _mk_worker(session_factory)
    t = await _mk_task(session_factory, worker_id=w.id)
    monkeypatch.setattr(main_module, "worker_proxy", AsyncMock())
    resp = await client.post(f"/api/tasks/{t.id}/chat",
                             json={"message": "x", "secret_ids": [1]})
    assert resp.status_code == 400


async def test_worker_retry_rejects_migrating_without_remote_mutation(
    client,
    session_factory,
    monkeypatch,
):
    worker = await _mk_worker(session_factory)
    task = await _mk_task(
        session_factory,
        worker_id=worker.id,
        status="migrating",
    )
    proxy = AsyncMock()
    monkeypatch.setattr(main_module, "worker_proxy", proxy)

    response = await client.post(f"/api/tasks/{task.id}/retry")

    assert response.status_code == 409
    proxy.proxy_to_worker.assert_not_awaited()
    async with session_factory() as db:
        current = await db.get(Task, task.id)
    assert current.status == "migrating"
    assert current.retry_count == task.retry_count


async def test_stop_session_proxies_for_worker_task(client, session_factory, monkeypatch):
    w = await _mk_worker(session_factory)
    t = await _mk_task(session_factory, worker_id=w.id)
    proxy = AsyncMock()
    proxy.proxy_to_worker.return_value = {"ok": True, "stopped": True, "cleared_messages": 0}
    monkeypatch.setattr(main_module, "worker_proxy", proxy)

    resp = await client.post(f"/api/tasks/{t.id}/stop-session")
    assert resp.status_code == 200
    assert resp.json()["stopped"] is True
    proxy.proxy_to_worker.assert_called_once()
    method, path = proxy.proxy_to_worker.call_args.args[1:3]
    assert method == "POST" and path == f"/api/tasks/{t.id}/stop-session"


async def test_delete_worker_task_remote_first_then_cleans_exact_manager_mirror(
    client,
    session_factory,
    monkeypatch,
):
    worker = await _mk_worker(session_factory)
    task = await _mk_task(
        session_factory,
        worker_id=worker.id,
        status="completed",
    )
    async with session_factory() as db:
        log = LogEntry(
            task_id=task.id,
            event_type="message",
            content="remote result",
        )
        monitor = MonitorSession(
            task_id=task.id,
            remote_id=17,
            description="stale mirror",
            status="running",
        )
        db.add_all([log, monitor])
        await db.flush()
        db.add(
            MonitorCheck(
                monitor_session_id=monitor.id,
                check_number=1,
                status="ok",
            )
        )
        await db.commit()

    proxy = AsyncMock()
    proxy.proxy_to_worker.return_value = {"ok": True}
    proxy.relay = Mock()
    proxy.task_operation_lock = Mock(return_value=asyncio.Lock())
    monkeypatch.setattr(main_module, "worker_proxy", proxy)
    monkeypatch.setattr(main_module, "task_migrator", None)

    response = await client.delete(f"/api/tasks/{task.id}")

    assert response.status_code == 200, response.text
    proxy.proxy_to_worker.assert_awaited_once()
    method, path = proxy.proxy_to_worker.call_args.args[1:3]
    assert method == "DELETE"
    assert path == f"/api/tasks/{task.id}"
    assert proxy.proxy_to_worker.call_args.kwargs == {
        "require_json": True,
        "allow_task_absent": True,
        "operation_lock_held": True,
    }
    proxy.relay.unsubscribe_task.assert_called_once_with(worker.id, task.id)
    async with session_factory() as db:
        assert await db.get(Task, task.id) is None
        assert not (
            await db.execute(
                select(LogEntry).where(LogEntry.task_id == task.id)
            )
        ).scalars().all()
        assert not (
            await db.execute(
                select(MonitorSession).where(
                    MonitorSession.task_id == task.id
                )
            )
        ).scalars().all()
        assert not (await db.execute(select(MonitorCheck))).scalars().all()


async def test_delete_worker_task_retry_converges_when_remote_is_already_absent(
    client,
    session_factory,
    monkeypatch,
):
    worker = await _mk_worker(session_factory)
    task = await _mk_task(
        session_factory,
        worker_id=worker.id,
        status="completed",
    )
    requests = _install_proxy_transport(
        monkeypatch,
        _ProxyResponse(404, {"detail": "Task not found"}),
    )
    relay = Mock()
    relay.subscribe_task = AsyncMock()
    proxy = WorkerProxy(session_factory, relay)
    monkeypatch.setattr(main_module, "worker_proxy", proxy)
    monkeypatch.setattr(main_module, "task_migrator", None)

    response = await client.delete(f"/api/tasks/{task.id}")

    assert response.status_code == 200, response.text
    assert requests[0][0] == "DELETE"
    relay.unsubscribe_task.assert_called_once_with(worker.id, task.id)
    async with session_factory() as db:
        assert await db.get(Task, task.id) is None


async def test_delete_worker_task_does_not_treat_unrelated_404_as_confirmation(
    client,
    session_factory,
    monkeypatch,
):
    worker = await _mk_worker(session_factory)
    task = await _mk_task(
        session_factory,
        worker_id=worker.id,
        status="completed",
    )
    _install_proxy_transport(
        monkeypatch,
        _ProxyResponse(404, {"detail": "Route not found"}),
    )
    relay = Mock()
    relay.subscribe_task = AsyncMock()
    proxy = WorkerProxy(session_factory, relay)
    monkeypatch.setattr(main_module, "worker_proxy", proxy)
    monkeypatch.setattr(main_module, "task_migrator", None)

    response = await client.delete(f"/api/tasks/{task.id}")

    assert response.status_code == 502
    relay.unsubscribe_task.assert_not_called()
    async with session_factory() as db:
        assert await db.get(Task, task.id) is not None


@pytest.mark.parametrize(
    "remote_outcome",
    [
        HTTPException(502, "Worker unreachable"),
        {"ok": False},
        {"deleted": True},
    ],
)
async def test_delete_worker_task_preserves_manager_mirror_without_confirmation(
    client,
    session_factory,
    monkeypatch,
    remote_outcome,
):
    worker = await _mk_worker(session_factory)
    task = await _mk_task(
        session_factory,
        worker_id=worker.id,
        status="failed",
    )
    async with session_factory() as db:
        db.add(
            LogEntry(
                task_id=task.id,
                event_type="system_event",
                content="retain me",
            )
        )
        await db.commit()

    proxy = AsyncMock()
    if isinstance(remote_outcome, BaseException):
        proxy.proxy_to_worker.side_effect = remote_outcome
    else:
        proxy.proxy_to_worker.return_value = remote_outcome
    proxy.relay = Mock()
    proxy.task_operation_lock = Mock(return_value=asyncio.Lock())
    monkeypatch.setattr(main_module, "worker_proxy", proxy)
    monkeypatch.setattr(main_module, "task_migrator", None)

    response = await client.delete(f"/api/tasks/{task.id}")

    assert response.status_code == 502
    proxy.relay.unsubscribe_task.assert_not_called()
    async with session_factory() as db:
        assert await db.get(Task, task.id) is not None
        assert (
            await db.execute(
                select(LogEntry).where(LogEntry.task_id == task.id)
            )
        ).scalars().one().content == "retain me"


async def test_delete_worker_task_converges_after_stale_relay_generation_update(
    client,
    session_factory,
    monkeypatch,
):
    worker = await _mk_worker(session_factory)
    task = await _mk_task(
        session_factory,
        worker_id=worker.id,
        status="completed",
    )

    async def remote_delete_then_relay_new_generation(*_args, **_kwargs):
        async with session_factory() as db:
            await db.execute(
                update(Task)
                .where(Task.id == task.id)
                .values(
                    status="in_progress",
                    retry_count=Task.retry_count + 1,
                )
            )
            await db.commit()
        return {"ok": True}

    proxy = AsyncMock()
    proxy.proxy_to_worker.side_effect = remote_delete_then_relay_new_generation
    proxy.relay = Mock()
    proxy.task_operation_lock = Mock(return_value=asyncio.Lock())
    monkeypatch.setattr(main_module, "worker_proxy", proxy)
    monkeypatch.setattr(main_module, "task_migrator", None)

    response = await client.delete(f"/api/tasks/{task.id}")

    assert response.status_code == 200, response.text
    proxy.relay.unsubscribe_task.assert_called_once_with(worker.id, task.id)
    async with session_factory() as db:
        assert await db.get(Task, task.id) is None


async def test_delete_worker_task_preserves_mirror_moved_to_another_worker(
    client,
    session_factory,
    monkeypatch,
):
    source = await _mk_worker(session_factory)
    destination = await _mk_worker(
        session_factory,
        private_ip="10.0.0.10",
    )
    task = await _mk_task(
        session_factory,
        worker_id=source.id,
        status="completed",
    )

    async def remote_delete_then_move_mirror(*_args, **_kwargs):
        async with session_factory() as db:
            await db.execute(
                update(Task)
                .where(Task.id == task.id)
                .values(worker_id=destination.id)
            )
            await db.commit()
        return {"ok": True}

    proxy = AsyncMock()
    proxy.proxy_to_worker.side_effect = remote_delete_then_move_mirror
    proxy.relay = Mock()
    proxy.task_operation_lock = Mock(return_value=asyncio.Lock())
    monkeypatch.setattr(main_module, "worker_proxy", proxy)
    monkeypatch.setattr(main_module, "task_migrator", None)

    response = await client.delete(f"/api/tasks/{task.id}")

    assert response.status_code == 409, response.text
    proxy.relay.unsubscribe_task.assert_called_once_with(source.id, task.id)
    async with session_factory() as db:
        preserved = await db.get(Task, task.id)
        assert preserved is not None
        assert preserved.worker_id == destination.id


async def test_monitor_delete_translates_remote_id(client, session_factory, monkeypatch):
    w = await _mk_worker(session_factory)
    t = await _mk_task(session_factory, worker_id=w.id)
    async with session_factory() as db:
        ms = MonitorSession(task_id=t.id, remote_id=5, description="m", status="running")
        db.add(ms)
        await db.commit()
        await db.refresh(ms)

    proxy = AsyncMock()
    proxy.proxy_to_worker.return_value = {"ok": True}
    monkeypatch.setattr(main_module, "worker_proxy", proxy)

    resp = await client.delete(f"/api/tasks/{t.id}/monitor-sessions/{ms.id}")
    assert resp.status_code == 200
    path = proxy.proxy_to_worker.call_args.args[2]
    assert path.endswith("/monitor-sessions/5")  # 用 remote_id，不是本地 id
    async with session_factory() as db:
        assert (await db.get(MonitorSession, ms.id)).status == "cancelled"


# === WS 认证 ===


async def test_ws_token_check():
    from backend.api.ws import _ws_token_ok
    from backend.config import settings

    class FakeWS:
        def __init__(self, headers=None, qp=None):
            self.headers = headers or {}
            self.query_params = qp or {}

    old = settings.auth_token
    try:
        settings.auth_token = ""
        assert _ws_token_ok(FakeWS()) is True  # 未配置 token 放行
        settings.auth_token = "secret"
        assert _ws_token_ok(FakeWS()) is False
        assert _ws_token_ok(FakeWS(headers={"authorization": "Bearer secret"})) is True
        assert _ws_token_ok(FakeWS(qp={"token": "secret"})) is True
        assert _ws_token_ok(FakeWS(qp={"token": "wrong"})) is False
    finally:
        settings.auth_token = old


# ---------------------------------------------------------------------------
# Backfill dedup — duplicate-message-on-reconnect regression
# ---------------------------------------------------------------------------

def _entry(et="message", role="assistant", content=None, tool_name=None,
           tool_input=None, tool_output=None, loop_iteration=None):
    return {
        "event_type": et, "role": role, "content": content,
        "tool_name": tool_name, "tool_input": tool_input,
        "tool_output": tool_output, "loop_iteration": loop_iteration,
    }


class TestBackfillDedup:
    """`_missing_by_fingerprint` must not re-insert already-present entries —
    the count-based `remote[local_count:]` slicing did exactly that whenever a
    gap was mid-stream (not tail-only) or the live relay raced the backfill."""

    def test_tail_only_missing(self):
        from backend.services.worker_relay import _missing_by_fingerprint
        remote = [_entry(content=str(i)) for i in range(5)]
        local = remote[:3]
        missing = _missing_by_fingerprint(local, remote)
        assert [m["content"] for m in missing] == ["3", "4"]

    def test_mid_stream_gap_does_not_duplicate(self):
        # local missed entry "2" in the middle but has "3"; count-based slicing
        # (remote[local_count=3:]) would re-insert "3" AND drop "2".
        from backend.services.worker_relay import _missing_by_fingerprint
        remote = [_entry(content=str(i)) for i in range(5)]  # 0,1,2,3,4
        local = [remote[0], remote[1], remote[3]]            # missing "2"
        missing = _missing_by_fingerprint(local, remote)
        assert [m["content"] for m in missing] == ["2", "4"]  # "3" NOT re-inserted

    def test_fully_synced_inserts_nothing(self):
        from backend.services.worker_relay import _missing_by_fingerprint
        remote = [_entry(content=str(i)) for i in range(4)]
        assert _missing_by_fingerprint(list(remote), remote) == []

    def test_truncated_tool_output_still_matches(self):
        # remote tool_output is truncated by the history endpoint; the local copy
        # is full. Prefix-capped fingerprint must still treat them as identical.
        from backend.services.worker_relay import _missing_by_fingerprint
        full = "x" * 50_000
        truncated = ("x" * 20_000) + "\n…(truncated)"
        local = [_entry(et="tool_result", tool_name="bash", tool_output=full)]
        remote = [_entry(et="tool_result", tool_name="bash", tool_output=truncated)]
        assert _missing_by_fingerprint(local, remote) == []

    def test_duplicate_fingerprints_preserve_multiplicity(self):
        from backend.services.worker_relay import _missing_by_fingerprint
        remote = [_entry(content="same") for _ in range(3)]
        local = [_entry(content="same")]  # only one present
        missing = _missing_by_fingerprint(local, remote)
        assert len(missing) == 2  # insert the two still-missing copies
