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

    with patch.object(svc, "_is_managed_by_systemd", return_value=True), \
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
async def test_migration_path_plain_popen_when_not_managed(tmp_path):
    svc = _make_service(tmp_path)
    state = _make_state()

    with patch.object(svc, "_is_managed_by_systemd", return_value=False), \
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


# ---- update_migrate.sh always brings the service back up ----


def _script_env(tmp_path: Path) -> tuple[dict, Path]:
    """Stub systemctl/uv into PATH; systemctl logs its calls."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    call_log = tmp_path / "systemctl.log"

    systemctl = bin_dir / "systemctl"
    systemctl.write_text(f'#!/bin/bash\necho "$@" >> {call_log}\nexit 0\n')
    # stub uv: alembic hangs so the test can kill the script mid-migration
    uv = bin_dir / "uv"
    uv.write_text('#!/bin/bash\nif [[ "$*" == *alembic* ]]; then sleep 30; fi\nexit 0\n')
    for f in (systemctl, uv):
        f.chmod(f.stat().st_mode | stat.S_IEXEC)

    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    return env, call_log


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
