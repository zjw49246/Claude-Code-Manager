"""Tests for stale state cleanup, zombie worker prevention, and orphan task handling.

Covers the combined fixes from PRs #12 and #13:
- Dispatcher startup cleanup of dead-PID instances and stuck tasks
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

from backend.models.instance import Instance
from backend.models.task import Task
from backend.models.log_entry import LogEntry
from backend.services.dispatcher import GlobalDispatcher
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


# === _cleanup_stale_state tests ===


@pytest.mark.asyncio
async def test_cleanup_resets_dead_pid_instance(db_factory):
    """Instance with dead PID is reset to idle on dispatcher start."""
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
        assert inst.status == "idle"
        assert inst.pid is None
        assert inst.current_task_id is None


@pytest.mark.asyncio
async def test_cleanup_leaves_alive_pid_instance(db_factory):
    """Instance whose PID is alive (current process) is NOT reset."""
    d = _make_dispatcher(db_factory)
    my_pid = os.getpid()

    async with db_factory() as db:
        inst = Instance(name="alive-worker", status="running", pid=my_pid, current_task_id=1)
        db.add(inst)
        await db.commit()
        await db.refresh(inst)
        inst_id = inst.id

    await d._cleanup_stale_state()

    async with db_factory() as db:
        inst = await db.get(Instance, inst_id)
        assert inst.status == "running"
        assert inst.pid == my_pid


@pytest.mark.asyncio
async def test_cleanup_resets_instance_with_no_pid(db_factory):
    """Instance stuck in running with pid=None is reset (crashed before PID was recorded)."""
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
        assert inst.status == "idle"


@pytest.mark.asyncio
async def test_cleanup_resets_stuck_executing_task(db_factory):
    """Task stuck in 'executing' is reset to 'completed' on dispatcher start."""
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
        assert t.status == "completed"


@pytest.mark.asyncio
async def test_cleanup_resets_stuck_in_progress_task(db_factory):
    """Task stuck in 'in_progress' is reset to 'completed' on dispatcher start."""
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
        assert t.status == "completed"


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
        assert t.status == "completed"
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
        assert inst.status == "idle"

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
        await db.commit()
        await db.refresh(inst)
        await db.refresh(task)
        inst_id = inst.id
        task_id = task.id

    await d._reset_instance_if_stale(inst_id, task_id)

    async with db_factory() as db:
        inst = await db.get(Instance, inst_id)
        assert inst.status == "idle"
        assert inst.pid is None
        assert inst.current_task_id is None
        t = await db.get(Task, task_id)
        assert t.status == "completed"


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

    await d._reset_instance_if_stale(inst_id, task_id)

    async with db_factory() as db:
        inst = await db.get(Instance, inst_id)
        assert inst.status == "idle"
        t = await db.get(Task, task_id)
        assert t.status == "completed"


@pytest.mark.asyncio
async def test_safety_reset_handles_db_error(db_factory):
    """Safety net does not raise on DB errors (logs instead)."""
    d = _make_dispatcher(db_factory)
    # Use a nonexistent instance_id — should not raise
    await d._reset_instance_if_stale(99999, 99999)


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
        assert (await db.get(Instance, ids["inst1"])).status == "idle"
        assert (await db.get(Instance, ids["inst2"])).status == "idle"
        assert (await db.get(Instance, ids["inst3"])).status == "idle"
        assert (await db.get(Task, ids["task1"])).status == "completed"
        assert (await db.get(Task, ids["task2"])).status == "completed"
        assert (await db.get(Task, ids["task3"])).status == "pending"
