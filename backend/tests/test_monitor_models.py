"""Tests for Monitor data layer — MonitorSession/MonitorCheck CRUD and enabled_skills."""
import pytest
from sqlalchemy import select

from backend.models.task import Task
from backend.models.monitor_session import MonitorSession, MonitorCheck


@pytest.mark.asyncio
async def test_monitor_session_crud(db_session):
    task = Task(title="t", description="d", enabled_skills={"monitor": True})
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    ms = MonitorSession(
        task_id=task.id,
        description="watch build",
        monitor_context="tail -f /tmp/build.log",
        interval=60,
        max_checks=10,
    )
    db_session.add(ms)
    await db_session.commit()
    await db_session.refresh(ms)

    assert ms.id is not None
    assert ms.status == "running"
    assert ms.checks_done == 0
    assert ms.interval == 60
    assert ms.max_checks == 10
    assert ms.completed_at is None

    loaded = await db_session.get(MonitorSession, ms.id)
    assert loaded.description == "watch build"
    assert loaded.monitor_context == "tail -f /tmp/build.log"


@pytest.mark.asyncio
async def test_monitor_check_crud(db_session):
    ms = MonitorSession(task_id=1, description="test monitor")
    db_session.add(ms)
    await db_session.commit()
    await db_session.refresh(ms)

    check = MonitorCheck(
        monitor_session_id=ms.id,
        check_number=1,
        status="success",
        summary="All good",
        full_output="detailed output here",
    )
    db_session.add(check)
    await db_session.commit()
    await db_session.refresh(check)

    assert check.id is not None
    assert check.check_number == 1
    assert check.status == "success"
    assert check.summary == "All good"


@pytest.mark.asyncio
async def test_monitor_session_defaults(db_session):
    ms = MonitorSession(task_id=1, description="minimal")
    db_session.add(ms)
    await db_session.commit()
    await db_session.refresh(ms)

    assert ms.interval == 120
    assert ms.max_checks == 50
    assert ms.model is None
    assert ms.status == "running"
    assert ms.checks_done == 0


@pytest.mark.asyncio
async def test_enabled_skills_json_field(db_session):
    task = Task(title="t", description="d", enabled_skills={"monitor": True})
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    assert task.enabled_skills == {"monitor": True}
    assert task.enabled_skills.get("monitor") is True


@pytest.mark.asyncio
async def test_enabled_skills_none(db_session):
    task = Task(title="t", description="d")
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    assert task.enabled_skills is None


@pytest.mark.asyncio
async def test_enabled_skills_multiple(db_session):
    task = Task(title="t", description="d", enabled_skills={"monitor": True, "worker": False})
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    assert task.enabled_skills["monitor"] is True
    assert task.enabled_skills["worker"] is False


@pytest.mark.asyncio
async def test_multiple_checks_per_session(db_session):
    ms = MonitorSession(task_id=1, description="multi-check")
    db_session.add(ms)
    await db_session.commit()
    await db_session.refresh(ms)

    for i in range(3):
        db_session.add(MonitorCheck(
            monitor_session_id=ms.id,
            check_number=i + 1,
            status="success" if i < 2 else "failed",
            summary=f"Check {i+1}",
        ))
    await db_session.commit()

    result = await db_session.execute(
        select(MonitorCheck).where(MonitorCheck.monitor_session_id == ms.id)
    )
    checks = list(result.scalars().all())
    assert len(checks) == 3
    assert checks[2].status == "failed"
