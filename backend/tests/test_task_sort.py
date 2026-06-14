"""Tests for task sorting: star toggle sort_order recalc, auto_sort_on_access setting,
and list_tasks ordering."""
import pytest
import pytest_asyncio
from datetime import datetime, timedelta
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.task import Task
from backend.models.global_settings import GlobalSettings
from backend.services.task_queue import TaskQueue


@pytest_asyncio.fixture
async def queue(db_session):
    return TaskQueue(db_session)


# ---------------------------------------------------------------------------
# Star toggle: sort_order recalculation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_star_moves_task_to_top_of_starred_group(queue: TaskQueue, db_session: AsyncSession):
    """Starring a task should place it at the top of the starred group."""
    t1 = await queue.create(title="Already starred", description="d", target_repo="/tmp")
    t1.starred = True
    t1.sort_order = 1000.0
    await db_session.commit()

    t2 = await queue.create(title="Will be starred", description="d", target_repo="/tmp")
    t2.sort_order = 500.0
    await db_session.commit()

    result = await queue.star(t2.id)
    assert result.starred is True
    assert result.sort_order > 1000.0  # should be max(starred group) + 60


@pytest.mark.asyncio
async def test_unstar_moves_task_to_top_of_non_starred_group(queue: TaskQueue, db_session: AsyncSession):
    """Unstarring a task should place it at the top of the non-starred group."""
    t1 = await queue.create(title="Non-starred", description="d", target_repo="/tmp")
    t1.sort_order = 800.0
    await db_session.commit()

    t2 = await queue.create(title="Will be unstarred", description="d", target_repo="/tmp")
    t2.starred = True
    t2.sort_order = 200.0  # low value in starred group
    await db_session.commit()

    result = await queue.star(t2.id)
    assert result.starred is False
    assert result.sort_order > 800.0  # should be max(non-starred group) + 60


@pytest.mark.asyncio
async def test_star_empty_group_uses_timestamp(queue: TaskQueue, db_session: AsyncSession):
    """Starring when no other starred tasks exist should set sort_order to current timestamp."""
    t = await queue.create(title="Only star", description="d", target_repo="/tmp")

    result = await queue.star(t.id)
    assert result.starred is True
    assert result.sort_order is not None
    # Should be roughly current timestamp
    now = datetime.utcnow().timestamp()
    assert abs(result.sort_order - now) < 10


@pytest.mark.asyncio
async def test_star_preserves_other_tasks_order(queue: TaskQueue, db_session: AsyncSession):
    """Starring should not change sort_order of other tasks."""
    t1 = await queue.create(title="Stays", description="d", target_repo="/tmp")
    t1.sort_order = 500.0
    await db_session.commit()

    t2 = await queue.create(title="Gets starred", description="d", target_repo="/tmp")
    t2.sort_order = 600.0
    await db_session.commit()

    await queue.star(t2.id)
    await db_session.refresh(t1)
    assert t1.sort_order == 500.0


# ---------------------------------------------------------------------------
# list_tasks ordering
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_tasks_starred_first(queue: TaskQueue, db_session: AsyncSession):
    """Starred tasks should appear before non-starred regardless of sort_order."""
    t_non = await queue.create(title="Non-starred high key", description="d", target_repo="/tmp")
    t_non.sort_order = 99999.0
    await db_session.commit()

    t_star = await queue.create(title="Starred low key", description="d", target_repo="/tmp")
    t_star.starred = True
    t_star.sort_order = 1.0
    await db_session.commit()

    tasks = await queue.list_tasks()
    assert tasks[0].id == t_star.id
    assert tasks[1].id == t_non.id


@pytest.mark.asyncio
async def test_list_tasks_sort_by_effective_key_desc(queue: TaskQueue, db_session: AsyncSession):
    """Within same starred group, higher effective_key comes first."""
    t1 = await queue.create(title="Lower", description="d", target_repo="/tmp")
    t1.sort_order = 100.0
    t2 = await queue.create(title="Higher", description="d", target_repo="/tmp")
    t2.sort_order = 200.0
    await db_session.commit()

    tasks = await queue.list_tasks()
    assert tasks[0].id == t2.id
    assert tasks[1].id == t1.id


# ---------------------------------------------------------------------------
# Auto sort on access setting (via API)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_auto_sort_on_access_default_true(client: AsyncClient):
    """Default value of auto_sort_on_access should be True."""
    resp = await client.get("/api/settings/runtime")
    assert resp.status_code == 200
    data = resp.json()
    assert data["auto_sort_on_access"] is True


@pytest.mark.asyncio
async def test_toggle_auto_sort_on_access(client: AsyncClient):
    """Should be able to toggle auto_sort_on_access."""
    resp = await client.put("/api/settings/runtime", json={"auto_sort_on_access": False})
    assert resp.status_code == 200
    assert resp.json()["auto_sort_on_access"] is False

    resp = await client.get("/api/settings/runtime")
    assert resp.json()["auto_sort_on_access"] is False

    resp = await client.put("/api/settings/runtime", json={"auto_sort_on_access": True})
    assert resp.status_code == 200
    assert resp.json()["auto_sort_on_access"] is True


@pytest.mark.asyncio
async def test_partial_update_runtime_settings(client: AsyncClient):
    """Updating only auto_sort_on_access should not affect use_pty_mode."""
    resp = await client.put("/api/settings/runtime", json={"auto_sort_on_access": False})
    assert resp.status_code == 200
    data = resp.json()
    assert data["auto_sort_on_access"] is False
    assert "use_pty_mode" in data  # should still be present


@pytest.mark.asyncio
async def test_touch_skips_sort_order_when_disabled(client: AsyncClient, session_factory):
    """When auto_sort_on_access is False, touch should update last_accessed_at but not sort_order."""
    # Create two tasks
    async with session_factory() as session:
        t1 = Task(title="Task 1", description="d", target_repo="/tmp", sort_order=1000.0)
        t2 = Task(title="Task 2", description="d", target_repo="/tmp", sort_order=2000.0)
        session.add_all([t1, t2])
        await session.commit()
        t1_id = t1.id

    # Disable auto sort
    await client.put("/api/settings/runtime", json={"auto_sort_on_access": False})

    # Touch task 1 (which has lower sort_order)
    resp = await client.get(f"/api/tasks/{t1_id}/chat/history?touch=true")
    assert resp.status_code == 200

    # Verify sort_order was NOT changed
    async with session_factory() as session:
        task = await session.get(Task, t1_id)
        assert task.sort_order == 1000.0
        assert task.last_accessed_at is not None  # but last_accessed_at was updated


@pytest.mark.asyncio
async def test_touch_updates_sort_order_when_enabled(client: AsyncClient, session_factory):
    """When auto_sort_on_access is True (default), touch should update sort_order."""
    async with session_factory() as session:
        t1 = Task(title="Task 1", description="d", target_repo="/tmp", sort_order=1000.0)
        t2 = Task(title="Task 2", description="d", target_repo="/tmp", sort_order=2000.0)
        session.add_all([t1, t2])
        await session.commit()
        t1_id = t1.id

    # Touch task 1 (lower sort_order) — should move it to top
    resp = await client.get(f"/api/tasks/{t1_id}/chat/history?touch=true")
    assert resp.status_code == 200

    async with session_factory() as session:
        task = await session.get(Task, t1_id)
        assert task.sort_order > 2000.0  # moved above task 2
