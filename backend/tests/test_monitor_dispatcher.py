"""Tests for the monitor sub-agent lifecycle (persistent subprocess design).

Current design (replaces the old per-check subprocess loop):
- ``start_monitor_session`` spawns ``_monitor_session_lifecycle`` as an asyncio task.
- The lifecycle launches ONE persistent Claude subprocess (``_launch_monitor_agent``)
  with a dedicated MCP config; the sub-agent loops internally and reports back via
  MCP tools that call the CCM API (POST .../checks, POST .../complete).
- MonitorCheck records and per-check broadcasts are therefore written by the API
  endpoints, not by the dispatcher; tests for those live at the API level below.
"""
import asyncio
import os
import signal
import sys
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy import select, update

from backend.models.task import Task
from backend.models.monitor_session import MonitorSession, MonitorCheck
from backend.services.dispatcher import GlobalDispatcher


@pytest.fixture
def mock_broadcaster():
    b = MagicMock()
    b.broadcast = AsyncMock()
    return b


@pytest.fixture
def dispatcher(db_factory, mock_broadcaster):
    d = GlobalDispatcher.__new__(GlobalDispatcher)
    d.db_factory = db_factory
    d.broadcaster = mock_broadcaster
    d.instance_manager = MagicMock()
    d._running_tasks = {}
    d._monitor_tasks = {}
    d._monitor_processes = {}
    d._monitor_log_fhs = {}
    return d


async def _seed_task_and_monitor(
    db_factory, status="in_progress", max_checks=50, interval=1, context=None
):
    async with db_factory() as db:
        task = Task(
            title="t", description="d", status=status,
            enabled_skills={"monitor": True}, target_repo="/tmp",
        )
        db.add(task)
        await db.commit()
        await db.refresh(task)
        ms = MonitorSession(
            task_id=task.id, description="test monitor",
            interval=interval, max_checks=max_checks, monitor_context=context,
        )
        db.add(ms)
        await db.commit()
        await db.refresh(ms)
        return task.id, ms.id


def _fake_proc(returncode=0):
    proc = MagicMock()
    proc.returncode = returncode
    proc.pid = 12345
    proc.wait = AsyncMock(return_value=returncode)
    proc.kill = MagicMock()
    return proc


def _pid_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    stat_path = f"/proc/{pid}/stat"
    try:
        with open(stat_path, encoding="utf-8") as stat_file:
            stat = stat_file.read()
    except (FileNotFoundError, PermissionError):
        return True
    close_paren = stat.rfind(")")
    state = stat[close_paren + 2:].split()[0] if close_paren >= 0 else ""
    return state != "Z"


async def _wait_for_pid_file(path, timeout: float = 2.0) -> int:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if path.exists():
            return int(path.read_text(encoding="utf-8"))
        await asyncio.sleep(0.01)
    raise AssertionError("Timed out waiting for auxiliary child PID")


async def _wait_until_not_running(pid: int, timeout: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if not _pid_is_running(pid):
            return
        await asyncio.sleep(0.02)
    assert not _pid_is_running(pid), f"Process {pid} is still running"


# === Prompt building ===


def test_build_monitor_agent_prompt(dispatcher):
    prompt = dispatcher._build_monitor_agent_prompt("watch build", "tail -f /tmp/log")
    assert "监控目标" in prompt
    assert "watch build" in prompt
    assert "上下文" in prompt
    assert "tail -f /tmp/log" in prompt
    # The sub-agent must be told about its MCP callback tools
    assert "report_status" in prompt
    assert "mark_complete" in prompt


def test_build_monitor_agent_prompt_no_context(dispatcher):
    prompt = dispatcher._build_monitor_agent_prompt("test", None)
    assert "test" in prompt
    assert "上下文" not in prompt


def test_build_monitor_agent_prompt_interval_guidance(dispatcher):
    """等待指引按 interval 生成：单次睡满间隔 + 显式大 timeout + 拆分兜底。

    2026-07-16 task 35：interval 3600/1800 的 monitor 首查后长 sleep 被 CLI
    转后台 → 子 agent 转投 ScheduleWakeup 结束回合 → -p 进程退出 → 误判 failed。
    """
    prompt = dispatcher._build_monitor_agent_prompt("watch", None, interval=1800)
    assert "1800 秒" in prompt
    assert "time.sleep(1800)" in prompt
    assert f"timeout={(1800 + 120) * 1000}" in prompt
    # 长 sleep 被拦时的拆分兜底
    assert "time.sleep(300)" in prompt


@pytest.mark.asyncio
async def test_launch_monitor_agent_raises_bash_max_timeout(dispatcher, tmp_path):
    """BASH_MAX_TIMEOUT_MS 按 interval 抬高，否则单次长 sleep 被 CLI 转后台。"""
    dispatcher.pool = None
    captured = {}

    async def fake_exec(*cmd, **kwargs):
        captured["env"] = kwargs["env"]
        return _fake_proc()

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        await dispatcher._launch_monitor_agent(
            prompt="p", cwd="/tmp", model=None,
            monitor_session_id=990001,
            mcp_config_path=tmp_path / "mcp.json",
            interval_seconds=3600,
        )

    assert captured["env"]["BASH_MAX_TIMEOUT_MS"] == str((3600 + 600) * 1000)
    dispatcher._monitor_log_fhs[990001].close()


@pytest.mark.asyncio
async def test_launch_monitor_agent_keeps_larger_env_timeout(
    dispatcher, tmp_path, monkeypatch
):
    """环境里已有更大的 BASH_MAX_TIMEOUT_MS 时只抬不降。"""
    dispatcher.pool = None
    monkeypatch.setenv("BASH_MAX_TIMEOUT_MS", "99999000")
    captured = {}

    async def fake_exec(*cmd, **kwargs):
        captured["env"] = kwargs["env"]
        return _fake_proc()

    with patch("asyncio.create_subprocess_exec", side_effect=fake_exec):
        await dispatcher._launch_monitor_agent(
            prompt="p", cwd="/tmp", model=None,
            monitor_session_id=990002,
            mcp_config_path=tmp_path / "mcp.json",
            interval_seconds=300,
        )

    assert captured["env"]["BASH_MAX_TIMEOUT_MS"] == "99999000"
    dispatcher._monitor_log_fhs[990002].close()


# === start_monitor_session ===


@pytest.mark.asyncio
async def test_start_monitor_session(dispatcher):
    ms = MagicMock()
    ms.id = 1
    with patch.object(dispatcher, "_monitor_session_lifecycle", new_callable=AsyncMock):
        dispatcher.start_monitor_session(ms)
    assert 1 in dispatcher._monitor_tasks
    dispatcher._monitor_tasks[1].cancel()
    try:
        await dispatcher._monitor_tasks[1]
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_auxiliary_admission_is_closed_before_shutdown_snapshot(dispatcher):
    dispatcher._shutting_down = True
    dispatcher._sub_agent_tasks = {}

    with pytest.raises(RuntimeError, match="monitor admission is closed"):
        dispatcher.start_monitor_session(MagicMock(id=91))
    with pytest.raises(RuntimeError, match="sub-agent admission is closed"):
        dispatcher.start_sub_agent_session(MagicMock(id=92))

    assert 91 not in dispatcher._monitor_tasks
    assert 92 not in dispatcher._sub_agent_tasks


@pytest.mark.asyncio
async def test_stop_aux_refreshes_process_registered_during_spawn_cancel(
    dispatcher,
):
    process = _fake_proc(returncode=None)
    process_map = {}
    task_map = {}

    async def spawn_window():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            # `_settle_aux_process_spawn` returns the exact handle only after
            # the caller cancellation; registration therefore happens here.
            process_map[93] = process
            raise

    lifecycle = asyncio.create_task(spawn_window())
    task_map[93] = lifecycle
    await asyncio.sleep(0)

    async def terminate(candidate):
        assert candidate is process
        candidate.returncode = -9

    dispatcher._terminate_aux_process = AsyncMock(side_effect=terminate)
    with patch.object(
        GlobalDispatcher, "_aux_process_group_alive", return_value=False
    ):
        await dispatcher._stop_aux_session(93, task_map, process_map)

    dispatcher._terminate_aux_process.assert_awaited_once_with(process)
    assert 93 not in process_map


@pytest.mark.asyncio
async def test_stop_aux_timeout_retains_lifecycle_evidence(dispatcher):
    release = asyncio.Event()

    async def ignores_first_cancellation():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await release.wait()

    lifecycle = asyncio.create_task(ignores_first_cancellation())
    task_map = {94: lifecycle}
    process_map = {}
    await asyncio.sleep(0)
    try:
        with pytest.raises(RuntimeError, match="did not stop"):
            await dispatcher._stop_aux_session(
                94,
                task_map,
                process_map,
                lifecycle_timeout=0.01,
            )
        assert task_map[94] is lifecycle
        assert not lifecycle.done()
    finally:
        release.set()
        await asyncio.wait_for(lifecycle, timeout=1)


# === Lifecycle: persistent subprocess state transitions ===


@pytest.mark.asyncio
async def test_lifecycle_completed_by_subagent(dispatcher, db_factory, mock_broadcaster):
    """Sub-agent calls mark_complete (via API) then exits → session stays completed."""
    task_id, ms_id = await _seed_task_and_monitor(db_factory)

    proc = _fake_proc(returncode=0)

    async def wait_and_complete():
        # Simulate the sub-agent's mark_complete MCP call before exiting
        async with db_factory() as db:
            await db.execute(
                update(MonitorSession).where(MonitorSession.id == ms_id)
                .values(status="completed")
            )
            await db.commit()
        return 0

    proc.wait = AsyncMock(side_effect=wait_and_complete)

    with patch.object(dispatcher, "_launch_monitor_agent", new_callable=AsyncMock, return_value=proc) as mock_launch, \
         patch("backend.services.mcp_config.cleanup_monitor_agent_mcp_config") as mock_cleanup:
        await dispatcher._monitor_session_lifecycle(ms_id)

    # Launched once with the monitor prompt and the session's cwd
    mock_launch.assert_awaited_once()
    launch_kwargs = mock_launch.call_args.kwargs
    assert "test monitor" in launch_kwargs["prompt"]
    assert launch_kwargs["monitor_session_id"] == ms_id

    async with db_factory() as db:
        ms = await db.get(MonitorSession, ms_id)
        assert ms.status == "completed"

    # No "failed" broadcast for a clean completion
    failed_events = [
        c for c in mock_broadcaster.broadcast.call_args_list
        if c[0][1].get("event") == "monitor_session_status" and c[0][1].get("status") == "failed"
    ]
    assert failed_events == []

    # MCP config cleaned up, bookkeeping dicts emptied
    mock_cleanup.assert_called_once_with(ms_id)
    assert ms_id not in dispatcher._monitor_tasks
    assert ms_id not in dispatcher._monitor_processes


@pytest.mark.asyncio
async def test_normal_parent_exit_kills_residual_monitor_group(
    dispatcher, db_factory
):
    """A clean CLI parent exit cannot leave its tool child running."""
    _, ms_id = await _seed_task_and_monitor(db_factory)
    proc = _fake_proc(returncode=0)
    group_alive = True
    signals = []

    async def wait_and_complete():
        async with db_factory() as db:
            await db.execute(
                update(MonitorSession)
                .where(MonitorSession.id == ms_id)
                .values(status="completed")
            )
            await db.commit()
        return 0

    proc.wait = AsyncMock(side_effect=wait_and_complete)

    async def launch_and_register(**kwargs):
        dispatcher._monitor_processes[ms_id] = proc
        return proc

    def kill_group(pid, sig):
        nonlocal group_alive
        assert pid == proc.pid
        if sig == 0:
            if group_alive:
                return None
            raise ProcessLookupError
        assert sig == signal.SIGKILL
        signals.append(sig)
        group_alive = False

    with (
        patch.object(
            dispatcher,
            "_launch_monitor_agent",
            side_effect=launch_and_register,
        ),
        patch("backend.services.dispatcher.os.killpg", side_effect=kill_group),
    ):
        await dispatcher._monitor_session_lifecycle(ms_id)

    assert signals == [signal.SIGKILL]
    assert ms_id not in dispatcher._monitor_processes


@pytest.mark.asyncio
async def test_failed_group_proof_retains_aux_process_evidence(dispatcher):
    proc = _fake_proc(returncode=0)
    dispatcher._monitor_processes[71] = proc

    with (
        patch.object(
            dispatcher,
            "_terminate_aux_process",
            new_callable=AsyncMock,
            side_effect=RuntimeError("cannot prove"),
        ),
        patch.object(
            GlobalDispatcher,
            "_aux_process_group_alive",
            return_value=True,
        ),
    ):
        delayed = await dispatcher._finalize_aux_lifecycle_process(
            session_id=71,
            process=proc,
            process_map=dispatcher._monitor_processes,
        )

    assert delayed is None
    assert dispatcher._monitor_processes[71] is proc


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "posix", reason="POSIX process groups only")
@pytest.mark.parametrize("map_kind", ["monitor", "sub-agent"])
async def test_aux_spawn_cancellation_settles_registers_and_reaps(
    dispatcher, tmp_path, map_kind
):
    """Cancellation inside spawn cannot lose the exact child group handle."""
    pid_file = tmp_path / f"{map_kind}-child.pid"
    log_path = tmp_path / f"{map_kind}.log"
    process_map = {}
    log_map = {}
    captured = {}
    spawned = asyncio.Event()
    release_spawn = asyncio.Event()
    real_create_subprocess_exec = asyncio.create_subprocess_exec

    script = """
import pathlib
import subprocess
import sys
import time

child = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(30)"],
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
pathlib.Path(sys.argv[1]).write_text(str(child.pid), encoding="utf-8")
time.sleep(30)
"""
    cmd = [sys.executable, "-c", script, str(pid_file)]

    async def delayed_spawn(*args, **kwargs):
        process = await real_create_subprocess_exec(*args, **kwargs)
        captured["process"] = process
        spawned.set()
        await release_spawn.wait()
        return process

    launch = None
    child_pid = None
    try:
        with patch(
            "backend.services.dispatcher.asyncio.create_subprocess_exec",
            side_effect=delayed_spawn,
        ):
            launch = asyncio.create_task(
                dispatcher._launch_registered_aux_process(
                    cmd=cmd,
                    cwd=str(tmp_path),
                    env=dict(os.environ),
                    log_path=log_path,
                    session_id=81,
                    process_map=process_map,
                    log_map=log_map,
                )
            )
            await spawned.wait()
            child_pid = await _wait_for_pid_file(pid_file)
            launch.cancel()
            await asyncio.sleep(0)
            assert not launch.done()
            release_spawn.set()
            with pytest.raises(asyncio.CancelledError):
                await launch

        assert captured["process"].returncode is not None
        await _wait_until_not_running(child_pid)
        assert process_map == {}
        assert log_map == {}
    finally:
        release_spawn.set()
        if launch is not None and not launch.done():
            launch.cancel()
            await asyncio.gather(launch, return_exceptions=True)
        process = captured.get("process")
        if process is not None and process.returncode is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await process.wait()
        if child_pid is not None and _pid_is_running(child_pid):
            try:
                os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


@pytest.mark.asyncio
async def test_lifecycle_abnormal_exit_marks_failed(dispatcher, db_factory, mock_broadcaster):
    """Process exits without mark_complete → session marked failed + broadcast."""
    task_id, ms_id = await _seed_task_and_monitor(db_factory)

    proc = _fake_proc(returncode=1)
    with patch.object(dispatcher, "_launch_monitor_agent", new_callable=AsyncMock, return_value=proc):
        await dispatcher._monitor_session_lifecycle(ms_id)

    async with db_factory() as db:
        ms = await db.get(MonitorSession, ms_id)
        assert ms.status == "failed"
        assert ms.completed_at is not None

    failed_events = [
        c for c in mock_broadcaster.broadcast.call_args_list
        if c[0][1].get("event") == "monitor_session_status" and c[0][1].get("status") == "failed"
    ]
    assert len(failed_events) == 1
    assert failed_events[0][0][0] == f"task:{task_id}"
    assert failed_events[0][0][1]["monitor_session_id"] == ms_id


@pytest.mark.asyncio
async def test_lifecycle_timeout_kills_process(dispatcher, db_factory, mock_broadcaster):
    """Overall lifecycle timeout → process killed, session marked failed."""
    task_id, ms_id = await _seed_task_and_monitor(db_factory)

    proc = _fake_proc(returncode=None)

    async def time_out():
        raise asyncio.TimeoutError

    proc.wait = AsyncMock(side_effect=time_out)
    group_alive = True
    signals = []

    def kill_group(pid, sig):
        nonlocal group_alive
        assert pid == proc.pid
        if sig == 0:
            if group_alive:
                return None
            raise ProcessLookupError
        signals.append(sig)
        group_alive = False
        proc.returncode = -9

    with (
        patch.object(
            dispatcher,
            "_launch_monitor_agent",
            new_callable=AsyncMock,
            return_value=proc,
        ),
        patch("backend.services.dispatcher.os.killpg", side_effect=kill_group),
    ):
        await dispatcher._monitor_session_lifecycle(ms_id)

    assert signals, "process group should be killed on timeout"

    async with db_factory() as db:
        ms = await db.get(MonitorSession, ms_id)
        assert ms.status == "failed"


@pytest.mark.asyncio
async def test_lifecycle_cancelled(dispatcher, db_factory, mock_broadcaster):
    """Cancelling the lifecycle task kills the subprocess and cleans up."""
    task_id, ms_id = await _seed_task_and_monitor(db_factory)

    proc = _fake_proc(returncode=None)

    async def hang():
        await asyncio.sleep(9999)

    proc.wait = AsyncMock(side_effect=hang)
    group_alive = True
    signals = []

    def kill_group(pid, sig):
        nonlocal group_alive
        assert pid == proc.pid
        if sig == 0:
            if group_alive:
                return None
            raise ProcessLookupError
        signals.append(sig)
        group_alive = False
        proc.returncode = -9

    async def launch_and_register(**kwargs):
        dispatcher._monitor_processes[ms_id] = proc
        return proc

    with (
        patch.object(
            dispatcher,
            "_launch_monitor_agent",
            side_effect=launch_and_register,
        ),
        patch("backend.services.dispatcher.os.killpg", side_effect=kill_group),
    ):
        lifecycle_task = asyncio.create_task(dispatcher._monitor_session_lifecycle(ms_id))
        await asyncio.sleep(0.1)
        lifecycle_task.cancel()
        try:
            await lifecycle_task
        except asyncio.CancelledError:
            pass

    assert signals, "subprocess group should be killed on cancellation"
    assert ms_id not in dispatcher._monitor_tasks
    assert ms_id not in dispatcher._monitor_processes


@pytest.mark.asyncio
async def test_lifecycle_launch_failure_marks_failed(dispatcher, db_factory, mock_broadcaster):
    """Unexpected exception (e.g. launch crash) → session marked failed."""
    task_id, ms_id = await _seed_task_and_monitor(db_factory)

    with patch.object(
        dispatcher, "_launch_monitor_agent",
        new_callable=AsyncMock, side_effect=RuntimeError("spawn failed"),
    ):
        await dispatcher._monitor_session_lifecycle(ms_id)

    async with db_factory() as db:
        ms = await db.get(MonitorSession, ms_id)
        assert ms.status == "failed"
        assert ms.completed_at is not None


# === API callbacks: MonitorCheck records + broadcasts ===
# In the new design the sub-agent reports via MCP tools that hit these endpoints,
# so the per-check persistence/broadcast coverage moved here.


async def _seed_via_api(client, session_factory, max_checks=50):
    resp = await client.post("/api/tasks", json={
        "title": "T", "description": "d", "target_repo": "/tmp",
        "enabled_skills": {"monitor": True},
    })
    task_id = resp.json()["id"]
    async with session_factory() as db:
        await db.execute(update(Task).where(Task.id == task_id).values(status="in_progress"))
        ms = MonitorSession(task_id=task_id, description="api monitor", max_checks=max_checks)
        db.add(ms)
        await db.commit()
        await db.refresh(ms)
        return task_id, ms.id


def _mock_main_dispatcher():
    d = MagicMock()
    d.broadcaster = MagicMock()
    d.broadcaster.broadcast = AsyncMock()
    d.enqueue_message = AsyncMock()
    d.stop_monitor_session_process = AsyncMock()
    d._monitor_processes = {}
    return d


@pytest.mark.asyncio
async def test_report_check_writes_record_and_broadcasts(client, session_factory):
    task_id, ms_id = await _seed_via_api(client, session_factory)
    mock_d = _mock_main_dispatcher()

    with patch("backend.main.dispatcher", mock_d):
        resp = await client.post(
            f"/api/tasks/{task_id}/monitor-sessions/{ms_id}/checks",
            json={"summary": "Process running at 45% CPU", "status": "success"},
        )
    assert resp.status_code == 200

    async with session_factory() as db:
        ms = await db.get(MonitorSession, ms_id)
        assert ms.checks_done == 1
        assert ms.last_summary == "Process running at 45% CPU"
        assert ms.status == "running"

        result = await db.execute(
            select(MonitorCheck).where(MonitorCheck.monitor_session_id == ms_id)
        )
        check = result.scalars().one()
        assert check.check_number == 1
        assert check.status == "success"
        assert check.summary == "Process running at 45% CPU"

    events = [
        c for c in mock_d.broadcaster.broadcast.call_args_list
        if c[0][1].get("event") == "monitor_check"
    ]
    assert len(events) == 1
    assert events[0][0][0] == f"task:{task_id}"
    assert events[0][0][1]["summary"] == "Process running at 45% CPU"
    # Non-important routine check does not interrupt the main agent
    mock_d.enqueue_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_report_check_important_enqueues_to_main_agent(client, session_factory):
    task_id, ms_id = await _seed_via_api(client, session_factory)
    mock_d = _mock_main_dispatcher()

    with patch("backend.main.dispatcher", mock_d):
        resp = await client.post(
            f"/api/tasks/{task_id}/monitor-sessions/{ms_id}/checks",
            json={"summary": "Build FAILED", "status": "success", "is_important": True},
        )
    assert resp.status_code == 200

    mock_d.enqueue_message.assert_awaited_once()
    kwargs = mock_d.enqueue_message.call_args.kwargs
    assert kwargs["task_id"] == task_id
    assert kwargs["source"] == "monitor:report"
    assert "Build FAILED" in kwargs["prompt"]


@pytest.mark.asyncio
async def test_report_check_max_checks_auto_completes(client, session_factory):
    task_id, ms_id = await _seed_via_api(client, session_factory, max_checks=1)
    mock_d = _mock_main_dispatcher()

    with patch("backend.main.dispatcher", mock_d):
        resp = await client.post(
            f"/api/tasks/{task_id}/monitor-sessions/{ms_id}/checks",
            json={"summary": "still running", "status": "success"},
        )
    assert resp.status_code == 200

    async with session_factory() as db:
        ms = await db.get(MonitorSession, ms_id)
        assert ms.status == "completed"
        assert ms.checks_done == 1
        assert ms.completed_at is not None

    status_events = [
        c for c in mock_d.broadcaster.broadcast.call_args_list
        if c[0][1].get("event") == "monitor_session_status"
    ]
    assert len(status_events) == 1
    assert status_events[0][0][1]["status"] == "completed"

    mock_d.enqueue_message.assert_awaited_once()
    assert mock_d.enqueue_message.call_args.kwargs["source"] == "monitor:complete"


@pytest.mark.asyncio
async def test_mark_complete_endpoint(client, session_factory):
    task_id, ms_id = await _seed_via_api(client, session_factory)
    mock_d = _mock_main_dispatcher()

    with patch("backend.main.dispatcher", mock_d):
        resp = await client.post(
            f"/api/tasks/{task_id}/monitor-sessions/{ms_id}/complete",
            json={"reason": "Build finished successfully"},
        )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True

    async with session_factory() as db:
        ms = await db.get(MonitorSession, ms_id)
        assert ms.status == "completed"
        assert ms.completed_at is not None
        assert ms.last_summary == "Build finished successfully"

        result = await db.execute(
            select(MonitorCheck).where(MonitorCheck.monitor_session_id == ms_id)
        )
        check = result.scalars().one()
        assert check.status == "completed"
        assert check.summary == "Build finished successfully"

    events = {c[0][1].get("event") for c in mock_d.broadcaster.broadcast.call_args_list}
    assert "monitor_check" in events
    assert "monitor_session_status" in events

    # Completion is relayed to the main agent
    mock_d.enqueue_message.assert_awaited_once()
    assert mock_d.enqueue_message.call_args.kwargs["source"] == "monitor:complete"


@pytest.mark.asyncio
async def test_report_check_session_not_running(client, session_factory):
    task_id, ms_id = await _seed_via_api(client, session_factory)
    async with session_factory() as db:
        await db.execute(
            update(MonitorSession).where(MonitorSession.id == ms_id)
            .values(status="completed")
        )
        await db.commit()

    resp = await client.post(
        f"/api/tasks/{task_id}/monitor-sessions/{ms_id}/checks",
        json={"summary": "late report", "status": "success"},
    )
    assert resp.status_code == 400
