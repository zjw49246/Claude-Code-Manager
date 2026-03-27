"""Tests for GlobalDispatcher — task dispatch and lifecycle management."""
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from backend.services.dispatcher import GlobalDispatcher
from backend.models.instance import Instance
from backend.models.task import Task


def _make_dispatcher(db_factory):
    """Create a GlobalDispatcher with mocked dependencies."""
    instance_manager = MagicMock()
    instance_manager.launch = AsyncMock(return_value=12345)
    instance_manager.processes = {}

    broadcaster = MagicMock()
    broadcaster.broadcast = AsyncMock()

    dispatcher = GlobalDispatcher(
        db_factory=db_factory,
        instance_manager=instance_manager,
        broadcaster=broadcaster,
    )
    return dispatcher


@pytest.mark.asyncio
async def test_status_not_running(db_factory):
    """status() returns running=False before start."""
    d = _make_dispatcher(db_factory)
    s = d.status()
    assert s["running"] is False
    assert s["active_tasks"] == {}


@pytest.mark.asyncio
async def test_start_sets_running(db_factory):
    """start() sets _running=True and creates dispatch task."""
    d = _make_dispatcher(db_factory)

    # Patch _dispatch_loop to avoid actual polling
    async def fake_loop():
        await asyncio.sleep(999)
    d._dispatch_loop = fake_loop

    await d.start()
    assert d.is_running is True
    assert d._dispatch_task is not None

    # Cleanup
    await d.stop()


@pytest.mark.asyncio
async def test_start_idempotent(db_factory):
    """Calling start() twice doesn't create a second dispatch task."""
    d = _make_dispatcher(db_factory)

    async def fake_loop():
        await asyncio.sleep(999)
    d._dispatch_loop = fake_loop

    await d.start()
    first_task = d._dispatch_task
    await d.start()
    assert d._dispatch_task is first_task

    await d.stop()


@pytest.mark.asyncio
async def test_stop(db_factory):
    """stop() cancels dispatch task and sets _running=False."""
    d = _make_dispatcher(db_factory)

    async def fake_loop():
        await asyncio.sleep(999)
    d._dispatch_loop = fake_loop

    await d.start()
    assert d.is_running is True

    await d.stop()
    assert d.is_running is False
    assert d._dispatch_task.done() or d._dispatch_task.cancelled()


@pytest.mark.asyncio
async def test_ensure_instances_creates_workers(db_factory):
    """_ensure_instances creates workers up to max_concurrent_instances."""
    d = _make_dispatcher(db_factory)

    with patch("backend.services.dispatcher.settings") as mock_settings:
        mock_settings.max_concurrent_instances = 3
        mock_settings.default_model = "sonnet"
        await d._ensure_instances()

    async with db_factory() as db:
        from sqlalchemy import select
        result = await db.execute(select(Instance))
        instances = list(result.scalars().all())
    assert len(instances) == 3
    assert instances[0].name == "worker-1"
    assert instances[2].name == "worker-3"


@pytest.mark.asyncio
async def test_ensure_instances_skips_if_enough(db_factory):
    """_ensure_instances does nothing if enough instances exist."""
    d = _make_dispatcher(db_factory)

    # Pre-create 2 instances
    async with db_factory() as db:
        db.add(Instance(name="w1"))
        db.add(Instance(name="w2"))
        await db.commit()

    with patch("backend.services.dispatcher.settings") as mock_settings:
        mock_settings.max_concurrent_instances = 2
        await d._ensure_instances()

    async with db_factory() as db:
        from sqlalchemy import select
        result = await db.execute(select(Instance))
        instances = list(result.scalars().all())
    assert len(instances) == 2


@pytest.mark.asyncio
async def test_lifecycle_success(db_factory):
    """_run_task_lifecycle completes task successfully."""
    d = _make_dispatcher(db_factory)

    async with db_factory() as db:
        inst = Instance(name="worker-1")
        db.add(inst)
        task = Task(title="test", description="do something", target_repo="/repo")
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

    assert d.broadcaster.broadcast.await_count >= 2


@pytest.mark.asyncio
async def test_lifecycle_failure_retry(db_factory):
    """Failed task with retries left goes back to pending."""
    d = _make_dispatcher(db_factory)

    async with db_factory() as db:
        inst = Instance(name="worker-1")
        db.add(inst)
        task = Task(title="retry-test", description="fail once",
                    target_repo="/repo", max_retries=3, retry_count=0)
        db.add(task)
        await db.commit()
        await db.refresh(inst)
        await db.refresh(task)
        inst_id = inst.id
        task_obj = task

    mock_proc = MagicMock()
    mock_proc.returncode = 1
    mock_proc.wait = AsyncMock(return_value=1)
    d.instance_manager.processes = {inst_id: mock_proc}

    await d._run_task_lifecycle(inst_id, task_obj)

    async with db_factory() as db:
        t = await db.get(Task, task_obj.id)
        assert t.status == "pending"
        assert t.retry_count == 1


@pytest.mark.asyncio
async def test_lifecycle_failure_max_retries(db_factory):
    """Task at max retries is marked failed."""
    d = _make_dispatcher(db_factory)

    async with db_factory() as db:
        inst = Instance(name="worker-1")
        db.add(inst)
        task = Task(title="max-retry", description="always fail",
                    target_repo="/repo", max_retries=2, retry_count=2)
        db.add(task)
        await db.commit()
        await db.refresh(inst)
        await db.refresh(task)
        inst_id = inst.id
        task_obj = task

    mock_proc = MagicMock()
    mock_proc.returncode = 1
    mock_proc.wait = AsyncMock(return_value=1)
    d.instance_manager.processes = {inst_id: mock_proc}

    await d._run_task_lifecycle(inst_id, task_obj)

    async with db_factory() as db:
        t = await db.get(Task, task_obj.id)
        assert t.status == "failed"


@pytest.mark.asyncio
async def test_lifecycle_exception(db_factory):
    """Unexpected exception marks task as failed."""
    d = _make_dispatcher(db_factory)
    d.instance_manager.launch = AsyncMock(side_effect=Exception("unexpected boom"))

    async with db_factory() as db:
        inst = Instance(name="worker-1")
        db.add(inst)
        task = Task(title="boom", description="test", target_repo="/repo")
        db.add(task)
        await db.commit()
        await db.refresh(inst)
        await db.refresh(task)
        inst_id = inst.id
        task_obj = task

    await d._run_task_lifecycle(inst_id, task_obj)

    async with db_factory() as db:
        t = await db.get(Task, task_obj.id)
        assert t.status == "failed"
        assert t.error_message is not None


@pytest.mark.asyncio
async def test_plan_phase(db_factory):
    """Plan-mode task runs plan phase and sets plan_review status."""
    d = _make_dispatcher(db_factory)

    async with db_factory() as db:
        inst = Instance(name="worker-1")
        db.add(inst)
        task = Task(title="plan-task", description="plan this",
                    target_repo="/repo", mode="plan", plan_approved=False)
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
        assert t.status == "plan_review"


# === Prompt construction with image_paths ===


@pytest.mark.asyncio
async def test_lifecycle_prompt_includes_images(db_factory):
    """When task.metadata_ has image_paths, launch prompt includes image list."""
    d = _make_dispatcher(db_factory)

    async with db_factory() as db:
        inst = Instance(name="worker-img")
        db.add(inst)
        task = Task(
            title="img-task",
            description="do the thing",
            target_repo="/repo",
            metadata_={"image_paths": ["/uploads/a.png", "/uploads/b.jpg"]},
        )
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

    # Check that launch was called with a prompt containing the image paths
    call_kwargs = d.instance_manager.launch.call_args
    prompt_used = call_kwargs.kwargs.get("prompt") or call_kwargs[1].get("prompt")
    assert "/uploads/a.png" in prompt_used
    assert "/uploads/b.jpg" in prompt_used
    assert "Read" in prompt_used


@pytest.mark.asyncio
async def test_lifecycle_prompt_no_images(db_factory):
    """When task has no image_paths, launch prompt uses standard format without image section."""
    d = _make_dispatcher(db_factory)

    async with db_factory() as db:
        inst = Instance(name="worker-noimg")
        db.add(inst)
        task = Task(
            title="no-img-task",
            description="plain task",
            target_repo="/repo",
        )
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

    call_kwargs = d.instance_manager.launch.call_args
    prompt_used = call_kwargs.kwargs.get("prompt") or call_kwargs[1].get("prompt")
    assert "plain task" in prompt_used
    # Should not contain image-related instruction
    assert "参考图片" not in prompt_used
    assert "Read 工具" not in prompt_used


# ===  Loop lifecycle tests ===


def _write_signal(signal_path, action: str, reason: str = "", progress: str | None = None):
    """Helper: write a signal file synchronously."""
    import json
    from pathlib import Path
    data = {"action": action, "reason": reason}
    if progress:
        data["progress"] = progress
    Path(signal_path).write_text(json.dumps(data), encoding="utf-8")


def _make_loop_task(db, tmp_path, max_iterations: int = 50) -> "Task":
    """Create a loop task pointing at tmp_path as target_repo."""
    return Task(
        title="loop-test",
        mode="loop",
        todo_file_path="TODO.md",
        target_repo=str(tmp_path),
        max_iterations=max_iterations,
    )


@pytest.mark.asyncio
async def test_loop_done_after_one_iteration(db_factory, tmp_path):
    """Loop task marked completed when Claude signals 'done' on first iteration."""
    d = _make_dispatcher(db_factory)

    async with db_factory() as db:
        inst = Instance(name="loop-worker-1")
        db.add(inst)
        task = _make_loop_task(db, tmp_path)
        db.add(task)
        await db.commit()
        await db.refresh(inst)
        await db.refresh(task)
        inst_id = inst.id
        task_obj = task

    signal_path = tmp_path / ".claude-manager" / f"loop_signal_{task_obj.id}.json"

    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.wait = AsyncMock(return_value=0)
    # Write signal file when launch is called
    async def launch_and_write(*args, **kwargs):
        signal_path.parent.mkdir(parents=True, exist_ok=True)
        _write_signal(signal_path, "done", "all done", "1/1")
        return 12345
    d.instance_manager.launch = AsyncMock(side_effect=launch_and_write)
    d.instance_manager.processes = {inst_id: mock_proc}

    await d._run_loop_lifecycle(inst_id, task_obj, str(tmp_path))

    async with db_factory() as db:
        t = await db.get(Task, task_obj.id)
        assert t.status == "completed"
        assert t.loop_progress == "1/1"


@pytest.mark.asyncio
async def test_loop_continue_then_done(db_factory, tmp_path):
    """Loop iterates twice: first 'continue', then 'done'."""
    d = _make_dispatcher(db_factory)

    async with db_factory() as db:
        inst = Instance(name="loop-worker-2")
        db.add(inst)
        task = _make_loop_task(db, tmp_path)
        db.add(task)
        await db.commit()
        await db.refresh(inst)
        await db.refresh(task)
        inst_id = inst.id
        task_obj = task

    signal_path = tmp_path / ".claude-manager" / f"loop_signal_{task_obj.id}.json"
    call_count = {"n": 0}

    async def launch_and_write(*args, **kwargs):
        signal_path.parent.mkdir(parents=True, exist_ok=True)
        call_count["n"] += 1
        if call_count["n"] == 1:
            _write_signal(signal_path, "continue", "one more", "1/2")
        else:
            _write_signal(signal_path, "done", "finished", "2/2")
        return 12345

    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.wait = AsyncMock(return_value=0)
    d.instance_manager.launch = AsyncMock(side_effect=launch_and_write)
    d.instance_manager.processes = {inst_id: mock_proc}

    await d._run_loop_lifecycle(inst_id, task_obj, str(tmp_path))

    assert call_count["n"] == 2
    async with db_factory() as db:
        t = await db.get(Task, task_obj.id)
        assert t.status == "completed"
        assert t.loop_progress == "2/2"


@pytest.mark.asyncio
async def test_loop_abort_on_signal(db_factory, tmp_path):
    """Loop marked failed when Claude signals 'abort' and no retries remain."""
    d = _make_dispatcher(db_factory)

    async with db_factory() as db:
        inst = Instance(name="loop-worker-3")
        db.add(inst)
        task = _make_loop_task(db, tmp_path)
        task.max_retries = 0  # skip retry so we get "failed" directly
        db.add(task)
        await db.commit()
        await db.refresh(inst)
        await db.refresh(task)
        inst_id = inst.id
        task_obj = task

    signal_path = tmp_path / ".claude-manager" / f"loop_signal_{task_obj.id}.json"

    async def launch_and_abort(*args, **kwargs):
        signal_path.parent.mkdir(parents=True, exist_ok=True)
        _write_signal(signal_path, "abort", "something went wrong")
        return 12345

    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.wait = AsyncMock(return_value=0)
    d.instance_manager.launch = AsyncMock(side_effect=launch_and_abort)
    d.instance_manager.processes = {inst_id: mock_proc}

    await d._run_loop_lifecycle(inst_id, task_obj, str(tmp_path))

    async with db_factory() as db:
        t = await db.get(Task, task_obj.id)
        assert t.status == "failed"
        assert "something went wrong" in (t.error_message or "")


@pytest.mark.asyncio
async def test_loop_abort_on_missing_signal(db_factory, tmp_path):
    """Loop marked failed when signal file is never written and no retries remain."""
    d = _make_dispatcher(db_factory)

    async with db_factory() as db:
        inst = Instance(name="loop-worker-4")
        db.add(inst)
        task = _make_loop_task(db, tmp_path)
        task.max_retries = 0  # skip retry so we get "failed" directly
        db.add(task)
        await db.commit()
        await db.refresh(inst)
        await db.refresh(task)
        inst_id = inst.id
        task_obj = task

    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.wait = AsyncMock(return_value=0)
    # launch does NOT write a signal file
    d.instance_manager.processes = {inst_id: mock_proc}

    await d._run_loop_lifecycle(inst_id, task_obj, str(tmp_path))

    async with db_factory() as db:
        t = await db.get(Task, task_obj.id)
        assert t.status == "failed"
        assert t.error_message is not None


@pytest.mark.asyncio
async def test_loop_max_iterations_exceeded(db_factory, tmp_path):
    """Loop stops and marks failed after hitting max_iterations."""
    d = _make_dispatcher(db_factory)

    async with db_factory() as db:
        inst = Instance(name="loop-worker-5")
        db.add(inst)
        # max_iterations=2 so third call should never happen
        task = _make_loop_task(db, tmp_path, max_iterations=2)
        db.add(task)
        await db.commit()
        await db.refresh(inst)
        await db.refresh(task)
        inst_id = inst.id
        task_obj = task

    signal_path = tmp_path / ".claude-manager" / f"loop_signal_{task_obj.id}.json"
    call_count = {"n": 0}

    async def launch_and_continue(*args, **kwargs):
        signal_path.parent.mkdir(parents=True, exist_ok=True)
        call_count["n"] += 1
        _write_signal(signal_path, "continue", "more work")
        return 12345

    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.wait = AsyncMock(return_value=0)
    d.instance_manager.launch = AsyncMock(side_effect=launch_and_continue)
    d.instance_manager.processes = {inst_id: mock_proc}

    await d._run_loop_lifecycle(inst_id, task_obj, str(tmp_path))

    # Should have run exactly 2 iterations (iteration 0 and 1), then stopped
    assert call_count["n"] == 2
    async with db_factory() as db:
        t = await db.get(Task, task_obj.id)
        assert t.status == "failed"
        assert "2" in (t.error_message or "")  # error mentions the limit


@pytest.mark.asyncio
async def test_loop_max_iterations_default_is_50(db_factory, tmp_path):
    """Default max_iterations is 50 when not specified."""
    from backend.models.task import Task as TaskModel
    async with db_factory() as db:
        t = TaskModel(title="t", mode="loop", todo_file_path="TODO.md")
        db.add(t)
        await db.commit()
        await db.refresh(t)
        assert t.max_iterations == 50


@pytest.mark.asyncio
async def test_loop_cancelled_between_iterations(db_factory, tmp_path):
    """Loop stops cleanly when task is externally cancelled between iterations."""
    d = _make_dispatcher(db_factory)

    async with db_factory() as db:
        inst = Instance(name="loop-worker-6")
        db.add(inst)
        task = _make_loop_task(db, tmp_path, max_iterations=10)
        db.add(task)
        await db.commit()
        await db.refresh(inst)
        await db.refresh(task)
        inst_id = inst.id
        task_obj = task

    signal_path = tmp_path / ".claude-manager" / f"loop_signal_{task_obj.id}.json"

    async def launch_continue_then_cancel(*args, **kwargs):
        signal_path.parent.mkdir(parents=True, exist_ok=True)
        _write_signal(signal_path, "continue", "keep going")
        # Cancel the task in the DB after first iteration
        async with db_factory() as db:
            from sqlalchemy import update
            await db.execute(
                update(Task).where(Task.id == task_obj.id).values(status="cancelled")
            )
            await db.commit()
        return 12345

    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.wait = AsyncMock(return_value=0)
    d.instance_manager.launch = AsyncMock(side_effect=launch_continue_then_cancel)
    d.instance_manager.processes = {inst_id: mock_proc}

    await d._run_loop_lifecycle(inst_id, task_obj, str(tmp_path))

    # loop stopped — launch called exactly once, task remains cancelled
    assert d.instance_manager.launch.await_count == 1
    async with db_factory() as db:
        t = await db.get(Task, task_obj.id)
        assert t.status == "cancelled"


@pytest.mark.asyncio
async def test_loop_prompt_contains_todo_path_and_signal_path(db_factory, tmp_path):
    """_build_loop_prompt includes todo_file_path and signal_path."""
    d = _make_dispatcher(db_factory)

    task = Task(
        title="prompt-test",
        mode="loop",
        todo_file_path="TASKS.md",
        target_repo=str(tmp_path),
        description="some context",
        max_iterations=10,
    )

    prompt = d._build_loop_prompt(task, iteration=0, signal_path="/repo/.claude-manager/loop_signal_99.json")
    assert "TASKS.md" in prompt
    assert "/repo/.claude-manager/loop_signal_99.json" in prompt
    assert "第 1 轮" in prompt
    assert "some context" in prompt


@pytest.mark.asyncio
async def test_loop_prompt_iteration_numbering(db_factory, tmp_path):
    """_build_loop_prompt shows human-readable 1-indexed iteration number."""
    d = _make_dispatcher(db_factory)
    task = Task(title="t", mode="loop", todo_file_path="TODO.md", max_iterations=10)

    p0 = d._build_loop_prompt(task, iteration=0, signal_path="/sig")
    p2 = d._build_loop_prompt(task, iteration=2, signal_path="/sig")
    assert "第 1 轮" in p0
    assert "第 3 轮" in p2


@pytest.mark.asyncio
async def test_lifecycle_routes_to_loop(db_factory, tmp_path):
    """_run_task_lifecycle delegates to _run_loop_lifecycle for mode='loop'."""
    d = _make_dispatcher(db_factory)

    loop_called = {"called": False}
    original = d._run_loop_lifecycle
    async def fake_loop(*args, **kwargs):
        loop_called["called"] = True
    d._run_loop_lifecycle = fake_loop

    async with db_factory() as db:
        inst = Instance(name="loop-router")
        db.add(inst)
        task = Task(
            title="route-test",
            mode="loop",
            todo_file_path="TODO.md",
            target_repo=str(tmp_path),
            max_iterations=5,
        )
        db.add(task)
        await db.commit()
        await db.refresh(inst)
        await db.refresh(task)
        inst_id = inst.id
        task_obj = task

    await d._run_task_lifecycle(inst_id, task_obj)
    assert loop_called["called"]


# === Resume fix for missing signal ===


@pytest.mark.asyncio
async def test_loop_resume_fix_on_missing_signal(db_factory, tmp_path):
    """When signal is missing, _resume_fix_signal is called; if it succeeds, loop continues normally."""
    d = _make_dispatcher(db_factory)

    async with db_factory() as db:
        inst = Instance(name="loop-resume-1")
        db.add(inst)
        task = _make_loop_task(db, tmp_path)
        db.add(task)
        await db.commit()
        await db.refresh(inst)
        await db.refresh(task)
        # Seed a session_id so resume is possible
        task.session_id = "sess-abc"
        await db.commit()
        await db.refresh(task)
        inst_id = inst.id
        task_obj = task

    signal_path = tmp_path / ".claude-manager" / f"loop_signal_{task_obj.id}.json"
    call_count = {"n": 0}

    async def launch_side_effect(*args, **kwargs):
        call_count["n"] += 1
        signal_path.parent.mkdir(parents=True, exist_ok=True)
        if call_count["n"] == 1:
            # First call: normal iteration — forget to write signal
            pass
        elif call_count["n"] == 2:
            # Second call: resume fix — write done signal
            _write_signal(signal_path, "done", "fixed", "1/1")
        return 12345

    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.wait = AsyncMock(return_value=0)
    d.instance_manager.launch = AsyncMock(side_effect=launch_side_effect)
    d.instance_manager.processes = {inst_id: mock_proc}

    await d._run_loop_lifecycle(inst_id, task_obj, str(tmp_path))

    # Should have launched twice: once for the iteration, once for the resume fix
    assert call_count["n"] == 2
    # Second launch must have used resume_session_id
    second_call = d.instance_manager.launch.call_args_list[1]
    resume_used = second_call.kwargs.get("resume_session_id") or second_call[1].get("resume_session_id")
    assert resume_used == "sess-abc"

    async with db_factory() as db:
        t = await db.get(Task, task_obj.id)
        assert t.status == "completed"


@pytest.mark.asyncio
async def test_loop_resume_fix_no_session_id_aborts(db_factory, tmp_path):
    """When signal is missing and task has no session_id, falls back to abort/retry immediately."""
    d = _make_dispatcher(db_factory)

    async with db_factory() as db:
        inst = Instance(name="loop-resume-2")
        db.add(inst)
        task = _make_loop_task(db, tmp_path)
        db.add(task)
        await db.commit()
        await db.refresh(inst)
        await db.refresh(task)
        # No session_id set — resume not possible
        inst_id = inst.id
        task_obj = task

    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.wait = AsyncMock(return_value=0)
    # launch never writes a signal file
    d.instance_manager.processes = {inst_id: mock_proc}

    await d._run_loop_lifecycle(inst_id, task_obj, str(tmp_path))

    # No session → resume fix skipped → retry path (max_retries=2, retry_count starts at 0)
    async with db_factory() as db:
        t = await db.get(Task, task_obj.id)
        assert t.status == "pending"   # retries remain (0 < 2)
        assert t.retry_count == 1


@pytest.mark.asyncio
async def test_loop_resume_fix_still_fails_aborts(db_factory, tmp_path):
    """When resume fix also fails to write signal, task is aborted/retried."""
    d = _make_dispatcher(db_factory)

    async with db_factory() as db:
        inst = Instance(name="loop-resume-3")
        db.add(inst)
        task = _make_loop_task(db, tmp_path)
        db.add(task)
        await db.commit()
        await db.refresh(inst)
        await db.refresh(task)
        task.session_id = "sess-xyz"
        task.max_retries = 0  # no retries so we get "failed"
        await db.commit()
        await db.refresh(task)
        inst_id = inst.id
        task_obj = task

    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.wait = AsyncMock(return_value=0)
    # Neither iteration nor resume fix writes the signal
    d.instance_manager.processes = {inst_id: mock_proc}

    await d._run_loop_lifecycle(inst_id, task_obj, str(tmp_path))

    async with db_factory() as db:
        t = await db.get(Task, task_obj.id)
        assert t.status == "failed"


# === Loop abort auto-retry ===


@pytest.mark.asyncio
async def test_loop_abort_triggers_retry_when_retries_remain(db_factory, tmp_path):
    """Loop abort sets status=pending when retry_count < max_retries."""
    d = _make_dispatcher(db_factory)

    async with db_factory() as db:
        inst = Instance(name="loop-retry-1")
        db.add(inst)
        task = _make_loop_task(db, tmp_path)
        task.max_retries = 2
        task.retry_count = 0
        db.add(task)
        await db.commit()
        await db.refresh(inst)
        await db.refresh(task)
        inst_id = inst.id
        task_obj = task

    signal_path = tmp_path / ".claude-manager" / f"loop_signal_{task_obj.id}.json"

    async def launch_and_abort(*args, **kwargs):
        signal_path.parent.mkdir(parents=True, exist_ok=True)
        _write_signal(signal_path, "abort", "something broke")
        return 12345

    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.wait = AsyncMock(return_value=0)
    d.instance_manager.launch = AsyncMock(side_effect=launch_and_abort)
    d.instance_manager.processes = {inst_id: mock_proc}

    await d._run_loop_lifecycle(inst_id, task_obj, str(tmp_path))

    async with db_factory() as db:
        t = await db.get(Task, task_obj.id)
        assert t.status == "pending"
        assert t.retry_count == 1


@pytest.mark.asyncio
async def test_loop_abort_marks_failed_when_retries_exhausted(db_factory, tmp_path):
    """Loop abort marks failed when retry_count == max_retries."""
    d = _make_dispatcher(db_factory)

    async with db_factory() as db:
        inst = Instance(name="loop-retry-2")
        db.add(inst)
        task = _make_loop_task(db, tmp_path)
        task.max_retries = 2
        task.retry_count = 2
        db.add(task)
        await db.commit()
        await db.refresh(inst)
        await db.refresh(task)
        inst_id = inst.id
        task_obj = task

    signal_path = tmp_path / ".claude-manager" / f"loop_signal_{task_obj.id}.json"

    async def launch_and_abort(*args, **kwargs):
        signal_path.parent.mkdir(parents=True, exist_ok=True)
        _write_signal(signal_path, "abort", "exhausted")
        return 12345

    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.wait = AsyncMock(return_value=0)
    d.instance_manager.launch = AsyncMock(side_effect=launch_and_abort)
    d.instance_manager.processes = {inst_id: mock_proc}

    await d._run_loop_lifecycle(inst_id, task_obj, str(tmp_path))

    async with db_factory() as db:
        t = await db.get(Task, task_obj.id)
        assert t.status == "failed"
        assert "exhausted" in (t.error_message or "")


# === Cancellation during iteration ===


@pytest.mark.asyncio
async def test_loop_cancel_during_iteration_stops_cleanly(db_factory, tmp_path):
    """If task is cancelled while process runs, loop exits without marking failed."""
    d = _make_dispatcher(db_factory)

    async with db_factory() as db:
        inst = Instance(name="loop-cancel-mid")
        db.add(inst)
        task = _make_loop_task(db, tmp_path, max_iterations=10)
        db.add(task)
        await db.commit()
        await db.refresh(inst)
        await db.refresh(task)
        inst_id = inst.id
        task_obj = task

    async def launch_and_cancel(*args, **kwargs):
        # Simulate: process runs but task gets cancelled externally before it writes signal
        async with db_factory() as db:
            from sqlalchemy import update as sa_update
            await db.execute(
                sa_update(Task).where(Task.id == task_obj.id).values(status="cancelled")
            )
            await db.commit()
        # Does NOT write signal
        return 12345

    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.wait = AsyncMock(return_value=0)
    d.instance_manager.launch = AsyncMock(side_effect=launch_and_cancel)
    d.instance_manager.processes = {inst_id: mock_proc}

    await d._run_loop_lifecycle(inst_id, task_obj, str(tmp_path))

    # Task should remain cancelled, not overwritten to failed
    async with db_factory() as db:
        t = await db.get(Task, task_obj.id)
        assert t.status == "cancelled"


# === total_tasks_completed counting ===


@pytest.mark.asyncio
async def test_auto_task_total_completed_incremented_once(db_factory):
    """Completing an auto-mode task increments total_tasks_completed exactly once."""
    d = _make_dispatcher(db_factory)

    async with db_factory() as db:
        inst = Instance(name="count-worker", total_tasks_completed=0)
        db.add(inst)
        task = Task(title="count-test", description="do it", target_repo="/repo")
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
        i = await db.get(Instance, inst_id)
        assert i.total_tasks_completed == 1


@pytest.mark.asyncio
async def test_loop_task_total_completed_incremented_once_on_done(db_factory, tmp_path):
    """Completing a loop task increments total_tasks_completed exactly once."""
    d = _make_dispatcher(db_factory)

    async with db_factory() as db:
        inst = Instance(name="loop-count-worker", total_tasks_completed=0)
        db.add(inst)
        task = _make_loop_task(db, tmp_path, max_iterations=5)
        db.add(task)
        await db.commit()
        await db.refresh(inst)
        await db.refresh(task)
        inst_id = inst.id
        task_obj = task

    signal_path = tmp_path / ".claude-manager" / f"loop_signal_{task_obj.id}.json"
    call_count = {"n": 0}

    async def launch_two_iters(*args, **kwargs):
        signal_path.parent.mkdir(parents=True, exist_ok=True)
        call_count["n"] += 1
        if call_count["n"] == 1:
            _write_signal(signal_path, "continue", "more", "1/2")
        else:
            _write_signal(signal_path, "done", "all done", "2/2")
        return 12345

    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.wait = AsyncMock(return_value=0)
    d.instance_manager.launch = AsyncMock(side_effect=launch_two_iters)
    d.instance_manager.processes = {inst_id: mock_proc}

    await d._run_loop_lifecycle(inst_id, task_obj, str(tmp_path))

    async with db_factory() as db:
        i = await db.get(Instance, inst_id)
        # Should be exactly 1 regardless of how many iterations ran
        assert i.total_tasks_completed == 1


@pytest.mark.asyncio
async def test_loop_total_completed_not_incremented_on_abort(db_factory, tmp_path):
    """Aborted loop task does NOT increment total_tasks_completed."""
    d = _make_dispatcher(db_factory)

    async with db_factory() as db:
        inst = Instance(name="abort-count-worker", total_tasks_completed=0)
        db.add(inst)
        task = _make_loop_task(db, tmp_path, max_iterations=5)
        task.max_retries = 0  # fail directly, no retry
        db.add(task)
        await db.commit()
        await db.refresh(inst)
        await db.refresh(task)
        inst_id = inst.id
        task_obj = task

    signal_path = tmp_path / ".claude-manager" / f"loop_signal_{task_obj.id}.json"

    async def launch_and_abort(*args, **kwargs):
        signal_path.parent.mkdir(parents=True, exist_ok=True)
        _write_signal(signal_path, "abort", "blocked")
        return 12345

    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.wait = AsyncMock(return_value=0)
    d.instance_manager.launch = AsyncMock(side_effect=launch_and_abort)
    d.instance_manager.processes = {inst_id: mock_proc}

    await d._run_loop_lifecycle(inst_id, task_obj, str(tmp_path))

    async with db_factory() as db:
        i = await db.get(Instance, inst_id)
        assert i.total_tasks_completed == 0


@pytest.mark.asyncio
async def test_resume_fix_signal_passes_loop_iteration(db_factory, tmp_path):
    """_resume_fix_signal passes the correct loop_iteration to instance_manager.launch."""
    d = _make_dispatcher(db_factory)

    async with db_factory() as db:
        inst = Instance(name="iter-check-worker")
        db.add(inst)
        task = _make_loop_task(db, tmp_path)
        db.add(task)
        await db.commit()
        await db.refresh(inst)
        await db.refresh(task)
        task.session_id = "sess-iter"
        await db.commit()
        await db.refresh(task)
        inst_id = inst.id
        task_obj = task

    signal_path = tmp_path / ".claude-manager" / f"loop_signal_{task_obj.id}.json"
    signal_path.parent.mkdir(parents=True, exist_ok=True)

    async def fix_launch(*args, **kwargs):
        _write_signal(signal_path, "done", "ok")
        return 99999

    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.wait = AsyncMock(return_value=0)
    d.instance_manager.launch = AsyncMock(side_effect=fix_launch)
    d.instance_manager.processes = {inst_id: mock_proc}

    result = await d._resume_fix_signal(inst_id, task_obj, str(tmp_path), signal_path, iteration=3, git_env={})

    call_kwargs = d.instance_manager.launch.call_args
    loop_iter_used = call_kwargs.kwargs.get("loop_iteration") or call_kwargs[1].get("loop_iteration")
    assert loop_iter_used == 3
    resume_used = call_kwargs.kwargs.get("resume_session_id") or call_kwargs[1].get("resume_session_id")
    assert resume_used == "sess-iter"
    assert result.get("action") == "done"


@pytest.mark.asyncio
async def test_resume_fix_signal_timeout_returns_abort(db_factory, tmp_path):
    """When the resume fix process times out, _resume_fix_signal returns abort."""
    d = _make_dispatcher(db_factory)

    async with db_factory() as db:
        inst = Instance(name="timeout-fix-worker")
        db.add(inst)
        task = _make_loop_task(db, tmp_path)
        db.add(task)
        await db.commit()
        await db.refresh(inst)
        await db.refresh(task)
        task.session_id = "sess-timeout"
        await db.commit()
        await db.refresh(task)
        inst_id = inst.id
        task_obj = task

    signal_path = tmp_path / ".claude-manager" / f"loop_signal_{task_obj.id}.json"
    signal_path.parent.mkdir(parents=True, exist_ok=True)

    async def hang_and_kill(*args, **kwargs):
        # Simulate process that hangs (timeout path in _resume_fix_signal uses 60s,
        # but we patch wait_for to raise TimeoutError directly)
        return 99999

    mock_proc = MagicMock()
    mock_proc.returncode = 0
    # First wait() raises TimeoutError (simulating the timeout); second wait() (cleanup) succeeds
    mock_proc.wait = AsyncMock(side_effect=[asyncio.TimeoutError(), None])
    mock_proc.kill = MagicMock()
    d.instance_manager.launch = AsyncMock(side_effect=hang_and_kill)
    d.instance_manager.processes = {inst_id: mock_proc}

    # Signal file is NOT written (process timed out before writing)
    result = await d._resume_fix_signal(inst_id, task_obj, str(tmp_path), signal_path, iteration=0, git_env={})

    assert result.get("action") == "abort"
    assert mock_proc.kill.called


@pytest.mark.asyncio
async def test_resume_fix_not_called_for_explicit_abort(db_factory, tmp_path):
    """_resume_fix_signal is NOT invoked when Claude writes an explicit 'abort' signal."""
    d = _make_dispatcher(db_factory)

    resume_fix_called = {"called": False}
    original_fix = d._resume_fix_signal
    async def spy_fix(*args, **kwargs):
        resume_fix_called["called"] = True
        return await original_fix(*args, **kwargs)
    d._resume_fix_signal = spy_fix

    async with db_factory() as db:
        inst = Instance(name="no-fix-worker")
        db.add(inst)
        task = _make_loop_task(db, tmp_path)
        task.max_retries = 0
        db.add(task)
        await db.commit()
        await db.refresh(inst)
        await db.refresh(task)
        inst_id = inst.id
        task_obj = task

    signal_path = tmp_path / ".claude-manager" / f"loop_signal_{task_obj.id}.json"

    async def launch_and_explicit_abort(*args, **kwargs):
        signal_path.parent.mkdir(parents=True, exist_ok=True)
        _write_signal(signal_path, "abort", "intentional stop")
        return 12345

    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.wait = AsyncMock(return_value=0)
    d.instance_manager.launch = AsyncMock(side_effect=launch_and_explicit_abort)
    d.instance_manager.processes = {inst_id: mock_proc}

    await d._run_loop_lifecycle(inst_id, task_obj, str(tmp_path))

    assert not resume_fix_called["called"]
    async with db_factory() as db:
        t = await db.get(Task, task_obj.id)
        assert t.status == "failed"


@pytest.mark.asyncio
async def test_lifecycle_prompt_empty_image_paths(db_factory):
    """Empty image_paths list in metadata_ behaves the same as no images."""
    d = _make_dispatcher(db_factory)

    async with db_factory() as db:
        inst = Instance(name="worker-emptyimg")
        db.add(inst)
        task = Task(
            title="empty-img-task",
            description="another plain task",
            target_repo="/repo",
            metadata_={"image_paths": []},
        )
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

    call_kwargs = d.instance_manager.launch.call_args
    prompt_used = call_kwargs.kwargs.get("prompt") or call_kwargs[1].get("prompt")
    assert "参考图片" not in prompt_used


# === _ensure_instances_for_pending_tasks tests ===


@pytest.mark.asyncio
async def test_ensure_instances_for_pending_tasks_creates_missing(db_factory):
    """Auto-creates an instance when a pending task requires a model with no instance."""
    from sqlalchemy import select as sa_select
    d = _make_dispatcher(db_factory)

    async with db_factory() as db:
        db.add(Task(title="t", description="d", target_repo="/tmp", status="pending", model="haiku"))
        await db.commit()

    await d._ensure_instances_for_pending_tasks()

    async with db_factory() as db:
        result = await db.execute(sa_select(Instance).where(Instance.model == "haiku"))
        instances = list(result.scalars().all())
    assert len(instances) == 1
    assert instances[0].name == "worker-haiku-1"


@pytest.mark.asyncio
async def test_ensure_instances_for_pending_tasks_skips_if_instance_exists(db_factory):
    """Does not create a duplicate instance when one already exists for that model."""
    from sqlalchemy import select as sa_select
    d = _make_dispatcher(db_factory)

    async with db_factory() as db:
        db.add(Instance(name="existing-opus", model="opus"))
        db.add(Task(title="t", description="d", target_repo="/tmp", status="pending", model="opus"))
        await db.commit()

    await d._ensure_instances_for_pending_tasks()

    async with db_factory() as db:
        result = await db.execute(sa_select(Instance).where(Instance.model == "opus"))
        instances = list(result.scalars().all())
    assert len(instances) == 1


@pytest.mark.asyncio
async def test_ensure_instances_for_pending_tasks_ignores_null_model(db_factory):
    """Tasks with model=None do not trigger auto-instance creation."""
    from sqlalchemy import select as sa_select
    d = _make_dispatcher(db_factory)

    async with db_factory() as db:
        db.add(Task(title="t", description="d", target_repo="/tmp", status="pending"))
        await db.commit()

    await d._ensure_instances_for_pending_tasks()

    async with db_factory() as db:
        result = await db.execute(sa_select(Instance))
        instances = list(result.scalars().all())
    assert len(instances) == 0


@pytest.mark.asyncio
async def test_ensure_instances_for_pending_tasks_ignores_non_pending(db_factory):
    """Completed/cancelled tasks do not trigger auto-instance creation."""
    from sqlalchemy import select as sa_select
    d = _make_dispatcher(db_factory)

    async with db_factory() as db:
        db.add(Task(title="done", description="d", target_repo="/tmp", status="completed", model="sonnet"))
        db.add(Task(title="cancelled", description="d", target_repo="/tmp", status="cancelled", model="sonnet"))
        await db.commit()

    await d._ensure_instances_for_pending_tasks()

    async with db_factory() as db:
        result = await db.execute(sa_select(Instance))
        instances = list(result.scalars().all())
    assert len(instances) == 0


@pytest.mark.asyncio
async def test_ensure_instances_for_pending_tasks_multiple_models(db_factory):
    """Creates one instance per missing model."""
    from sqlalchemy import select as sa_select
    d = _make_dispatcher(db_factory)

    async with db_factory() as db:
        db.add(Task(title="t1", description="d", target_repo="/tmp", status="pending", model="opus"))
        db.add(Task(title="t2", description="d", target_repo="/tmp", status="pending", model="sonnet"))
        await db.commit()

    await d._ensure_instances_for_pending_tasks()

    async with db_factory() as db:
        result = await db.execute(sa_select(Instance))
        instances = list(result.scalars().all())
    models = {i.model for i in instances}
    assert models == {"opus", "sonnet"}
