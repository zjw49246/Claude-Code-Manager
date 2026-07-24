"""Tests for RalphLoop — only lifecycle management, not the full _loop body."""
import asyncio
from datetime import datetime

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from backend.models.instance import Instance
from backend.models.task import Task
from backend.services.ralph_loop import RalphLoop


def _make_ralph_loop():
    return RalphLoop(
        db_factory=MagicMock(),
        instance_manager=MagicMock(),
        broadcaster=MagicMock(),
    )


@pytest.mark.asyncio
async def test_start_creates_task():
    rl = _make_ralph_loop()
    # Patch _loop to be a simple coroutine that sleeps forever
    async def fake_loop(instance_id):
        await asyncio.sleep(999)

    rl._loop = fake_loop
    await rl.start(1)
    assert 1 in rl._loops
    assert not rl._loops[1].done()
    # Cleanup
    rl._loops[1].cancel()
    try:
        await rl._loops[1]
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_start_idempotent():
    rl = _make_ralph_loop()

    async def fake_loop(instance_id):
        await asyncio.sleep(999)

    rl._loop = fake_loop
    await rl.start(1)
    first_task = rl._loops[1]
    await rl.start(1)
    assert rl._loops[1] is first_task  # Same task, not replaced
    # Cleanup
    first_task.cancel()
    try:
        await first_task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_stop_cancels():
    rl = _make_ralph_loop()

    async def fake_loop(instance_id):
        await asyncio.sleep(999)

    rl._loop = fake_loop
    await rl.start(1)
    task = rl._loops[1]
    await rl.stop(1)
    assert 1 not in rl._loops
    # Give event loop a tick for cancellation
    await asyncio.sleep(0)
    assert task.cancelled() or task.done()


@pytest.mark.asyncio
async def test_stop_timeout_retains_live_loop_evidence():
    rl = _make_ralph_loop()
    release = asyncio.Event()

    async def ignores_first_cancellation(_instance_id):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await release.wait()

    rl._loop = ignores_first_cancellation
    await rl.start(1)
    task = rl._loops[1]
    await asyncio.sleep(0)
    try:
        assert await rl.stop(1, timeout=0.01) is False
        assert rl._loops[1] is task
        assert rl.is_running(1)
    finally:
        release.set()
        await asyncio.wait_for(task, timeout=1)
        assert await rl.stop(1, timeout=0.01) is True
        assert 1 not in rl._loops


@pytest.mark.asyncio
async def test_wait_for_turn_fails_closed_when_output_consumer_times_out():
    process = MagicMock(returncode=0)
    process.wait = AsyncMock(return_value=0)
    instance_manager = MagicMock()
    instance_manager.wait_for_output_consumer = AsyncMock(
        side_effect=asyncio.TimeoutError
    )
    rl = RalphLoop(
        db_factory=MagicMock(),
        instance_manager=instance_manager,
        broadcaster=MagicMock(),
    )
    task = MagicMock(id=23, provider="claude")

    with pytest.raises(
        RuntimeError,
        match="Output consumer did not finish after Task run for task 23",
    ):
        await rl._wait_for_turn(
            7,
            task,
            process,
            label="Task run",
        )

    instance_manager.wait_for_output_consumer.assert_awaited_once_with(
        7,
        provider="claude",
        timeout=30,
        expected_process=process,
    )


@pytest.mark.asyncio
async def test_stop_returns_claimed_task_to_pending_before_it_returns(db_factory):
    broadcaster = MagicMock()
    broadcaster.broadcast = AsyncMock()
    instance_manager = MagicMock()
    instance_manager.is_running.return_value = False
    instance_manager.stop = AsyncMock()
    instance_manager.wait_for_output_consumer = AsyncMock()
    rl = RalphLoop(
        db_factory=db_factory,
        instance_manager=instance_manager,
        broadcaster=broadcaster,
    )

    async with db_factory() as db:
        instance = Instance(name="ralph-cancel-worker")
        task = Task(title="claimed", description="work")
        db.add_all([instance, task])
        await db.commit()
        await db.refresh(instance)
        await db.refresh(task)
        instance_id, task_id = instance.id, task.id

    launch_entered = asyncio.Event()
    never_finish = asyncio.Event()

    async def blocked_launch(*_args, **_kwargs):
        launch_entered.set()
        await never_finish.wait()

    rl._launch_task_on_bound_account = blocked_launch
    await rl.start(instance_id)
    await asyncio.wait_for(launch_entered.wait(), timeout=1)

    async with db_factory() as db:
        claimed = await db.get(Task, task_id)
        assert claimed.status == "in_progress"
        assert claimed.instance_id == instance_id

    loop_task = rl._loops[instance_id]
    await rl.stop(instance_id)

    assert loop_task.cancelled()
    assert instance_id not in rl._loops
    async with db_factory() as db:
        released = await db.get(Task, task_id)
        assert released.status == "pending"
        assert released.instance_id is None
        assert "Ralph loop stopped" in released.error_message
    instance_manager.stop.assert_not_awaited()


@pytest.mark.asyncio
async def test_ralph_dequeue_waits_for_shared_maintenance_gate(db_factory):
    from backend.services.dispatcher import GlobalDispatcher

    broadcaster = MagicMock()
    broadcaster.broadcast = AsyncMock()
    instance_manager = MagicMock()
    gate = GlobalDispatcher(db_factory, instance_manager, broadcaster)
    await gate.pause_dispatching()
    rl = RalphLoop(db_factory, instance_manager, broadcaster)

    async with db_factory() as db:
        instance = Instance(name="ralph-maintenance-worker")
        task = Task(title="must stay pending", description="work")
        db.add_all([instance, task])
        await db.commit()
        await db.refresh(instance)
        await db.refresh(task)
        instance_id, task_id = instance.id, task.id

    with patch("backend.main.dispatcher", gate):
        await rl.start(instance_id)
        await asyncio.sleep(0.05)
        async with db_factory() as db:
            task = await db.get(Task, task_id)
            assert task.status == "pending"
        instance_manager.launch.assert_not_called()
        await rl.stop(instance_id)


@pytest.mark.asyncio
async def test_stale_dequeue_claim_is_not_published_or_launched(db_factory):
    """A cancelled/retried claim must not emit a late in-progress event."""

    broadcaster = MagicMock()
    broadcaster.broadcast = AsyncMock()
    instance_manager = MagicMock()
    instance_manager.is_running.return_value = False
    instance_manager.stop = AsyncMock()
    instance_manager.wait_for_output_consumer = AsyncMock()
    rl = RalphLoop(
        db_factory=db_factory,
        instance_manager=instance_manager,
        broadcaster=broadcaster,
    )

    async with db_factory() as db:
        instance = Instance(name="ralph-stale-claim")
        task = Task(title="stale claim", description="must not launch")
        db.add_all([instance, task])
        await db.commit()
        await db.refresh(instance)
        instance_id = instance.id

    claim_checked = asyncio.Event()

    async def reject_stale_claim(*_args, **_kwargs):
        claim_checked.set()
        return False

    rl._broadcast_generation_event = AsyncMock(
        side_effect=reject_stale_claim
    )
    rl._launch_task_on_bound_account = AsyncMock()

    loop_task = asyncio.create_task(rl._loop(instance_id))
    try:
        await asyncio.wait_for(claim_checked.wait(), timeout=1)
        await asyncio.sleep(0)
    finally:
        loop_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await loop_task

    rl._launch_task_on_bound_account.assert_not_awaited()
    broadcaster.broadcast.assert_not_awaited()


@pytest.mark.asyncio
async def test_is_running_true():
    rl = _make_ralph_loop()

    async def fake_loop(instance_id):
        await asyncio.sleep(999)

    rl._loop = fake_loop
    await rl.start(1)
    assert rl.is_running(1) is True
    # Cleanup
    rl._loops[1].cancel()
    try:
        await rl._loops[1]
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_is_running_false():
    rl = _make_ralph_loop()
    assert rl.is_running(1) is False
    assert rl.is_running(999) is False


@pytest.mark.asyncio
async def test_codex_task_launch_resolves_home_and_resumes_native_thread():
    rl = _make_ralph_loop()
    rl.instance_manager.launch = AsyncMock(return_value=4321)
    dispatcher = MagicMock()
    dispatcher._resolve_resume_config_dir = AsyncMock(
        return_value="/pool/codex-2"
    )
    task = MagicMock(
        id=77,
        provider="codex",
        session_id="thread-ralph-1",
        thinking_budget=1234,
    )

    with patch("backend.main.dispatcher", dispatcher):
        pid = await rl._launch_task_on_bound_account(
            9,
            task,
            "continue work",
            "/repo",
        )

    assert pid == 4321
    dispatcher._resolve_resume_config_dir.assert_awaited_once_with(
        "thread-ralph-1",
        "codex",
        task_id=77,
    )
    launch_kwargs = rl.instance_manager.launch.await_args.kwargs
    assert launch_kwargs["config_dir"] == "/pool/codex-2"
    assert launch_kwargs["resume_session_id"] == "thread-ralph-1"
    assert launch_kwargs["provider"] == "codex"


@pytest.mark.asyncio
async def test_claude_task_launch_uses_resolved_home_without_forcing_resume():
    rl = _make_ralph_loop()
    rl.instance_manager.launch = AsyncMock(return_value=123)
    dispatcher = MagicMock()
    dispatcher._resolve_resume_config_dir = AsyncMock(
        return_value="/pool/claude-2"
    )
    task = MagicMock(
        id=78,
        provider="claude",
        session_id="claude-session",
        thinking_budget=None,
    )

    with patch("backend.main.dispatcher", dispatcher):
        await rl._launch_task_on_bound_account(10, task, "work", "/repo")

    dispatcher._resolve_resume_config_dir.assert_awaited_once_with(
        "claude-session",
        "claude",
        task_id=78,
    )
    launch_kwargs = rl.instance_manager.launch.await_args.kwargs
    assert launch_kwargs["config_dir"] == "/pool/claude-2"
    assert launch_kwargs["resume_session_id"] is None


@pytest.mark.asyncio
async def test_retryable_account_routing_failure_defers_claimed_task(db_factory):
    broadcaster = MagicMock()
    broadcaster.broadcast = AsyncMock()
    rl = RalphLoop(
        db_factory=db_factory,
        instance_manager=MagicMock(),
        broadcaster=broadcaster,
    )
    async with db_factory() as db:
        instance = Instance(name="ralph-routing-worker")
        db.add(instance)
        await db.flush()
        task = Task(
            title="routing wait",
            description="continue",
            provider="codex",
            status="in_progress",
            instance_id=instance.id,
            retry_count=2,
        )
        db.add(task)
        await db.commit()
        await db.refresh(task)
        task_id = task.id
        instance_id = instance.id

    delay = await rl._handle_account_routing_failure(
        instance_id,
        task,
        "all Codex accounts are cooling down",
        retry_after=7,
    )

    assert delay == 7
    async with db_factory() as db:
        task = await db.get(Task, task_id)
        assert task.status == "pending"
        assert task.instance_id is None
        assert task.retry_count == 2
        assert "cooling down" in task.error_message
    broadcaster.broadcast.assert_awaited_once()


@pytest.mark.parametrize("retry_after", [None, 7.0])
@pytest.mark.asyncio
async def test_account_routing_failure_cannot_mutate_reassigned_task(
    db_factory,
    retry_after,
):
    broadcaster = MagicMock()
    broadcaster.broadcast = AsyncMock()
    rl = RalphLoop(
        db_factory=db_factory,
        instance_manager=MagicMock(),
        broadcaster=broadcaster,
    )
    async with db_factory() as db:
        old_instance = Instance(name="old-ralph-owner")
        new_instance = Instance(name="new-task-owner")
        db.add_all([old_instance, new_instance])
        await db.flush()
        task = Task(
            title="reassigned while routing",
            description="continue",
            provider="codex",
            status="executing",
            instance_id=new_instance.id,
            error_message="new generation is running",
        )
        db.add(task)
        await db.commit()
        await db.refresh(task)
        task_id = task.id
        old_instance_id = old_instance.id
        new_instance_id = new_instance.id

    delay = await rl._handle_account_routing_failure(
        old_instance_id,
        task,
        "stale routing failure",
        retry_after=retry_after,
    )

    assert delay == 0
    async with db_factory() as db:
        task = await db.get(Task, task_id)
        assert task.status == "executing"
        assert task.instance_id == new_instance_id
        assert task.error_message == "new generation is running"
    broadcaster.broadcast.assert_not_awaited()


@pytest.mark.asyncio
async def test_permanent_routing_failure_does_not_overwrite_cancellation(db_factory):
    broadcaster = MagicMock()
    broadcaster.broadcast = AsyncMock()
    rl = RalphLoop(
        db_factory=db_factory,
        instance_manager=MagicMock(),
        broadcaster=broadcaster,
    )
    async with db_factory() as db:
        task = Task(
            title="cancel wins routing failure",
            description="continue",
            provider="codex",
            status="cancelled",
        )
        db.add(task)
        await db.commit()
        await db.refresh(task)
        task_id = task.id

    delay = await rl._handle_account_routing_failure(
        7,
        task,
        "permanent account binding error",
        retry_after=None,
    )

    assert delay == 0
    async with db_factory() as db:
        task = await db.get(Task, task_id)
        assert task.status == "cancelled"
    broadcaster.broadcast.assert_not_awaited()


@pytest.mark.asyncio
async def test_unexpected_error_fails_claim_before_reaping_exact_process(
    db_factory,
):
    broadcaster = MagicMock()
    broadcaster.broadcast = AsyncMock()
    instance_manager = MagicMock()
    instance_manager.kill_process_generation = AsyncMock(return_value=True)
    instance_manager.wait_for_output_consumer = AsyncMock()
    rl = RalphLoop(
        db_factory=db_factory,
        instance_manager=instance_manager,
        broadcaster=broadcaster,
    )

    process = MagicMock(pid=2468, returncode=None)
    async with db_factory() as db:
        instance = Instance(
            name="ralph-error-worker",
            status="running",
            pid=process.pid,
            started_at=datetime(2026, 2, 3, 4, 5, 6),
        )
        db.add(instance)
        await db.flush()
        task = Task(
            title="unexpected launch error",
            description="work",
            provider="claude",
            status="executing",
            instance_id=instance.id,
        )
        db.add(task)
        await db.flush()
        instance.current_task_id = task.id
        await db.commit()
        await db.refresh(task)
        instance_id = instance.id
        task_id = task.id

    instance_manager.processes = {instance_id: process}

    await rl._fail_unexpected_claim(
        instance_id,
        task,
        RuntimeError("consumer bookkeeping exploded"),
    )

    async with db_factory() as db:
        failed = await db.get(Task, task_id)
        assert failed.status == "failed"
        assert failed.instance_id == instance_id
        assert "consumer bookkeeping exploded" in failed.error_message
    instance_manager.kill_process_generation.assert_awaited_once_with(
        instance_id,
        process,
    )
    instance_manager.wait_for_output_consumer.assert_awaited_once_with(
        instance_id,
        provider="claude",
        timeout=30,
        expected_process=process,
        preserve_error=True,
    )
    event = broadcaster.broadcast.await_args.args[1]
    assert event["new_status"] == "failed"
    assert event["reason"] == "ralph_internal_error"


@pytest.mark.asyncio
async def test_unexpected_error_with_unknown_persisted_process_fails_closed(
    db_factory,
):
    broadcaster = MagicMock()
    broadcaster.broadcast = AsyncMock(
        side_effect=RuntimeError("websocket unavailable")
    )
    instance_manager = MagicMock()
    instance_manager.processes = {}
    rl = RalphLoop(
        db_factory=db_factory,
        instance_manager=instance_manager,
        broadcaster=broadcaster,
    )

    async with db_factory() as db:
        instance = Instance(
            name="ralph-unmanaged-error",
            status="running",
            pid=97531,
            started_at=datetime(2026, 2, 3, 4, 5, 6),
        )
        db.add(instance)
        await db.flush()
        task = Task(
            title="unknown process",
            description="work",
            status="in_progress",
            instance_id=instance.id,
        )
        db.add(task)
        await db.flush()
        instance.current_task_id = task.id
        await db.commit()
        await db.refresh(task)
        instance_id = instance.id
        task_id = task.id

    await rl._fail_unexpected_claim(
        instance_id,
        task,
        RuntimeError("spawn state was lost"),
    )

    async with db_factory() as db:
        failed = await db.get(Task, task_id)
        instance = await db.get(Instance, instance_id)
        assert failed.status == "failed"
        assert failed.instance_id == instance_id
        assert instance.status == "error"
        assert instance.pid == 97531
        assert instance.current_task_id == task_id


@pytest.mark.asyncio
async def test_unexpected_error_cannot_fail_reassigned_generation(db_factory):
    broadcaster = MagicMock()
    broadcaster.broadcast = AsyncMock()
    instance_manager = MagicMock()
    instance_manager.processes = {}
    rl = RalphLoop(
        db_factory=db_factory,
        instance_manager=instance_manager,
        broadcaster=broadcaster,
    )

    async with db_factory() as db:
        old_instance = Instance(name="old-ralph-error-owner")
        new_instance = Instance(name="new-ralph-error-owner")
        db.add_all([old_instance, new_instance])
        await db.flush()
        task = Task(
            title="reassigned error",
            description="work",
            status="executing",
            instance_id=new_instance.id,
            error_message="new generation healthy",
        )
        db.add(task)
        await db.commit()
        await db.refresh(task)
        old_instance_id = old_instance.id
        new_instance_id = new_instance.id
        task_id = task.id

    await rl._fail_unexpected_claim(
        old_instance_id,
        task,
        RuntimeError("stale generation failed"),
    )

    async with db_factory() as db:
        current = await db.get(Task, task_id)
        assert current.status == "executing"
        assert current.instance_id == new_instance_id
        assert current.error_message == "new generation healthy"
    broadcaster.broadcast.assert_not_awaited()


@pytest.mark.asyncio
async def test_unexpected_error_cannot_fail_same_slot_retry_aba(db_factory):
    """retry_count/start fences distinguish the same task and Instance ids."""

    from backend.services.task_queue import TaskQueue

    broadcaster = MagicMock()
    broadcaster.broadcast = AsyncMock()
    instance_manager = MagicMock()
    instance_manager.processes = {}
    rl = RalphLoop(
        db_factory=db_factory,
        instance_manager=instance_manager,
        broadcaster=broadcaster,
    )
    old_started_at = datetime(2026, 2, 3, 4, 5, 6)

    async with db_factory() as db:
        instance = Instance(
            name="same-slot-error-aba",
            status="running",
            pid=1111,
            started_at=old_started_at,
        )
        db.add(instance)
        await db.flush()
        old_task = Task(
            title="same task retried",
            description="work",
            status="executing",
            retry_count=0,
            started_at=old_started_at,
            instance_id=instance.id,
        )
        db.add(old_task)
        await db.flush()
        instance.current_task_id = old_task.id
        await db.commit()
        await db.refresh(old_task)
        instance_id = instance.id
        task_id = old_task.id

    # Complete the old generation, retry it, and reclaim the exact same slot
    # before the old error handler runs.
    async with db_factory() as db:
        task = await db.get(Task, task_id)
        task.status = "failed"
        await db.commit()
        queue = TaskQueue(db)
        assert await queue.retry(task_id) is not None
        replacement = await queue.dequeue(instance_id=instance_id)
        assert replacement is not None
        instance = await db.get(Instance, instance_id)
        instance.status = "running"
        instance.pid = 2222
        instance.started_at = replacement.started_at
        instance.current_task_id = task_id
        await db.commit()

    await rl._fail_unexpected_claim(
        instance_id,
        old_task,
        RuntimeError("late failure from old turn"),
    )

    async with db_factory() as db:
        current = await db.get(Task, task_id)
        instance = await db.get(Instance, instance_id)
        assert current.status == "in_progress"
        assert current.retry_count == 1
        assert current.instance_id == instance_id
        assert instance.status == "running"
        assert instance.pid == 2222
    broadcaster.broadcast.assert_not_awaited()


@pytest.mark.asyncio
async def test_unexpected_error_suppresses_failed_event_after_rapid_retry(
    db_factory,
):
    """Cleanup latency must not publish failed for a replacement generation."""

    from backend.services.task_queue import TaskQueue

    broadcaster = MagicMock()
    broadcaster.broadcast = AsyncMock()
    process = MagicMock(pid=3333, returncode=None)
    instance_manager = MagicMock()
    instance_manager.processes = {}
    instance_manager.wait_for_output_consumer = AsyncMock()
    rl = RalphLoop(
        db_factory=db_factory,
        instance_manager=instance_manager,
        broadcaster=broadcaster,
    )

    async with db_factory() as db:
        instance = Instance(
            name="retry-during-error-cleanup",
            status="running",
            pid=process.pid,
            started_at=datetime(2026, 3, 4, 5, 6, 7),
        )
        db.add(instance)
        await db.flush()
        task = Task(
            title="retry during cleanup",
            description="work",
            provider="claude",
            status="executing",
            instance_id=instance.id,
        )
        db.add(task)
        await db.flush()
        instance.current_task_id = task.id
        await db.commit()
        await db.refresh(task)
        instance_id = instance.id
        task_id = task.id

    instance_manager.processes = {instance_id: process}

    async def retry_before_cleanup_returns(stopped_instance_id, exact_process):
        assert stopped_instance_id == instance_id
        assert exact_process is process
        async with db_factory() as db:
            queue = TaskQueue(db)
            assert await queue.retry(task_id) is not None
            replacement = await queue.dequeue(instance_id=instance_id)
            assert replacement is not None
            instance = await db.get(Instance, instance_id)
            instance.status = "running"
            instance.pid = 4444
            instance.started_at = replacement.started_at
            instance.current_task_id = task_id
            await db.commit()
        return True

    instance_manager.kill_process_generation = AsyncMock(
        side_effect=retry_before_cleanup_returns
    )

    await rl._fail_unexpected_claim(
        instance_id,
        task,
        RuntimeError("old generation failed"),
    )

    async with db_factory() as db:
        current = await db.get(Task, task_id)
        assert current.status == "in_progress"
        assert current.retry_count == 1
        assert current.instance_id == instance_id
    broadcaster.broadcast.assert_not_awaited()


@pytest.mark.asyncio
async def test_plan_result_requires_active_status_and_same_instance_owner(db_factory):
    rl = RalphLoop(
        db_factory=db_factory,
        instance_manager=MagicMock(),
        broadcaster=MagicMock(),
    )
    async with db_factory() as db:
        old_instance = Instance(name="old-plan-owner")
        new_instance = Instance(name="new-plan-owner")
        db.add_all([old_instance, new_instance])
        await db.flush()
        cancelled = Task(
            title="cancelled plan",
            description="plan",
            mode="plan",
            status="cancelled",
            instance_id=old_instance.id,
            plan_content="keep cancelled content",
        )
        reassigned = Task(
            title="reassigned plan",
            description="plan",
            mode="plan",
            status="executing",
            instance_id=new_instance.id,
            plan_content="keep new generation content",
        )
        db.add_all([cancelled, reassigned])
        await db.commit()
        await db.refresh(cancelled)
        await db.refresh(reassigned)
        old_instance_id = old_instance.id
        cancelled_id = cancelled.id
        reassigned_id = reassigned.id

    assert not await rl._store_plan_if_owned(
        old_instance_id,
        cancelled,
        "stale plan",
    )
    assert not await rl._store_plan_if_owned(
        old_instance_id,
        reassigned,
        "stale plan",
    )

    async with db_factory() as db:
        cancelled = await db.get(Task, cancelled_id)
        reassigned = await db.get(Task, reassigned_id)
        assert cancelled.status == "cancelled"
        assert cancelled.plan_content == "keep cancelled content"
        assert reassigned.status == "executing"
        assert reassigned.plan_content == "keep new generation content"


@pytest.mark.asyncio
async def test_plan_result_moves_owned_active_task_to_review(db_factory):
    rl = RalphLoop(
        db_factory=db_factory,
        instance_manager=MagicMock(),
        broadcaster=MagicMock(),
    )
    async with db_factory() as db:
        instance = Instance(name="owned-plan-worker")
        db.add(instance)
        await db.flush()
        task = Task(
            title="owned plan",
            description="plan",
            mode="plan",
            status="in_progress",
            instance_id=instance.id,
        )
        db.add(task)
        await db.commit()
        await db.refresh(task)
        instance_id = instance.id
        task_id = task.id

    assert await rl._store_plan_if_owned(
        instance_id,
        task,
        "safe plan",
    )

    async with db_factory() as db:
        task = await db.get(Task, task_id)
        assert task.status == "plan_review"
        assert task.plan_content == "safe plan"


@pytest.mark.asyncio
async def test_plan_result_rejects_same_slot_retry_aba(db_factory):
    from backend.services.task_queue import TaskQueue

    rl = RalphLoop(
        db_factory=db_factory,
        instance_manager=MagicMock(),
        broadcaster=MagicMock(),
    )
    async with db_factory() as db:
        instance = Instance(name="plan-aba-worker")
        db.add(instance)
        await db.flush()
        old_task = Task(
            title="old plan generation",
            description="plan",
            mode="plan",
            status="in_progress",
            retry_count=0,
            instance_id=instance.id,
        )
        db.add(old_task)
        await db.commit()
        await db.refresh(old_task)
        task_id = old_task.id
        instance_id = instance.id

    async with db_factory() as db:
        task = await db.get(Task, task_id)
        task.status = "failed"
        await db.commit()
        queue = TaskQueue(db)
        assert await queue.retry(task_id) is not None
        assert await queue.dequeue(instance_id=instance_id) is not None

    assert not await rl._store_plan_if_owned(
        instance_id,
        old_task,
        "late old plan",
    )
    async with db_factory() as db:
        current = await db.get(Task, task_id)
        assert current.status == "in_progress"
        assert current.retry_count == 1
        assert current.plan_content is None


@pytest.mark.asyncio
async def test_successful_stop_does_not_touch_immediate_same_instance_reclaim(
    db_factory,
):
    broadcaster = MagicMock()
    broadcaster.broadcast = AsyncMock()
    instance_manager = MagicMock()
    instance_manager.is_running.return_value = True
    rl = RalphLoop(
        db_factory=db_factory,
        instance_manager=instance_manager,
        broadcaster=broadcaster,
    )
    async with db_factory() as db:
        instance = Instance(
            name="rapidly-reused-ralph-worker",
            status="running",
            pid=1001,
        )
        db.add(instance)
        await db.flush()
        task = Task(
            title="rapidly reclaimed task",
            description="work",
            status="in_progress",
            instance_id=instance.id,
        )
        db.add(task)
        await db.flush()
        instance.current_task_id = task.id
        await db.commit()
        await db.refresh(task)
        instance_id = instance.id
        task_id = task.id

    async def stop_then_reclaim(
        stopped_instance_id,
        *,
        expected_task_id,
        expected_pid,
        expected_started_at,
        terminal_consumer_timeout,
        consumer_cancel_timeout,
    ):
        assert stopped_instance_id == instance_id
        assert expected_task_id == task_id
        assert expected_pid == 1001
        assert expected_started_at is None
        assert terminal_consumer_timeout == 30.0
        assert consumer_cancel_timeout == 10.0
        # Model InstanceManager.stop's successful release followed immediately
        # by a dispatcher claim of the same task on the same reusable slot.
        async with db_factory() as db:
            task = await db.get(Task, task_id)
            instance = await db.get(Instance, instance_id)
            task.status = "executing"
            task.instance_id = instance_id
            task.error_message = "new generation"
            instance.status = "running"
            instance.pid = 2002
            instance.current_task_id = task_id
            await db.commit()
        return True

    instance_manager.stop = AsyncMock(side_effect=stop_then_reclaim)

    await rl._release_cancelled_claim(instance_id, task)

    instance_manager.stop.assert_awaited_once()
    instance_manager.is_running.assert_called_once_with(instance_id)
    async with db_factory() as db:
        task = await db.get(Task, task_id)
        instance = await db.get(Instance, instance_id)
        assert task.status == "executing"
        assert task.instance_id == instance_id
        assert task.error_message == "new generation"
        assert instance.status == "running"
        assert instance.pid == 2002
        assert instance.current_task_id == task_id
    broadcaster.broadcast.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_stop_does_not_overwrite_new_same_task_instance_generation(
    db_factory,
):
    broadcaster = MagicMock()
    broadcaster.broadcast = AsyncMock()
    instance_manager = MagicMock()
    instance_manager.is_running.return_value = True
    rl = RalphLoop(
        db_factory=db_factory,
        instance_manager=instance_manager,
        broadcaster=broadcaster,
    )
    old_started_at = datetime(2026, 1, 1, 1, 0, 0)
    new_started_at = datetime(2026, 1, 1, 1, 0, 1)
    async with db_factory() as db:
        instance = Instance(
            name="failed-stop-reused-worker",
            status="running",
            pid=3456,
            started_at=old_started_at,
        )
        db.add(instance)
        await db.flush()
        task = Task(
            title="same task retried immediately",
            description="work",
            status="in_progress",
            instance_id=instance.id,
        )
        db.add(task)
        await db.flush()
        instance.current_task_id = task.id
        await db.commit()
        await db.refresh(task)
        instance_id = instance.id
        task_id = task.id

    async def stop_fails_after_reclaim(
        stopped_instance_id,
        *,
        expected_task_id,
        expected_pid,
        expected_started_at,
        terminal_consumer_timeout,
        consumer_cancel_timeout,
    ):
        assert stopped_instance_id == instance_id
        assert expected_task_id == task_id
        assert expected_pid == 3456
        assert expected_started_at == old_started_at
        assert terminal_consumer_timeout == 30.0
        assert consumer_cancel_timeout == 10.0
        # The old stop unwinds, then an immediate retry reuses the same task,
        # slot, status and even PID. started_at is the remaining generation
        # fence that must prevent the old failure recorder from matching.
        async with db_factory() as db:
            task = await db.get(Task, task_id)
            instance = await db.get(Instance, instance_id)
            task.status = "executing"
            task.instance_id = instance_id
            task.error_message = "new generation is healthy"
            instance.status = "running"
            instance.pid = 3456
            instance.current_task_id = task_id
            instance.started_at = new_started_at
            await db.commit()
        raise RuntimeError("old generation cleanup failed")

    instance_manager.stop = AsyncMock(side_effect=stop_fails_after_reclaim)

    await rl._release_cancelled_claim(instance_id, task)

    instance_manager.stop.assert_awaited_once()
    async with db_factory() as db:
        task = await db.get(Task, task_id)
        instance = await db.get(Instance, instance_id)
        assert task.status == "executing"
        assert task.instance_id == instance_id
        assert task.error_message == "new generation is healthy"
        assert instance.status == "running"
        assert instance.pid == 3456
        assert instance.current_task_id == task_id
        assert instance.started_at == new_started_at
    broadcaster.broadcast.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancel_cleanup_failure_never_requeues_possibly_live_process(
    db_factory,
):
    broadcaster = MagicMock()
    broadcaster.broadcast = AsyncMock()
    instance_manager = MagicMock()
    instance_manager.is_running.return_value = True
    instance_manager.stop = AsyncMock(
        side_effect=RuntimeError("process group survived SIGKILL")
    )
    rl = RalphLoop(
        db_factory=db_factory,
        instance_manager=instance_manager,
        broadcaster=broadcaster,
    )
    async with db_factory() as db:
        instance = Instance(
            name="unreaped-ralph-worker",
            status="running",
            pid=43210,
        )
        db.add(instance)
        await db.flush()
        task = Task(
            title="possibly still running",
            description="work",
            status="in_progress",
            instance_id=instance.id,
        )
        db.add(task)
        await db.flush()
        instance.current_task_id = task.id
        await db.commit()
        await db.refresh(task)
        instance_id = instance.id
        task_id = task.id

    await rl._release_cancelled_claim(instance_id, task)

    instance_manager.stop.assert_awaited_once_with(
        instance_id,
        expected_task_id=task_id,
        expected_pid=43210,
        expected_started_at=None,
        terminal_consumer_timeout=30.0,
        consumer_cancel_timeout=10.0,
    )
    async with db_factory() as db:
        task = await db.get(Task, task_id)
        instance = await db.get(Instance, instance_id)
        assert task.status == "failed"
        assert task.instance_id == instance_id
        assert "cleanup could not be confirmed" in task.error_message
        assert instance.status == "error"
        assert instance.pid == 43210
        assert instance.current_task_id == task_id
    event = broadcaster.broadcast.await_args.args[1]
    assert event["new_status"] == "failed"
    assert event["reason"] == "ralph_stop_cleanup_failed"


@pytest.mark.asyncio
async def test_cancel_with_persisted_owner_but_no_managed_generation_fails_closed(
    db_factory,
):
    broadcaster = MagicMock()
    broadcaster.broadcast = AsyncMock()
    instance_manager = MagicMock()
    instance_manager.is_running.return_value = False
    instance_manager.stop = AsyncMock()
    rl = RalphLoop(
        db_factory=db_factory,
        instance_manager=instance_manager,
        broadcaster=broadcaster,
    )
    async with db_factory() as db:
        instance = Instance(
            name="unknown-ralph-generation",
            status="running",
            pid=8765,
        )
        db.add(instance)
        await db.flush()
        task = Task(
            title="unknown process owner",
            description="work",
            status="executing",
            instance_id=instance.id,
        )
        db.add(task)
        await db.flush()
        instance.current_task_id = task.id
        await db.commit()
        await db.refresh(task)
        instance_id = instance.id
        task_id = task.id

    await rl._release_cancelled_claim(instance_id, task)

    instance_manager.stop.assert_not_awaited()
    async with db_factory() as db:
        task = await db.get(Task, task_id)
        instance = await db.get(Instance, instance_id)
        assert task.status == "failed"
        assert task.instance_id == instance_id
        assert instance.status == "error"
        assert instance.pid == 8765
        assert instance.current_task_id == task_id
