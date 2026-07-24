"""Tests for Monitor API endpoints."""
import asyncio

import pytest
from fastapi import HTTPException
from unittest.mock import AsyncMock, MagicMock, patch
from sqlalchemy import func, select, update

from backend.models.task import Task
from backend.models.monitor_session import MonitorSession, MonitorCheck
from backend.models.sub_agent import SubAgentReport


async def _create_task_with_monitor(client, session_factory, status="in_progress"):
    # monitor 是 claude-only（codex 任务显式 400），默认 provider 已是 codex，
    # 这里必须显式钉住 claude 才测得到 skill/limit 等后续分支
    resp = await client.post("/api/tasks", json={
        "title": "T", "description": "d", "target_repo": "/tmp",
        "enabled_skills": {"monitor": True}, "provider": "claude",
    })
    task_id = resp.json()["id"]
    if status != "pending":
        async with session_factory() as db:
            await db.execute(
                update(Task).where(Task.id == task_id).values(status=status)
            )
            await db.commit()
    return task_id


async def _create_task_with_sub_agent(
    client,
    session_factory,
    status="in_progress",
):
    resp = await client.post("/api/tasks", json={
        "title": "T",
        "description": "d",
        "target_repo": "/tmp",
        "enabled_skills": {"sub-agent": True},
        "provider": "claude",
    })
    task_id = resp.json()["id"]
    if status != "pending":
        async with session_factory() as db:
            await db.execute(
                update(Task).where(Task.id == task_id).values(status=status)
            )
            await db.commit()
    return task_id


@pytest.mark.asyncio
async def test_create_monitor_session(client, session_factory):
    task_id = await _create_task_with_monitor(client, session_factory)

    mock_dispatcher = MagicMock()
    mock_dispatcher.start_monitor_session = MagicMock()
    mock_dispatcher.broadcaster = MagicMock()
    mock_dispatcher.broadcaster.broadcast = AsyncMock()

    with patch("backend.main.dispatcher", mock_dispatcher):
        resp = await client.post(f"/api/tasks/{task_id}/monitor-sessions", json={
            "description": "watch build",
            "interval": 60,
            "max_checks": 10,
        })

    assert resp.status_code == 200
    data = resp.json()
    assert data["description"] == "watch build"
    assert data["status"] == "running"
    assert data["interval"] == 60
    assert data["max_checks"] == 10
    assert data["task_id"] == task_id
    mock_dispatcher.start_monitor_session.assert_called_once()


@pytest.mark.asyncio
async def test_cancelled_monitor_create_still_admits_committed_row(
    client,
    session_factory,
):
    from backend.api.monitor import create_monitor_session
    from backend.schemas.monitor_session import MonitorSessionCreate

    task_id = await _create_task_with_monitor(client, session_factory)
    commit_started = asyncio.Event()
    release_commit = asyncio.Event()
    dispatcher = MagicMock()
    dispatcher.start_monitor_session = MagicMock()
    dispatcher.broadcaster.broadcast = AsyncMock()

    async with session_factory() as db:
        original_commit = db.commit

        async def blocked_commit():
            commit_started.set()
            await release_commit.wait()
            await original_commit()

        with (
            patch.object(db, "commit", side_effect=blocked_commit),
            patch("backend.main.dispatcher", dispatcher),
        ):
            request_task = asyncio.create_task(
                create_monitor_session(
                    task_id,
                    MonitorSessionCreate(description="cancel-window"),
                    db,
                )
            )
            await commit_started.wait()
            request_task.cancel()
            release_commit.set()
            with pytest.raises(asyncio.CancelledError):
                await request_task

    dispatcher.start_monitor_session.assert_called_once()
    async with session_factory() as db:
        rows = list(
            (
                await db.execute(
                    select(MonitorSession).where(
                        MonitorSession.task_id == task_id,
                        MonitorSession.description == "cancel-window",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1
    assert rows[0].status == "running"


@pytest.mark.asyncio
async def test_cancelled_sub_agent_create_still_admits_committed_row(
    client,
    session_factory,
):
    from backend.api.sub_agent_tasks import (
        SubAgentSessionCreate,
        create_sub_agent_session,
    )

    task_id = await _create_task_with_sub_agent(client, session_factory)
    commit_started = asyncio.Event()
    release_commit = asyncio.Event()
    dispatcher = MagicMock()
    dispatcher.start_sub_agent_session = MagicMock()
    dispatcher.broadcaster.broadcast = AsyncMock()

    async with session_factory() as db:
        original_commit = db.commit

        async def blocked_commit():
            commit_started.set()
            await release_commit.wait()
            await original_commit()

        with (
            patch.object(db, "commit", side_effect=blocked_commit),
            patch("backend.main.dispatcher", dispatcher),
        ):
            request_task = asyncio.create_task(
                create_sub_agent_session(
                    task_id,
                    SubAgentSessionCreate(
                        name="cancel-window",
                        prompt="work",
                    ),
                    db,
                )
            )
            await commit_started.wait()
            request_task.cancel()
            release_commit.set()
            with pytest.raises(asyncio.CancelledError):
                await request_task

    dispatcher.start_sub_agent_session.assert_called_once()
    async with session_factory() as db:
        rows = list(
            (
                await db.execute(
                    select(MonitorSession).where(
                        MonitorSession.task_id == task_id,
                        MonitorSession.agent_type == "sub_agent",
                        MonitorSession.description == "cancel-window",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1
    assert rows[0].status == "running"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "path", "payload", "starter_name"),
    (
        (
            "monitor",
            "monitor-sessions",
            {"description": "shutdown-race"},
            "start_monitor_session",
        ),
        (
            "sub_agent",
            "sub-agent-sessions",
            {"name": "shutdown-race", "prompt": "work"},
            "start_sub_agent_session",
        ),
    ),
)
async def test_failed_auxiliary_admission_marks_committed_row_failed(
    client,
    session_factory,
    kind,
    path,
    payload,
    starter_name,
):
    if kind == "monitor":
        task_id = await _create_task_with_monitor(client, session_factory)
    else:
        task_id = await _create_task_with_sub_agent(client, session_factory)

    dispatcher = MagicMock()
    setattr(
        dispatcher,
        starter_name,
        MagicMock(side_effect=RuntimeError("shutdown admission closed")),
    )
    dispatcher.broadcaster.broadcast = AsyncMock()
    with patch("backend.main.dispatcher", dispatcher):
        response = await client.post(f"/api/tasks/{task_id}/{path}", json=payload)

    assert response.status_code == 503
    async with session_factory() as db:
        row = await db.scalar(
            select(MonitorSession).where(
                MonitorSession.task_id == task_id,
                MonitorSession.agent_type == kind,
                MonitorSession.description == "shutdown-race",
            )
        )
    assert row is not None
    assert row.status == "failed"
    assert row.completed_at is not None


@pytest.mark.asyncio
async def test_create_monitor_no_skill(client, session_factory):
    resp = await client.post("/api/tasks", json={
        "title": "T", "description": "d", "target_repo": "/tmp",
        "provider": "claude",
    })
    task_id = resp.json()["id"]
    async with session_factory() as db:
        await db.execute(update(Task).where(Task.id == task_id).values(status="in_progress"))
        await db.commit()

    resp = await client.post(f"/api/tasks/{task_id}/monitor-sessions", json={
        "description": "test",
    })
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_create_monitor_task_not_found(client):
    resp = await client.post("/api/tasks/9999/monitor-sessions", json={
        "description": "test",
    })
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_create_monitor_task_completed(client, session_factory):
    task_id = await _create_task_with_monitor(client, session_factory, status="completed")

    resp = await client.post(f"/api/tasks/{task_id}/monitor-sessions", json={
        "description": "test",
    })
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_create_monitor_concurrency_limit(client, session_factory):
    task_id = await _create_task_with_monitor(client, session_factory)

    async with session_factory() as db:
        for i in range(5):
            db.add(MonitorSession(task_id=task_id, description=f"m{i}", status="running"))
        await db.commit()

    resp = await client.post(f"/api/tasks/{task_id}/monitor-sessions", json={
        "description": "one too many",
    })
    assert resp.status_code == 429


@pytest.mark.asyncio
async def test_concurrent_monitor_admission_never_exceeds_sqlite_cap(
    client,
    session_factory,
):
    task_id = await _create_task_with_monitor(client, session_factory)
    async with session_factory() as db:
        db.add_all([
            MonitorSession(
                task_id=task_id,
                agent_type="monitor",
                source="ccm",
                description=f"existing-{index}",
                status="running",
            )
            for index in range(4)
        ])
        await db.commit()

    dispatcher = MagicMock()
    dispatcher.start_monitor_session = MagicMock()
    dispatcher.broadcaster.broadcast = AsyncMock()
    with patch("backend.main.dispatcher", dispatcher):
        responses = await asyncio.gather(
            client.post(
                f"/api/tasks/{task_id}/monitor-sessions",
                json={"description": "candidate-a"},
            ),
            client.post(
                f"/api/tasks/{task_id}/monitor-sessions",
                json={"description": "candidate-b"},
            ),
        )

    assert sorted(response.status_code for response in responses) == [200, 429]
    async with session_factory() as db:
        count = await db.scalar(
            select(func.count(MonitorSession.id)).where(
                MonitorSession.task_id == task_id,
                MonitorSession.agent_type == "monitor",
                MonitorSession.source == "ccm",
                MonitorSession.status == "running",
            )
        )
    assert count == 5
    assert dispatcher.start_monitor_session.call_count == 1


@pytest.mark.asyncio
async def test_concurrent_sub_agent_admission_never_exceeds_sqlite_cap(
    client,
    session_factory,
):
    task_id = await _create_task_with_sub_agent(client, session_factory)
    async with session_factory() as db:
        db.add_all([
            MonitorSession(
                task_id=task_id,
                agent_type="sub_agent",
                source="ccm",
                description=f"existing-{index}",
                status="running",
            )
            for index in range(2)
        ])
        await db.commit()

    dispatcher = MagicMock()
    dispatcher.start_sub_agent_session = MagicMock()
    dispatcher.broadcaster.broadcast = AsyncMock()
    with patch("backend.main.dispatcher", dispatcher):
        responses = await asyncio.gather(
            client.post(
                f"/api/tasks/{task_id}/sub-agent-sessions",
                json={"name": "candidate-a", "prompt": "a"},
            ),
            client.post(
                f"/api/tasks/{task_id}/sub-agent-sessions",
                json={"name": "candidate-b", "prompt": "b"},
            ),
        )

    assert sorted(response.status_code for response in responses) == [201, 429]
    async with session_factory() as db:
        count = await db.scalar(
            select(func.count(MonitorSession.id)).where(
                MonitorSession.task_id == task_id,
                MonitorSession.agent_type == "sub_agent",
                MonitorSession.source == "ccm",
                MonitorSession.status == "running",
            )
        )
    assert count == 3
    assert dispatcher.start_sub_agent_session.call_count == 1


@pytest.mark.asyncio
async def test_worker_sub_agent_create_is_proxied_without_local_start(
    client,
    session_factory,
):
    from backend.api.sub_agent_tasks import (
        SubAgentSessionCreate,
        create_sub_agent_session,
    )

    task_id = await _create_task_with_sub_agent(client, session_factory)
    async with session_factory() as db:
        await db.execute(
            update(Task)
            .where(Task.id == task_id)
            # Routing belongs to the Worker before the Manager applies its
            # local claude-only gate; the Worker validates its own provider.
            .values(worker_id=77, provider="codex")
        )
        await db.commit()

    proxy = MagicMock()
    proxy.proxy_to_worker = AsyncMock(return_value={"proxied": True})
    dispatcher = MagicMock()
    dispatcher.start_sub_agent_session = MagicMock()
    async with session_factory() as db:
        with (
            patch("backend.main.worker_proxy", proxy),
            patch("backend.main.dispatcher", dispatcher),
        ):
            result = await create_sub_agent_session(
                task_id,
                SubAgentSessionCreate(name="remote", prompt="work"),
                db,
            )

    assert result == {"proxied": True}
    proxy.proxy_to_worker.assert_awaited_once()
    proxied_task, method, path = proxy.proxy_to_worker.call_args.args
    assert proxied_task.id == task_id
    assert proxied_task.worker_id == 77
    assert method == "POST"
    assert path == f"/api/tasks/{task_id}/sub-agent-sessions"
    dispatcher.start_sub_agent_session.assert_not_called()


@pytest.mark.asyncio
async def test_worker_monitor_create_routes_before_local_codex_gate(
    client,
    session_factory,
):
    from backend.api.monitor import create_monitor_session
    from backend.schemas.monitor_session import MonitorSessionCreate

    task_id = await _create_task_with_monitor(client, session_factory)
    async with session_factory() as db:
        await db.execute(
            update(Task)
            .where(Task.id == task_id)
            .values(worker_id=79, provider="codex")
        )
        await db.commit()

    proxy = MagicMock()
    proxy.proxy_to_worker = AsyncMock(return_value={"proxied": True})
    dispatcher = MagicMock()
    dispatcher.start_monitor_session = MagicMock()
    async with session_factory() as db:
        with (
            patch("backend.main.worker_proxy", proxy),
            patch("backend.main.dispatcher", dispatcher),
        ):
            result = await create_monitor_session(
                task_id,
                MonitorSessionCreate(description="remote monitor"),
                db,
            )

    assert result == {"proxied": True}
    proxy.proxy_to_worker.assert_awaited_once()
    proxied_task, method, path = proxy.proxy_to_worker.call_args.args
    assert proxied_task.id == task_id
    assert proxied_task.worker_id == 79
    assert method == "POST"
    assert path == f"/api/tasks/{task_id}/monitor-sessions"
    dispatcher.start_monitor_session.assert_not_called()


@pytest.mark.asyncio
async def test_sub_agent_create_routes_migration_before_local_write_guard(
    client,
    session_factory,
):
    from backend.api.sub_agent_tasks import (
        SubAgentSessionCreate,
        create_sub_agent_session,
    )

    task_id = await _create_task_with_sub_agent(client, session_factory)
    proxy = MagicMock()
    proxy.proxy_to_worker = AsyncMock(return_value={"proxied": True})
    dispatcher = MagicMock()
    dispatcher.start_sub_agent_session = MagicMock()

    async with session_factory() as db:
        original_rollback = db.rollback
        rollback_count = 0

        async def migrate_after_routing_read():
            nonlocal rollback_count
            rollback_count += 1
            await original_rollback()
            if rollback_count == 1:
                async with session_factory() as migration_db:
                    await migration_db.execute(
                        update(Task)
                        .where(Task.id == task_id)
                        .values(worker_id=88)
                    )
                    await migration_db.commit()

        with (
            patch.object(db, "rollback", side_effect=migrate_after_routing_read),
            patch("backend.main.worker_proxy", proxy),
            patch("backend.main.dispatcher", dispatcher),
        ):
            result = await create_sub_agent_session(
                task_id,
                SubAgentSessionCreate(name="migrated", prompt="work"),
                db,
            )

    assert result == {"proxied": True}
    proxy.proxy_to_worker.assert_awaited_once()
    proxied_task = proxy.proxy_to_worker.call_args.args[0]
    assert proxied_task.worker_id == 88
    dispatcher.start_sub_agent_session.assert_not_called()
    async with session_factory() as db:
        local_count = await db.scalar(
            select(func.count(MonitorSession.id)).where(
                MonitorSession.task_id == task_id,
                MonitorSession.agent_type == "sub_agent",
            )
        )
    assert local_count == 0


@pytest.mark.asyncio
async def test_list_monitor_sessions(client, session_factory):
    task_id = await _create_task_with_monitor(client, session_factory)

    async with session_factory() as db:
        db.add(MonitorSession(task_id=task_id, description="m1"))
        db.add(MonitorSession(task_id=task_id, description="m2"))
        await db.commit()

    resp = await client.get(f"/api/tasks/{task_id}/monitor-sessions")
    assert resp.status_code == 200
    assert len(resp.json()) == 2


@pytest.mark.asyncio
async def test_get_monitor_session(client, session_factory):
    task_id = await _create_task_with_monitor(client, session_factory)

    async with session_factory() as db:
        ms = MonitorSession(task_id=task_id, description="get-test")
        db.add(ms)
        await db.commit()
        await db.refresh(ms)
        ms_id = ms.id

    resp = await client.get(f"/api/tasks/{task_id}/monitor-sessions/{ms_id}")
    assert resp.status_code == 200
    assert resp.json()["description"] == "get-test"


@pytest.mark.asyncio
async def test_get_monitor_session_not_found(client, session_factory):
    task_id = await _create_task_with_monitor(client, session_factory)
    resp = await client.get(f"/api/tasks/{task_id}/monitor-sessions/9999")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_monitor_session(client, session_factory):
    task_id = await _create_task_with_monitor(client, session_factory)

    async with session_factory() as db:
        ms = MonitorSession(task_id=task_id, description="del-test", status="running")
        db.add(ms)
        await db.commit()
        await db.refresh(ms)
        ms_id = ms.id

    mock_dispatcher = MagicMock()
    mock_dispatcher._monitor_tasks = {}
    mock_dispatcher._monitor_processes = {}
    mock_dispatcher.stop_monitor_session_process = AsyncMock()
    mock_dispatcher.broadcaster = MagicMock()
    mock_dispatcher.broadcaster.broadcast = AsyncMock()

    with patch("backend.main.dispatcher", mock_dispatcher):
        resp = await client.delete(f"/api/tasks/{task_id}/monitor-sessions/{ms_id}")

    assert resp.status_code == 200
    assert resp.json()["ok"] is True

    async with session_factory() as db:
        ms = await db.get(MonitorSession, ms_id)
        assert ms.status == "cancelled"
        assert ms.completed_at is not None


@pytest.mark.asyncio
async def test_monitor_complete_loses_cas_to_concurrent_cancel(
    client,
    session_factory,
):
    from backend.api.monitor import complete_monitor_session
    from backend.schemas.monitor_session import MonitorCompleteRequest

    task_id = await _create_task_with_monitor(client, session_factory)
    async with session_factory() as db:
        session = MonitorSession(
            task_id=task_id,
            agent_type="monitor",
            source="ccm",
            description="complete-race",
            status="running",
        )
        db.add(session)
        await db.commit()
        session_id = session.id

    callback_ready = asyncio.Event()
    release_callback = asyncio.Event()
    dispatcher = MagicMock()
    dispatcher.broadcaster.broadcast = AsyncMock()
    dispatcher.enqueue_message = AsyncMock()

    async with session_factory() as callback_db:
        original_execute = callback_db.execute
        delayed = False

        async def delay_terminal_update(statement, *args, **kwargs):
            nonlocal delayed
            table = getattr(statement, "table", None)
            if (
                not delayed
                and getattr(statement, "is_update", False)
                and getattr(table, "name", None) == "sub_agent_sessions"
            ):
                delayed = True
                callback_ready.set()
                await release_callback.wait()
            return await original_execute(statement, *args, **kwargs)

        with (
            patch.object(
                callback_db,
                "execute",
                new=AsyncMock(side_effect=delay_terminal_update),
            ),
            patch("backend.main.dispatcher", dispatcher),
        ):
            callback = asyncio.create_task(
                complete_monitor_session(
                    task_id,
                    session_id,
                    MonitorCompleteRequest(reason="too late"),
                    callback_db,
                )
            )
            await callback_ready.wait()
            async with session_factory() as cancel_db:
                await cancel_db.execute(
                    update(MonitorSession)
                    .where(
                        MonitorSession.id == session_id,
                        MonitorSession.status == "running",
                    )
                    .values(status="cancelled")
                )
                await cancel_db.commit()
            release_callback.set()
            with pytest.raises(HTTPException) as exc_info:
                await callback

    assert exc_info.value.status_code == 400
    dispatcher.broadcaster.broadcast.assert_not_awaited()
    dispatcher.enqueue_message.assert_not_awaited()
    async with session_factory() as db:
        session = await db.get(MonitorSession, session_id)
        report_count = await db.scalar(
            select(func.count(MonitorCheck.id)).where(
                MonitorCheck.monitor_session_id == session_id
            )
        )
    assert session.status == "cancelled"
    assert report_count == 0


@pytest.mark.asyncio
async def test_sub_agent_result_loses_cas_to_concurrent_stop(
    client,
    session_factory,
):
    from backend.api.sub_agent_tasks import (
        SubAgentResultRequest,
        sub_agent_submit_result,
    )

    task_id = await _create_task_with_sub_agent(client, session_factory)
    async with session_factory() as db:
        session = MonitorSession(
            task_id=task_id,
            agent_type="sub_agent",
            source="ccm",
            description="result-race",
            status="running",
        )
        db.add(session)
        await db.commit()
        session_id = session.id

    callback_ready = asyncio.Event()
    release_callback = asyncio.Event()
    dispatcher = MagicMock()
    dispatcher.broadcaster.broadcast = AsyncMock()
    dispatcher.enqueue_message = AsyncMock()
    dispatcher.stop_sub_agent_session_process = AsyncMock()

    async with session_factory() as callback_db:
        original_execute = callback_db.execute
        delayed = False

        async def delay_terminal_update(statement, *args, **kwargs):
            nonlocal delayed
            table = getattr(statement, "table", None)
            if (
                not delayed
                and getattr(statement, "is_update", False)
                and getattr(table, "name", None) == "sub_agent_sessions"
            ):
                delayed = True
                callback_ready.set()
                await release_callback.wait()
            return await original_execute(statement, *args, **kwargs)

        with (
            patch.object(
                callback_db,
                "execute",
                new=AsyncMock(side_effect=delay_terminal_update),
            ),
            patch("backend.main.dispatcher", dispatcher),
        ):
            callback = asyncio.create_task(
                sub_agent_submit_result(
                    task_id,
                    session_id,
                    SubAgentResultRequest(result="too late"),
                    callback_db,
                )
            )
            await callback_ready.wait()
            async with session_factory() as stop_db:
                await stop_db.execute(
                    update(MonitorSession)
                    .where(
                        MonitorSession.id == session_id,
                        MonitorSession.status == "running",
                    )
                    .values(status="stopped")
                )
                await stop_db.commit()
            release_callback.set()
            with pytest.raises(HTTPException) as exc_info:
                await callback

    assert exc_info.value.status_code == 400
    dispatcher.broadcaster.broadcast.assert_not_awaited()
    dispatcher.enqueue_message.assert_not_awaited()
    dispatcher.stop_sub_agent_session_process.assert_not_awaited()
    async with session_factory() as db:
        session = await db.get(MonitorSession, session_id)
        report_count = await db.scalar(
            select(func.count(SubAgentReport.id)).where(
                SubAgentReport.session_id == session_id
            )
        )
    assert session.status == "stopped"
    assert report_count == 0


@pytest.mark.asyncio
async def test_late_progress_callbacks_do_not_write_after_terminal_state(
    client,
    session_factory,
):
    task_id = await _create_task_with_monitor(client, session_factory)
    sub_task_id = await _create_task_with_sub_agent(client, session_factory)
    async with session_factory() as db:
        monitor = MonitorSession(
            task_id=task_id,
            agent_type="monitor",
            source="ccm",
            description="late-check",
            status="cancelled",
        )
        sub_agent = MonitorSession(
            task_id=sub_task_id,
            agent_type="sub_agent",
            source="ccm",
            description="late-progress",
            status="stopped",
        )
        db.add_all([monitor, sub_agent])
        await db.commit()
        monitor_id = monitor.id
        sub_agent_id = sub_agent.id

    dispatcher = MagicMock()
    dispatcher.broadcaster.broadcast = AsyncMock()
    dispatcher.enqueue_message = AsyncMock()
    with patch("backend.main.dispatcher", dispatcher):
        monitor_response = await client.post(
            f"/api/tasks/{task_id}/monitor-sessions/{monitor_id}/checks",
            json={"summary": "late", "is_important": True},
        )
        sub_agent_response = await client.post(
            f"/api/tasks/{sub_task_id}/sub-agent-sessions/"
            f"{sub_agent_id}/progress",
            json={"summary": "late"},
        )

    assert monitor_response.status_code == 400
    assert sub_agent_response.status_code == 400
    dispatcher.broadcaster.broadcast.assert_not_awaited()
    dispatcher.enqueue_message.assert_not_awaited()
    async with session_factory() as db:
        monitor_reports = await db.scalar(
            select(func.count(MonitorCheck.id)).where(
                MonitorCheck.monitor_session_id == monitor_id
            )
        )
        sub_reports = await db.scalar(
            select(func.count(SubAgentReport.id)).where(
                SubAgentReport.session_id == sub_agent_id
            )
        )
    assert monitor_reports == 0
    assert sub_reports == 0


@pytest.mark.asyncio
async def test_delete_does_not_overwrite_completed_auxiliary_status(
    client,
    session_factory,
):
    task_id = await _create_task_with_monitor(client, session_factory)
    sub_task_id = await _create_task_with_sub_agent(client, session_factory)
    async with session_factory() as db:
        monitor = MonitorSession(
            task_id=task_id,
            agent_type="monitor",
            source="ccm",
            description="done-monitor",
            status="completed",
        )
        sub_agent = MonitorSession(
            task_id=sub_task_id,
            agent_type="sub_agent",
            source="ccm",
            description="done-sub-agent",
            status="completed",
        )
        db.add_all([monitor, sub_agent])
        await db.commit()
        monitor_id = monitor.id
        sub_agent_id = sub_agent.id

    dispatcher = MagicMock()
    dispatcher.stop_monitor_session_process = AsyncMock()
    dispatcher.stop_sub_agent_session_process = AsyncMock()
    dispatcher.broadcaster.broadcast = AsyncMock()
    with patch("backend.main.dispatcher", dispatcher):
        monitor_response = await client.delete(
            f"/api/tasks/{task_id}/monitor-sessions/{monitor_id}"
        )
        sub_agent_response = await client.delete(
            f"/api/tasks/{sub_task_id}/sub-agent-sessions/{sub_agent_id}"
        )

    assert monitor_response.status_code == 200
    assert sub_agent_response.status_code == 200
    dispatcher.broadcaster.broadcast.assert_not_awaited()
    async with session_factory() as db:
        monitor = await db.get(MonitorSession, monitor_id)
        sub_agent = await db.get(MonitorSession, sub_agent_id)
    assert monitor.status == "completed"
    assert sub_agent.status == "completed"


@pytest.mark.asyncio
async def test_get_monitor_checks(client, session_factory):
    task_id = await _create_task_with_monitor(client, session_factory)

    async with session_factory() as db:
        ms = MonitorSession(task_id=task_id, description="checks-test")
        db.add(ms)
        await db.commit()
        await db.refresh(ms)
        ms_id = ms.id

        for i in range(3):
            db.add(MonitorCheck(
                monitor_session_id=ms_id,
                check_number=i + 1,
                status="success",
                summary=f"check {i+1}",
            ))
        await db.commit()

    resp = await client.get(f"/api/tasks/{task_id}/monitor-sessions/{ms_id}/checks")
    assert resp.status_code == 200
    checks = resp.json()
    assert len(checks) == 3


@pytest.mark.asyncio
async def test_monitor_checks_increment_atomically_and_auto_complete(
    client,
    session_factory,
):
    task_id = await _create_task_with_monitor(client, session_factory)
    async with session_factory() as db:
        session = MonitorSession(
            task_id=task_id,
            agent_type="monitor",
            source="ccm",
            description="two checks",
            status="running",
            max_checks=2,
        )
        db.add(session)
        await db.commit()
        session_id = session.id

    dispatcher = MagicMock()
    dispatcher.broadcaster.broadcast = AsyncMock()
    dispatcher.enqueue_message = AsyncMock()
    dispatcher.stop_monitor_session_process = AsyncMock()
    with patch("backend.main.dispatcher", dispatcher):
        first = await client.post(
            f"/api/tasks/{task_id}/monitor-sessions/{session_id}/checks",
            json={"summary": "first"},
        )
        async with session_factory() as db:
            after_first = await db.get(MonitorSession, session_id)
            assert after_first.status == "running"
            assert after_first.checks_done == 1
            assert after_first.completed_at is None
        second = await client.post(
            f"/api/tasks/{task_id}/monitor-sessions/{session_id}/checks",
            json={"summary": "second"},
        )

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.json()["check_number"] == 1
    assert second.json()["check_number"] == 2
    async with session_factory() as db:
        session = await db.get(MonitorSession, session_id)
        reports = list(
            (
                await db.execute(
                    select(MonitorCheck)
                    .where(MonitorCheck.monitor_session_id == session_id)
                    .order_by(MonitorCheck.check_number)
                )
            )
            .scalars()
            .all()
        )
    assert session.status == "completed"
    assert session.checks_done == 2
    assert [report.check_number for report in reports] == [1, 2]
    dispatcher.stop_monitor_session_process.assert_awaited_once_with(session_id)


@pytest.mark.asyncio
async def test_sub_agent_progress_then_result_uses_unique_report_numbers(
    client,
    session_factory,
):
    task_id = await _create_task_with_sub_agent(client, session_factory)
    async with session_factory() as db:
        session = MonitorSession(
            task_id=task_id,
            agent_type="sub_agent",
            source="ccm",
            description="progress-result",
            status="running",
        )
        db.add(session)
        await db.commit()
        session_id = session.id

    dispatcher = MagicMock()
    dispatcher.broadcaster.broadcast = AsyncMock()
    dispatcher.enqueue_message = AsyncMock()
    dispatcher.stop_sub_agent_session_process = AsyncMock()
    with patch("backend.main.dispatcher", dispatcher):
        progress = await client.post(
            f"/api/tasks/{task_id}/sub-agent-sessions/{session_id}/progress",
            json={"summary": "halfway"},
        )
        result = await client.post(
            f"/api/tasks/{task_id}/sub-agent-sessions/{session_id}/result",
            json={"result": "done", "status": "completed"},
        )

    assert progress.status_code == 200, progress.text
    assert progress.json()["progress_count"] == 1
    assert result.status_code == 200, result.text
    async with session_factory() as db:
        session = await db.get(MonitorSession, session_id)
        reports = list(
            (
                await db.execute(
                    select(SubAgentReport)
                    .where(SubAgentReport.session_id == session_id)
                    .order_by(SubAgentReport.check_number)
                )
            )
            .scalars()
            .all()
        )
    assert session.status == "completed"
    assert session.checks_done == 2
    assert [report.check_number for report in reports] == [1, 2]
    dispatcher.enqueue_message.assert_awaited_once()
    dispatcher.stop_sub_agent_session_process.assert_awaited_once_with(
        session_id
    )


@pytest.mark.asyncio
async def test_task_delete_cleans_monitors(client, session_factory):
    task_id = await _create_task_with_monitor(client, session_factory, status="completed")

    async with session_factory() as db:
        ms = MonitorSession(task_id=task_id, description="will-delete", status="completed")
        db.add(ms)
        await db.commit()
        await db.refresh(ms)
        ms_id = ms.id
        db.add(MonitorCheck(
            monitor_session_id=ms_id, check_number=1, status="success", summary="ok",
        ))
        await db.commit()

    resp = await client.delete(f"/api/tasks/{task_id}")
    assert resp.status_code == 200

    async with session_factory() as db:
        ms_result = await db.execute(select(MonitorSession).where(MonitorSession.task_id == task_id))
        assert len(list(ms_result.scalars().all())) == 0
        check_result = await db.execute(select(MonitorCheck).where(MonitorCheck.monitor_session_id == ms_id))
        assert len(list(check_result.scalars().all())) == 0


@pytest.mark.asyncio
async def test_task_cancel_cancels_monitors(client, session_factory):
    task_id = await _create_task_with_monitor(client, session_factory)

    async with session_factory() as db:
        ms = MonitorSession(task_id=task_id, description="will-cancel", status="running")
        db.add(ms)
        await db.commit()
        await db.refresh(ms)
        ms_id = ms.id

    mock_dispatcher = MagicMock()
    mock_dispatcher._monitor_tasks = {}
    mock_dispatcher._monitor_processes = {}
    mock_dispatcher.abort_task_queue = AsyncMock(return_value=0)
    mock_dispatcher.stop_monitor_session_process = AsyncMock()

    with patch("backend.main.dispatcher", mock_dispatcher):
        resp = await client.post(f"/api/tasks/{task_id}/cancel")
    assert resp.status_code == 200

    async with session_factory() as db:
        ms = await db.get(MonitorSession, ms_id)
        assert ms.status == "cancelled"


@pytest.mark.asyncio
async def test_task_cancel_routes_ccm_auxiliary_reapers_by_agent_type(
    client,
    session_factory,
):
    task_id = await _create_task_with_monitor(client, session_factory)

    async with session_factory() as db:
        monitor = MonitorSession(
            task_id=task_id,
            agent_type="monitor",
            source="ccm",
            description="monitor",
            status="running",
        )
        sub_agent = MonitorSession(
            task_id=task_id,
            agent_type="sub_agent",
            source="ccm",
            description="one-shot",
            status="running",
        )
        native = MonitorSession(
            task_id=task_id,
            agent_type="native-agent",
            source="native",
            description="native child",
            status="running",
        )
        db.add_all([monitor, sub_agent, native])
        await db.commit()
        monitor_id = monitor.id
        sub_agent_id = sub_agent.id

    mock_dispatcher = MagicMock()
    mock_dispatcher.abort_task_queue = AsyncMock(return_value=0)
    mock_dispatcher.stop_monitor_session_process = AsyncMock()
    mock_dispatcher.stop_sub_agent_session_process = AsyncMock()

    with patch("backend.main.dispatcher", mock_dispatcher):
        response = await client.post(f"/api/tasks/{task_id}/cancel")

    assert response.status_code == 200, response.text
    mock_dispatcher.stop_monitor_session_process.assert_awaited_once_with(
        monitor_id
    )
    mock_dispatcher.stop_sub_agent_session_process.assert_awaited_once_with(
        sub_agent_id
    )


@pytest.mark.asyncio
async def test_create_monitor_rejects_codex_task(client, session_factory):
    """Monitor 子 agent 硬编码 claude CLI——codex 任务必须显式 400，
    而不是静默起一个 Claude 子进程。"""
    resp = await client.post("/api/tasks", json={
        "title": "T", "description": "d", "target_repo": "/tmp",
        "provider": "codex",
        "enabled_skills": {"monitor": True},
    })
    task_id = resp.json()["id"]
    async with session_factory() as db:
        await db.execute(update(Task).where(Task.id == task_id).values(status="in_progress"))
        await db.commit()

    resp = await client.post(f"/api/tasks/{task_id}/monitor-sessions", json={
        "description": "watch build",
    })
    assert resp.status_code == 400
    assert "claude-only" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_create_sub_agent_rejects_codex_task(client, session_factory):
    resp = await client.post("/api/tasks", json={
        "title": "T", "description": "d", "target_repo": "/tmp",
        "provider": "codex",
        "enabled_skills": {"sub-agent": True},
    })
    task_id = resp.json()["id"]
    async with session_factory() as db:
        await db.execute(update(Task).where(Task.id == task_id).values(status="in_progress"))
        await db.commit()

    resp = await client.post(f"/api/tasks/{task_id}/sub-agent-sessions", json={
        "name": "review", "prompt": "review the code",
    })
    assert resp.status_code == 400
    assert "claude-only" in resp.json()["detail"]
