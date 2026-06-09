"""Tests for Monitor Session — CRUD, lifecycle, API, cleanup."""
import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, patch, MagicMock

import pytest
import pytest_asyncio
from sqlalchemy import select, update

from backend.models.task import Task
from backend.models.monitor_session import MonitorSession, MonitorCheck
from backend.services.task_queue import TaskQueue


@pytest_asyncio.fixture
async def queue(db_session):
    return TaskQueue(db_session)


# ── Model CRUD ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_monitor_session_defaults(db_session):
    task = Task(title="t", description="d", mode="auto")
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    ms = MonitorSession(task_id=task.id, description="watch build")
    db_session.add(ms)
    await db_session.commit()
    await db_session.refresh(ms)

    assert ms.id is not None
    assert ms.status == "running"
    assert ms.checks_done == 0
    assert ms.interval == 300
    assert ms.max_checks == 100
    assert ms.source == "manual"
    assert ms.last_summary is None
    assert ms.completed_at is None


@pytest.mark.asyncio
async def test_monitor_check_crud(db_session):
    task = Task(title="t", description="d", mode="auto")
    db_session.add(task)
    await db_session.commit()

    ms = MonitorSession(task_id=task.id, description="watch")
    db_session.add(ms)
    await db_session.commit()
    await db_session.refresh(ms)

    check = MonitorCheck(
        monitor_session_id=ms.id,
        check_number=1,
        status="completed",
        summary="all good",
        full_output="detailed output here",
    )
    db_session.add(check)
    await db_session.commit()
    await db_session.refresh(check)

    assert check.id is not None
    assert check.monitor_session_id == ms.id
    assert check.check_number == 1
    assert check.status == "completed"
    assert check.summary == "all good"


# ── Task cancel/delete cleanup ──────────────────────────────────


@pytest.mark.asyncio
async def test_cancel_task_cancels_monitors(db_session, queue):
    task = await queue.create(title="t", description="d", target_repo="/tmp", mode="auto")
    await db_session.execute(update(Task).where(Task.id == task.id).values(status="in_progress"))
    await db_session.commit()

    ms = MonitorSession(task_id=task.id, description="watch", status="running")
    db_session.add(ms)
    await db_session.commit()
    await db_session.refresh(ms)

    await queue.cancel(task.id)

    await db_session.refresh(ms)
    assert ms.status == "cancelled"
    assert ms.completed_at is not None


@pytest.mark.asyncio
async def test_delete_task_cleans_monitors(db_session, queue):
    task = await queue.create(title="t", description="d", target_repo="/tmp", mode="auto")

    ms = MonitorSession(task_id=task.id, description="watch")
    db_session.add(ms)
    await db_session.commit()
    await db_session.refresh(ms)

    check = MonitorCheck(monitor_session_id=ms.id, check_number=1, status="completed")
    db_session.add(check)
    await db_session.commit()

    result = await queue.delete(task.id)
    assert result is True

    checks = (await db_session.execute(select(MonitorCheck))).scalars().all()
    sessions = (await db_session.execute(select(MonitorSession))).scalars().all()
    assert len(checks) == 0
    assert len(sessions) == 0


# ── API permission tests ──────────────────────────────────────


@pytest.mark.asyncio
async def test_api_create_monitor_requires_auto_mode(client, session_factory):
    async with session_factory() as db:
        task = Task(title="t", description="d", mode="loop", status="in_progress")
        db.add(task)
        await db.commit()
        await db.refresh(task)
        task_id = task.id

    resp = await client.post(f"/api/tasks/{task_id}/monitor-sessions", json={"description": "watch"})
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_api_create_monitor_requires_active_task(client, session_factory):
    async with session_factory() as db:
        task = Task(title="t", description="d", mode="auto", status="completed")
        db.add(task)
        await db.commit()
        await db.refresh(task)
        task_id = task.id

    resp = await client.post(f"/api/tasks/{task_id}/monitor-sessions", json={"description": "watch"})
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_api_create_monitor_not_found(client):
    resp = await client.post("/api/tasks/99999/monitor-sessions", json={"description": "watch"})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_api_delete_system_monitor_forbidden(client, session_factory):
    async with session_factory() as db:
        task = Task(title="t", description="d", mode="auto", status="in_progress")
        db.add(task)
        await db.commit()
        await db.refresh(task)

        ms = MonitorSession(task_id=task.id, description="system", source="loop")
        db.add(ms)
        await db.commit()
        await db.refresh(ms)

    resp = await client.delete(f"/api/tasks/{task.id}/monitor-sessions/{ms.id}")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_api_delete_manual_monitor(client, session_factory):
    async with session_factory() as db:
        task = Task(title="t", description="d", mode="auto", status="in_progress")
        db.add(task)
        await db.commit()
        await db.refresh(task)

        ms = MonitorSession(task_id=task.id, description="manual", source="manual")
        db.add(ms)
        await db.commit()
        await db.refresh(ms)

    resp = await client.delete(f"/api/tasks/{task.id}/monitor-sessions/{ms.id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "cancelled"


@pytest.mark.asyncio
async def test_api_list_monitor_sessions(client, session_factory):
    async with session_factory() as db:
        task = Task(title="t", description="d", mode="auto", status="in_progress")
        db.add(task)
        await db.commit()
        await db.refresh(task)

        ms1 = MonitorSession(task_id=task.id, description="m1")
        ms2 = MonitorSession(task_id=task.id, description="m2")
        db.add_all([ms1, ms2])
        await db.commit()

    resp = await client.get(f"/api/tasks/{task.id}/monitor-sessions")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2


@pytest.mark.asyncio
async def test_api_get_checks(client, session_factory):
    async with session_factory() as db:
        task = Task(title="t", description="d", mode="auto", status="in_progress")
        db.add(task)
        await db.commit()
        await db.refresh(task)

        ms = MonitorSession(task_id=task.id, description="m")
        db.add(ms)
        await db.commit()
        await db.refresh(ms)

        c1 = MonitorCheck(monitor_session_id=ms.id, check_number=1, status="completed", summary="ok")
        c2 = MonitorCheck(monitor_session_id=ms.id, check_number=2, status="failed", summary="error")
        db.add_all([c1, c2])
        await db.commit()

    resp = await client.get(f"/api/tasks/{task.id}/monitor-sessions/{ms.id}/checks")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2


@pytest.mark.asyncio
async def test_api_task_id_mismatch_404(client, session_factory):
    async with session_factory() as db:
        task1 = Task(title="t1", description="d", mode="auto", status="in_progress")
        task2 = Task(title="t2", description="d", mode="auto", status="in_progress")
        db.add_all([task1, task2])
        await db.commit()
        await db.refresh(task1)
        await db.refresh(task2)

        ms = MonitorSession(task_id=task1.id, description="m")
        db.add(ms)
        await db.commit()
        await db.refresh(ms)

    resp = await client.get(f"/api/tasks/{task2.id}/monitor-sessions/{ms.id}/checks")
    assert resp.status_code == 404


# ── Dispatcher cleanup ──────────────────────────────────────


@pytest.mark.asyncio
async def test_stale_monitor_cleanup(db_factory):
    from backend.services.instance_manager import InstanceManager
    from backend.services.ws_broadcaster import WebSocketBroadcaster
    from backend.services.dispatcher import GlobalDispatcher

    broadcaster = WebSocketBroadcaster()
    im = InstanceManager(db_factory=db_factory, broadcaster=broadcaster)
    dispatcher = GlobalDispatcher(db_factory=db_factory, instance_manager=im, broadcaster=broadcaster)

    async with db_factory() as db:
        task = Task(title="t", description="d", mode="auto", status="completed")
        db.add(task)
        await db.commit()
        await db.refresh(task)

        ms = MonitorSession(task_id=task.id, description="stale", status="running")
        db.add(ms)
        await db.commit()
        await db.refresh(ms)
        ms_id = ms.id

    await dispatcher._cleanup_stale_state()

    async with db_factory() as db:
        ms = await db.get(MonitorSession, ms_id)
        assert ms.status == "failed"
        assert ms.completed_at is not None
