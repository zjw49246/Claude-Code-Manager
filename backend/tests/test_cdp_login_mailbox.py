import datetime
import io
from pathlib import Path

import pytest

from scripts import cdp_login as login_module


class FakeProcess:
    def __init__(self, *, running: bool = True, pid: int = 1234):
        self.pid = pid
        self.returncode = None if running else 1
        self.stdout = io.BytesIO()
        self.kill_calls = 0
        self.wait_calls = 0

    def poll(self):
        return self.returncode

    def kill(self):
        self.kill_calls += 1
        self.returncode = -9

    def wait(self, timeout=None):
        self.wait_calls += 1
        return self.returncode


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class FakeHttpClient:
    def __init__(self, payload, **_kwargs):
        self.payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def get(self, *_args, **_kwargs):
        return FakeResponse(self.payload)


async def _no_sleep(_seconds):
    return None


def _temporary_auth_dir(monkeypatch, tmp_path: Path) -> Path:
    auth_dir = tmp_path / "temporary-auth"

    def fake_mkdtemp(**_kwargs):
        auth_dir.mkdir()
        return str(auth_dir)

    monkeypatch.setattr(login_module.tempfile, "mkdtemp", fake_mkdtemp)
    return auth_dir


def test_temporary_auth_commit_copies_all_credential_files(tmp_path):
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    (source / ".credentials.json").write_text('{"token": "new"}')
    (source / ".claude.json").write_text('{"onboarding": true}')

    login_module._commit_temporary_credentials(source, target)

    assert (target / ".credentials.json").read_bytes() == b'{"token": "new"}'
    assert (target / ".claude.json").read_bytes() == b'{"onboarding": true}'
    assert (target / ".credentials.json").stat().st_mode & 0o777 == 0o600
    assert (target / ".claude.json").stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_preflight_without_oauth_url_reaps_cli_and_removes_temp_dir(
    monkeypatch, tmp_path
):
    auth_dir = _temporary_auth_dir(monkeypatch, tmp_path)
    cli = FakeProcess()
    monkeypatch.setattr(login_module.subprocess, "Popen", lambda *_a, **_k: cli)

    async def no_oauth_url(*_args):
        return None, b"preflight failed"

    monkeypatch.setattr(login_module, "_wait_cli_oauth_url", no_oauth_url)

    result = await login_module.cdp_login(
        "user@onet.pl", "query-token", str(tmp_path / "account"), mail_provider="onet"
    )

    assert result is None
    assert cli.kill_calls == 1
    assert cli.wait_calls == 1
    assert not auth_dir.exists()


@pytest.mark.asyncio
async def test_chrome_launch_exception_cleans_preflight_and_stderr(monkeypatch, tmp_path):
    auth_dir = _temporary_auth_dir(monkeypatch, tmp_path)
    cli = FakeProcess()
    chrome_stderr = io.StringIO()
    calls = 0

    def fake_popen(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return cli
        raise RuntimeError("chrome failed to launch")

    async def oauth_url(*_args):
        return "https://claude.com/cai/oauth/authorize?state=test", b""

    monkeypatch.setattr(login_module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(login_module.subprocess, "run", lambda *_a, **_k: None)
    monkeypatch.setattr(login_module.asyncio, "sleep", _no_sleep)
    monkeypatch.setattr(login_module, "_wait_cli_oauth_url", oauth_url)
    monkeypatch.setattr(login_module, "open", lambda *_a, **_k: chrome_stderr, raising=False)

    with pytest.raises(RuntimeError, match="chrome failed"):
        await login_module.cdp_login(
            "user@onet.pl", "query-token", str(tmp_path / "account"), mail_provider="onet"
        )

    assert cli.kill_calls == 1
    assert cli.wait_calls == 1
    assert chrome_stderr.closed
    assert not auth_dir.exists()


@pytest.mark.asyncio
async def test_chrome_early_exit_is_reaped_and_stderr_is_closed(monkeypatch, tmp_path):
    chrome = FakeProcess(running=False)
    chrome_stderr = io.StringIO()
    monkeypatch.setattr(login_module.subprocess, "Popen", lambda *_a, **_k: chrome)
    monkeypatch.setattr(login_module.subprocess, "run", lambda *_a, **_k: None)
    monkeypatch.setattr(login_module.asyncio, "sleep", _no_sleep)
    monkeypatch.setattr(login_module, "open", lambda *_a, **_k: chrome_stderr, raising=False)

    result = await login_module.cdp_login(
        "user@example.com", "query-token", str(tmp_path / "account")
    )

    assert result is None
    assert chrome.kill_calls == 0
    assert chrome.wait_calls == 1
    assert chrome_stderr.closed


@pytest.mark.asyncio
async def test_chrome_uses_configured_port_and_disk_profile(monkeypatch, tmp_path):
    chrome = FakeProcess(running=False)
    chrome_stderr = io.StringIO()
    popen_calls = []

    def fake_popen(command, **kwargs):
        popen_calls.append((command, kwargs))
        return chrome

    login_tmp = tmp_path / "disk-login-tmp"
    monkeypatch.setenv("CCM_LOGIN_TMPDIR", str(login_tmp))
    monkeypatch.setenv("CCM_LOGIN_CDP_PORT", "9322")
    monkeypatch.setattr(login_module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(login_module.asyncio, "sleep", _no_sleep)
    monkeypatch.setattr(login_module, "open", lambda *_a, **_k: chrome_stderr, raising=False)

    result = await login_module.cdp_login(
        "user@example.com", "query-token", str(tmp_path / "account")
    )

    assert result is None
    command = popen_calls[0][0]
    assert "--remote-debugging-port=9322" in command
    profile_arg = next(arg for arg in command if arg.startswith("--user-data-dir="))
    assert profile_arg.startswith(f"--user-data-dir={login_tmp}/")
    assert not any(part == "pkill" for part in command)


@pytest.mark.asyncio
@pytest.mark.parametrize("tabs", [[], [{"type": "service_worker"}]])
async def test_missing_page_tab_reaps_chrome_and_closes_stderr(
    monkeypatch, tmp_path, tabs
):
    chrome = FakeProcess()
    chrome_stderr = io.StringIO()
    monkeypatch.setattr(login_module.subprocess, "Popen", lambda *_a, **_k: chrome)
    monkeypatch.setattr(login_module.subprocess, "run", lambda *_a, **_k: None)
    monkeypatch.setattr(login_module.asyncio, "sleep", _no_sleep)
    monkeypatch.setattr(login_module, "open", lambda *_a, **_k: chrome_stderr, raising=False)
    monkeypatch.setattr(
        login_module.httpx,
        "AsyncClient",
        lambda **kwargs: FakeHttpClient(tabs, **kwargs),
    )

    result = await login_module.cdp_login(
        "user@example.com", "query-token", str(tmp_path / "account")
    )

    assert result is None
    assert chrome.kill_calls == 1
    assert chrome.wait_calls == 1
    assert chrome_stderr.closed


@pytest.mark.asyncio
async def test_websocket_exception_still_reaps_chrome(monkeypatch, tmp_path):
    chrome = FakeProcess()
    chrome_stderr = io.StringIO()
    monkeypatch.setattr(login_module.subprocess, "Popen", lambda *_a, **_k: chrome)
    monkeypatch.setattr(login_module.subprocess, "run", lambda *_a, **_k: None)
    monkeypatch.setattr(login_module.asyncio, "sleep", _no_sleep)
    monkeypatch.setattr(login_module, "open", lambda *_a, **_k: chrome_stderr, raising=False)
    monkeypatch.setattr(
        login_module.httpx,
        "AsyncClient",
        lambda **kwargs: FakeHttpClient(
            [{"type": "page", "webSocketDebuggerUrl": "ws://test"}], **kwargs
        ),
    )

    class FailingWebsocket:
        async def __aenter__(self):
            raise RuntimeError("websocket failed")

        async def __aexit__(self, *_args):
            return None

    monkeypatch.setattr(login_module.websockets, "connect", lambda *_a, **_k: FailingWebsocket())

    with pytest.raises(RuntimeError, match="websocket failed"):
        await login_module.cdp_login(
            "user@example.com", "query-token", str(tmp_path / "account")
        )

    assert chrome.kill_calls == 1
    assert chrome.wait_calls == 1
    assert chrome_stderr.closed


@pytest.mark.asyncio
async def test_magic_link_freshness_prefers_lowercase_payload_date(monkeypatch):
    now = datetime.datetime.now(datetime.timezone.utc)
    payload = {
        "code": 200,
        "data": {
            "date": now.isoformat(),
            "subject": "Claude login | 2000-01-01 00:00:00",
            "code": "https://claude.ai/magic-link#fresh",
        },
    }
    monkeypatch.setattr(
        login_module.httpx,
        "AsyncClient",
        lambda **kwargs: FakeHttpClient(payload, **kwargs),
    )

    link = await login_module.poll_mailcatcher_magic_link(
        "query-token", now.timestamp(), timeout_s=1
    )

    assert link == "https://claude.ai/magic-link#fresh"
