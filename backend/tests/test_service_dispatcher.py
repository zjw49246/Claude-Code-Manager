"""Tests for GlobalDispatcher — task dispatch and lifecycle management."""
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from sqlalchemy import select

from backend.services.dispatcher import GlobalDispatcher
from backend.models.instance import Instance
from backend.models.task import Task


def _make_dispatcher(db_factory):
    """Create a GlobalDispatcher with mocked dependencies."""
    instance_manager = MagicMock()
    instance_manager.launch = AsyncMock(return_value=12345)
    instance_manager.processes = {}
    instance_manager._tasks = {}
    instance_manager.wait_for_output_consumer = AsyncMock()
    # Model the real InstanceManager interface used by failure classification.
    instance_manager.pty_mode_enabled = False
    instance_manager.transient_error_seen = MagicMock(return_value=False)
    instance_manager.get_last_stderr = MagicMock(return_value="")
    instance_manager.get_recent_log_contents = AsyncMock(return_value=[])
    # PTY proactive pool switch path (dispatcher._process_task_lifecycle)
    instance_manager.pty_rate_limit_seen = MagicMock(return_value=False)
    instance_manager._try_proactive_pool_switch = AsyncMock()
    instance_manager._pty_rate_limit_seen = set()

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
async def test_pause_dispatching_does_not_stop_dispatcher(db_factory):
    d = _make_dispatcher(db_factory)
    d._running = True

    await d.pause_dispatching()

    assert d.status()["running"] is True
    assert d.status()["paused"] is True

    d.resume_dispatching()
    assert d.status()["paused"] is False


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

    with patch("backend.services.dispatcher.settings") as mock_settings, \
         patch("backend.services.dispatcher._provider_available", return_value=True):
        mock_settings.max_concurrent_instances = 3
        mock_settings.default_provider = "claude"
        mock_settings.default_model = "sonnet"
        await d._ensure_instances()

    async with db_factory() as db:
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
async def test_ensure_instances_ignores_terminal_workers(db_factory):
    """Startup replenishes workers even when terminal rows exceed the cap."""
    d = _make_dispatcher(db_factory)

    async with db_factory() as db:
        for i in range(9):
            status = "error" if i < 8 else "stopped"
            db.add(Instance(name=f"old-worker-{i + 1}", status=status))
        await db.commit()

    with patch("backend.services.dispatcher.settings") as mock_settings:
        mock_settings.max_concurrent_instances = 8
        await d._ensure_instances()

    async with db_factory() as db:
        result = await db.execute(select(Instance))
        instances = list(result.scalars().all())
    assert sum(1 for i in instances if i.status == "idle") == 8
    assert sum(1 for i in instances if i.status in ("idle", "running")) == 8


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
@pytest.mark.parametrize("pty_enabled, expected_switches", [(False, 0), (True, 1)])
async def test_lifecycle_proactive_switch_is_pty_only(
    db_factory, pty_enabled, expected_switches,
):
    """The subprocess consumer owns non-PTY switching; lifecycle owns PTY."""
    d = _make_dispatcher(db_factory)
    d.instance_manager.pty_mode_enabled = pty_enabled
    d.instance_manager.pty_rate_limit_seen.return_value = True
    d.instance_manager.pty_rate_limit_info = MagicMock(return_value={
        "status": "allowed_warning",
        "rateLimitType": "five_hour",
        "utilization": 0.95,
    })
    d.instance_manager.clear_pty_rate_limit = MagicMock()

    async with db_factory() as db:
        inst = Instance(name=f"quota-pty-{pty_enabled}")
        task = Task(title="quota gate", description="done", target_repo="/repo")
        db.add_all([inst, task])
        await db.commit()
        await db.refresh(inst)
        await db.refresh(task)

    process = MagicMock(returncode=0, wait=AsyncMock(return_value=0))
    d.instance_manager.processes = {inst.id: process}

    await d._run_task_lifecycle(inst.id, task)

    assert d.instance_manager._try_proactive_pool_switch.await_count == expected_switches
    assert d.instance_manager.clear_pty_rate_limit.call_count == expected_switches
    if not pty_enabled:
        d.instance_manager.pty_rate_limit_seen.assert_not_called()


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
async def test_fresh_codex_all_accounts_cooling_defers_without_retry_budget(
    db_factory, tmp_path,
):
    """Pool backpressure keeps a fresh task pending instead of hard-failing."""
    import json
    import time

    from backend.services.codex_pool import CodexPool

    config_path = tmp_path / "codex-accounts.json"
    homes = [tmp_path / "codex-a", tmp_path / "codex-b"]
    config_path.write_text(json.dumps({"accounts": [
        {"id": "codex-a", "codex_home": str(homes[0]), "enabled": True},
        {"id": "codex-b", "codex_home": str(homes[1]), "enabled": True},
    ]}))

    d = _make_dispatcher(db_factory)
    d.codex_pool = CodexPool(config_path=config_path, cooldown_seconds=60)
    future = time.time() + 60
    d.codex_pool._cooldowns = {"codex-a": future, "codex-b": future}

    async with db_factory() as db:
        inst = Instance(name="codex-cooldown-worker")
        task = Task(
            title="fresh-codex",
            description="do work",
            target_repo="/repo",
            provider="codex",
            max_retries=0,
        )
        db.add_all([inst, task])
        await db.commit()
        await db.refresh(inst)
        await db.refresh(task)
        inst_id = inst.id
        task_id = task.id
        task_obj = task

    await d._run_task_lifecycle(inst_id, task_obj)

    async with db_factory() as db:
        deferred = await db.get(Task, task_id)
        assert deferred.status == "pending"
        assert deferred.retry_count == 0
        assert deferred.instance_id is None
        assert deferred.started_at is None
        assert deferred.completed_at is None
        assert "no available account" in deferred.error_message
    assert task_id in d._codex_routing_not_before
    assert d._codex_routing_not_before[task_id] > time.monotonic()
    d.instance_manager.launch.assert_not_awaited()


@pytest.mark.asyncio
async def test_fresh_codex_maintenance_busy_defers_without_retry_budget(
    db_factory,
):
    """A login-maintenance race is scheduling backpressure, not task failure."""
    from backend.services.codex_app_server import CodexAppServerBusyError

    d = _make_dispatcher(db_factory)
    d._resolve_resume_config_dir = AsyncMock(return_value="/tmp/codex-a")
    d.instance_manager.launch = AsyncMock(
        side_effect=CodexAppServerBusyError("account is under maintenance")
    )

    async with db_factory() as db:
        inst = Instance(name="codex-maintenance-worker")
        task = Task(
            title="fresh-codex-maintenance",
            description="do work",
            target_repo="/repo",
            provider="codex",
            max_retries=0,
        )
        db.add_all([inst, task])
        await db.commit()
        await db.refresh(inst)
        await db.refresh(task)
        inst_id = inst.id
        task_id = task.id
        task_obj = task

    await d._run_task_lifecycle(inst_id, task_obj)

    async with db_factory() as db:
        deferred = await db.get(Task, task_id)
        assert deferred.status == "pending"
        assert deferred.retry_count == 0
        assert deferred.started_at is None
        assert "under maintenance" in deferred.error_message
    assert task_id in d._codex_routing_not_before


@pytest.mark.asyncio
async def test_permanent_codex_routing_error_still_fails_fresh_task(db_factory):
    """Ambiguous/unsafe ownership is not retried forever as if it were cooldown."""
    from backend.services.dispatcher import CodexAccountRoutingError

    d = _make_dispatcher(db_factory)
    d._resolve_resume_config_dir = AsyncMock(
        side_effect=CodexAccountRoutingError("ambiguous rollout ownership")
    )

    async with db_factory() as db:
        inst = Instance(name="codex-permanent-routing-worker")
        task = Task(
            title="unsafe-codex",
            description="do work",
            target_repo="/repo",
            provider="codex",
        )
        db.add_all([inst, task])
        await db.commit()
        await db.refresh(inst)
        await db.refresh(task)
        inst_id = inst.id
        task_id = task.id
        task_obj = task

    await d._run_task_lifecycle(inst_id, task_obj)

    async with db_factory() as db:
        failed = await db.get(Task, task_id)
        assert failed.status == "failed"
        assert "ambiguous rollout ownership" in failed.error_message
    assert task_id not in d._codex_routing_not_before


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
async def test_loop_iteration_passes_pool_config_dir(db_factory, tmp_path):
    """Regression (#770): loop launches must resolve a pool account via
    _resolve_resume_config_dir and pass it as config_dir.

    Before the fix the loop omitted config_dir entirely, so the child process
    inherited the hardcoded systemd CLAUDE_CONFIG_DIR — the pool was never
    consulted, cooled-down accounts were never avoided, and a PTY resume could
    land on the wrong account ("No conversation found").
    """
    d = _make_dispatcher(db_factory)
    d._resolve_resume_config_dir = AsyncMock(return_value="/pool/acc-7")

    async with db_factory() as db:
        inst = Instance(name="loop-pool-worker")
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

    async def launch_and_write(*args, **kwargs):
        signal_path.parent.mkdir(parents=True, exist_ok=True)
        _write_signal(signal_path, "done", "all done", "1/1")
        return 12345
    d.instance_manager.launch = AsyncMock(side_effect=launch_and_write)
    d.instance_manager.processes = {inst_id: mock_proc}

    await d._run_loop_lifecycle(inst_id, task_obj, str(tmp_path))

    # Pool was consulted, and its choice flowed into the launch.
    d._resolve_resume_config_dir.assert_awaited()
    # iteration 0 has no session to resume yet → resolver called with None
    assert d._resolve_resume_config_dir.await_args.args == (None, "claude")
    assert d.instance_manager.launch.await_args.kwargs["config_dir"] == "/pool/acc-7"


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


# === Effort level tests ===


@pytest.mark.asyncio
async def test_lifecycle_passes_effort_level_from_task(db_factory):
    """Task-level effort_level is passed to instance_manager.launch()."""
    d = _make_dispatcher(db_factory)

    async with db_factory() as db:
        inst = Instance(name="worker-1")
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
    assert call_kwargs["effort_level"] == "max"


@pytest.mark.asyncio
async def test_lifecycle_falls_back_to_default_effort(db_factory):
    """When task has no effort_level, settings.default_effort is used."""
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
async def test_goal_turn_passes_pool_config_dir(db_factory):
    """Regression (#770 follow-up): goal launches must resolve a pool account
    via _resolve_resume_config_dir and pass it as config_dir.

    Same defect as loop mode — turn 0 (and followups) passed no config_dir, so
    the child inherited the hardcoded systemd CLAUDE_CONFIG_DIR and the pool was
    never consulted.
    """
    d = _make_dispatcher(db_factory)
    d._resolve_resume_config_dir = AsyncMock(return_value="/pool/acc-9")

    async with db_factory() as db:
        inst = Instance(name="goal-pool-worker")
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
               return_value=GoalEvalResult(achieved=True, reason="done")):
        await d._run_goal_lifecycle(inst_id, task_obj, "/repo")

    d._resolve_resume_config_dir.assert_awaited()
    # turn 0 launches a fresh session → resolver called with None
    assert d._resolve_resume_config_dir.await_args.args == (None, "claude")
    assert d.instance_manager.launch.await_args.kwargs["config_dir"] == "/pool/acc-9"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure_text",
    [
        "You have hit your usage limit. Try again later.",
        "The refresh token was revoked. Please log in again.",
    ],
    ids=["usage-limit", "auth-failure"],
)
async def test_codex_goal_evaluator_rotates_without_consuming_extra_turn(
    db_factory, failure_text,
):
    """Evaluator account failure retries evaluation, not the completed agent turn."""
    d = _make_dispatcher(db_factory)
    d._resolve_resume_config_dir = AsyncMock(return_value="/codex/account-a")
    d._check_rate_limit_and_rotate = AsyncMock(return_value={
        "config_dir": "/codex/account-b",
        "session_id": "codex-thread-1",
        "excluded": {"codex-a"},
    })

    async with db_factory() as db:
        inst = Instance(name="goal-codex-evaluator-worker")
        db.add(inst)
        task = _make_goal_task(db)
        task.provider = "codex"
        task.model = "gpt-5.6-sol"
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

    from backend.services.goal_evaluator import GoalEvaluationError, GoalEvalResult
    evaluator_error = GoalEvaluationError(
        "Goal evaluation exited with code 1",
        provider="codex",
        returncode=1,
        stderr=failure_text,
    )
    with patch(
        "backend.services.goal_evaluator.GoalEvaluator.evaluate",
        new_callable=AsyncMock,
    ) as evaluate:
        evaluate.side_effect = [
            evaluator_error,
            GoalEvalResult(achieved=True, reason="done"),
        ]
        await d._run_goal_lifecycle(inst_id, task_obj, "/repo")

    assert d.instance_manager.launch.await_count == 1
    assert evaluate.await_count == 2
    assert evaluate.await_args_list[0].kwargs["codex_home"] == "/codex/account-a"
    assert evaluate.await_args_list[1].kwargs["codex_home"] == "/codex/account-b"
    rotation_kwargs = d._check_rate_limit_and_rotate.await_args.kwargs
    assert failure_text in rotation_kwargs["combined"]
    async with db_factory() as db:
        persisted = await db.get(Task, task_obj.id)
        assert persisted.status == "completed"
        assert persisted.goal_turns_used == 1


@pytest.mark.asyncio
async def test_goal_lifecycle_retry_resumes_persisted_turn_and_session(db_factory):
    """A requeued lifecycle continues its durable turn/session progress."""
    d = _make_dispatcher(db_factory)
    d._resolve_resume_config_dir = AsyncMock(return_value="/pool/resident")

    async with db_factory() as db:
        inst = Instance(name="goal-resume-worker")
        db.add(inst)
        task = _make_goal_task(db, goal_max_turns=5)
        task.goal_turns_used = 2
        task.goal_last_reason = "two checks remain"
        task.session_id = "sess-goal-persisted"
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
    with patch(
        "backend.services.goal_evaluator.GoalEvaluator.evaluate",
        return_value=GoalEvalResult(achieved=True, reason="now complete"),
    ):
        await d._run_goal_lifecycle(inst_id, task_obj, "/repo")

    launch_kwargs = d.instance_manager.launch.await_args.kwargs
    assert launch_kwargs["resume_session_id"] == "sess-goal-persisted"
    assert launch_kwargs["loop_iteration"] == 2
    assert "two checks remain" in launch_kwargs["prompt"]
    d._resolve_resume_config_dir.assert_awaited_once_with(
        "sess-goal-persisted", "claude", task_id=task_obj.id,
    )
    async with db_factory() as db:
        persisted = await db.get(Task, task_obj.id)
        assert persisted.status == "completed"
        assert persisted.goal_turns_used == 3
        assert persisted.goal_last_reason == "now complete"


@pytest.mark.asyncio
async def test_goal_evaluation_error_requeues_without_advancing_turn(db_factory):
    """Operational evaluator errors use lifecycle retry budget, not goal turns."""
    d = _make_dispatcher(db_factory)

    async with db_factory() as db:
        inst = Instance(name="goal-evaluator-error-worker")
        db.add(inst)
        task = _make_goal_task(db, goal_max_turns=5)
        task.goal_turns_used = 1
        task.goal_last_reason = "work remains"
        task.session_id = "sess-goal-existing"
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

    from backend.services.goal_evaluator import GoalEvaluationError
    with patch(
        "backend.services.goal_evaluator.GoalEvaluator.evaluate",
        new_callable=AsyncMock,
        side_effect=GoalEvaluationError(
            "Goal evaluation process failed",
            provider="claude",
            stderr="temporary evaluator failure",
        ),
    ):
        await d._run_goal_lifecycle(inst_id, task_obj, "/repo")

    async with db_factory() as db:
        persisted = await db.get(Task, task_obj.id)
        assert persisted.status == "pending"
        assert persisted.retry_count == 1
        assert persisted.goal_turns_used == 1
        assert persisted.session_id == "sess-goal-existing"


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


# ---------- PTY 模式 loop：同一热会话，一个迭代一个 turn ----------

async def _run_two_iteration_loop(d, db_factory, tmp_path, *, pty_enabled):
    """Helper: run a loop that signals continue then done; capture launches."""
    async with db_factory() as db:
        inst = Instance(name="loop-pty-worker")
        db.add(inst)
        task = _make_loop_task(db, tmp_path)
        db.add(task)
        await db.commit()
        await db.refresh(inst)
        await db.refresh(task)
        inst_id, task_obj = inst.id, task

    signal_path = tmp_path / ".claude-manager" / f"loop_signal_{task_obj.id}.json"
    launches = []

    async def fake_launch(*args, **kwargs):
        launches.append(kwargs)
        # 第一轮写 continue，第二轮写 done；并模拟 session_id 入库
        signal_path.parent.mkdir(parents=True, exist_ok=True)
        action = "continue" if len(launches) == 1 else "done"
        _write_signal(signal_path, action, "step", f"{len(launches)}/2")
        async with db_factory() as db:
            t = await db.get(Task, task_obj.id)
            t.session_id = "loop-sid-1"
            await db.commit()
        return 12345

    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.wait = AsyncMock(return_value=0)
    d.instance_manager.launch = AsyncMock(side_effect=fake_launch)
    d.instance_manager.processes = {inst_id: mock_proc}
    d.instance_manager.pty_mode_enabled = pty_enabled
    d.instance_manager.release_pty_session = AsyncMock()

    await d._run_loop_lifecycle(inst_id, task_obj, str(tmp_path))
    return launches, d


@pytest.mark.asyncio
async def test_loop_pty_mode_reuses_session_per_iteration(db_factory, tmp_path):
    d = _make_dispatcher(db_factory)
    launches, d = await _run_two_iteration_loop(d, db_factory, tmp_path, pty_enabled=True)

    assert len(launches) == 2
    # 迭代 0 全新会话；迭代 1 复用同一会话（一个迭代一个 turn）
    assert launches[0].get("resume_session_id") is None
    assert launches[1].get("resume_session_id") == "loop-sid-1"
    # loop 结束后会话归还，不污染池
    d.instance_manager.release_pty_session.assert_awaited_once_with("loop-sid-1")


@pytest.mark.asyncio
async def test_loop_p_mode_stays_stateless(db_factory, tmp_path):
    d = _make_dispatcher(db_factory)
    launches, d = await _run_two_iteration_loop(d, db_factory, tmp_path, pty_enabled=False)

    assert len(launches) == 2
    # -p 模式语义不变：每轮无 resume
    assert launches[0].get("resume_session_id") is None
    assert launches[1].get("resume_session_id") is None


# ---------------------------------------------------------------------------
# clear_task_queue — interrupt drops pending chat messages
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_clear_task_queue_drops_pending_messages(db_factory):
    """clear_task_queue drains all pending messages and returns the count."""
    import time
    from backend.services.dispatcher import QueuedMessage, PRIORITY_USER

    dispatcher = _make_dispatcher(db_factory)
    q = dispatcher._get_task_queue(1)
    for i in range(3):
        q.put_nowait(QueuedMessage(
            priority=PRIORITY_USER, timestamp=time.monotonic(),
            prompt=f"msg {i}", source="user",
        ))

    cleared = await dispatcher.clear_task_queue(1)

    assert cleared == 3
    assert q.empty()


@pytest.mark.asyncio
async def test_clear_task_queue_no_queue_returns_zero(db_factory):
    """clear_task_queue on a task with no queue is a no-op returning 0."""
    dispatcher = _make_dispatcher(db_factory)
    assert await dispatcher.clear_task_queue(999) == 0


class TestResolveTimeout:
    """任务级超时解析：NULL=全局默认，0=不限时，>0=小时数。"""

    def _dispatcher(self):
        from backend.services.dispatcher import GlobalDispatcher
        return GlobalDispatcher.__new__(GlobalDispatcher)

    def test_null_uses_global_default(self):
        from backend.config import settings
        from unittest.mock import MagicMock
        t = MagicMock(timeout_hours=None)
        assert self._dispatcher()._resolve_timeout(t) == settings.task_timeout_seconds

    def test_zero_means_no_limit(self):
        from unittest.mock import MagicMock
        t = MagicMock(timeout_hours=0)
        assert self._dispatcher()._resolve_timeout(t) is None

    def test_hours_converted_to_seconds(self):
        from unittest.mock import MagicMock
        t = MagicMock(timeout_hours=2.5)
        assert self._dispatcher()._resolve_timeout(t) == 9000

    async def test_wait_process_kills_on_timeout(self):
        import asyncio
        from unittest.mock import MagicMock
        d = self._dispatcher()
        t = MagicMock(timeout_hours=0.0001, id=1)  # 0.36s

        class FakeProc:
            killed = False
            async def wait(self):
                if self.killed:
                    return -9
                await asyncio.sleep(5)
            def kill(self):
                self.killed = True
        p = FakeProc()
        await d._wait_process(p, t, "test")
        assert p.killed is True


@pytest.mark.asyncio
async def test_create_task_fills_default_model_and_effort(client):
    """创建任务不指定 model/effort → 自动填入全局默认值。"""
    from backend.config import settings
    resp = await client.post("/api/tasks", json={
        "title": "T", "description": "d", "target_repo": "/tmp",
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["provider"] == settings.default_provider == "codex"
    assert data["model"] == settings.default_codex_model == "gpt-5.6-sol"
    assert data["effort_level"] == settings.default_effort


# === Per-task queue consumer: no concurrent `--resume` on one session (task #728) ===


@pytest.mark.asyncio
async def test_mode_turn_retries_same_native_session_after_codex_rotation(db_factory):
    """Plan/loop/goal bypass normal Step 5, so their turn helper must rotate."""
    d = _make_dispatcher(db_factory)
    manager = MagicMock()
    manager.processes = {}
    manager._tasks = {}
    codes = iter([1, 0])

    async def fake_launch(**kwargs):
        manager.processes[kwargs["instance_id"]] = MagicMock(
            returncode=next(codes)
        )

    manager.launch = AsyncMock(side_effect=fake_launch)
    manager.wait_for_output_consumer = AsyncMock()
    manager.get_config_dir = MagicMock(return_value="/tmp/codex-b")
    d.instance_manager = manager
    d._wait_process = AsyncMock()
    d._collect_failure_output = AsyncMock(
        return_value="You've hit your usage limit for codex"
    )
    d._check_rate_limit_and_rotate = AsyncMock(return_value={
        "config_dir": "/tmp/codex-b",
        "session_id": "thread-1",
        "excluded": {"codex-a"},
    })
    task = MagicMock(
        id=42,
        model="gpt-5.6-sol",
        provider="codex",
        thinking_budget=None,
        effort_level="high",
        enable_workflows=False,
        enabled_skills=None,
    )

    exit_code, home = await d._launch_mode_turn_with_rotation(
        7,
        task,
        "/repo",
        {},
        prompt="mode prompt",
        config_dir="/tmp/codex-a",
        resume_session_id=None,
        loop_iteration=0,
        effort_level="high",
        label="Goal turn",
    )

    assert exit_code == 0
    assert home == "/tmp/codex-b"
    assert manager.launch.await_count == 2
    assert manager.launch.await_args_list[1].kwargs["config_dir"] == "/tmp/codex-b"
    assert manager.launch.await_args_list[1].kwargs["resume_session_id"] == "thread-1"


@pytest.mark.asyncio
async def test_successful_mode_turn_returns_home_after_proactive_switch(db_factory):
    """Goal evaluation must follow a quota switch completed by the consumer."""
    d = _make_dispatcher(db_factory)
    manager = MagicMock()
    manager.processes = {7: MagicMock(returncode=0)}
    manager._tasks = {}
    manager.launch = AsyncMock()
    manager.wait_for_output_consumer = AsyncMock()
    manager.get_config_dir = MagicMock(return_value="/tmp/codex-after-switch")
    d.instance_manager = manager
    d._wait_process = AsyncMock()
    task = MagicMock(
        id=43,
        model="gpt-5.6-sol",
        provider="codex",
        thinking_budget=None,
        effort_level="high",
        enable_workflows=False,
        enabled_skills=None,
    )

    exit_code, home = await d._launch_mode_turn_with_rotation(
        7,
        task,
        "/repo",
        {},
        prompt="goal prompt",
        config_dir="/tmp/codex-before-switch",
        resume_session_id="thread-1",
        loop_iteration=0,
        effort_level="high",
        label="Goal turn",
    )

    assert exit_code == 0
    assert home == "/tmp/codex-after-switch"


@pytest.mark.asyncio
async def test_codex_mode_turn_does_not_timeout_output_consumer(
    db_factory, monkeypatch,
):
    """Final CODEX_HOME is unavailable until quota/rebind cleanup finishes."""
    import backend.services.dispatcher as dispatcher_module

    d = _make_dispatcher(db_factory)
    manager = MagicMock()
    manager.processes = {7: MagicMock(returncode=0)}
    switch_finished = asyncio.Event()

    async def finish_switch():
        await asyncio.sleep(0)
        switch_finished.set()

    consumer = asyncio.create_task(finish_switch())
    manager._tasks = {7: consumer}
    manager.launch = AsyncMock()

    async def wait_for_consumer(*_args, **_kwargs):
        await consumer

    manager.wait_for_output_consumer = AsyncMock(
        side_effect=wait_for_consumer
    )
    manager.get_config_dir = MagicMock(
        side_effect=lambda _instance_id: (
            "/tmp/codex-after-switch"
            if switch_finished.is_set()
            else "/tmp/codex-before-switch"
        )
    )
    d.instance_manager = manager
    d._wait_process = AsyncMock()
    task = MagicMock(
        id=44,
        model="gpt-5.6-sol",
        provider="codex",
        thinking_budget=None,
        effort_level="high",
        enable_workflows=False,
        enabled_skills=None,
    )

    # Claude retains its bounded cleanup wait. Codex must not use that timeout:
    # its consumer owns rollout migration and the final account binding.
    wait_for = AsyncMock(side_effect=AssertionError("Codex consumer was timed out"))
    monkeypatch.setattr(dispatcher_module.asyncio, "wait_for", wait_for)

    exit_code, home = await d._launch_mode_turn_with_rotation(
        7,
        task,
        "/repo",
        {},
        prompt="goal prompt",
        config_dir="/tmp/codex-before-switch",
        resume_session_id="thread-1",
        loop_iteration=0,
        effort_level="high",
        label="Goal turn",
    )

    assert exit_code == 0
    assert home == "/tmp/codex-after-switch"
    assert consumer.done()
    wait_for.assert_not_awaited()


@pytest.mark.asyncio
async def test_codex_routing_failure_requeues_exact_message(db_factory, monkeypatch):
    """A temporary account/home conflict must not acknowledge and lose chat."""
    import backend.services.dispatcher as disp_mod

    monkeypatch.setattr(disp_mod, "CODEX_ROUTING_RETRY_DELAY", 0.01)
    d = _make_dispatcher(db_factory)
    processed = asyncio.Event()
    seen = []

    async def fake_process(task_id, msg):
        seen.append(msg)
        if len(seen) == 1:
            raise disp_mod.CodexAccountRoutingError("all accounts cooling down")
        processed.set()

    d._process_queued_message = fake_process
    await d.enqueue_message(1, "must survive")
    await asyncio.wait_for(processed.wait(), 1)

    assert len(seen) == 2
    assert seen[0] is seen[1]
    assert seen[1].prompt == "must survive"
    d._task_queue_workers[1].cancel()


@pytest.mark.asyncio
async def test_long_turn_does_not_respawn_consumer(db_factory, monkeypatch):
    """A turn longer than the stuck-threshold must NOT trip the watchdog into
    respawning the consumer.

    Regression for prod task #728: an ~14-min turn froze the activity heartbeat,
    the >120s watchdog cancelled+respawned the consumer (without killing the
    running `claude` subprocess), and the backlog got flushed as concurrent
    `claude --resume <same session>` processes. The lifetime heartbeat keeps the
    consumer marked alive so the watchdog stays quiet and processing stays serial.
    """
    import backend.services.dispatcher as disp_mod
    monkeypatch.setattr(disp_mod, "QUEUE_STUCK_THRESHOLD", 0.2)
    monkeypatch.setattr(disp_mod, "QUEUE_HEARTBEAT_INTERVAL", 0.02)

    d = _make_dispatcher(db_factory)

    started = asyncio.Event()
    release = asyncio.Event()
    active = 0
    max_active = 0

    async def fake_process(task_id, msg):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        started.set()
        await release.wait()  # hold the "turn" open past the stuck-threshold
        active -= 1

    d._process_queued_message = fake_process

    await d.enqueue_message(1, "first")
    await asyncio.wait_for(started.wait(), 1)
    worker1 = d._task_queue_workers[1]

    # Hold the turn open well past QUEUE_STUCK_THRESHOLD, then enqueue more work.
    await asyncio.sleep(0.4)
    await d.enqueue_message(1, "second")
    await asyncio.sleep(0.1)

    # Same consumer (not cancelled/respawned), and "second" did not start a
    # concurrent turn while "first" was still running.
    assert d._task_queue_workers[1] is worker1
    assert max_active == 1

    # Drain and clean up.
    release.set()
    await asyncio.sleep(0.1)
    assert max_active == 1
    worker1.cancel()


@pytest.mark.asyncio
async def test_watchdog_respawn_keeps_live_worker(db_factory, monkeypatch):
    """When the watchdog DOES respawn the consumer, the cancelled consumer's
    cleanup must not evict the freshly-registered worker.

    Regression for prod task #728: the old `finally` popped
    `_task_queue_workers[task_id]` unconditionally, erasing the new consumer's
    registration so a later enqueue spawned a *second* live consumer → two
    concurrent `--resume`. The guard only deregisters when the dict still points
    at the exiting task.
    """
    import backend.services.dispatcher as disp_mod
    # Negative threshold → any _ensure_queue_worker on an existing worker treats
    # it as stuck and respawns, deterministically forcing the race.
    monkeypatch.setattr(disp_mod, "QUEUE_STUCK_THRESHOLD", -1)
    monkeypatch.setattr(disp_mod, "QUEUE_HEARTBEAT_INTERVAL", 0.02)

    d = _make_dispatcher(db_factory)
    started = asyncio.Event()
    release = asyncio.Event()

    async def fake_process(task_id, msg):
        started.set()
        await release.wait()

    d._process_queued_message = fake_process

    await d.enqueue_message(1, "first")
    await asyncio.wait_for(started.wait(), 1)
    c1 = d._task_queue_workers[1]

    started.clear()
    # Forces watchdog: cancels c1, spawns c2, registers c2.
    await d.enqueue_message(1, "second")
    c2 = d._task_queue_workers[1]
    assert c2 is not c1

    # Let c1's cancellation + finally run and c2 begin processing "second".
    release.set()
    await asyncio.wait_for(started.wait(), 1)
    await asyncio.sleep(0.05)

    # The live worker registration must survive c1's cleanup.
    assert d._task_queue_workers.get(1) is c2
    assert not c2.done()

    c2.cancel()


# === Instance contention: queued-message must not steal a claimed instance (task #676) ===


async def _setup_queued_msg_two_idle(db_factory, monkeypatch):
    """Two idle instances + a resumable task; returns (d, id1, id2, task_id, msg)."""
    import time
    import backend.api.tasks as tasks_mod
    from backend.services.dispatcher import QueuedMessage, PRIORITY_USER

    # Session JSONL "present" → skip the failed/session-gone recovery branch.
    monkeypatch.setattr(
        tasks_mod,
        "_find_session_jsonl",
        lambda sid, provider="claude": "/tmp/fake.jsonl",
    )

    d = _make_dispatcher(db_factory)
    d._resolve_resume_config_dir = AsyncMock(return_value=None)

    async with db_factory() as db:
        inst1 = Instance(name="worker-1", status="idle")
        inst2 = Instance(name="worker-2", status="idle")
        db.add(inst1)
        db.add(inst2)
        task = Task(
            title="t", description="d", target_repo="/repo",
            status="executing", session_id="sess-1",
        )
        db.add(task)
        await db.commit()
        await db.refresh(inst1)
        await db.refresh(inst2)
        await db.refresh(task)
        id1, id2, task_id = inst1.id, inst2.id, task.id

    msg = QueuedMessage(
        priority=PRIORITY_USER, timestamp=time.monotonic(),
        prompt="hi", source="user",
    )
    return d, id1, id2, task_id, msg


@pytest.mark.asyncio
async def test_queued_resume_waits_at_maintenance_gate_and_stays_blocking(
    db_factory, monkeypatch,
):
    d, _id1, _id2, task_id, _msg = await _setup_queued_msg_two_idle(
        db_factory, monkeypatch
    )
    launched = asyncio.Event()

    async def launch(**_kwargs):
        launched.set()
        return 12345

    d.instance_manager.launch = AsyncMock(side_effect=launch)
    async with db_factory() as db:
        task = await db.get(Task, task_id)
        task.status = "completed"
        await db.commit()
    await d.pause_dispatching()
    await d.enqueue_message(task_id, "continue", source="monitor:complete")

    await asyncio.sleep(0.05)
    assert not launched.is_set()
    assert await d.pending_task_start_ids() == {task_id}
    async with db_factory() as db:
        task = await db.get(Task, task_id)
        assert task.status == "completed"

    d.resume_dispatching()
    await asyncio.wait_for(launched.wait(), timeout=1)

    for _ in range(20):
        if not await d.pending_task_start_ids():
            break
        await asyncio.sleep(0.01)
    assert await d.pending_task_start_ids() == set()

    worker = d._task_queue_workers.get(task_id)
    if worker and not worker.done():
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker


@pytest.mark.asyncio
async def test_pause_wins_after_queued_resume_preparation_before_launch(
    db_factory, monkeypatch,
):
    """Late admission closes the exact preparation -> executing race."""
    d, _id1, _id2, task_id, _msg = await _setup_queued_msg_two_idle(
        db_factory, monkeypatch
    )
    async with db_factory() as db:
        task = await db.get(Task, task_id)
        task.status = "completed"
        await db.commit()

    preparation_reached = asyncio.Event()
    release_preparation = asyncio.Event()
    launched = asyncio.Event()

    async def resolve(*_args, **_kwargs):
        preparation_reached.set()
        await release_preparation.wait()
        return None

    async def launch(**_kwargs):
        launched.set()
        return 12345

    d._resolve_resume_config_dir = AsyncMock(side_effect=resolve)
    d.instance_manager.launch = AsyncMock(side_effect=launch)
    await d.enqueue_message(task_id, "continue")
    await asyncio.wait_for(preparation_reached.wait(), timeout=1)

    await d.pause_dispatching()
    release_preparation.set()
    await asyncio.sleep(0.05)

    assert not launched.is_set()
    assert await d.pending_task_start_ids() == {task_id}
    async with d.maintenance_shutdown_guard() as pending_ids:
        assert pending_ids == {task_id}
    async with db_factory() as db:
        task = await db.get(Task, task_id)
        assert task.status == "completed"

    d.resume_dispatching()
    await asyncio.wait_for(launched.wait(), timeout=1)
    worker = d._task_queue_workers.get(task_id)
    if worker and not worker.done():
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker


@pytest.mark.asyncio
async def test_queued_codex_busy_launch_rolls_back_status_and_temp_skills(
    db_factory, monkeypatch,
):
    from backend.services.codex_app_server import CodexAppServerBusyError

    d, _id1, _id2, task_id, msg = await _setup_queued_msg_two_idle(
        db_factory, monkeypatch
    )
    d._resolve_resume_config_dir = AsyncMock(return_value="/tmp/codex-a")
    d.instance_manager.launch = AsyncMock(
        side_effect=CodexAppServerBusyError("account maintenance")
    )
    msg.source = "monitor:complete"
    msg.command_skills = {"temporary": True}

    async with db_factory() as db:
        task = await db.get(Task, task_id)
        task.provider = "codex"
        task.status = "completed"
        task.enabled_skills = {"base": True}
        await db.commit()

    with pytest.raises(CodexAppServerBusyError):
        await d._process_queued_message(task_id, msg)

    async with db_factory() as db:
        task = await db.get(Task, task_id)
        assert task.status == "completed"
        assert task.enabled_skills == {"base": True}
    assert msg.source_logged is True
    assert not d._launching_instances


@pytest.mark.asyncio
async def test_queued_message_skips_dispatch_claimed_instance(db_factory, monkeypatch):
    """Regression for prod task #676.

    The dispatch loop claims an idle instance for a freshly-dispatched task and
    registers it in _running_tasks, but the instance's DB status only flips to
    "running" once launch() finishes spawning the PTY session. During that
    window the queued-message path used to select the same instance purely by
    DB status=='idle', then its launch killed the dispatch loop's half-started
    session — orphaning the first task in 'executing' with no session (no chat
    button). The queued-message selection must exclude _running_tasks instances.
    """
    d, id1, id2, task_id, msg = await _setup_queued_msg_two_idle(db_factory, monkeypatch)

    # Dispatch loop has claimed inst1 (not-done lifecycle).
    claimed = asyncio.get_event_loop().create_future()
    d._running_tasks[id1] = claimed
    try:
        await d._process_queued_message(task_id, msg)

        assert d.instance_manager.launch.await_count == 1
        assert d.instance_manager.launch.call_args.kwargs["instance_id"] == id2
        # Transient launch claim released after launch.
        assert id2 not in d._launching_instances
    finally:
        claimed.cancel()


@pytest.mark.asyncio
async def test_queued_message_skips_launching_instance(db_factory, monkeypatch):
    """A concurrent queued-message launch marks its instance in
    _launching_instances; another queued-message must skip it (task #676)."""
    d, id1, id2, task_id, msg = await _setup_queued_msg_two_idle(db_factory, monkeypatch)

    d._launching_instances.add(id1)

    await d._process_queued_message(task_id, msg)

    assert d.instance_manager.launch.await_count == 1
    assert d.instance_manager.launch.call_args.kwargs["instance_id"] == id2
    # The pre-existing claim on id1 is untouched; id2's transient claim is freed.
    assert id1 in d._launching_instances
    assert id2 not in d._launching_instances


@pytest.mark.asyncio
async def test_failed_codex_task_reuses_present_native_thread(db_factory, monkeypatch):
    """A failed turn must not clone/replace a valid Codex rollout."""
    import backend.api.tasks as tasks_mod

    d, _id1, _id2, task_id, msg = await _setup_queued_msg_two_idle(
        db_factory, monkeypatch
    )
    clone = AsyncMock()
    monkeypatch.setattr(tasks_mod, "_clone_session", clone)
    async with db_factory() as db:
        task = await db.get(Task, task_id)
        task.provider = "codex"
        task.status = "failed"
        await db.commit()

    await d._process_queued_message(task_id, msg)

    clone.assert_not_awaited()
    assert d.instance_manager.launch.await_args.kwargs["resume_session_id"] == "sess-1"


@pytest.mark.asyncio
async def test_queued_codex_turn_never_runs_claude_pty_finalizer(
    db_factory, monkeypatch,
):
    """Global PTY enablement must not synthesize Codex completion/exit events."""
    d, _id1, _id2, task_id, msg = await _setup_queued_msg_two_idle(
        db_factory, monkeypatch
    )
    d._resolve_resume_config_dir = AsyncMock(return_value="/tmp/codex-a")
    d.instance_manager.pty_mode_enabled = True
    d.instance_manager.pty_rate_limit_seen.return_value = True
    d.instance_manager.transient_error_seen.return_value = True

    async with db_factory() as db:
        task = await db.get(Task, task_id)
        task.provider = "codex"
        await db.commit()

    await d._process_queued_message(task_id, msg)

    async with db_factory() as db:
        task = await db.get(Task, task_id)
        assert task.status == "executing"
    d.instance_manager._try_proactive_pool_switch.assert_not_awaited()
    assert not any(
        call.args[1].get("event_type") == "process_exit"
        for call in d.broadcaster.broadcast.await_args_list
        if len(call.args) > 1 and isinstance(call.args[1], dict)
    )


@pytest.mark.asyncio
async def test_queued_codex_message_waits_for_replacement_turn_chain(
    db_factory, monkeypatch,
):
    """A rotation/retry launched by output cleanup stays inside queue serialization."""
    d, _id1, _id2, task_id, msg = await _setup_queued_msg_two_idle(
        db_factory, monkeypatch
    )
    d._resolve_resume_config_dir = AsyncMock(return_value="/tmp/codex-a")
    first = MagicMock(name="first-turn", returncode=1)
    second = MagicMock(name="replacement-turn", returncode=0)
    first_waited = asyncio.Event()
    second_waited = asyncio.Event()
    replacement_finished = asyncio.Event()

    async with db_factory() as db:
        task = await db.get(Task, task_id)
        task.provider = "codex"
        await db.commit()

    async def fake_launch(**kwargs):
        instance_id = kwargs["instance_id"]

        async def replacement_consumer():
            await second_waited.wait()
            d.instance_manager.processes.pop(instance_id, None)
            d.instance_manager._tasks.pop(instance_id, None)
            replacement_finished.set()

        async def first_consumer():
            await first_waited.wait()
            d.instance_manager.processes[instance_id] = second
            d.instance_manager._tasks[instance_id] = asyncio.create_task(
                replacement_consumer()
            )

        d.instance_manager.processes[instance_id] = first
        d.instance_manager._tasks[instance_id] = asyncio.create_task(
            first_consumer()
        )

    async def fake_wait(process, _task, _label):
        if process is first:
            first_waited.set()
        elif process is second:
            second_waited.set()

    d.instance_manager.launch = AsyncMock(side_effect=fake_launch)
    d._wait_process = AsyncMock(side_effect=fake_wait)

    await d._process_queued_message(task_id, msg)

    assert replacement_finished.is_set()
    assert [call.args[0] for call in d._wait_process.await_args_list] == [
        first,
        second,
    ]


# === Codex provider prompt tests (provider-aware agent doc) ===


@pytest.mark.asyncio
async def test_goal_initial_prompt_codex_references_agents_md(db_factory):
    """Codex goal tasks are pointed at AGENTS.md instead of CLAUDE.md."""
    d = _make_dispatcher(db_factory)
    task = Task(
        title="t", description="implement X", mode="goal",
        goal_condition="all tests pass", goal_max_turns=10, provider="codex",
    )
    prompt = d._build_goal_initial_prompt(task)
    assert "AGENTS.md" in prompt


@pytest.mark.asyncio
async def test_build_task_prompt_provider_doc(db_factory):
    """Task prompt preamble follows the task's provider."""
    d = _make_dispatcher(db_factory)
    claude_prompt = await d._build_task_prompt(
        Task(title="t", description="do X", provider="claude")
    )
    codex_prompt = await d._build_task_prompt(
        Task(title="t", description="do X", provider="codex")
    )
    assert "请阅读项目根目录的 CLAUDE.md" in claude_prompt
    # Codex loads AGENTS.md natively; explicitly asking it to read the file
    # again turns trivial prompts into redundant shell/file work.
    assert "请阅读项目根目录的 AGENTS.md" not in codex_prompt
    assert "关键内容必须保持同步" in codex_prompt


@pytest.mark.asyncio
async def test_build_task_prompt_carries_doc_sync_note(db_factory):
    """两种 provider 的 prompt 前导都下发 CLAUDE.md/AGENTS.md 同步纪律。"""
    d = _make_dispatcher(db_factory)
    for provider in ("claude", "codex"):
        prompt = await d._build_task_prompt(
            Task(title="t", description="do X", provider=provider)
        )
        assert "关键内容必须保持同步" in prompt


@pytest.mark.asyncio
async def test_build_task_prompt_codex_skips_skill_templates(db_factory, monkeypatch):
    """Skill 模板描述 MCP 工具，MCP config 只注入 claude —— codex 不应收到模板。"""
    from backend.services.command_registry import COMMAND_REGISTRY, Command
    monkeypatch.setitem(COMMAND_REGISTRY, "fakeskill", Command(
        name="fakeskill", description="test", prompt_template="FAKESKILL_TEMPLATE",
    ))
    d = _make_dispatcher(db_factory)
    claude_prompt = await d._build_task_prompt(
        Task(title="t", description="do X", provider="claude",
             enabled_skills={"fakeskill": True})
    )
    codex_prompt = await d._build_task_prompt(
        Task(title="t", description="do X", provider="codex",
             enabled_skills={"fakeskill": True})
    )
    assert "FAKESKILL_TEMPLATE" in claude_prompt
    assert "FAKESKILL_TEMPLATE" not in codex_prompt


def test_loop_prompt_codex_references_agents_md(db_factory):
    """Loop prompts reference AGENTS.md for codex tasks."""
    d = _make_dispatcher(db_factory)
    task = Task(
        title="t", description="bg", mode="loop", todo_file_path="TODO.md",
        provider="codex", max_iterations=5,
    )
    prompt = d._build_loop_prompt(task, 0, "/tmp/sig.json")
    assert "AGENTS.md" in prompt
    assert "CLAUDE.md" not in prompt


@pytest.mark.asyncio
async def test_lifecycle_backfills_agents_md(db_factory, tmp_path):
    """任务启动时把 AGENTS.md 惰性补进有 CLAUDE.md 的存量项目。"""
    (tmp_path / "CLAUDE.md").write_text("# guide\n")
    d = _make_dispatcher(db_factory)

    async with db_factory() as db:
        inst = Instance(name="worker-1")
        db.add(inst)
        task = Task(title="t", description="do X", target_repo=str(tmp_path))
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

    assert (tmp_path / "AGENTS.md").exists()
