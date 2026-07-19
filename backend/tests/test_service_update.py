"""Tests for UpdateService — the self-update pipeline.

Regression tests for the 2026-07-16 ccm-xiaoyu incident: the migration-path
script lived in the service's own cgroup, so its `systemctl stop` killed the
script itself and the service was never started again (502 until manual fix).
"""
import json
import os
import signal
import stat
import subprocess
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.services.update_service import (
    STEP_NAMES,
    StepInfo,
    UpdateService,
    UpdateState,
)

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "update_migrate.sh"


def _make_service(tmp_path: Path) -> UpdateService:
    broadcaster = MagicMock()
    broadcaster.broadcast = AsyncMock()
    svc = UpdateService(broadcaster, port=8999, project_dir=str(tmp_path))
    svc._status_file = tmp_path / "status.json"
    return svc


def _make_state() -> UpdateState:
    return UpdateState(
        update_id="upd_test",
        status="running",
        steps=[StepInfo(name=n) for n in STEP_NAMES],
        old_commit="old" * 10,
        backup_file="/tmp/backup.db",
    )


# ---- _migration_path escapes the service cgroup ----


@pytest.mark.asyncio
async def test_migration_path_uses_systemd_run_when_managed(tmp_path):
    svc = _make_service(tmp_path)
    state = _make_state()

    with patch.object(svc, "_systemd_scope", return_value="user"), \
         patch("backend.services.update_service.subprocess.Popen") as popen, \
         patch("backend.services.update_service.asyncio.sleep", new=AsyncMock()):
        await svc._migration_path(state)

    argv = popen.call_args[0][0]
    assert "systemd-run" in Path(argv[0]).name
    assert "--user" in argv
    assert "--collect" in argv
    assert f"--unit=ccm-update-{svc.port}" in argv
    assert str(SCRIPT.name) in " ".join(argv)
    # the script itself must NOT rely on start_new_session here
    assert "start_new_session" not in popen.call_args.kwargs
    assert state.status == "restarting"


@pytest.mark.asyncio
async def test_migration_path_uses_system_systemd_run_for_system_service(tmp_path):
    svc = _make_service(tmp_path)
    state = _make_state()

    with patch.object(svc, "_systemd_scope", return_value="system"), \
         patch("backend.services.update_service.subprocess.Popen") as popen, \
         patch("backend.services.update_service.asyncio.sleep", new=AsyncMock()):
        await svc._migration_path(state)

    argv = popen.call_args[0][0]
    assert argv[:4] == [svc._tools["sudo"], "-n", svc._tools["systemd-run"], "--collect"]
    assert "--user" not in argv
    assert argv[-1] == "system"


@pytest.mark.asyncio
async def test_migration_path_plain_popen_when_not_managed(tmp_path):
    svc = _make_service(tmp_path)
    state = _make_state()

    with patch.object(svc, "_systemd_scope", return_value=None), \
         patch("backend.services.update_service.subprocess.Popen") as popen, \
         patch("backend.services.update_service.asyncio.sleep", new=AsyncMock()):
        await svc._migration_path(state)

    argv = popen.call_args[0][0]
    assert "systemd-run" not in argv[0]
    assert popen.call_args.kwargs.get("start_new_session") is True


# ---- recover_from_status_file handles interrupted updates ----


def _write_status(svc: UpdateService, status: str, step: str):
    svc._status_file.write_text(json.dumps({
        "status": status,
        "message": "x",
        "step": step,
        "old_commit": "abc",
        "backup_file": "/tmp/b.db",
        "port": 8999,
        "timestamp": "2026-07-16T05:33:40+00:00",
    }))


@pytest.mark.parametrize("status,step", [("stopping", "stop_service"), ("migrating", "alembic_upgrade")])
def test_recover_marks_interrupted_update_failed(tmp_path, status, step):
    svc = _make_service(tmp_path)
    _write_status(svc, status, step)

    svc.recover_from_status_file()

    assert svc._current is not None
    assert svc._current.status == "failed"
    assert "中断" in svc._current.error
    failed = [s for s in svc._current.steps if s.status == "failed"]
    assert [s.name for s in failed] == [step]


@pytest.mark.parametrize("status", ["restarting", "starting"])
def test_recover_marks_restart_completed(tmp_path, status):
    svc = _make_service(tmp_path)
    _write_status(svc, status, "start_service")

    svc.recover_from_status_file()

    assert svc._current is not None
    assert svc._current.status == "completed"


# ---- rollback must never touch the DB while the service is running ----


@pytest.mark.asyncio
async def test_rollback_delegates_to_script_when_managed(tmp_path):
    svc = _make_service(tmp_path)
    svc._current = _make_state()
    backup = tmp_path / "backup.db"
    backup.write_text("db")
    svc._current.backup_file = str(backup)

    with patch.object(svc, "_systemd_scope", return_value="user"), \
         patch("backend.services.update_service.subprocess.Popen") as popen, \
         patch("backend.services.update_service.asyncio.sleep", new=AsyncMock()):
        result = await svc.rollback()

    assert result["status"] == "rolling_back"
    argv = popen.call_args[0][0]
    assert "systemd-run" in Path(argv[0]).name
    assert "rollback" in argv


@pytest.mark.asyncio
async def test_rollback_non_systemd_delegates_to_script_kill_mode(tmp_path):
    svc = _make_service(tmp_path)
    svc._current = _make_state()
    backup = tmp_path / "backup.db"
    backup.write_text("db")
    svc._current.backup_file = str(backup)

    with patch.object(svc, "_systemd_scope", return_value=None), \
         patch("backend.services.update_service.subprocess.Popen") as popen, \
         patch("backend.services.update_service.asyncio.sleep", new=AsyncMock()):
        result = await svc.rollback()

    assert result["status"] == "rolling_back"
    argv = popen.call_args[0][0]
    assert "systemd-run" not in argv[0]
    assert "-" in argv and "rollback" in argv          # kill/respawn mode
    assert str(os.getpid()) in argv                    # pid to kill


# ---- _is_managed_by_systemd answers for THIS process, not the unit ----


def test_is_managed_true_only_when_self_in_service_cgroup(tmp_path):
    svc = _make_service(tmp_path)
    svc._service_name = "ccm.service"  # pin: settings.service_name varies per .env
    with patch.object(svc, "_cgroup_text",
                      return_value="0::/user.slice/user-1000.slice/user@1000.service/app.slice/ccm.service\n"):
        assert svc._is_managed_by_systemd() is True
        assert svc._systemd_scope() == "user"
    with patch.object(svc, "_cgroup_text",
                      return_value="0::/system.slice/ccm.service\n"):
        assert svc._is_managed_by_systemd() is True
        assert svc._systemd_scope() == "system"
    # orphan uvicorn in a login session — unit may be active, but WE are not it
    with patch.object(svc, "_cgroup_text",
                      return_value="0::/user.slice/user-1000.slice/session-19215.scope\n"):
        assert svc._is_managed_by_systemd() is False
    with patch.object(svc, "_cgroup_text", side_effect=FileNotFoundError):
        assert svc._is_managed_by_systemd() is False


def test_migrate_script_rollback_mode(tmp_path):
    """Script rollback mode: stop → restore DB → git reset → start."""
    env, call_log = _script_env(tmp_path)

    project = tmp_path / "proj"
    project.mkdir()
    genv = {**env, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "init", "-q"], cwd=project, check=True, env=genv)
    (project / "f.txt").write_text("v1")
    subprocess.run(["git", "add", "."], cwd=project, check=True, env=genv)
    subprocess.run(["git", "commit", "-qm", "v1"], cwd=project, check=True, env=genv)
    old = subprocess.run(["git", "rev-parse", "HEAD"], cwd=project, capture_output=True, text=True, env=genv).stdout.strip()
    (project / "f.txt").write_text("v2")
    subprocess.run(["git", "commit", "-aqm", "v2"], cwd=project, check=True, env=genv)

    db = tmp_path / "claude_manager.db"
    db.write_text("corrupted")
    backup = tmp_path / "backup.db"
    backup.write_text("good-data")

    subprocess.run(
        ["bash", str(SCRIPT), str(project), old, str(backup), "8999",
         str(db), "ccm.service", "rollback"],
        env=env, check=True, timeout=30,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

    assert db.read_text() == "good-data"
    assert (project / "f.txt").read_text() == "v1"
    calls = call_log.read_text()
    assert "stop ccm.service" in calls and "start ccm.service" in calls
    status = json.loads(Path("/tmp/ccm-update-status-8999.json").read_text())
    assert status["status"] == "rolled_back"


# ---- update_migrate.sh always brings the service back up ----


def _script_env(tmp_path: Path) -> tuple[dict, Path]:
    """Stub service-management tools into PATH; systemctl logs its calls.

    The test runner itself may live under /system.slice. update_migrate.sh can
    then choose system scope and call `sudo -n systemctl ...`, so sudo must be
    stubbed too; otherwise this test can escape the fake PATH and touch real
    systemd units.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    call_log = tmp_path / "systemctl.log"

    systemctl = bin_dir / "systemctl"
    systemctl.write_text(f'#!/bin/bash\necho "$@" >> {call_log}\nexit 0\n')
    sudo = bin_dir / "sudo"
    sudo.write_text('#!/bin/bash\nif [ "${1:-}" = "-n" ]; then shift; fi\nexec "$@"\n')
    # stub uv: alembic hangs so the test can kill the script mid-migration
    uv = bin_dir / "uv"
    uv.write_text('#!/bin/bash\nif [[ "$*" == *alembic* ]]; then sleep 30; fi\nexit 0\n')
    for f in (systemctl, sudo, uv):
        f.chmod(f.stat().st_mode | stat.S_IEXEC)

    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    return env, call_log


def test_migrate_script_bare_uvicorn_mode(tmp_path):
    """SERVICE_NAME='-': stop = kill the given pid, start = respawn uvicorn."""
    env, call_log = _script_env(tmp_path)
    bin_dir = tmp_path / "bin"
    python_stub = bin_dir / "python-stub"
    python_stub.write_text(f'#!/bin/bash\necho "python $@" >> {call_log}\n')
    python_stub.chmod(python_stub.stat().st_mode | stat.S_IEXEC)

    project = tmp_path / "proj"
    project.mkdir()
    genv = {**env, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "init", "-q"], cwd=project, check=True, env=genv)
    (project / "f.txt").write_text("v1")
    subprocess.run(["git", "add", "."], cwd=project, check=True, env=genv)
    subprocess.run(["git", "commit", "-qm", "v1"], cwd=project, check=True, env=genv)
    old = subprocess.run(["git", "rev-parse", "HEAD"], cwd=project, capture_output=True, text=True, env=genv).stdout.strip()

    db = tmp_path / "claude_manager.db"
    db.write_text("corrupted")
    backup = tmp_path / "backup.db"
    backup.write_text("good-data")

    dummy_server = subprocess.Popen(["sleep", "60"])
    try:
        subprocess.run(
            ["bash", str(SCRIPT), str(project), old, str(backup), "8999",
             str(db), "-", "rollback", str(dummy_server.pid), str(python_stub)],
            env=env, check=True, timeout=60,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        assert dummy_server.wait(timeout=5) != 0  # killed by svc_stop
        assert db.read_text() == "good-data"
        calls = call_log.read_text()
        assert "systemctl" not in calls or "ccm.service" not in calls
        assert "python -m uvicorn backend.main:app" in calls  # respawned
    finally:
        if dummy_server.poll() is None:
            dummy_server.kill()


def test_migrate_script_trap_starts_service_even_if_killed(tmp_path):
    """Reproduces the incident: script dies after stopping the service —
    the EXIT trap must still start the service."""
    env, call_log = _script_env(tmp_path)
    (tmp_path / "backup.db").write_text("db")
    project = tmp_path / "proj"
    project.mkdir()

    proc = subprocess.Popen(
        ["bash", str(SCRIPT), str(project), "deadbeef",
         str(tmp_path / "backup.db"), "8999",
         str(tmp_path / "claude_manager.db"), "ccm.service"],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    # wait until it has stopped the service and is stuck in "migration"
    deadline = time.time() + 10
    while time.time() < deadline:
        if call_log.exists() and "stop ccm.service" in call_log.read_text():
            break
        time.sleep(0.1)
    time.sleep(1.5)  # let it pass `sleep 1` and enter the hanging alembic

    proc.send_signal(signal.SIGTERM)
    proc.wait(timeout=10)

    calls = call_log.read_text()
    assert "stop ccm.service" in calls
    assert "start ccm.service" in calls, "EXIT trap must restart the service"


# ---- self-escape trampoline: the script itself must survive old backends ----
# The 2026-07-16 production outage: a pre-systemd-run backend spawned the
# (freshly pulled, already "fixed") script as a plain uvicorn child — inside
# the service cgroup — and its own `systemctl stop` killed it mid-stop.
# Fixing the Python spawn can't reach old deployments (they run their old
# Python), but the script is pulled fresh each update, so it must save itself.


def _self_cgroup_leaf() -> str | None:
    """Leaf name of the current process's cgroup (cgroup v2), e.g.
    'session-42.scope' — lets tests trigger the trampoline's match for real."""
    try:
        text = Path("/proc/self/cgroup").read_text()
    except OSError:
        return None
    path = text.strip().splitlines()[0].split("::", 1)[-1]
    leaf = path.rstrip("/").rsplit("/", 1)[-1]
    return leaf or None


def _stub_systemd_run(tmp_path: Path) -> Path:
    """Replace systemd-run with a logger so the escape is observable."""
    run_log = tmp_path / "systemd-run.log"
    stub = tmp_path / "bin" / "systemd-run"
    stub.write_text(f'#!/bin/bash\necho "$@" >> {run_log}\nexit 0\n')
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    return run_log


def test_migrate_script_escapes_own_service_cgroup(tmp_path):
    """Launched inside the service's own cgroup, the script must re-exec via
    systemd-run and NOT run `systemctl stop` from its doomed position."""
    env, call_log = _script_env(tmp_path)
    leaf = _self_cgroup_leaf()
    if not leaf:
        pytest.skip("cgroup v2 unavailable")
    run_log = _stub_systemd_run(tmp_path)

    project = tmp_path / "proj"
    project.mkdir()
    # SERVICE_NAME = our own cgroup leaf → the in-service detection matches
    result = subprocess.run(
        ["bash", str(SCRIPT), str(project), "deadbeef",
         str(tmp_path / "backup.db"), "8999",
         str(tmp_path / "claude_manager.db"), leaf, "migrate"],
        env=env, timeout=30, capture_output=True, text=True,
    )

    assert result.returncode == 0
    calls = run_log.read_text()
    assert "--setenv=CCM_ESCAPED=1" in calls
    assert str(SCRIPT) in calls, "must re-exec itself by absolute path"
    assert f"--working-directory={project}" in calls, \
        "transient units don't inherit cwd — must be pinned"
    stop_calls = call_log.read_text() if call_log.exists() else ""
    assert "stop" not in stop_calls, "must never stop the service from inside its cgroup"


def test_migrate_script_trampoline_runs_once(tmp_path):
    """CCM_ESCAPED=1 (the re-exec'd copy) must skip the trampoline and proceed
    into the normal flow — no infinite escape loop."""
    env, call_log = _script_env(tmp_path)
    leaf = _self_cgroup_leaf()
    if not leaf:
        pytest.skip("cgroup v2 unavailable")
    run_log = _stub_systemd_run(tmp_path)
    env["CCM_ESCAPED"] = "1"

    # project dir intentionally missing: proceeding past the trampoline means
    # dying at the `cd` guard with exit 1 — proof the escape was skipped
    result = subprocess.run(
        ["bash", str(SCRIPT), str(tmp_path / "missing"), "deadbeef",
         str(tmp_path / "backup.db"), "8999",
         str(tmp_path / "claude_manager.db"), leaf, "migrate"],
        env=env, timeout=30, capture_output=True, text=True,
    )

    assert result.returncode == 1
    assert not run_log.exists(), "escaped copy must not systemd-run again"
