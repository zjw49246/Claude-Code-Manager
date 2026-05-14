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
    instance_manager._tasks = {}

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


def _write_signal(signal_path, action: str, reason: str = "", progress: str | None = None, summary: str | None = None, plan: str | None = None):
    """Helper: write a signal file synchronously."""
    import json
    from pathlib import Path
    data = {"action": action, "reason": reason}
    if progress:
        data["progress"] = progress
    if summary:
        data["summary"] = summary
    if plan:
        data["plan"] = plan
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
async def test_loop_prompt_no_history_no_anchor(db_factory):
    """First iteration: no history section, progress hint is generic."""
    d = _make_dispatcher(db_factory)
    task = Task(title="t", mode="loop", todo_file_path="TODO.md", max_iterations=10)

    prompt = d._build_loop_prompt(task, iteration=0, signal_path="/sig", history=None, anchored_total=None)
    assert "前几轮完成情况" not in prompt
    assert "已完成数/总数" in prompt
    assert "不要重新计数" not in prompt


@pytest.mark.asyncio
async def test_loop_prompt_with_history_and_anchor(db_factory):
    """Subsequent iterations include history and anchored total in denominator."""
    d = _make_dispatcher(db_factory)
    task = Task(title="t", mode="loop", todo_file_path="TODO.md", max_iterations=10)

    history = [
        {"iteration": 1, "progress": "2/10", "summary": "完成了登录和注册"},
        {"iteration": 2, "progress": "5/10", "summary": "实现了JWT刷新"},
    ]
    prompt = d._build_loop_prompt(task, iteration=2, signal_path="/sig", history=history, anchored_total=10)

    # History section present
    assert "=== 前几轮完成情况 ===" in prompt
    assert "第 1 轮 | 进度: 2/10 | 完成了登录和注册" in prompt
    assert "第 2 轮 | 进度: 5/10 | 实现了JWT刷新" in prompt
    assert "=== 前几轮完成情况结束 ===" in prompt

    # Anchored denominator
    assert "已完成数/10" in prompt
    assert "任务总数已确定为 10" in prompt
    assert "不要重新计数" in prompt

    # Generic hint should NOT appear
    assert "已完成数/总数" not in prompt


@pytest.mark.asyncio
async def test_loop_prompt_history_without_summary(db_factory):
    """History entries with missing summary still render correctly."""
    d = _make_dispatcher(db_factory)
    task = Task(title="t", mode="loop", todo_file_path="TODO.md", max_iterations=10)

    history = [{"iteration": 1, "progress": "1/5", "summary": ""}]
    prompt = d._build_loop_prompt(task, iteration=1, signal_path="/sig", history=history, anchored_total=5)

    assert "第 1 轮 | 进度: 1/5" in prompt
    # No trailing " | " when summary is empty
    assert "第 1 轮 | 进度: 1/5 |" not in prompt


@pytest.mark.asyncio
async def test_loop_prompt_history_without_progress(db_factory):
    """History entries with missing progress still render correctly."""
    d = _make_dispatcher(db_factory)
    task = Task(title="t", mode="loop", todo_file_path="TODO.md", max_iterations=10)

    history = [{"iteration": 1, "progress": "", "summary": "did something"}]
    prompt = d._build_loop_prompt(task, iteration=1, signal_path="/sig", history=history, anchored_total=None)

    assert "第 1 轮 | did something" in prompt
    assert "进度:" not in prompt


@pytest.mark.asyncio
async def test_loop_prompt_all_actions_include_summary_and_progress(db_factory):
    """All three signal templates (continue/done/abort) include summary and progress fields."""
    d = _make_dispatcher(db_factory)
    task = Task(title="t", mode="loop", todo_file_path="TODO.md", max_iterations=10)

    prompt = d._build_loop_prompt(task, iteration=0, signal_path="/sig")

    # Each action block should have both progress and summary
    for action in ["continue", "done", "abort"]:
        # Find the line containing this action
        lines = [l for l in prompt.split("\n") if f'"action": "{action}"' in l]
        assert len(lines) == 1, f"Expected exactly one template line for action={action}"
        assert '"progress":' in lines[0], f"action={action} missing progress field"
        assert '"summary":' in lines[0], f"action={action} missing summary field"


@pytest.mark.asyncio
async def test_loop_lifecycle_anchors_total_from_first_signal(db_factory, tmp_path):
    """Total is anchored from the first progress report and used in subsequent prompts."""
    d = _make_dispatcher(db_factory)

    async with db_factory() as db:
        inst = Instance(name="anchor-worker")
        db.add(inst)
        task = _make_loop_task(db, tmp_path, max_iterations=10)
        db.add(task)
        await db.commit()
        await db.refresh(inst)
        await db.refresh(task)
        inst_id = inst.id
        task_obj = task

    signal_path = tmp_path / ".claude-manager" / f"loop_signal_{task_obj.id}.json"
    prompts_sent: list[str] = []
    call_count = {"n": 0}

    async def launch_capture_prompt(*args, **kwargs):
        prompt = kwargs.get("prompt", args[1] if len(args) > 1 else "")
        prompts_sent.append(prompt)
        signal_path.parent.mkdir(parents=True, exist_ok=True)
        call_count["n"] += 1
        if call_count["n"] == 1:
            _write_signal(signal_path, "continue", "more to do", "3/10", "完成了前三项")
        elif call_count["n"] == 2:
            _write_signal(signal_path, "continue", "more to do", "7/10", "完成了四项")
        else:
            _write_signal(signal_path, "done", "all done", "10/10", "最后三项")
        return 12345

    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.wait = AsyncMock(return_value=0)
    d.instance_manager.launch = AsyncMock(side_effect=launch_capture_prompt)
    d.instance_manager.processes = {inst_id: mock_proc}

    await d._run_loop_lifecycle(inst_id, task_obj, str(tmp_path))

    assert call_count["n"] == 3

    # First prompt: no anchor yet, generic hint
    assert "已完成数/总数" in prompts_sent[0]
    assert "前几轮完成情况" not in prompts_sent[0]

    # Second prompt: anchored total=10, has first iteration history
    assert "已完成数/10" in prompts_sent[1]
    assert "任务总数已确定为 10" in prompts_sent[1]
    assert "第 1 轮 | 进度: 3/10 | 完成了前三项" in prompts_sent[1]

    # Third prompt: still anchored at 10, has two iterations of history
    assert "已完成数/10" in prompts_sent[2]
    assert "第 1 轮 | 进度: 3/10 | 完成了前三项" in prompts_sent[2]
    assert "第 2 轮 | 进度: 7/10 | 完成了四项" in prompts_sent[2]

    # Final progress stored in DB
    async with db_factory() as db:
        t = await db.get(Task, task_obj.id)
        assert t.status == "completed"
        assert t.loop_progress == "10/10"


@pytest.mark.asyncio
async def test_loop_lifecycle_anchor_survives_missing_progress(db_factory, tmp_path):
    """If a later signal omits progress, the anchored total is still used for prompts."""
    d = _make_dispatcher(db_factory)

    async with db_factory() as db:
        inst = Instance(name="anchor-missing-worker")
        db.add(inst)
        task = _make_loop_task(db, tmp_path, max_iterations=10)
        db.add(task)
        await db.commit()
        await db.refresh(inst)
        await db.refresh(task)
        inst_id = inst.id
        task_obj = task

    signal_path = tmp_path / ".claude-manager" / f"loop_signal_{task_obj.id}.json"
    prompts_sent: list[str] = []
    call_count = {"n": 0}

    async def launch_capture(*args, **kwargs):
        prompt = kwargs.get("prompt", args[1] if len(args) > 1 else "")
        prompts_sent.append(prompt)
        signal_path.parent.mkdir(parents=True, exist_ok=True)
        call_count["n"] += 1
        if call_count["n"] == 1:
            _write_signal(signal_path, "continue", "ok", "1/8", "第一项")
        elif call_count["n"] == 2:
            # Claude forgets to include progress
            _write_signal(signal_path, "continue", "ok")
        else:
            _write_signal(signal_path, "done", "done", "8/8")
        return 12345

    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.wait = AsyncMock(return_value=0)
    d.instance_manager.launch = AsyncMock(side_effect=launch_capture)
    d.instance_manager.processes = {inst_id: mock_proc}

    await d._run_loop_lifecycle(inst_id, task_obj, str(tmp_path))

    # Third prompt should still have anchored_total=8 even though second signal had no progress
    assert "已完成数/8" in prompts_sent[2]
    assert "任务总数已确定为 8" in prompts_sent[2]


@pytest.mark.asyncio
async def test_loop_lifecycle_non_numeric_progress_no_anchor(db_factory, tmp_path):
    """If progress is not in N/M format, anchoring is skipped gracefully."""
    d = _make_dispatcher(db_factory)

    async with db_factory() as db:
        inst = Instance(name="non-numeric-worker")
        db.add(inst)
        task = _make_loop_task(db, tmp_path, max_iterations=10)
        db.add(task)
        await db.commit()
        await db.refresh(inst)
        await db.refresh(task)
        inst_id = inst.id
        task_obj = task

    signal_path = tmp_path / ".claude-manager" / f"loop_signal_{task_obj.id}.json"
    prompts_sent: list[str] = []
    call_count = {"n": 0}

    async def launch_capture(*args, **kwargs):
        prompt = kwargs.get("prompt", args[1] if len(args) > 1 else "")
        prompts_sent.append(prompt)
        signal_path.parent.mkdir(parents=True, exist_ok=True)
        call_count["n"] += 1
        if call_count["n"] == 1:
            _write_signal(signal_path, "continue", "ok", "一些进度")
        else:
            _write_signal(signal_path, "done", "done", "全部完成")
        return 12345

    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.wait = AsyncMock(return_value=0)
    d.instance_manager.launch = AsyncMock(side_effect=launch_capture)
    d.instance_manager.processes = {inst_id: mock_proc}

    await d._run_loop_lifecycle(inst_id, task_obj, str(tmp_path))

    # No anchoring happened — still using generic hint
    assert "已完成数/总数" in prompts_sent[1]
    assert "不要重新计数" not in prompts_sent[1]
    # But the history still shows the raw progress string
    assert "一些进度" in prompts_sent[1]


@pytest.mark.asyncio
async def test_must_complete_prompt_first_iteration(db_factory):
    """must_complete first iteration requires planning and shows total rounds."""
    d = _make_dispatcher(db_factory)
    task = Task(title="t", mode="loop", todo_file_path="TODO.md", max_iterations=8, must_complete=True)

    prompt = d._build_loop_prompt(task, iteration=0, signal_path="/sig")
    assert "必须全部完成" in prompt
    assert "总共有 8 轮" in prompt
    assert "制定整体执行计划" in prompt
    assert '"plan":' in prompt
    assert "已完成数/总数" in prompt


@pytest.mark.asyncio
async def test_must_complete_prompt_subsequent_iteration(db_factory):
    """must_complete subsequent iteration shows remaining rounds, plan, and anchored total."""
    d = _make_dispatcher(db_factory)
    task = Task(title="t", mode="loop", todo_file_path="TODO.md", max_iterations=10, must_complete=True)

    history = [
        {"iteration": 1, "progress": "3/10", "summary": "完成前三项"},
    ]
    plan_text = "第2轮: 权限; 第3轮: 测试"
    prompt = d._build_loop_prompt(
        task, iteration=1, signal_path="/sig",
        history=history, anchored_total=10, plan=plan_text,
    )

    assert "必须全部完成" in prompt
    assert "第 2 轮" in prompt
    assert "还剩 9 轮" in prompt
    assert "=== 整体计划 ===" in prompt
    assert plan_text in prompt
    assert "任务总数已确定为 10" in prompt
    assert "还剩 7 项未完成" in prompt
    assert "已完成数/10" in prompt


@pytest.mark.asyncio
async def test_must_complete_prompt_no_anchor_subsequent(db_factory):
    """must_complete subsequent iteration without anchored_total doesn't show total info."""
    d = _make_dispatcher(db_factory)
    task = Task(title="t", mode="loop", todo_file_path="TODO.md", max_iterations=5, must_complete=True)

    history = [{"iteration": 1, "progress": "", "summary": "did something"}]
    prompt = d._build_loop_prompt(task, iteration=1, signal_path="/sig", history=history, anchored_total=None)

    assert "必须全部完成" in prompt
    assert "还剩 4 轮" in prompt
    assert "任务总数已确定为" not in prompt
    assert "已完成数/总数" in prompt


@pytest.mark.asyncio
async def test_must_complete_prompt_plan_update_field(db_factory):
    """must_complete continue template includes plan field for updating."""
    d = _make_dispatcher(db_factory)
    task = Task(title="t", mode="loop", todo_file_path="TODO.md", max_iterations=5, must_complete=True)

    prompt = d._build_loop_prompt(task, iteration=1, signal_path="/sig", history=[{"iteration": 1, "progress": "1/5", "summary": "x"}], anchored_total=5)
    continue_lines = [l for l in prompt.split("\n") if '"action": "continue"' in l]
    assert len(continue_lines) == 1
    assert '"plan":' in continue_lines[0]


@pytest.mark.asyncio
async def test_normal_loop_prompt_no_plan_field(db_factory):
    """Normal (non-must_complete) loop prompt does NOT include plan field."""
    d = _make_dispatcher(db_factory)
    task = Task(title="t", mode="loop", todo_file_path="TODO.md", max_iterations=10, must_complete=False)

    prompt = d._build_loop_prompt(task, iteration=0, signal_path="/sig")
    assert "必须全部完成" not in prompt
    assert '"plan":' not in prompt
    assert "制定整体执行计划" not in prompt


@pytest.mark.asyncio
async def test_must_complete_rejects_premature_done(db_factory, tmp_path):
    """must_complete rejects done signal when progress numerator < anchored total."""
    d = _make_dispatcher(db_factory)

    async with db_factory() as db:
        inst = Instance(name="mc-reject-worker")
        db.add(inst)
        task = Task(
            title="mc-reject", mode="loop", todo_file_path="TODO.md",
            target_repo=str(tmp_path), max_iterations=10, must_complete=True,
        )
        db.add(task)
        await db.commit()
        await db.refresh(inst)
        await db.refresh(task)
        inst_id = inst.id
        task_obj = task

    signal_path = tmp_path / ".claude-manager" / f"loop_signal_{task_obj.id}.json"
    call_count = {"n": 0}

    async def launch_side(*args, **kwargs):
        signal_path.parent.mkdir(parents=True, exist_ok=True)
        call_count["n"] += 1
        if call_count["n"] == 1:
            _write_signal(signal_path, "continue", "more", "3/10", "did 3", plan="第2轮: 做剩下的")
        elif call_count["n"] == 2:
            # Premature done — only 7/10
            _write_signal(signal_path, "done", "I think I'm done", "7/10", "did 4")
        elif call_count["n"] == 3:
            # After rejection, continue
            _write_signal(signal_path, "continue", "ok more", "9/10", "did 2")
        else:
            _write_signal(signal_path, "done", "really done", "10/10", "did last 1")
        return 12345

    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.wait = AsyncMock(return_value=0)
    d.instance_manager.launch = AsyncMock(side_effect=launch_side)
    d.instance_manager.processes = {inst_id: mock_proc}

    await d._run_loop_lifecycle(inst_id, task_obj, str(tmp_path))

    # Should have run 4 iterations: iter0(continue), iter1(done→rejected), iter2(continue), iter3(done→accepted)
    assert call_count["n"] == 4
    async with db_factory() as db:
        t = await db.get(Task, task_obj.id)
        assert t.status == "completed"
        assert t.loop_progress == "10/10"


@pytest.mark.asyncio
async def test_must_complete_accepts_done_when_complete(db_factory, tmp_path):
    """must_complete accepts done signal when progress numerator == anchored total."""
    d = _make_dispatcher(db_factory)

    async with db_factory() as db:
        inst = Instance(name="mc-accept-worker")
        db.add(inst)
        task = Task(
            title="mc-accept", mode="loop", todo_file_path="TODO.md",
            target_repo=str(tmp_path), max_iterations=10, must_complete=True,
        )
        db.add(task)
        await db.commit()
        await db.refresh(inst)
        await db.refresh(task)
        inst_id = inst.id
        task_obj = task

    signal_path = tmp_path / ".claude-manager" / f"loop_signal_{task_obj.id}.json"
    call_count = {"n": 0}

    async def launch_side(*args, **kwargs):
        signal_path.parent.mkdir(parents=True, exist_ok=True)
        call_count["n"] += 1
        if call_count["n"] == 1:
            _write_signal(signal_path, "continue", "more", "5/10", "half done", plan="finish rest")
        else:
            _write_signal(signal_path, "done", "all done", "10/10", "finished all")
        return 12345

    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.wait = AsyncMock(return_value=0)
    d.instance_manager.launch = AsyncMock(side_effect=launch_side)
    d.instance_manager.processes = {inst_id: mock_proc}

    await d._run_loop_lifecycle(inst_id, task_obj, str(tmp_path))

    assert call_count["n"] == 2
    async with db_factory() as db:
        t = await db.get(Task, task_obj.id)
        assert t.status == "completed"


@pytest.mark.asyncio
async def test_must_complete_done_unparseable_progress_accepted(db_factory, tmp_path):
    """must_complete accepts done if progress can't be parsed (can't verify, don't block)."""
    d = _make_dispatcher(db_factory)

    async with db_factory() as db:
        inst = Instance(name="mc-noparse-worker")
        db.add(inst)
        task = Task(
            title="mc-noparse", mode="loop", todo_file_path="TODO.md",
            target_repo=str(tmp_path), max_iterations=10, must_complete=True,
        )
        db.add(task)
        await db.commit()
        await db.refresh(inst)
        await db.refresh(task)
        inst_id = inst.id
        task_obj = task

    signal_path = tmp_path / ".claude-manager" / f"loop_signal_{task_obj.id}.json"

    async def launch_side(*args, **kwargs):
        signal_path.parent.mkdir(parents=True, exist_ok=True)
        _write_signal(signal_path, "done", "all done", "全部完成")
        return 12345

    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.wait = AsyncMock(return_value=0)
    d.instance_manager.launch = AsyncMock(side_effect=launch_side)
    d.instance_manager.processes = {inst_id: mock_proc}

    await d._run_loop_lifecycle(inst_id, task_obj, str(tmp_path))

    async with db_factory() as db:
        t = await db.get(Task, task_obj.id)
        assert t.status == "completed"


@pytest.mark.asyncio
async def test_must_complete_max_iterations_fail_message(db_factory, tmp_path):
    """must_complete uses specific fail message when max iterations exceeded."""
    d = _make_dispatcher(db_factory)

    async with db_factory() as db:
        inst = Instance(name="mc-maxiter-worker")
        db.add(inst)
        task = Task(
            title="mc-maxiter", mode="loop", todo_file_path="TODO.md",
            target_repo=str(tmp_path), max_iterations=2, must_complete=True,
        )
        db.add(task)
        await db.commit()
        await db.refresh(inst)
        await db.refresh(task)
        inst_id = inst.id
        task_obj = task

    signal_path = tmp_path / ".claude-manager" / f"loop_signal_{task_obj.id}.json"

    async def launch_side(*args, **kwargs):
        signal_path.parent.mkdir(parents=True, exist_ok=True)
        _write_signal(signal_path, "continue", "more", "1/5", "did one", plan="plan")
        return 12345

    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.wait = AsyncMock(return_value=0)
    d.instance_manager.launch = AsyncMock(side_effect=launch_side)
    d.instance_manager.processes = {inst_id: mock_proc}

    await d._run_loop_lifecycle(inst_id, task_obj, str(tmp_path))

    async with db_factory() as db:
        t = await db.get(Task, task_obj.id)
        assert t.status == "failed"
        assert "未能在 2 轮内完成所有任务项" in t.error_message
        assert "1/5" in t.error_message


@pytest.mark.asyncio
async def test_must_complete_plan_captured_and_updated(db_factory, tmp_path):
    """Plan is captured from signal and latest version is passed to subsequent prompts."""
    d = _make_dispatcher(db_factory)

    async with db_factory() as db:
        inst = Instance(name="mc-plan-worker")
        db.add(inst)
        task = Task(
            title="mc-plan", mode="loop", todo_file_path="TODO.md",
            target_repo=str(tmp_path), max_iterations=10, must_complete=True,
        )
        db.add(task)
        await db.commit()
        await db.refresh(inst)
        await db.refresh(task)
        inst_id = inst.id
        task_obj = task

    signal_path = tmp_path / ".claude-manager" / f"loop_signal_{task_obj.id}.json"
    prompts_sent: list[str] = []
    call_count = {"n": 0}

    async def launch_side(*args, **kwargs):
        prompt = kwargs.get("prompt", args[1] if len(args) > 1 else "")
        prompts_sent.append(prompt)
        signal_path.parent.mkdir(parents=True, exist_ok=True)
        call_count["n"] += 1
        if call_count["n"] == 1:
            _write_signal(signal_path, "continue", "more", "2/6", "did 2", plan="第2轮: A+B; 第3轮: C+D")
        elif call_count["n"] == 2:
            # Update the plan
            _write_signal(signal_path, "continue", "more", "4/6", "did 2", plan="第3轮: C+D+E（合并了）")
        else:
            _write_signal(signal_path, "done", "done", "6/6", "finished")
        return 12345

    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.wait = AsyncMock(return_value=0)
    d.instance_manager.launch = AsyncMock(side_effect=launch_side)
    d.instance_manager.processes = {inst_id: mock_proc}

    await d._run_loop_lifecycle(inst_id, task_obj, str(tmp_path))

    assert call_count["n"] == 3

    # First prompt: no plan section (first iteration)
    assert "整体计划" not in prompts_sent[0]

    # Second prompt: has original plan
    assert "第2轮: A+B; 第3轮: C+D" in prompts_sent[1]

    # Third prompt: has UPDATED plan (not the original)
    assert "第3轮: C+D+E（合并了）" in prompts_sent[2]
    assert "第2轮: A+B" not in prompts_sent[2]


@pytest.mark.asyncio
async def test_must_complete_plan_persists_when_not_updated(db_factory, tmp_path):
    """Plan from earlier iteration persists if later signals don't include plan."""
    d = _make_dispatcher(db_factory)

    async with db_factory() as db:
        inst = Instance(name="mc-plan-persist-worker")
        db.add(inst)
        task = Task(
            title="mc-plan-persist", mode="loop", todo_file_path="TODO.md",
            target_repo=str(tmp_path), max_iterations=10, must_complete=True,
        )
        db.add(task)
        await db.commit()
        await db.refresh(inst)
        await db.refresh(task)
        inst_id = inst.id
        task_obj = task

    signal_path = tmp_path / ".claude-manager" / f"loop_signal_{task_obj.id}.json"
    prompts_sent: list[str] = []
    call_count = {"n": 0}

    async def launch_side(*args, **kwargs):
        prompt = kwargs.get("prompt", args[1] if len(args) > 1 else "")
        prompts_sent.append(prompt)
        signal_path.parent.mkdir(parents=True, exist_ok=True)
        call_count["n"] += 1
        if call_count["n"] == 1:
            _write_signal(signal_path, "continue", "more", "2/6", "did 2", plan="原始计划")
        elif call_count["n"] == 2:
            # No plan in this signal
            _write_signal(signal_path, "continue", "more", "4/6", "did 2")
        else:
            _write_signal(signal_path, "done", "done", "6/6", "finished")
        return 12345

    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.wait = AsyncMock(return_value=0)
    d.instance_manager.launch = AsyncMock(side_effect=launch_side)
    d.instance_manager.processes = {inst_id: mock_proc}

    await d._run_loop_lifecycle(inst_id, task_obj, str(tmp_path))

    # Third prompt: still has the original plan since second signal didn't update it
    assert "原始计划" in prompts_sent[2]


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


# === Effort level tests ===


@pytest.mark.asyncio
async def test_lifecycle_passes_effort_level_from_task(db_factory):
    """Task-level effort_level is passed to instance_manager.launch()."""
    d = _make_dispatcher(db_factory)

    async with db_factory() as db:
        inst = Instance(name="worker-1", effort_level="low")
        db.add(inst)
        task = Task(title="effort-task", description="d", target_repo="/repo", effort_level="max")
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

    call_kwargs = d.instance_manager.launch.call_args.kwargs
    # Task effort should take precedence over instance effort
    assert call_kwargs["effort_level"] == "max"


@pytest.mark.asyncio
async def test_lifecycle_falls_back_to_instance_effort(db_factory):
    """When task has no effort_level, instance effort_level is used."""
    d = _make_dispatcher(db_factory)

    async with db_factory() as db:
        inst = Instance(name="worker-effort", effort_level="xhigh")
        db.add(inst)
        task = Task(title="no-effort-task", description="d", target_repo="/repo")
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

    call_kwargs = d.instance_manager.launch.call_args.kwargs
    assert call_kwargs["effort_level"] == "xhigh"


@pytest.mark.asyncio
async def test_lifecycle_falls_back_to_default_effort(db_factory):
    """When neither task nor instance has effort_level, settings.default_effort is used."""
    d = _make_dispatcher(db_factory)

    async with db_factory() as db:
        inst = Instance(name="worker-default-effort")
        db.add(inst)
        task = Task(title="default-effort-task", description="d", target_repo="/repo")
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

    with patch("backend.services.dispatcher.settings") as mock_settings:
        mock_settings.default_effort = "medium"
        mock_settings.task_timeout_seconds = 1800
        await d._run_task_lifecycle(inst_id, task_obj)

    call_kwargs = d.instance_manager.launch.call_args.kwargs
    assert call_kwargs["effort_level"] == "medium"


@pytest.mark.asyncio
async def test_get_effort_level_from_instance(db_factory):
    """_get_effort_level returns instance's effort_level when set."""
    d = _make_dispatcher(db_factory)

    async with db_factory() as db:
        inst = Instance(name="effort-inst", effort_level="high")
        db.add(inst)
        await db.commit()
        await db.refresh(inst)
        inst_id = inst.id

    result = await d._get_effort_level(inst_id)
    assert result == "high"


@pytest.mark.asyncio
async def test_get_effort_level_falls_back_to_default(db_factory):
    """_get_effort_level returns default_effort when instance has no effort_level."""
    d = _make_dispatcher(db_factory)

    async with db_factory() as db:
        inst = Instance(name="no-effort-inst")
        db.add(inst)
        await db.commit()
        await db.refresh(inst)
        inst_id = inst.id

    result = await d._get_effort_level(inst_id)
    from backend.config import settings
    assert result == settings.default_effort


# === Goal mode lifecycle tests ===


def _make_goal_task(db, target_repo: str = "/repo", goal_max_turns: int = 5) -> Task:
    """Create a goal mode task."""
    return Task(
        title="goal-test",
        description="implement feature X",
        mode="goal",
        goal_condition="all tests pass and lint is clean",
        goal_max_turns=goal_max_turns,
        target_repo=target_repo,
    )


@pytest.mark.asyncio
async def test_goal_achieved_after_one_turn(db_factory):
    """Goal task marked completed when evaluator says achieved on first turn."""
    d = _make_dispatcher(db_factory)

    async with db_factory() as db:
        inst = Instance(name="goal-worker-1")
        db.add(inst)
        task = _make_goal_task(db)
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

    from backend.services.goal_evaluator import GoalEvalResult
    with patch("backend.services.goal_evaluator.GoalEvaluator.evaluate",
               return_value=GoalEvalResult(achieved=True, reason="all tests pass")):
        await d._run_goal_lifecycle(inst_id, task_obj, "/repo")

    async with db_factory() as db:
        t = await db.get(Task, task_obj.id)
        assert t.status == "completed"
        assert t.goal_turns_used == 1
        assert t.goal_last_reason == "all tests pass"


@pytest.mark.asyncio
async def test_goal_achieved_after_multiple_turns(db_factory):
    """Goal task continues until evaluator says achieved."""
    d = _make_dispatcher(db_factory)

    async with db_factory() as db:
        inst = Instance(name="goal-worker-2")
        db.add(inst)
        task = _make_goal_task(db, goal_max_turns=10)
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

    call_count = {"n": 0}
    from backend.services.goal_evaluator import GoalEvalResult

    async def eval_side_effect(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] < 3:
            return GoalEvalResult(achieved=False, reason=f"still {3 - call_count['n']} issues")
        return GoalEvalResult(achieved=True, reason="all clear")

    with patch("backend.services.goal_evaluator.GoalEvaluator.evaluate",
               side_effect=eval_side_effect):
        await d._run_goal_lifecycle(inst_id, task_obj, "/repo")

    assert call_count["n"] == 3
    async with db_factory() as db:
        t = await db.get(Task, task_obj.id)
        assert t.status == "completed"
        assert t.goal_turns_used == 3


@pytest.mark.asyncio
async def test_goal_max_turns_exceeded(db_factory):
    """Goal task fails when max turns exhausted."""
    d = _make_dispatcher(db_factory)

    async with db_factory() as db:
        inst = Instance(name="goal-worker-3")
        db.add(inst)
        task = _make_goal_task(db, goal_max_turns=2)
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

    from backend.services.goal_evaluator import GoalEvalResult
    with patch("backend.services.goal_evaluator.GoalEvaluator.evaluate",
               return_value=GoalEvalResult(achieved=False, reason="tests still failing")):
        await d._run_goal_lifecycle(inst_id, task_obj, "/repo")

    async with db_factory() as db:
        t = await db.get(Task, task_obj.id)
        assert t.status == "failed"
        assert "2" in t.error_message
        assert t.goal_turns_used == 2


@pytest.mark.asyncio
async def test_goal_cancelled_between_turns(db_factory):
    """Goal task stops cleanly when cancelled externally between turns."""
    d = _make_dispatcher(db_factory)

    async with db_factory() as db:
        inst = Instance(name="goal-worker-4")
        db.add(inst)
        task = _make_goal_task(db, goal_max_turns=10)
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

    eval_count = {"n": 0}
    from backend.services.goal_evaluator import GoalEvalResult

    async def eval_and_cancel(*args, **kwargs):
        eval_count["n"] += 1
        # Cancel task after first evaluation
        async with db_factory() as db:
            from sqlalchemy import update as sa_update
            await db.execute(
                sa_update(Task).where(Task.id == task_obj.id).values(status="cancelled")
            )
            await db.commit()
        return GoalEvalResult(achieved=False, reason="not done")

    with patch("backend.services.goal_evaluator.GoalEvaluator.evaluate",
               side_effect=eval_and_cancel):
        await d._run_goal_lifecycle(inst_id, task_obj, "/repo")

    assert eval_count["n"] == 1
    async with db_factory() as db:
        t = await db.get(Task, task_obj.id)
        assert t.status == "cancelled"


@pytest.mark.asyncio
async def test_goal_cancelled_during_execution(db_factory):
    """Goal task stops when cancelled while Claude is running."""
    d = _make_dispatcher(db_factory)

    async with db_factory() as db:
        inst = Instance(name="goal-worker-5")
        db.add(inst)
        task = _make_goal_task(db, goal_max_turns=10)
        db.add(task)
        await db.commit()
        await db.refresh(inst)
        await db.refresh(task)
        inst_id = inst.id
        task_obj = task

    async def launch_and_cancel(*args, **kwargs):
        async with db_factory() as db:
            from sqlalchemy import update as sa_update
            await db.execute(
                sa_update(Task).where(Task.id == task_obj.id).values(status="cancelled")
            )
            await db.commit()
        return 12345

    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.wait = AsyncMock(return_value=0)
    d.instance_manager.launch = AsyncMock(side_effect=launch_and_cancel)
    d.instance_manager.processes = {inst_id: mock_proc}

    await d._run_goal_lifecycle(inst_id, task_obj, "/repo")

    async with db_factory() as db:
        t = await db.get(Task, task_obj.id)
        assert t.status == "cancelled"


@pytest.mark.asyncio
async def test_goal_uses_resume_on_subsequent_turns(db_factory):
    """Goal task uses --resume for turns after the first."""
    d = _make_dispatcher(db_factory)

    async with db_factory() as db:
        inst = Instance(name="goal-worker-6")
        db.add(inst)
        task = _make_goal_task(db, goal_max_turns=5)
        db.add(task)
        await db.commit()
        await db.refresh(inst)
        await db.refresh(task)
        inst_id = inst.id
        task_obj = task

    launch_calls: list[dict] = []

    async def capture_launch(*args, **kwargs):
        launch_calls.append(kwargs)
        # Set session_id on first call
        if len(launch_calls) == 1:
            async with db_factory() as db:
                from sqlalchemy import update as sa_update
                await db.execute(
                    sa_update(Task).where(Task.id == task_obj.id).values(session_id="sess-goal-123")
                )
                await db.commit()
        return 12345

    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.wait = AsyncMock(return_value=0)
    d.instance_manager.launch = AsyncMock(side_effect=capture_launch)
    d.instance_manager.processes = {inst_id: mock_proc}

    eval_count = {"n": 0}
    from backend.services.goal_evaluator import GoalEvalResult

    async def eval_twice(*args, **kwargs):
        eval_count["n"] += 1
        if eval_count["n"] < 2:
            return GoalEvalResult(achieved=False, reason="not yet")
        return GoalEvalResult(achieved=True, reason="done")

    with patch("backend.services.goal_evaluator.GoalEvaluator.evaluate",
               side_effect=eval_twice):
        await d._run_goal_lifecycle(inst_id, task_obj, "/repo")

    assert len(launch_calls) == 2
    # First call: no resume_session_id
    assert launch_calls[0].get("resume_session_id") is None
    # Second call: should use resume
    assert launch_calls[1].get("resume_session_id") == "sess-goal-123"


@pytest.mark.asyncio
async def test_goal_broadcasts_evaluation_events(db_factory):
    """Goal lifecycle broadcasts goal_evaluation events."""
    d = _make_dispatcher(db_factory)

    async with db_factory() as db:
        inst = Instance(name="goal-worker-7")
        db.add(inst)
        task = _make_goal_task(db, goal_max_turns=5)
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

    from backend.services.goal_evaluator import GoalEvalResult
    with patch("backend.services.goal_evaluator.GoalEvaluator.evaluate",
               return_value=GoalEvalResult(achieved=True, reason="done")):
        await d._run_goal_lifecycle(inst_id, task_obj, "/repo")

    # Check broadcasts
    broadcast_calls = d.broadcaster.broadcast.call_args_list
    goal_eval_events = [
        c for c in broadcast_calls
        if isinstance(c[0][1], dict) and c[0][1].get("event_type") == "goal_evaluation"
    ]
    assert len(goal_eval_events) >= 1
    assert goal_eval_events[0][0][1]["achieved"] is True
    assert goal_eval_events[0][0][1]["turn"] == 1


@pytest.mark.asyncio
async def test_goal_total_completed_incremented_once(db_factory):
    """Completing a goal task increments total_tasks_completed exactly once."""
    d = _make_dispatcher(db_factory)

    async with db_factory() as db:
        inst = Instance(name="goal-count-worker", total_tasks_completed=0)
        db.add(inst)
        task = _make_goal_task(db, goal_max_turns=5)
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

    eval_count = {"n": 0}
    from backend.services.goal_evaluator import GoalEvalResult

    async def eval_multi(*args, **kwargs):
        eval_count["n"] += 1
        if eval_count["n"] < 3:
            return GoalEvalResult(achieved=False, reason="not yet")
        return GoalEvalResult(achieved=True, reason="done")

    with patch("backend.services.goal_evaluator.GoalEvaluator.evaluate",
               side_effect=eval_multi):
        await d._run_goal_lifecycle(inst_id, task_obj, "/repo")

    async with db_factory() as db:
        i = await db.get(Instance, inst_id)
        assert i.total_tasks_completed == 1


@pytest.mark.asyncio
async def test_goal_total_completed_not_incremented_on_failure(db_factory):
    """Failed goal task does NOT increment total_tasks_completed."""
    d = _make_dispatcher(db_factory)

    async with db_factory() as db:
        inst = Instance(name="goal-fail-count-worker", total_tasks_completed=0)
        db.add(inst)
        task = _make_goal_task(db, goal_max_turns=1)
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

    from backend.services.goal_evaluator import GoalEvalResult
    with patch("backend.services.goal_evaluator.GoalEvaluator.evaluate",
               return_value=GoalEvalResult(achieved=False, reason="nope")):
        await d._run_goal_lifecycle(inst_id, task_obj, "/repo")

    async with db_factory() as db:
        i = await db.get(Instance, inst_id)
        assert i.total_tasks_completed == 0


@pytest.mark.asyncio
async def test_lifecycle_routes_to_goal(db_factory):
    """_run_task_lifecycle delegates to _run_goal_lifecycle for mode='goal'."""
    d = _make_dispatcher(db_factory)

    goal_called = {"called": False}
    async def fake_goal(*args, **kwargs):
        goal_called["called"] = True
    d._run_goal_lifecycle = fake_goal

    async with db_factory() as db:
        inst = Instance(name="goal-router")
        db.add(inst)
        task = Task(
            title="route-goal",
            description="do it",
            mode="goal",
            goal_condition="tests pass",
            target_repo="/repo",
        )
        db.add(task)
        await db.commit()
        await db.refresh(inst)
        await db.refresh(task)
        inst_id = inst.id
        task_obj = task

    await d._run_task_lifecycle(inst_id, task_obj)
    assert goal_called["called"]


# === Goal prompt construction tests ===


@pytest.mark.asyncio
async def test_goal_initial_prompt_contains_condition(db_factory):
    """_build_goal_initial_prompt includes the goal condition."""
    d = _make_dispatcher(db_factory)
    task = Task(
        title="t", description="implement X", mode="goal",
        goal_condition="all tests pass", goal_max_turns=10,
    )
    prompt = d._build_goal_initial_prompt(task)
    assert "all tests pass" in prompt
    assert "implement X" in prompt
    assert "CLAUDE.md" in prompt
    assert "10" in prompt


@pytest.mark.asyncio
async def test_goal_initial_prompt_with_images(db_factory):
    """_build_goal_initial_prompt includes image paths from metadata."""
    d = _make_dispatcher(db_factory)
    task = Task(
        title="t", description="implement X", mode="goal",
        goal_condition="condition", goal_max_turns=5,
        metadata_={"image_paths": ["/uploads/a.png"]},
    )
    prompt = d._build_goal_initial_prompt(task)
    assert "/uploads/a.png" in prompt
    assert "Read" in prompt


@pytest.mark.asyncio
async def test_goal_followup_prompt_contains_reason(db_factory):
    """_build_goal_followup_prompt includes evaluator reason and remaining turns."""
    d = _make_dispatcher(db_factory)
    prompt = d._build_goal_followup_prompt("3 tests still failing", turn=2, max_turns=10)
    assert "3 tests still failing" in prompt
    assert "8" in prompt  # remaining turns


@pytest.mark.asyncio
async def test_goal_conversation_collection(db_factory):
    """_collect_goal_conversation returns formatted log entries."""
    d = _make_dispatcher(db_factory)

    from backend.models.log_entry import LogEntry

    async with db_factory() as db:
        task = Task(
            title="conv-test", description="d", mode="goal",
            goal_condition="c", target_repo="/repo",
        )
        db.add(task)
        await db.commit()
        await db.refresh(task)

        db.add(LogEntry(
            instance_id=1, task_id=task.id, event_type="message",
            role="assistant", content="Implemented feature A",
            loop_iteration=0,
        ))
        db.add(LogEntry(
            instance_id=1, task_id=task.id, event_type="message",
            role="assistant", content="Fixed test failures",
            loop_iteration=1,
        ))
        await db.commit()

        task_id = task.id

    summary = await d._collect_goal_conversation(task_id, current_turn=1)
    assert "Implemented feature A" in summary
    assert "Fixed test failures" in summary
    assert "[Turn 1]" in summary
    assert "[Turn 2]" in summary


@pytest.mark.asyncio
async def test_goal_conversation_empty_log(db_factory):
    """_collect_goal_conversation handles empty log gracefully."""
    d = _make_dispatcher(db_factory)

    async with db_factory() as db:
        task = Task(
            title="empty-conv", description="d", mode="goal",
            goal_condition="c", target_repo="/repo",
        )
        db.add(task)
        await db.commit()
        await db.refresh(task)
        task_id = task.id

    summary = await d._collect_goal_conversation(task_id, current_turn=0)
    assert "No conversation" in summary


# === Deleted task handling tests ===


@pytest.mark.asyncio
async def test_loop_deleted_between_iterations(db_factory, tmp_path):
    """Loop stops cleanly when task is deleted between iterations."""
    d = _make_dispatcher(db_factory)

    async with db_factory() as db:
        inst = Instance(name="loop-del-worker-1")
        db.add(inst)
        task = _make_loop_task(db, tmp_path, max_iterations=10)
        db.add(task)
        await db.commit()
        await db.refresh(inst)
        await db.refresh(task)
        inst_id = inst.id
        task_obj = task

    signal_path = tmp_path / ".claude-manager" / f"loop_signal_{task_obj.id}.json"

    async def launch_continue_then_delete(*args, **kwargs):
        signal_path.parent.mkdir(parents=True, exist_ok=True)
        _write_signal(signal_path, "continue", "keep going")
        # Delete the task from DB after first iteration
        async with db_factory() as db:
            t = await db.get(Task, task_obj.id)
            if t:
                await db.delete(t)
                await db.commit()
        return 12345

    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.wait = AsyncMock(return_value=0)
    d.instance_manager.launch = AsyncMock(side_effect=launch_continue_then_delete)
    d.instance_manager.processes = {inst_id: mock_proc}

    await d._run_loop_lifecycle(inst_id, task_obj, str(tmp_path))

    # loop stopped — launch called exactly once
    assert d.instance_manager.launch.await_count == 1
    # Task should be deleted
    async with db_factory() as db:
        t = await db.get(Task, task_obj.id)
        assert t is None


@pytest.mark.asyncio
async def test_goal_deleted_between_turns(db_factory):
    """Goal task stops cleanly when deleted externally between turns."""
    d = _make_dispatcher(db_factory)

    async with db_factory() as db:
        inst = Instance(name="goal-del-worker-1")
        db.add(inst)
        task = _make_goal_task(db, goal_max_turns=10)
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

    eval_count = {"n": 0}
    from backend.services.goal_evaluator import GoalEvalResult

    async def eval_and_delete(*args, **kwargs):
        eval_count["n"] += 1
        # Delete task after first evaluation
        async with db_factory() as db:
            t = await db.get(Task, task_obj.id)
            if t:
                await db.delete(t)
                await db.commit()
        return GoalEvalResult(achieved=False, reason="not done")

    with patch("backend.services.goal_evaluator.GoalEvaluator.evaluate",
               side_effect=eval_and_delete):
        await d._run_goal_lifecycle(inst_id, task_obj, "/repo")

    assert eval_count["n"] == 1
    async with db_factory() as db:
        t = await db.get(Task, task_obj.id)
        assert t is None


@pytest.mark.asyncio
async def test_goal_deleted_during_execution(db_factory):
    """Goal task stops when deleted while Claude is running."""
    d = _make_dispatcher(db_factory)

    async with db_factory() as db:
        inst = Instance(name="goal-del-worker-2")
        db.add(inst)
        task = _make_goal_task(db, goal_max_turns=10)
        db.add(task)
        await db.commit()
        await db.refresh(inst)
        await db.refresh(task)
        inst_id = inst.id
        task_obj = task

    async def launch_and_delete(*args, **kwargs):
        async with db_factory() as db:
            t = await db.get(Task, task_obj.id)
            if t:
                await db.delete(t)
                await db.commit()
        return 12345

    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.wait = AsyncMock(return_value=0)
    d.instance_manager.launch = AsyncMock(side_effect=launch_and_delete)
    d.instance_manager.processes = {inst_id: mock_proc}

    await d._run_goal_lifecycle(inst_id, task_obj, "/repo")

    assert d.instance_manager.launch.await_count == 1
    async with db_factory() as db:
        t = await db.get(Task, task_obj.id)
        assert t is None
