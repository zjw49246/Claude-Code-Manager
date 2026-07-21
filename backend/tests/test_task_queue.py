"""Tests for TaskQueue — priority ordering, dequeue, status transitions."""
import asyncio

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.database import Base
from backend.models.task import Task
from backend.services.task_queue import TaskQueue


@pytest_asyncio.fixture
async def queue(db_session):
    return TaskQueue(db_session)


@pytest.mark.asyncio
async def test_create_task(queue):
    task = await queue.create(
        title="Test task",
        description="Do something",
        target_repo="/tmp/repo",
    )
    assert task.id is not None
    assert task.title == "Test task"
    assert task.status == "pending"
    assert task.priority == 0


@pytest.mark.asyncio
async def test_dequeue_priority_order(queue):
    """P0 should be dequeued before P1 (lower number = higher priority)."""
    await queue.create(title="Low priority", description="d", target_repo="/tmp", priority=10)
    await queue.create(title="High priority", description="d", target_repo="/tmp", priority=0)
    await queue.create(title="Medium priority", description="d", target_repo="/tmp", priority=5)

    first = await queue.dequeue()
    assert first is not None
    assert first.title == "High priority"
    assert first.priority == 0
    assert first.status == "in_progress"

    second = await queue.dequeue()
    assert second is not None
    assert second.title == "Medium priority"
    assert second.priority == 5

    third = await queue.dequeue()
    assert third is not None
    assert third.title == "Low priority"
    assert third.priority == 10


@pytest.mark.asyncio
async def test_dequeue_fifo_within_same_priority(queue):
    """Tasks with the same priority should be dequeued in FIFO order."""
    await queue.create(title="First", description="d", target_repo="/tmp", priority=0)
    await queue.create(title="Second", description="d", target_repo="/tmp", priority=0)

    first = await queue.dequeue()
    assert first.title == "First"
    second = await queue.dequeue()
    assert second.title == "Second"


@pytest.mark.asyncio
async def test_concurrent_dequeue_claims_each_task_once(tmp_path):
    """Independent Ralph/dispatcher sessions cannot claim the same row."""

    db_path = tmp_path / "atomic-dequeue.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(
            engine, class_=AsyncSession, expire_on_commit=False,
        )
        async with factory() as db:
            first = Task(title="first", description="d", priority=0)
            second = Task(title="second", description="d", priority=1)
            db.add_all([first, second])
            await db.commit()
            first_id, second_id = first.id, second.id

        async with factory() as db1, factory() as db2:
            claimed = await asyncio.gather(
                TaskQueue(db1).dequeue(),
                TaskQueue(db2).dequeue(),
            )

        assert {task.id for task in claimed if task is not None} == {
            first_id,
            second_id,
        }
        assert len([task for task in claimed if task is not None]) == 2
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_dequeue_returns_none_when_empty(queue):
    result = await queue.dequeue()
    assert result is None


@pytest.mark.asyncio
async def test_dequeue_skips_temporarily_excluded_task(queue):
    waiting = await queue.create(
        title="waiting-codex", description="d", target_repo="/tmp", priority=0,
    )
    runnable = await queue.create(
        title="runnable", description="d", target_repo="/tmp", priority=1,
    )

    selected = await queue.dequeue(exclude_ids={waiting.id})

    assert selected is not None
    assert selected.id == runnable.id
    assert (await queue.get(waiting.id)).status == "pending"


@pytest.mark.asyncio
async def test_mark_completed(queue):
    task = await queue.create(title="t", description="d", target_repo="/tmp")
    await queue.mark_completed(task.id)
    updated = await queue.get(task.id)
    assert updated.status == "completed"
    assert updated.completed_at is not None


@pytest.mark.asyncio
async def test_mark_failed(queue):
    task = await queue.create(title="t", description="d", target_repo="/tmp")
    await queue.mark_failed(task.id, "something broke")
    updated = await queue.get(task.id)
    assert updated.status == "failed"
    assert updated.error_message == "something broke"


@pytest.mark.asyncio
async def test_mark_status_generic(queue):
    task = await queue.create(title="t", description="d", target_repo="/tmp")
    await queue.mark_status(task.id, "executing")
    updated = await queue.get(task.id)
    assert updated.status == "executing"


@pytest.mark.asyncio
async def test_retry_increments_count(queue):
    task = await queue.create(title="t", description="d", target_repo="/tmp")
    await queue.mark_failed(task.id, "error")
    retried = await queue.retry(task.id)
    assert retried.status == "pending"
    assert retried.retry_count == 1
    assert retried.error_message is None


@pytest.mark.asyncio
async def test_defer_returns_active_task_without_consuming_retry_budget(queue):
    task = await queue.create(title="t", description="d", target_repo="/tmp")
    task_id = task.id
    claimed = await queue.dequeue()
    assert claimed.id == task_id
    claimed.instance_id = 99
    await queue.db.commit()

    assert await queue.defer(task_id, "all Codex accounts cooling down") is True

    queue.db.expire_all()
    deferred = await queue.get(task_id)
    assert deferred.status == "pending"
    assert deferred.retry_count == 0
    assert deferred.instance_id is None
    assert deferred.started_at is None
    assert deferred.completed_at is None
    assert deferred.error_message == "all Codex accounts cooling down"


@pytest.mark.asyncio
async def test_defer_does_not_resurrect_cancelled_task(queue):
    task = await queue.create(title="t", description="d", target_repo="/tmp")
    task_id = task.id
    await queue.dequeue()
    await queue.cancel(task_id)

    assert await queue.defer(task_id, "temporary routing failure") is False

    queue.db.expire_all()
    assert (await queue.get(task_id)).status == "cancelled"


@pytest.mark.asyncio
async def test_cancel_task(queue):
    task = await queue.create(title="t", description="d", target_repo="/tmp")
    cancelled = await queue.cancel(task.id)
    assert cancelled.status == "cancelled"


@pytest.mark.asyncio
async def test_cancel_executing_task(queue):
    """Should be able to cancel tasks in executing/merging states."""
    task = await queue.create(title="t", description="d", target_repo="/tmp")
    await queue.mark_status(task.id, "executing")
    cancelled = await queue.cancel(task.id)
    assert cancelled.status == "cancelled"


@pytest.mark.asyncio
async def test_delete_conflict_task(queue):
    """Should be able to delete tasks in conflict state."""
    task = await queue.create(title="t", description="d", target_repo="/tmp")
    await queue.mark_status(task.id, "conflict")
    result = await queue.delete(task.id)
    assert result is True


@pytest.mark.asyncio
async def test_delete_running_task_rejected(queue):
    """Should NOT be able to delete in_progress tasks."""
    task = await queue.create(title="t", description="d", target_repo="/tmp")
    _ = await queue.dequeue()  # sets to in_progress
    result = await queue.delete(task.id)
    assert result is False


@pytest.mark.asyncio
async def test_list_tasks_ordered(queue):
    await queue.create(title="B", description="d", target_repo="/tmp", priority=5)
    await queue.create(title="A", description="d", target_repo="/tmp", priority=1)
    tasks = await queue.list_tasks()
    assert tasks[0].title == "A"
    assert tasks[1].title == "B"


@pytest.mark.asyncio
async def test_list_tasks_filter_status(queue):
    await queue.create(title="pending", description="d", target_repo="/tmp")
    t2 = await queue.create(title="done", description="d", target_repo="/tmp")
    await queue.mark_completed(t2.id)
    pending = await queue.list_tasks(status="pending")
    assert len(pending) == 1
    assert pending[0].title == "pending"


# === Dequeue picks any pending task ===


@pytest.mark.asyncio
async def test_dequeue_picks_any_model_task(queue):
    """dequeue() picks any pending task regardless of model."""
    await queue.create(title="opus-task", description="d", target_repo="/tmp", model="opus")
    task = await queue.dequeue()
    assert task is not None
    assert task.title == "opus-task"


@pytest.mark.asyncio
async def test_dequeue_picks_any_provider_task(queue):
    """dequeue() picks any pending task regardless of provider."""
    await queue.create(title="codex-task", description="d", target_repo="/tmp", provider="codex")
    task = await queue.dequeue()
    assert task is not None
    assert task.title == "codex-task"
