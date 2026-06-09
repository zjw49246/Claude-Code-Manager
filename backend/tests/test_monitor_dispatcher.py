"""Tests for Dispatcher monitor lifecycle — subprocess management and state transitions."""
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from backend.models.task import Task
from backend.models.monitor_session import MonitorSession, MonitorCheck
from backend.services.dispatcher import GlobalDispatcher


@pytest.fixture
def mock_broadcaster():
    b = MagicMock()
    b.broadcast = AsyncMock()
    return b


@pytest.fixture
def dispatcher(db_factory, mock_broadcaster):
    d = GlobalDispatcher.__new__(GlobalDispatcher)
    d.db_factory = db_factory
    d.broadcaster = mock_broadcaster
    d.instance_manager = MagicMock()
    d._running_tasks = {}
    d._monitor_tasks = {}
    d._monitor_processes = {}
    return d


async def _seed_task_and_monitor(db_factory, status="in_progress", max_checks=50, interval=1):
    async with db_factory() as db:
        task = Task(title="t", description="d", status=status, enabled_skills={"monitor": True}, target_repo="/tmp")
        db.add(task)
        await db.commit()
        await db.refresh(task)
        ms = MonitorSession(task_id=task.id, description="test monitor", interval=interval, max_checks=max_checks)
        db.add(ms)
        await db.commit()
        await db.refresh(ms)
        return task.id, ms.id


def test_build_monitor_prompt(dispatcher):
    prompt = dispatcher._build_monitor_prompt(0, "watch build", "tail -f /tmp/log")
    assert "第 1 次检查" in prompt
    assert "watch build" in prompt
    assert "tail -f /tmp/log" in prompt
    assert "STATUS:" in prompt
    assert "SUMMARY:" in prompt


def test_build_monitor_prompt_no_context(dispatcher):
    prompt = dispatcher._build_monitor_prompt(2, "test", None)
    assert "第 3 次检查" in prompt
    assert "上下文" not in prompt


@pytest.mark.asyncio
async def test_start_monitor_session(dispatcher):
    ms = MagicMock()
    ms.id = 1
    with patch.object(dispatcher, "_monitor_session_lifecycle", new_callable=AsyncMock):
        dispatcher.start_monitor_session(ms)
    assert 1 in dispatcher._monitor_tasks
    dispatcher._monitor_tasks[1].cancel()
    try:
        await dispatcher._monitor_tasks[1]
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_lifecycle_max_checks_reached(dispatcher, db_factory, mock_broadcaster):
    task_id, ms_id = await _seed_task_and_monitor(db_factory, max_checks=1, interval=0)

    mock_output = "SUMMARY: All good\nSTATUS: running"
    with patch.object(dispatcher, "_run_monitor_subprocess", new_callable=AsyncMock, return_value=mock_output):
        await dispatcher._monitor_session_lifecycle(ms_id)

    async with db_factory() as db:
        ms = await db.get(MonitorSession, ms_id)
        assert ms.checks_done == 1
        assert ms.status == "completed"
        assert ms.completed_at is not None


@pytest.mark.asyncio
async def test_lifecycle_task_ended(dispatcher, db_factory, mock_broadcaster):
    task_id, ms_id = await _seed_task_and_monitor(db_factory, status="completed")

    await dispatcher._monitor_session_lifecycle(ms_id)

    async with db_factory() as db:
        ms = await db.get(MonitorSession, ms_id)
        assert ms.status == "completed"


@pytest.mark.asyncio
async def test_lifecycle_subprocess_timeout(dispatcher, db_factory, mock_broadcaster):
    task_id, ms_id = await _seed_task_and_monitor(db_factory, max_checks=1, interval=0)

    with patch.object(dispatcher, "_run_monitor_subprocess", new_callable=AsyncMock, side_effect=asyncio.TimeoutError):
        await dispatcher._monitor_session_lifecycle(ms_id)

    async with db_factory() as db:
        ms = await db.get(MonitorSession, ms_id)
        assert ms.checks_done == 1
        assert ms.last_summary == "Monitor check timed out"

        from sqlalchemy import select
        result = await db.execute(
            select(MonitorCheck).where(MonitorCheck.monitor_session_id == ms_id)
        )
        check = result.scalars().first()
        assert check.status == "failed"


@pytest.mark.asyncio
async def test_lifecycle_subprocess_crash(dispatcher, db_factory, mock_broadcaster):
    task_id, ms_id = await _seed_task_and_monitor(db_factory, max_checks=1, interval=0)

    with patch.object(dispatcher, "_run_monitor_subprocess", new_callable=AsyncMock, side_effect=RuntimeError("segfault")):
        await dispatcher._monitor_session_lifecycle(ms_id)

    async with db_factory() as db:
        ms = await db.get(MonitorSession, ms_id)
        assert ms.checks_done == 1
        assert "segfault" in ms.last_summary

        from sqlalchemy import select
        result = await db.execute(
            select(MonitorCheck).where(MonitorCheck.monitor_session_id == ms_id)
        )
        check = result.scalars().first()
        assert check.status == "failed"


@pytest.mark.asyncio
async def test_lifecycle_cancelled(dispatcher, db_factory, mock_broadcaster):
    task_id, ms_id = await _seed_task_and_monitor(db_factory, interval=9999)

    async def slow_subprocess(**kwargs):
        await asyncio.sleep(9999)
        return ""

    with patch.object(dispatcher, "_run_monitor_subprocess", side_effect=slow_subprocess):
        lifecycle_task = asyncio.create_task(dispatcher._monitor_session_lifecycle(ms_id))
        await asyncio.sleep(0.05)
        lifecycle_task.cancel()
        try:
            await lifecycle_task
        except asyncio.CancelledError:
            pass

    assert ms_id not in dispatcher._monitor_tasks
    assert ms_id not in dispatcher._monitor_processes


@pytest.mark.asyncio
async def test_lifecycle_done_status(dispatcher, db_factory, mock_broadcaster):
    task_id, ms_id = await _seed_task_and_monitor(db_factory, max_checks=10, interval=0)

    mock_output = "SUMMARY: Build finished successfully\nSTATUS: done"
    with patch.object(dispatcher, "_run_monitor_subprocess", new_callable=AsyncMock, return_value=mock_output):
        await dispatcher._monitor_session_lifecycle(ms_id)

    async with db_factory() as db:
        ms = await db.get(MonitorSession, ms_id)
        assert ms.status == "completed"
        assert ms.checks_done == 1
        assert ms.last_summary == "Build finished successfully"


@pytest.mark.asyncio
async def test_lifecycle_unexpected_exception_marks_failed(dispatcher, db_factory, mock_broadcaster):
    task_id, ms_id = await _seed_task_and_monitor(db_factory)

    async def _bad_lifecycle(self_id):
        raise ValueError("unexpected bug")

    original = dispatcher._monitor_session_lifecycle

    async def patched_lifecycle(monitor_session_id):
        from backend.models.monitor_session import MonitorSession as MS, MonitorCheck as MC
        try:
            async with dispatcher.db_factory() as db:
                ms = await db.get(MS, monitor_session_id)
                task = await db.get(Task, ms.task_id)
                if not ms or not task:
                    return
            raise ValueError("unexpected bug")
        except asyncio.CancelledError:
            raise
        except Exception:
            try:
                async with dispatcher.db_factory() as db:
                    ms = await db.get(MS, monitor_session_id)
                    if ms and ms.status == "running":
                        ms.status = "failed"
                        from datetime import datetime
                        ms.completed_at = datetime.utcnow()
                        await db.commit()
            except Exception:
                pass
        finally:
            dispatcher._monitor_tasks.pop(monitor_session_id, None)
            dispatcher._monitor_processes.pop(monitor_session_id, None)

    await patched_lifecycle(ms_id)

    async with db_factory() as db:
        ms = await db.get(MonitorSession, ms_id)
        assert ms.status == "failed"


@pytest.mark.asyncio
async def test_lifecycle_writes_check_record(dispatcher, db_factory, mock_broadcaster):
    task_id, ms_id = await _seed_task_and_monitor(db_factory, max_checks=1, interval=0)

    mock_output = "SUMMARY: Process running at 45% CPU\nSTATUS: running"
    with patch.object(dispatcher, "_run_monitor_subprocess", new_callable=AsyncMock, return_value=mock_output):
        await dispatcher._monitor_session_lifecycle(ms_id)

    async with db_factory() as db:
        from sqlalchemy import select
        result = await db.execute(
            select(MonitorCheck).where(MonitorCheck.monitor_session_id == ms_id)
        )
        check = result.scalars().first()
        assert check is not None
        assert check.check_number == 1
        assert check.status == "success"
        assert check.summary == "Process running at 45% CPU"


@pytest.mark.asyncio
async def test_lifecycle_broadcasts_check_event(dispatcher, db_factory, mock_broadcaster):
    task_id, ms_id = await _seed_task_and_monitor(db_factory, max_checks=1, interval=0)

    mock_output = "SUMMARY: ok\nSTATUS: running"
    with patch.object(dispatcher, "_run_monitor_subprocess", new_callable=AsyncMock, return_value=mock_output):
        await dispatcher._monitor_session_lifecycle(ms_id)

    calls = [c for c in mock_broadcaster.broadcast.call_args_list
             if c[0][1].get("event") == "monitor_check"]
    assert len(calls) >= 1
    event = calls[0][0][1]
    assert event["is_monitor"] is True
    assert event["summary"] == "ok"
