from __future__ import annotations

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import backend.api.codex_pool as codex_pool_api
import backend.api.pool as claude_pool_api
from backend.services import login_runtime


def _configure_runtime(monkeypatch, tmp_path: Path, *, display: str = ":199") -> None:
    monkeypatch.setenv("CCM_XVFB_DISPLAY", display)
    monkeypatch.setenv("CCM_LOGIN_CDP_PORT", "9322")
    monkeypatch.setenv("CCM_LOGIN_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("CCM_LOGIN_TMPDIR", str(tmp_path / "login-tmp"))
    monkeypatch.setenv("CCM_LOGIN_MIN_AVAILABLE_MB", "0")
    monkeypatch.setenv("CCM_LOGIN_MIN_TEMP_FREE_MB", "0")


def test_claude_and_codex_pool_share_one_login_lock():
    assert claude_pool_api._login_lock is login_runtime.login_lock
    assert codex_pool_api._login_lock is login_runtime.login_lock


def test_login_child_environment_uses_configured_isolated_runtime(
    monkeypatch,
    tmp_path,
):
    _configure_runtime(monkeypatch, tmp_path, display=":101")

    env = login_runtime.login_child_environment(extra={"EXTRA": "yes"})

    assert env["DISPLAY"] == ":101"
    assert env["CCM_LOGIN_CDP_PORT"] == "9322"
    assert env["TMPDIR"] == str(tmp_path / "login-tmp")
    assert env["XAUTHORITY"].endswith("display-101.auth")
    assert env["EXTRA"] == "yes"


def test_resource_guard_rejects_low_available_memory(monkeypatch, tmp_path):
    _configure_runtime(monkeypatch, tmp_path)
    monkeypatch.setenv("CCM_LOGIN_MIN_AVAILABLE_MB", "512")

    with pytest.raises(login_runtime.LoginResourceError, match="available memory 77 MiB"):
        login_runtime.ensure_login_capacity(
            temp_dir=login_runtime.login_temp_directory(),
            mem_available_bytes=77 * 1024 * 1024,
        )


def test_xauthority_cookie_is_private_and_not_exposed_in_argv(
    monkeypatch,
    tmp_path,
):
    auth_path = tmp_path / "display.auth"
    run = Mock(return_value=SimpleNamespace(returncode=0))
    monkeypatch.setattr(login_runtime.subprocess, "run", run)
    monkeypatch.setattr(login_runtime.secrets, "token_hex", lambda _size: "cookie")

    login_runtime.XvfbManager._write_xauthority(":199", auth_path)

    assert auth_path.stat().st_mode & 0o777 == 0o600
    assert run.call_args.args[0] == ["xauth", "-f", str(auth_path)]
    assert "cookie" not in run.call_args.args[0]
    assert "MIT-MAGIC-COOKIE-1 cookie" in run.call_args.kwargs["input"]


def test_cached_xvfb_is_polled_before_reuse(monkeypatch, tmp_path):
    _configure_runtime(monkeypatch, tmp_path)
    manager = login_runtime.XvfbManager()
    proc = SimpleNamespace(returncode=None, poll=Mock(), pid=123)
    manager._proc = proc
    monkeypatch.setattr(manager, "_display_ready", lambda *_args: True)

    runtime = manager._ensure_sync()

    proc.poll.assert_called_once()
    assert runtime.display == ":199"


def test_ready_xvfb_from_sibling_process_is_reused_without_popen(
    monkeypatch,
    tmp_path,
):
    _configure_runtime(monkeypatch, tmp_path)
    manager = login_runtime.XvfbManager()
    monkeypatch.setattr(manager, "_display_ready", lambda *_args: True)
    popen = Mock(side_effect=AssertionError("must not start another Xvfb"))
    monkeypatch.setattr(login_runtime.subprocess, "Popen", popen)

    runtime = manager._ensure_sync()

    assert runtime.display == ":199"
    popen.assert_not_called()


def test_foreign_x_socket_is_not_killed_or_replaced(monkeypatch, tmp_path):
    _configure_runtime(monkeypatch, tmp_path)
    manager = login_runtime.XvfbManager()
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    socket_path = tmp_path / "foreign-X199"
    socket_path.touch()
    monkeypatch.setattr(
        manager,
        "_paths",
        lambda _number: (
            runtime_dir / "display.lock",
            runtime_dir / "display.auth",
            runtime_dir / "stderr.log",
            socket_path,
        ),
    )
    monkeypatch.setattr(manager, "_display_ready", lambda *_args: False)
    popen = Mock(side_effect=AssertionError("must not replace a foreign X server"))
    monkeypatch.setattr(login_runtime.subprocess, "Popen", popen)

    with pytest.raises(login_runtime.LoginRuntimeError, match="cannot be authenticated"):
        manager._ensure_sync()

    popen.assert_not_called()


def test_xvfb_start_waits_for_real_display_readiness(monkeypatch, tmp_path):
    _configure_runtime(monkeypatch, tmp_path)
    manager = login_runtime.XvfbManager()
    ready = iter([False, False, True])
    monkeypatch.setattr(manager, "_display_ready", lambda *_args: next(ready))
    monkeypatch.setattr(manager, "_write_xauthority", lambda _display, path: path.touch())
    monkeypatch.setattr(login_runtime.time, "sleep", lambda _seconds: None)

    class FakeProcess:
        pid = 321
        returncode = None

        def poll(self):
            return self.returncode

    popen = Mock(return_value=FakeProcess())
    monkeypatch.setattr(login_runtime.subprocess, "Popen", popen)

    runtime = manager._ensure_sync()

    assert runtime.display == ":199"
    command = popen.call_args.args[0]
    assert command[:2] == ["Xvfb", ":199"]
    assert "-ac" not in command
    assert "-auth" in command
