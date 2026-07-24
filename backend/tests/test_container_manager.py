"""Process-lifecycle tests for shared-project container execution."""

import asyncio
import os
import signal
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from backend.services.container_manager import (
    ContainerExecSpec,
    ContainerExecSpawnCleanupError,
    ContainerManager,
    _EXEC_CONTROL,
    _EXEC_SUPERVISOR,
)
from backend.services.instance_manager import InstanceManager
from backend.services.process_safety import UnsafeProcessGroupError


@pytest.mark.asyncio
async def test_exec_command_uses_tokenized_supervisor_and_host_session():
    manager = ContainerManager()
    manager._containers[7] = "ccm-project-7"
    process = MagicMock()

    with (
        patch(
            "backend.services.container_manager.secrets.token_hex",
            return_value="fixed-token",
        ),
        patch(
            "backend.services.container_manager.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=process),
        ) as spawn,
    ):
        returned = await manager.exec_command(
            7,
            ["claude", "-p", "literal; not shell"],
            env={"SAFE": "value with spaces"},
        )

    assert returned is process
    assert manager.owns_exec(process)
    args = spawn.await_args.args
    assert args[:5] == ("docker", "exec", "-i", "-w", "/workspace")
    assert "CCM_CONTAINER_EXEC_TOKEN=fixed-token" in args
    assert "CCM_CONTAINER_EXEC_ROLE=supervisor" in args
    assert args[-3:] == ("claude", "-p", "literal; not shell")
    assert _EXEC_SUPERVISOR in args
    if os.name == "posix":
        assert spawn.await_args.kwargs["start_new_session"] is True


@pytest.mark.asyncio
async def test_cancelled_exec_spawn_reaps_host_and_tokenized_inner():
    manager = ContainerManager()
    manager._containers[7] = "ccm-project-7"
    process = MagicMock(pid=54_340, returncode=None)
    process.wait = AsyncMock(return_value=-signal.SIGKILL)
    spawn_started = asyncio.Event()
    release_spawn = asyncio.Event()

    async def delayed_spawn(*args, **kwargs):
        spawn_started.set()
        await release_spawn.wait()
        return process

    def kill_host_group(pid, sig):
        assert pid == process.pid
        if sig == signal.SIGKILL:
            process.returncode = -signal.SIGKILL

    with (
        patch(
            "backend.services.container_manager.secrets.token_hex",
            return_value="cancelled-token",
        ),
        patch(
            "backend.services.container_manager.asyncio.create_subprocess_exec",
            side_effect=delayed_spawn,
        ),
        patch(
            "backend.services.container_manager.os.killpg",
            side_effect=kill_host_group,
        ) as killpg,
        patch.object(
            manager,
            "_control_spec",
            new_callable=AsyncMock,
            side_effect=[0, 3],
        ) as control,
    ):
        execution = asyncio.create_task(
            manager.exec_command(7, ["claude", "-p", "work"])
        )
        await asyncio.wait_for(spawn_started.wait(), timeout=2.0)
        execution.cancel()
        release_spawn.set()
        with pytest.raises(asyncio.CancelledError):
            await execution

    assert (process.pid, signal.SIGKILL) in [
        call.args for call in killpg.call_args_list
    ]
    assert control.await_args_list[0].kwargs == {
        "action": "signal",
        "sig": signal.SIGKILL,
        "wait_seconds": 2.0,
    }
    assert control.await_args_list[1].kwargs == {"action": "check"}
    assert not manager.owns_exec(process)


@pytest.mark.asyncio
async def test_cancelled_exec_spawn_cleanup_failure_exposes_exact_process():
    manager = ContainerManager()
    manager._containers[7] = "ccm-project-7"
    process = MagicMock(pid=54_341, returncode=None)
    process.wait = AsyncMock(return_value=-signal.SIGKILL)
    spawn_started = asyncio.Event()
    release_spawn = asyncio.Event()

    async def delayed_spawn(*args, **kwargs):
        spawn_started.set()
        await release_spawn.wait()
        return process

    def kill_host_group(pid, sig):
        if sig == signal.SIGKILL:
            process.returncode = -signal.SIGKILL

    with (
        patch(
            "backend.services.container_manager.asyncio.create_subprocess_exec",
            side_effect=delayed_spawn,
        ),
        patch(
            "backend.services.container_manager.os.killpg",
            side_effect=kill_host_group,
        ),
        patch.object(
            manager,
            "_control_spec",
            new_callable=AsyncMock,
            side_effect=RuntimeError("docker control unavailable"),
        ),
    ):
        execution = asyncio.create_task(
            manager.exec_command(7, ["claude", "-p", "work"])
        )
        await asyncio.wait_for(spawn_started.wait(), timeout=2.0)
        execution.cancel()
        release_spawn.set()
        with pytest.raises(ContainerExecSpawnCleanupError) as caught:
            await execution

    assert caught.value.process is process
    assert manager.owns_exec(process)


@pytest.mark.parametrize("unsafe_pid", [None, -1, 0, 1, False, True])
@pytest.mark.asyncio
async def test_cancelled_exec_cleanup_rejects_unsafe_host_group_without_signal(
    unsafe_pid,
):
    manager = ContainerManager()
    process = MagicMock(pid=unsafe_pid, returncode=None)
    process.kill = MagicMock()
    process.wait = AsyncMock()
    spec = ContainerExecSpec(
        container_name="ccm-project-7",
        token="unsafe-host-group",
        pid_file="/tmp/ccm-exec-unsafe-host-group.pid",
    )

    with (
        patch("backend.services.container_manager.os.killpg") as killpg,
        patch.object(
            manager,
            "_control_spec",
            new_callable=AsyncMock,
        ) as control,
    ):
        with pytest.raises(
            UnsafeProcessGroupError,
            match="Refusing unsafe process group identity",
        ):
            await manager._cleanup_cancelled_exec_spawn(process, spec)

    killpg.assert_not_called()
    process.kill.assert_not_called()
    process.wait.assert_not_awaited()
    control.assert_not_awaited()


@pytest.mark.asyncio
async def test_signal_exec_targets_exact_tokenized_container_generation():
    manager = ContainerManager()
    manager._containers[7] = "ccm-project-7"
    process = MagicMock(returncode=None)

    with (
        patch(
            "backend.services.container_manager.secrets.token_hex",
            return_value="exact-token",
        ),
        patch(
            "backend.services.container_manager.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=process),
        ),
    ):
        await manager.exec_command(7, ["claude"])

    manager._run = AsyncMock(return_value=(0, ""))
    assert await manager.signal_exec(process, signal.SIGKILL) is True

    control_cmd = manager._run.await_args.args[0]
    assert control_cmd[:3] == ["docker", "exec", "ccm-project-7"]
    assert "exact-token" in control_cmd
    assert str(int(signal.SIGKILL)) in control_cmd


def test_pty_wrapper_uses_supervisor_and_is_instance_unique(tmp_path):
    manager = ContainerManager()
    manager._containers[7] = "ccm-project-7"

    with (
        patch(
            "backend.services.container_manager.secrets.token_hex",
            return_value="pty-token",
        ),
        patch(
            "backend.services.container_manager.tempfile.gettempdir",
            return_value=str(tmp_path),
        ),
    ):
        wrapper_path, spec = manager.create_pty_wrapper(7, 19)

    try:
        assert wrapper_path.endswith(
            "ccm-docker-claude-19-pty-token.sh"
        )
        wrapper = Path(wrapper_path).read_text(encoding="utf-8")
        assert "CCM_CONTAINER_EXEC_TOKEN=pty-token" in wrapper
        assert "CCM_CONTAINER_EXEC_ROLE=supervisor" in wrapper
        assert "\"$@\"" in wrapper
        assert oct(os.stat(wrapper_path).st_mode & 0o777) == "0o700"
    finally:
        manager.discard_spec(spec)
    assert not os.path.exists(wrapper_path)


@pytest.mark.asyncio
async def test_container_control_failure_is_fail_closed():
    manager = ContainerManager()
    manager._containers[7] = "ccm-project-7"
    process = MagicMock(returncode=None)

    with patch(
        "backend.services.container_manager.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=process),
    ):
        await manager.exec_command(7, ["claude"])

    manager._run = AsyncMock(return_value=(125, "docker daemon unavailable"))
    with pytest.raises(RuntimeError, match="docker daemon unavailable"):
        await manager.signal_exec(process, signal.SIGKILL)
    assert manager.owns_exec(process)


@pytest.mark.asyncio
async def test_instance_manager_signals_inner_exec_before_host_group():
    manager = InstanceManager(MagicMock(), MagicMock())
    process = MagicMock(pid=43210, returncode=None)
    container_manager = MagicMock()
    container_manager.owns_exec.return_value = True
    container_manager.signal_exec = AsyncMock(return_value=True)
    manager._container_mgr = container_manager
    manager._container_exec_processes[9] = process
    manager._container_tasks[9] = 7
    manager._process_groups[9] = process

    with patch("backend.services.instance_manager.os.killpg") as killpg:
        await manager._signal_managed_process_tree(
            9, process, signal.SIGTERM
        )

    container_manager.signal_exec.assert_awaited_once_with(
        process, signal.SIGTERM
    )
    killpg.assert_called_once_with(process.pid, signal.SIGTERM)


@pytest.mark.asyncio
async def test_instance_manager_waits_for_inner_group_before_forgetting_exec():
    manager = InstanceManager(MagicMock(), MagicMock())
    process = MagicMock(pid=43210, returncode=0)
    process.wait = AsyncMock(return_value=0)
    container_manager = MagicMock()
    container_manager.owns_exec.return_value = True
    container_manager.exec_is_alive = AsyncMock(side_effect=[True, False])
    manager._container_mgr = container_manager
    manager._container_exec_processes[9] = process
    manager._container_tasks[9] = 7

    with patch.object(manager, "_process_group_alive", return_value=False):
        await manager._wait_process_tree(9, process, 1.0)

    assert container_manager.exec_is_alive.await_count == 2
    container_manager.forget_exec.assert_called_once_with(process)
    assert 9 not in manager._container_exec_processes
    assert 9 not in manager._container_tasks


@pytest.mark.asyncio
async def test_inner_signal_failure_retains_generation_evidence():
    manager = InstanceManager(MagicMock(), MagicMock())
    process = MagicMock(pid=43210, returncode=None)
    container_manager = MagicMock()
    container_manager.owns_exec.return_value = True
    container_manager.signal_exec = AsyncMock(
        side_effect=RuntimeError("inner state unknown")
    )
    manager._container_mgr = container_manager
    manager._container_exec_processes[9] = process
    manager._container_tasks[9] = 7
    manager._process_groups[9] = process

    with (
        patch("backend.services.instance_manager.os.killpg"),
        pytest.raises(RuntimeError, match="inner state unknown"),
    ):
        await manager._signal_managed_process_tree(
            9, process, signal.SIGKILL
        )

    assert manager._container_exec_processes[9] is process
    assert manager._process_groups[9] is process


@pytest.mark.asyncio
async def test_pty_exit_kills_inner_survivors_before_forgetting_generation():
    manager = InstanceManager(MagicMock(), MagicMock())
    process = MagicMock(pid=43210, returncode=0)
    container_manager = MagicMock()
    container_manager.owns_exec.return_value = True
    container_manager.exec_is_alive = AsyncMock(side_effect=[True, False])
    container_manager.signal_exec = AsyncMock(return_value=True)
    manager._container_mgr = container_manager
    manager._container_exec_processes[9] = process
    manager._container_tasks[9] = 7

    await manager.finalize_pty_container_exec(9)

    container_manager.signal_exec.assert_awaited_once_with(
        process, signal.SIGKILL
    )
    container_manager.forget_exec.assert_called_once_with(process)
    assert 9 not in manager._container_exec_processes
    assert 9 not in manager._container_tasks


@pytest.mark.asyncio
async def test_stale_pty_exit_does_not_signal_replacement_container_generation():
    manager = InstanceManager(MagicMock(), MagicMock())
    old_process = MagicMock(pid=43209, returncode=0)
    replacement = MagicMock(pid=43210, returncode=None)
    container_manager = MagicMock()
    container_manager.owns_exec.return_value = True
    container_manager.exec_is_alive = AsyncMock(return_value=True)
    container_manager.signal_exec = AsyncMock()
    manager._container_mgr = container_manager
    manager._container_exec_processes[9] = replacement

    await manager.finalize_pty_container_exec(
        9, expected_process=old_process
    )

    container_manager.exec_is_alive.assert_not_awaited()
    container_manager.signal_exec.assert_not_awaited()
    assert manager._container_exec_processes[9] is replacement


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "posix", reason="requires POSIX sessions")
async def test_inner_supervisor_kills_descendants_after_agent_leader_exits(
    tmp_path,
):
    """Exercise the real supervisor without requiring a Docker daemon."""

    pid_file = tmp_path / "agent.pid"
    descendant_file = tmp_path / "descendant.pid"
    leader = (
        "import pathlib,signal,subprocess,sys;"
        "p=subprocess.Popen([sys.executable,'-c',"
        "'import signal,time;"
        "signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(60)']);"
        f"pathlib.Path({str(descendant_file)!r}).write_text(str(p.pid))"
    )
    env = os.environ.copy()
    env["CCM_CONTAINER_EXEC_TOKEN"] = "supervisor-test"
    env["CCM_CONTAINER_EXEC_ROLE"] = "supervisor"
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        _EXEC_SUPERVISOR,
        str(pid_file),
        sys.executable,
        "-c",
        leader,
        env=env,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )

    await asyncio.wait_for(process.wait(), timeout=5.0)
    descendant_pid = int(descendant_file.read_text())

    def descendant_is_live() -> bool:
        try:
            state = Path(f"/proc/{descendant_pid}/stat").read_text(
                encoding="utf-8"
            ).split()[2]
            return state != "Z"
        except FileNotFoundError:
            return False

    deadline = asyncio.get_running_loop().time() + 2.0
    while descendant_is_live() and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.02)
    assert not descendant_is_live()
    assert not pid_file.exists()


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "posix", reason="requires POSIX /proc")
async def test_exact_token_controller_stops_live_inner_group(tmp_path):
    """Validate the control protocol against real local processes."""

    pid_file = tmp_path / "controlled.pid"
    token = "exact-controller-test"
    env = os.environ.copy()
    env["CCM_CONTAINER_EXEC_TOKEN"] = token
    env["CCM_CONTAINER_EXEC_ROLE"] = "supervisor"
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        _EXEC_SUPERVISOR,
        str(pid_file),
        sys.executable,
        "-c",
        "import time; time.sleep(60)",
        env=env,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        deadline = asyncio.get_running_loop().time() + 2.0
        while (
            not pid_file.exists()
            and asyncio.get_running_loop().time() < deadline
        ):
            await asyncio.sleep(0.02)
        assert pid_file.exists()

        controller = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            _EXEC_CONTROL,
            token,
            str(pid_file),
            "signal",
            str(int(signal.SIGKILL)),
            "1.0",
        )
        assert await asyncio.wait_for(controller.wait(), timeout=3.0) == 0
        assert await asyncio.wait_for(process.wait(), timeout=3.0) == (
            128 + int(signal.SIGKILL)
        )
        assert not pid_file.exists()
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
