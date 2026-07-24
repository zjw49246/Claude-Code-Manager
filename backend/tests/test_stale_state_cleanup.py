"""Tests for stale state cleanup, zombie worker prevention, and orphan task handling.

Covers dispatcher ownership recovery and stale-state cleanup:
- Unowned persisted PIDs are quarantined without signalling unknown processes
- Manager-owned in-process generations survive Pause -> Start
- Unowned task claims return to pending for safe retry
- Safety-net instance/task reset after lifecycle ends
- Instance.current_task_id cleanup on task deletion
- Orphaned task handling on stop-session
- Interrupted task status change (pending → completed)
"""
import asyncio
import os
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.instance import Instance
from backend.models.task import Task
from backend.models.log_entry import LogEntry
from backend.services.dispatcher import (
    GlobalDispatcher,
    _TaskLifecycleGeneration,
)
from backend.services.task_queue import TaskQueue


# === Helpers ===

def _make_dispatcher(db_factory):
    """Create a GlobalDispatcher with mocked dependencies."""
    instance_manager = MagicMock()
    instance_manager.launch = AsyncMock(return_value=12345)
    # Lifecycle completion now waits for the output consumer to finish its
    # final persistence/account-routing work before deciding the task status.
    instance_manager.wait_for_output_consumer = AsyncMock()
    instance_manager.processes = {}
    instance_manager._tasks = {}
    instance_manager.pty_mode_enabled = False
    instance_manager.transient_error_seen = MagicMock(return_value=False)
    instance_manager.get_last_stderr = MagicMock(return_value="")
    instance_manager.get_recent_log_contents = AsyncMock(return_value=[])
    # PTY proactive pool switch path (dispatcher._run_task_lifecycle)
    instance_manager.pty_rate_limit_seen = MagicMock(return_value=False)
    instance_manager._try_proactive_pool_switch = AsyncMock()
    instance_manager._pty_rate_limit_seen = set()

    broadcaster = MagicMock()
    broadcaster.broadcast = AsyncMock()

    return GlobalDispatcher(
        db_factory=db_factory,
        instance_manager=instance_manager,
        broadcaster=broadcaster,
    )


async def _lifecycle_generation(dispatcher, db_factory, task_id):
    async with db_factory() as db:
        task = await db.get(Task, task_id)
        assert task is not None
        return dispatcher._task_lifecycle_generation(task)


# === _cleanup_stale_state tests ===


@pytest.mark.asyncio
async def test_cleanup_resets_dead_pid_instance(db_factory):
    """An unowned persisted PID is quarantined instead of treated as attachable."""
    d = _make_dispatcher(db_factory)

    async with db_factory() as db:
        inst = Instance(name="zombie-worker", status="running", pid=999999, current_task_id=42)
        db.add(inst)
        await db.commit()
        await db.refresh(inst)
        inst_id = inst.id

    await d._cleanup_stale_state()

    async with db_factory() as db:
        inst = await db.get(Instance, inst_id)
        assert inst.status == "error"
        assert inst.pid is None
        assert inst.current_task_id is None


@pytest.mark.asyncio
async def test_cleanup_preserves_manager_owned_live_generation(db_factory):
    """Pause -> Start preserves a process/consumer owned by this manager."""
    d = _make_dispatcher(db_factory)

    async with db_factory() as db:
        task = Task(
            title="live-task",
            description="test",
            status="executing",
        )
        db.add(task)
        await db.flush()
        inst = Instance(
            name="alive-worker",
            status="running",
            pid=43210,
            current_task_id=task.id,
        )
        db.add(inst)
        await db.commit()
        await db.refresh(inst)
        task.instance_id = inst.id
        await db.commit()
        inst_id = inst.id
        task_id = task.id

    d.instance_manager.processes[inst_id] = MagicMock(returncode=None)

    await d._cleanup_stale_state()

    async with db_factory() as db:
        inst = await db.get(Instance, inst_id)
        assert inst.status == "running"
        assert inst.pid == 43210
        task = await db.get(Task, task_id)
        assert task.status == "executing"
        assert task.instance_id == inst_id


@pytest.mark.asyncio
async def test_cleanup_preserves_prelaunch_lifecycle_claim(db_factory):
    """A paused lifecycle may own a slot before InstanceManager maps a process."""
    d = _make_dispatcher(db_factory)
    async with db_factory() as db:
        inst = Instance(name="prelaunch", status="idle")
        db.add(inst)
        await db.flush()
        task = Task(
            title="prelaunch",
            description="d",
            status="executing",
            instance_id=inst.id,
        )
        db.add(task)
        await db.commit()
        inst_id, task_id = inst.id, task.id

    lifecycle = asyncio.create_task(asyncio.sleep(60))
    d._running_tasks[inst_id] = lifecycle
    try:
        await d._cleanup_stale_state()
        async with db_factory() as db:
            task = await db.get(Task, task_id)
            assert task.status == "executing"
            assert task.instance_id == inst_id
    finally:
        lifecycle.cancel()
        await asyncio.gather(lifecycle, return_exceptions=True)


@pytest.mark.asyncio
async def test_cleanup_fail_closes_unowned_pid_that_may_be_alive(db_factory):
    """Unknown live PID is never auto-retried, which could duplicate writes."""
    d = _make_dispatcher(db_factory)
    async with db_factory() as db:
        task = Task(title="orphan", description="d", status="executing")
        db.add(task)
        await db.flush()
        inst = Instance(
            name="unknown-live",
            status="running",
            pid=os.getpid(),
            current_task_id=task.id,
        )
        db.add(inst)
        await db.flush()
        task.instance_id = inst.id
        await db.commit()
        task_id, inst_id = task.id, inst.id

    await d._cleanup_stale_state()

    async with db_factory() as db:
        inst = await db.get(Instance, inst_id)
        assert inst.status == "error"
        assert inst.pid == os.getpid()
        assert inst.current_task_id == task_id
        task = await db.get(Task, task_id)
        assert task.status == "failed"
        assert task.instance_id == inst_id
        assert "duplicate execution" in task.error_message


@pytest.mark.asyncio
async def test_cleanup_quarantines_idle_row_with_live_orphan_pid(db_factory):
    """``idle`` cannot make a persisted live generation dispatchable."""
    d = _make_dispatcher(db_factory)
    async with db_factory() as db:
        task = Task(title="dirty idle", description="d", status="executing")
        db.add(task)
        await db.flush()
        instance = Instance(
            name="dirty-idle-owner",
            status="idle",
            pid=os.getpid(),
            current_task_id=task.id,
        )
        db.add(instance)
        await db.flush()
        task.instance_id = instance.id
        await db.commit()
        task_id, instance_id = task.id, instance.id

    await d._cleanup_stale_state()

    async with db_factory() as db:
        instance = await db.get(Instance, instance_id)
        task = await db.get(Task, task_id)
        assert instance.status == "error"
        assert instance.pid == os.getpid()
        assert instance.current_task_id == task_id
        assert task.status == "failed"
        assert task.instance_id == instance_id


@pytest.mark.asyncio
async def test_idle_reservation_refuses_orphan_evidence(db_factory):
    """Admission independently rejects dirty idle PID/owner fields."""
    d = _make_dispatcher(db_factory)
    async with db_factory() as db:
        instance = Instance(
            name="dirty-idle",
            status="idle",
            pid=os.getpid(),
            current_task_id=987654,
        )
        db.add(instance)
        await db.commit()

    async with db_factory() as db:
        assert await d._reserve_idle_instance(db) == (None, None)


@pytest.mark.asyncio
async def test_cleanup_generation_cas_preserves_concurrent_replacement(db_factory):
    """A generation changed after SELECT wins; stale cleanup touches neither owner."""
    d = _make_dispatcher(db_factory)
    async with db_factory() as db:
        old_task = Task(title="old owner", description="d", status="executing")
        new_task = Task(title="new owner", description="d", status="executing")
        db.add_all([old_task, new_task])
        await db.flush()
        instance = Instance(
            name="owner-race",
            status="idle",
            pid=os.getpid(),
            current_task_id=old_task.id,
        )
        db.add(instance)
        await db.flush()
        old_task.instance_id = instance.id
        new_task.instance_id = instance.id
        await db.commit()
        instance_id = instance.id
        old_task_id, new_task_id = old_task.id, new_task.id

    original_execute = AsyncSession.execute
    injected = False

    async def execute_with_owner_race(session, statement, *args, **kwargs):
        nonlocal injected
        table = getattr(statement, "table", None)
        if not injected and getattr(table, "name", None) == "instances":
            injected = True
            await original_execute(
                session,
                update(Instance)
                .where(Instance.id == instance_id)
                .values(
                    status="running",
                    pid=os.getpid(),
                    current_task_id=new_task_id,
                ),
            )
        return await original_execute(session, statement, *args, **kwargs)

    with patch.object(AsyncSession, "execute", new=execute_with_owner_race):
        await d._cleanup_stale_state()

    assert injected
    async with db_factory() as db:
        instance = await db.get(Instance, instance_id)
        old_task = await db.get(Task, old_task_id)
        new_task = await db.get(Task, new_task_id)
        assert instance.status == "running"
        assert instance.pid == os.getpid()
        assert instance.current_task_id == new_task_id
        assert old_task.status == "executing"
        assert new_task.status == "executing"


@pytest.mark.asyncio
async def test_cleanup_instance_cas_includes_started_at_generation(db_factory):
    """Same owner/PID with a new start timestamp is a replacement generation."""
    from datetime import datetime, timedelta

    d = _make_dispatcher(db_factory)
    old_started = datetime(2026, 7, 23, 10, 0, 0)
    new_started = old_started + timedelta(seconds=1)
    async with db_factory() as db:
        task = Task(title="started-at ABA", status="executing")
        db.add(task)
        await db.flush()
        instance = Instance(
            name="started-at-race",
            status="running",
            pid=os.getpid(),
            current_task_id=task.id,
            started_at=old_started,
        )
        db.add(instance)
        await db.flush()
        task.instance_id = instance.id
        await db.commit()
        instance_id, task_id = instance.id, task.id

    original_execute = AsyncSession.execute
    injected = False

    async def execute_with_started_at_race(session, statement, *args, **kwargs):
        nonlocal injected
        if (
            not injected
            and getattr(getattr(statement, "table", None), "name", None)
            == "instances"
        ):
            injected = True
            await original_execute(
                session,
                update(Instance)
                .where(Instance.id == instance_id)
                .values(started_at=new_started),
            )
        return await original_execute(session, statement, *args, **kwargs)

    with patch.object(
        AsyncSession, "execute", new=execute_with_started_at_race
    ):
        await d._cleanup_stale_state()

    async with db_factory() as db:
        instance = await db.get(Instance, instance_id)
        task = await db.get(Task, task_id)
        assert instance.status == "running"
        assert instance.started_at == new_started
        assert task.status == "executing"


@pytest.mark.asyncio
async def test_pending_orphan_quarantine_never_overwrites_new_slot_owner(
    db_factory,
):
    d = _make_dispatcher(db_factory)
    async with db_factory() as db:
        task = Task(title="new pending owner", status="pending")
        db.add(task)
        await db.flush()
        orphan = Instance(
            name="old-live-orphan",
            status="running",
            pid=os.getpid(),
            current_task_id=task.id,
        )
        replacement = Instance(name="new-slot", status="idle")
        db.add_all([orphan, replacement])
        await db.flush()
        task.instance_id = replacement.id
        await db.commit()
        task_id = task.id
        orphan_id = orphan.id
        replacement_id = replacement.id

    await d._cleanup_stale_state()

    async with db_factory() as db:
        task = await db.get(Task, task_id)
        orphan = await db.get(Instance, orphan_id)
        assert task.status == "pending"
        assert task.instance_id == replacement_id
        assert orphan.status == "error"
        assert orphan.pid == os.getpid()


@pytest.mark.asyncio
async def test_cleanup_fail_closes_pending_task_still_owned_by_live_orphan(
    db_factory,
):
    """A stale pending write cannot make an unknown live PID dispatchable."""
    d = _make_dispatcher(db_factory)
    async with db_factory() as db:
        task = Task(title="dirty pending", description="d", status="pending")
        db.add(task)
        await db.flush()
        instance = Instance(
            name="dirty-live-owner",
            status="running",
            pid=os.getpid(),
            current_task_id=task.id,
        )
        db.add(instance)
        await db.commit()
        task_id, instance_id = task.id, instance.id

    await d._cleanup_stale_state()

    async with db_factory() as db:
        task = await db.get(Task, task_id)
        instance = await db.get(Instance, instance_id)
        assert task.status == "failed"
        assert task.instance_id == instance_id
        assert "duplicate execution" in task.error_message
        assert instance.status == "error"
        assert instance.pid == os.getpid()
        assert instance.current_task_id == task_id


@pytest.mark.asyncio
async def test_cleanup_resets_instance_with_no_pid(db_factory):
    """A running row without an owned generation is terminal error history."""
    d = _make_dispatcher(db_factory)

    async with db_factory() as db:
        inst = Instance(name="no-pid-worker", status="running", pid=None)
        db.add(inst)
        await db.commit()
        await db.refresh(inst)
        inst_id = inst.id

    await d._cleanup_stale_state()

    async with db_factory() as db:
        inst = await db.get(Instance, inst_id)
        assert inst.status == "error"


@pytest.mark.asyncio
async def test_cleanup_resets_stuck_executing_task(db_factory):
    """An unowned executing claim returns to pending, never fake success."""
    d = _make_dispatcher(db_factory)

    async with db_factory() as db:
        task = Task(title="stuck-task", description="test", status="executing")
        db.add(task)
        await db.commit()
        await db.refresh(task)
        task_id = task.id

    await d._cleanup_stale_state()

    async with db_factory() as db:
        t = await db.get(Task, task_id)
        assert t.status == "pending"
        assert t.instance_id is None


@pytest.mark.asyncio
async def test_cleanup_resets_stuck_in_progress_task(db_factory):
    """An unowned in-progress claim returns to pending."""
    d = _make_dispatcher(db_factory)

    async with db_factory() as db:
        task = Task(title="stuck-task-2", description="test", status="in_progress")
        db.add(task)
        await db.commit()
        await db.refresh(task)
        task_id = task.id

    await d._cleanup_stale_state()

    async with db_factory() as db:
        t = await db.get(Task, task_id)
        assert t.status == "pending"


@pytest.mark.asyncio
async def test_cleanup_preserves_session_id(db_factory):
    """Stuck task reset preserves session_id so user can resume chat."""
    d = _make_dispatcher(db_factory)

    async with db_factory() as db:
        task = Task(title="session-task", description="test", status="executing",
                    session_id="abc-123")
        db.add(task)
        await db.commit()
        await db.refresh(task)
        task_id = task.id

    await d._cleanup_stale_state()

    async with db_factory() as db:
        t = await db.get(Task, task_id)
        assert t.status == "pending"
        assert t.session_id == "abc-123"


@pytest.mark.asyncio
async def test_cleanup_does_not_touch_pending_tasks(db_factory):
    """Pending tasks are not affected by cleanup."""
    d = _make_dispatcher(db_factory)

    async with db_factory() as db:
        task = Task(title="pending-task", description="test", status="pending")
        db.add(task)
        await db.commit()
        await db.refresh(task)
        task_id = task.id

    await d._cleanup_stale_state()

    async with db_factory() as db:
        t = await db.get(Task, task_id)
        assert t.status == "pending"


@pytest.mark.asyncio
async def test_cleanup_does_not_touch_completed_tasks(db_factory):
    """Completed tasks are not affected by cleanup."""
    d = _make_dispatcher(db_factory)

    async with db_factory() as db:
        task = Task(title="done-task", description="test", status="completed")
        db.add(task)
        await db.commit()
        await db.refresh(task)
        task_id = task.id

    await d._cleanup_stale_state()

    async with db_factory() as db:
        t = await db.get(Task, task_id)
        assert t.status == "completed"


@pytest.mark.asyncio
async def test_cleanup_does_not_touch_idle_instances(db_factory):
    """Idle instances are not affected by cleanup."""
    d = _make_dispatcher(db_factory)

    async with db_factory() as db:
        inst = Instance(name="idle-worker", status="idle")
        db.add(inst)
        await db.commit()
        await db.refresh(inst)
        inst_id = inst.id

    await d._cleanup_stale_state()

    async with db_factory() as db:
        inst = await db.get(Instance, inst_id)
        assert inst.status == "idle"


@pytest.mark.asyncio
async def test_cleanup_acquires_task_write_before_instance_write(db_factory):
    """Startup reconciliation follows the global Task -> Instance lock order."""

    d = _make_dispatcher(db_factory)
    async with db_factory() as db:
        task = Task(title="ordered-cleanup", status="executing")
        db.add(task)
        await db.flush()
        instance = Instance(
            name="ordered-cleanup",
            status="running",
            pid=876543,
            current_task_id=task.id,
        )
        db.add(instance)
        await db.flush()
        task.instance_id = instance.id
        await db.commit()

    original_execute = AsyncSession.execute
    write_tables: list[str] = []

    async def record_writes(session, statement, *args, **kwargs):
        table_name = getattr(
            getattr(statement, "table", None),
            "name",
            None,
        )
        if table_name in {"tasks", "instances"}:
            write_tables.append(table_name)
        return await original_execute(session, statement, *args, **kwargs)

    with (
        patch(
            "backend.services.dispatcher.os.kill",
            side_effect=ProcessLookupError,
        ),
        patch.object(AsyncSession, "execute", new=record_writes),
    ):
        await d._cleanup_stale_state()

    assert "tasks" in write_tables
    assert "instances" in write_tables
    assert write_tables.index("tasks") < write_tables.index("instances")


@pytest.mark.asyncio
async def test_cleanup_called_on_start(db_factory):
    """_cleanup_stale_state is called during dispatcher start()."""
    d = _make_dispatcher(db_factory)

    async def fake_loop():
        await asyncio.sleep(999)
    d._dispatch_loop = fake_loop

    async with db_factory() as db:
        inst = Instance(name="stale-on-start", status="running", pid=999999)
        db.add(inst)
        await db.commit()
        await db.refresh(inst)
        inst_id = inst.id

    await d.start()

    async with db_factory() as db:
        inst = await db.get(Instance, inst_id)
        assert inst.status == "error"

    await d.stop()


# === _reset_instance_if_stale (safety net) tests ===


@pytest.mark.asyncio
async def test_safety_reset_instance_still_running(db_factory):
    """If instance is still 'running' after lifecycle, safety net resets it."""
    d = _make_dispatcher(db_factory)

    async with db_factory() as db:
        inst = Instance(name="stuck-worker", status="running", pid=12345, current_task_id=1)
        db.add(inst)
        task = Task(title="test", description="test", status="executing")
        db.add(task)
        await db.flush()
        inst.current_task_id = task.id
        task.instance_id = inst.id
        await db.commit()
        await db.refresh(inst)
        await db.refresh(task)
        inst_id = inst.id
        task_id = task.id

    await d._reset_instance_if_stale(
        inst_id, await _lifecycle_generation(d, db_factory, task_id)
    )

    async with db_factory() as db:
        inst = await db.get(Instance, inst_id)
        assert inst.status == "idle"
        assert inst.pid is None
        assert inst.current_task_id is None
        t = await db.get(Task, task_id)
        assert t.status == "completed"


@pytest.mark.asyncio
async def test_safety_reset_writes_task_before_instance(db_factory):
    """The fallback completion cannot invert the lifecycle DB lock order."""

    d = _make_dispatcher(db_factory)
    async with db_factory() as db:
        task = Task(title="ordered-reset", status="executing")
        db.add(task)
        await db.flush()
        instance = Instance(
            name="ordered-reset",
            status="running",
            pid=12345,
            current_task_id=task.id,
        )
        db.add(instance)
        await db.flush()
        task.instance_id = instance.id
        await db.commit()
        task_id, instance_id = task.id, instance.id

    original_execute = AsyncSession.execute
    write_tables: list[str] = []

    async def record_writes(session, statement, *args, **kwargs):
        table_name = getattr(
            getattr(statement, "table", None),
            "name",
            None,
        )
        if table_name in {"tasks", "instances"}:
            write_tables.append(table_name)
        return await original_execute(session, statement, *args, **kwargs)

    with patch.object(AsyncSession, "execute", new=record_writes):
        await d._reset_instance_if_stale(
            instance_id, await _lifecycle_generation(d, db_factory, task_id)
        )

    assert "tasks" in write_tables
    assert "instances" in write_tables
    assert write_tables.index("tasks") < write_tables.index("instances")


@pytest.mark.asyncio
async def test_safety_reset_does_not_complete_unbound_recovery_task(db_factory):
    """An old lifecycle cannot treat ``instance_id IS NULL`` as its owner."""
    d = _make_dispatcher(db_factory)

    async with db_factory() as db:
        task = Task(title="recovering", description="d", status="executing")
        db.add(task)
        await db.flush()
        instance = Instance(
            name="old-generation",
            status="running",
            pid=12345,
            current_task_id=task.id,
        )
        db.add(instance)
        await db.commit()
        task_id, instance_id = task.id, instance.id

    await d._reset_instance_if_stale(
        instance_id, await _lifecycle_generation(d, db_factory, task_id)
    )

    async with db_factory() as db:
        instance = await db.get(Instance, instance_id)
        task = await db.get(Task, task_id)
        assert instance.status == "running"
        assert instance.current_task_id == task_id
        assert instance.pid == 12345
        assert task.status == "executing"
        assert task.instance_id is None


@pytest.mark.asyncio
async def test_safety_reset_skips_already_idle_instance(db_factory):
    """If instance is already idle (consume_output cleaned up), safety net is a no-op."""
    d = _make_dispatcher(db_factory)

    async with db_factory() as db:
        inst = Instance(name="clean-worker", status="idle")
        db.add(inst)
        task = Task(title="test", description="test", status="completed")
        db.add(task)
        await db.commit()
        await db.refresh(inst)
        await db.refresh(task)
        inst_id = inst.id
        task_id = task.id

    await d._reset_instance_if_stale(
        inst_id, await _lifecycle_generation(d, db_factory, task_id)
    )

    async with db_factory() as db:
        inst = await db.get(Instance, inst_id)
        assert inst.status == "idle"
        t = await db.get(Task, task_id)
        assert t.status == "completed"


@pytest.mark.asyncio
async def test_safety_reset_old_lifecycle_cannot_clear_recycled_owner(db_factory):
    """An old lifecycle finally must not erase a newer task on the same slot."""
    d = _make_dispatcher(db_factory)

    async with db_factory() as db:
        old_task = Task(
            title="old",
            description="d",
            status="executing",
        )
        new_task = Task(
            title="new",
            description="d",
            status="executing",
        )
        db.add_all([old_task, new_task])
        await db.flush()
        inst = Instance(
            name="recycled",
            status="running",
            pid=222,
            current_task_id=new_task.id,
        )
        db.add(inst)
        await db.flush()
        old_task.instance_id = inst.id
        new_task.instance_id = inst.id
        await db.commit()
        old_id, new_id, inst_id = old_task.id, new_task.id, inst.id

    d.instance_manager.processes[inst_id] = MagicMock(returncode=0)
    d.instance_manager._instance_lifecycle_lock = MagicMock(
        return_value=asyncio.Lock()
    )

    await d._reset_instance_if_stale(
        inst_id, await _lifecycle_generation(d, db_factory, old_id)
    )

    async with db_factory() as db:
        inst = await db.get(Instance, inst_id)
        assert inst.status == "running"
        assert inst.current_task_id == new_id
        assert inst.pid == 222
        assert (await db.get(Task, old_id)).status == "executing"
        assert (await db.get(Task, new_id)).status == "executing"


@pytest.mark.asyncio
async def test_safety_reset_cannot_clear_same_task_same_slot_reclaim(
    db_factory,
):
    """Old finally cannot complete/clear a retried generation before spawn."""

    from datetime import datetime, timedelta

    d = _make_dispatcher(db_factory)
    old_task_started = datetime.utcnow() - timedelta(minutes=2)
    old_instance_started = datetime.utcnow() - timedelta(minutes=1)
    new_task_started = datetime.utcnow()
    new_instance_started = datetime.utcnow()
    async with db_factory() as db:
        task = Task(
            title="same-task-reclaim",
            status="executing",
            retry_count=0,
            started_at=old_task_started,
        )
        db.add(task)
        await db.flush()
        instance = Instance(
            name="same-task-reclaim",
            status="running",
            pid=111,
            current_task_id=task.id,
            started_at=old_instance_started,
        )
        db.add(instance)
        await db.flush()
        task.instance_id = instance.id
        await db.commit()
        old_generation = d._task_lifecycle_generation(task)
        await db.execute(
            update(Task)
            .where(Task.id == task.id)
            .values(
                retry_count=1,
                started_at=new_task_started,
            )
        )
        await db.execute(
            update(Instance)
            .where(Instance.id == instance.id)
            .values(pid=222, started_at=new_instance_started)
        )
        await db.commit()
        task_id, instance_id = task.id, instance.id

    await d._reset_instance_if_stale(instance_id, old_generation)

    async with db_factory() as db:
        task = await db.get(Task, task_id)
        instance = await db.get(Instance, instance_id)
        assert task.status == "executing"
        assert task.retry_count == 1
        assert task.started_at == new_task_started
        assert instance.status == "running"
        assert instance.current_task_id == task_id
        assert instance.pid == 222
        assert instance.started_at == new_instance_started


@pytest.mark.asyncio
async def test_safety_reset_handles_db_error(db_factory):
    """Safety net does not raise on DB errors (logs instead)."""
    d = _make_dispatcher(db_factory)
    # Use a nonexistent instance_id — should not raise
    await d._reset_instance_if_stale(
        99999,
        _TaskLifecycleGeneration(
            task_id=99999,
            worker_id=None,
            shared_from_id=None,
            retry_count=0,
            instance_id=99999,
            started_at=None,
            completed_at=None,
        ),
    )


# === Interrupted task status tests ===


@pytest.mark.asyncio
async def test_interrupted_task_marked_completed(db_factory):
    """User-interrupted task (exit code -2/130) is marked completed, not pending."""
    d = _make_dispatcher(db_factory)

    async with db_factory() as db:
        inst = Instance(name="int-worker")
        db.add(inst)
        task = Task(title="interrupt-test", description="test", target_repo="/repo")
        db.add(task)
        await db.commit()
        await db.refresh(inst)
        await db.refresh(task)
        task.status = "in_progress"
        task.instance_id = inst.id
        await db.commit()
        inst_id = inst.id
        task_obj = task

    mock_proc = MagicMock()
    mock_proc.returncode = -2  # SIGINT
    mock_proc.wait = AsyncMock(return_value=-2)
    d.instance_manager.processes = {inst_id: mock_proc}

    await d._run_task_lifecycle(inst_id, task_obj)

    async with db_factory() as db:
        t = await db.get(Task, task_obj.id)
        assert t.status == "completed"

    # Verify broadcast sent "completed" not "pending"
    calls = d.broadcaster.broadcast.call_args_list
    status_events = [c for c in calls if c[0][0] == "tasks" and c[0][1].get("new_status")]
    last_status = status_events[-1][0][1]["new_status"]
    assert last_status == "completed"


@pytest.mark.asyncio
async def test_interrupted_task_exit_130(db_factory):
    """Exit code 130 (SIGINT) also marks task completed."""
    d = _make_dispatcher(db_factory)

    async with db_factory() as db:
        inst = Instance(name="int-worker-130")
        db.add(inst)
        task = Task(title="interrupt-130", description="test", target_repo="/repo")
        db.add(task)
        await db.commit()
        await db.refresh(inst)
        await db.refresh(task)
        task.status = "in_progress"
        task.instance_id = inst.id
        await db.commit()
        inst_id = inst.id
        task_obj = task

    mock_proc = MagicMock()
    mock_proc.returncode = 130
    mock_proc.wait = AsyncMock(return_value=130)
    d.instance_manager.processes = {inst_id: mock_proc}

    await d._run_task_lifecycle(inst_id, task_obj)

    async with db_factory() as db:
        t = await db.get(Task, task_obj.id)
        assert t.status == "completed"


@pytest.mark.asyncio
async def test_interrupted_lifecycle_cannot_overwrite_concurrent_cancel(db_factory):
    """A stale exit-code result must lose to the user's cancelled status CAS."""
    d = _make_dispatcher(db_factory)

    async with db_factory() as db:
        inst = Instance(name="cancel-race")
        task = Task(title="cancel-race", description="d", status="pending")
        db.add_all([inst, task])
        await db.commit()
        await db.refresh(inst)
        await db.refresh(task)
        task.status = "in_progress"
        task.instance_id = inst.id
        await db.commit()
        inst_id, task_id, task_obj = inst.id, task.id, task

    class Process:
        returncode = None

        async def wait(self):
            async with db_factory() as db:
                assert await TaskQueue(db).cancel(task_id) is not None
            self.returncode = -2
            return -2

    process = Process()
    d.instance_manager.processes[inst_id] = process
    d.instance_manager._instance_lifecycle_lock = MagicMock(
        return_value=asyncio.Lock()
    )

    await d._run_task_lifecycle(inst_id, task_obj)

    async with db_factory() as db:
        assert (await db.get(Task, task_id)).status == "cancelled"
    completed_events = [
        call
        for call in d.broadcaster.broadcast.await_args_list
        if len(call.args) > 1
        and call.args[0] == "tasks"
        and call.args[1].get("new_status") == "completed"
    ]
    assert not completed_events


# === Lifecycle finally block integration tests ===


@pytest.mark.asyncio
async def test_lifecycle_resets_instance_on_exception(db_factory):
    """Instance is reset to idle even when lifecycle throws an exception."""
    d = _make_dispatcher(db_factory)
    d.instance_manager.launch = AsyncMock(side_effect=RuntimeError("boom"))

    async with db_factory() as db:
        inst = Instance(name="exc-worker", status="running", pid=12345)
        db.add(inst)
        task = Task(title="exc-test", description="test", target_repo="/repo")
        db.add(task)
        await db.commit()
        await db.refresh(inst)
        await db.refresh(task)
        task.status = "in_progress"
        task.instance_id = inst.id
        await db.commit()
        inst_id = inst.id
        task_obj = task

    await d._run_task_lifecycle(inst_id, task_obj)

    async with db_factory() as db:
        inst = await db.get(Instance, inst_id)
        assert inst.status == "idle"
        assert inst.pid is None


@pytest.mark.asyncio
async def test_lifecycle_success_does_not_double_reset(db_factory):
    """On normal success, instance ends in idle state (consume_output or safety net)."""
    d = _make_dispatcher(db_factory)

    async with db_factory() as db:
        inst = Instance(name="success-worker")
        db.add(inst)
        task = Task(title="success-test", description="test", target_repo="/repo")
        db.add(task)
        await db.commit()
        await db.refresh(inst)
        await db.refresh(task)
        task.status = "in_progress"
        task.instance_id = inst.id
        await db.commit()
        inst_id = inst.id
        task_obj = task

    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.wait = AsyncMock(return_value=0)
    d.instance_manager.processes = {inst_id: mock_proc}

    await d._run_task_lifecycle(inst_id, task_obj)

    async with db_factory() as db:
        t = await db.get(Task, task_obj.id)
        assert t.status == "completed"


# === Task deletion clears instance.current_task_id ===


@pytest.mark.asyncio
async def test_delete_task_clears_instance_current_task_id(db_factory):
    """Deleting a task clears current_task_id on any instance pointing to it."""
    async with db_factory() as db:
        inst = Instance(name="ref-worker", current_task_id=None)
        db.add(inst)
        task = Task(title="del-test", description="test", status="completed")
        db.add(task)
        await db.commit()
        await db.refresh(inst)
        await db.refresh(task)
        inst_id = inst.id
        task_id = task.id
        # Set current_task_id after we know the task ID
        inst.current_task_id = task_id
        await db.commit()

    async with db_factory() as db:
        queue = TaskQueue(db)
        result = await queue.delete(task_id)
        assert result is True

    async with db_factory() as db:
        inst = await db.get(Instance, inst_id)
        assert inst.current_task_id is None


@pytest.mark.asyncio
async def test_delete_task_no_instance_reference(db_factory):
    """Deleting a task with no instance reference works fine."""
    async with db_factory() as db:
        task = Task(title="orphan-task", description="test", status="completed")
        db.add(task)
        await db.commit()
        await db.refresh(task)
        task_id = task.id

    async with db_factory() as db:
        queue = TaskQueue(db)
        result = await queue.delete(task_id)
        assert result is True


# === Stop-session orphan handling ===


@pytest.mark.asyncio
async def test_stop_session_orphaned_task_marked_completed(client, session_factory):
    """Stop-session with no process marks executing task as completed."""
    async with session_factory() as db:
        task = Task(title="orphan-stop", description="test", status="executing",
                    session_id="sess-123")
        db.add(task)
        await db.commit()
        await db.refresh(task)
        task_id = task.id

    with patch("backend.api.tasks._stop_task_process", new_callable=AsyncMock, return_value=False):
        resp = await client.post(f"/api/tasks/{task_id}/stop-session")

    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert "completed" in data.get("note", "")

    async with session_factory() as db:
        t = await db.get(Task, task_id)
        assert t.status == "completed"
        assert t.session_id == "sess-123"


@pytest.mark.asyncio
async def test_stop_session_pending_task_returns_error(client, session_factory):
    """Stop-session on a pending task (no process, not executing) returns 400."""
    async with session_factory() as db:
        task = Task(title="pending-stop", description="test", status="pending")
        db.add(task)
        await db.commit()
        await db.refresh(task)
        task_id = task.id

    with patch("backend.api.tasks._stop_task_process", new_callable=AsyncMock, return_value=False):
        resp = await client.post(f"/api/tasks/{task_id}/stop-session")

    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_stop_session_completed_task_returns_error(client, session_factory):
    """Stop-session on a completed task (no process) returns 400."""
    async with session_factory() as db:
        task = Task(title="done-stop", description="test", status="completed")
        db.add(task)
        await db.commit()
        await db.refresh(task)
        task_id = task.id

    with patch("backend.api.tasks._stop_task_process", new_callable=AsyncMock, return_value=False):
        resp = await client.post(f"/api/tasks/{task_id}/stop-session")

    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_stop_session_in_progress_task_marked_completed(client, session_factory):
    """Stop-session with no process marks in_progress task as completed."""
    async with session_factory() as db:
        task = Task(title="in-progress-stop", description="test", status="in_progress")
        db.add(task)
        await db.commit()
        await db.refresh(task)
        task_id = task.id

    with patch("backend.api.tasks._stop_task_process", new_callable=AsyncMock, return_value=False):
        resp = await client.post(f"/api/tasks/{task_id}/stop-session")

    assert resp.status_code == 200
    assert resp.json()["ok"] is True


# === Mixed scenario: startup with multiple stale entities ===


@pytest.mark.asyncio
async def test_cleanup_multiple_stale_entities(db_factory):
    """Cleanup handles multiple stale instances and tasks in one pass."""
    d = _make_dispatcher(db_factory)

    async with db_factory() as db:
        # Two dead instances
        inst1 = Instance(name="dead-1", status="running", pid=999991)
        inst2 = Instance(name="dead-2", status="running", pid=999992)
        # One alive instance
        inst3 = Instance(name="alive", status="idle")
        # Two stuck tasks
        task1 = Task(title="stuck-1", description="t", status="executing")
        task2 = Task(title="stuck-2", description="t", status="in_progress")
        # One normal task
        task3 = Task(title="normal", description="t", status="pending")
        for obj in [inst1, inst2, inst3, task1, task2, task3]:
            db.add(obj)
        await db.commit()
        for obj in [inst1, inst2, inst3, task1, task2, task3]:
            await db.refresh(obj)
        ids = {
            "inst1": inst1.id, "inst2": inst2.id, "inst3": inst3.id,
            "task1": task1.id, "task2": task2.id, "task3": task3.id,
        }

    await d._cleanup_stale_state()

    async with db_factory() as db:
        assert (await db.get(Instance, ids["inst1"])).status == "error"
        assert (await db.get(Instance, ids["inst2"])).status == "error"
        assert (await db.get(Instance, ids["inst3"])).status == "idle"
        assert (await db.get(Task, ids["task1"])).status == "pending"
        assert (await db.get(Task, ids["task2"])).status == "pending"
        assert (await db.get(Task, ids["task3"])).status == "pending"
