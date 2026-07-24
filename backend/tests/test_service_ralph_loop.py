"""Tests for RalphLoop — only lifecycle management, not the full _loop body."""
import asyncio
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
        task_id,
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
        task_id,
        "permanent account binding error",
        retry_after=None,
    )

    assert delay == 0
    async with db_factory() as db:
        task = await db.get(Task, task_id)
        assert task.status == "cancelled"
    broadcaster.broadcast.assert_not_awaited()
