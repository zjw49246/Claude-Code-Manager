"""Tests for Monitor API endpoints."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from sqlalchemy import select, update

from backend.models.task import Task
from backend.models.monitor_session import MonitorSession, MonitorCheck


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

    with patch("backend.main.dispatcher", mock_dispatcher):
        resp = await client.post(f"/api/tasks/{task_id}/cancel")
    assert resp.status_code == 200

    async with session_factory() as db:
        ms = await db.get(MonitorSession, ms_id)
        assert ms.status == "cancelled"


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
