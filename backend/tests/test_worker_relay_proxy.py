"""Phase 2 测试：WorkerRelay 事件处理 / Dispatcher 双路径 / Chat 与操作代理。"""
import asyncio
import json
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import select

import backend.main as main_module
import backend.services.worker_proxy as worker_proxy_module
from backend.models.log_entry import LogEntry
from backend.models.monitor_session import MonitorCheck, MonitorSession
from backend.models.task import Task
from backend.models.worker import Worker
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

    await relay._handle({
        "channel": "tasks",
        "data": {"event": "status_change", "task_id": t.id,
                 "old_status": "in_progress", "new_status": "completed"},
    }, w)
    async with session_factory() as db:
        assert (await db.get(Task, t.id)).status == "completed"


async def test_relay_plan_ready_fetches_content(relay, session_factory):
    w = await _mk_worker(session_factory)
    t = await _mk_task(session_factory, worker_id=w.id, mode="plan")
    relay._tasks[w.id] = {t.id}
    relay._fetch_task_field = AsyncMock(return_value="THE PLAN")

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


# === API 代理 ===


class _ProxyResponse:
    def __init__(self, status_code: int, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


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
