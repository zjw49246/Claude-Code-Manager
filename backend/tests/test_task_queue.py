"""Tests for TaskQueue — priority ordering, dequeue, status transitions."""
import pytest
import pytest_asyncio

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
async def test_dequeue_returns_none_when_empty(queue):
    result = await queue.dequeue()
    assert result is None


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


# === Model-based dequeue tests ===


@pytest.mark.asyncio
async def test_dequeue_no_model_instance_only_picks_null_model_tasks(queue):
    """instance_model=None/default only picks up tasks with model=None."""
    await queue.create(title="has-model", description="d", target_repo="/tmp", model="opus")
    await queue.create(title="no-model", description="d", target_repo="/tmp")

    task = await queue.dequeue(instance_model=None)
    assert task is not None
    assert task.title == "no-model"
    assert task.model is None


@pytest.mark.asyncio
async def test_dequeue_default_instance_picks_default_model_tasks(queue):
    """instance_model='default' picks tasks whose model matches the configured default_model."""
    await queue.create(title="opus-task", description="d", target_repo="/tmp", model="opus")

    task = await queue.dequeue(instance_model="default")
    assert task is not None
    assert task.title == "opus-task"


@pytest.mark.asyncio
async def test_dequeue_default_instance_skips_non_default_model_tasks(queue):
    """instance_model='default' does not pick tasks with a non-default model."""
    await queue.create(title="sonnet-task", description="d", target_repo="/tmp", model="sonnet")

    task = await queue.dequeue(instance_model="default")
    assert task is None


@pytest.mark.asyncio
async def test_dequeue_specific_model_picks_exact_match(queue):
    """Specific model instance picks tasks with the matching model."""
    await queue.create(title="opus-task", description="d", target_repo="/tmp", model="opus")

    task = await queue.dequeue(instance_model="opus")
    assert task is not None
    assert task.title == "opus-task"
    assert task.model == "opus"
    assert task.status == "in_progress"


@pytest.mark.asyncio
async def test_dequeue_specific_model_also_picks_null_model_tasks(queue):
    """Specific model instance falls back to tasks with no model when nothing matches."""
    await queue.create(title="null-model", description="d", target_repo="/tmp")

    task = await queue.dequeue(instance_model="sonnet")
    assert task is not None
    assert task.title == "null-model"


@pytest.mark.asyncio
async def test_dequeue_specific_model_prefers_exact_over_null(queue):
    """Exact model match is preferred over null-model tasks."""
    await queue.create(title="null-model", description="d", target_repo="/tmp", priority=0)
    await queue.create(title="opus-task", description="d", target_repo="/tmp", model="opus", priority=5)

    task = await queue.dequeue(instance_model="opus")
    assert task is not None
    assert task.title == "opus-task"


@pytest.mark.asyncio
async def test_dequeue_does_not_steal_other_model_tasks(queue):
    """Sonnet instance should not pick an opus-only task."""
    await queue.create(title="opus-task", description="d", target_repo="/tmp", model="opus")

    task = await queue.dequeue(instance_model="sonnet")
    # No null-model task either, so returns None... wait, opus != sonnet but the rule
    # is: pick exact OR null. opus-task has model="opus", not null, so sonnet can't pick it.
    assert task is None


@pytest.mark.asyncio
async def test_dequeue_model_priority_order(queue):
    """Among same model tasks, priority ordering is preserved."""
    await queue.create(title="low", description="d", target_repo="/tmp", model="opus", priority=10)
    await queue.create(title="high", description="d", target_repo="/tmp", model="opus", priority=0)

    first = await queue.dequeue(instance_model="opus")
    assert first.title == "high"

    second = await queue.dequeue(instance_model="opus")
    assert second.title == "low"


@pytest.mark.asyncio
async def test_dequeue_no_args_backward_compat(queue):
    """Calling dequeue() with no args still works (backward compatibility)."""
    await queue.create(title="null-task", description="d", target_repo="/tmp")
    task = await queue.dequeue()
    assert task is not None
    assert task.title == "null-task"
