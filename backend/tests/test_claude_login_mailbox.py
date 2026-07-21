from __future__ import annotations

import stat
import sys
import types
from pathlib import Path

import scripts.auto_login as auto_login


def test_detect_claude_mailbox_provider():
    assert auto_login.detect_login_method("user@onet.pl") == "onet"
    assert auto_login.detect_login_method("user@GAZETA.PL") == "gazeta"
    assert auto_login.detect_login_method("user@mail.com") == "mailcom"
    assert auto_login.detect_login_method("user@example.com") == "171mail"


def test_onet_and_gazeta_use_mailcatcher_decode_api():
    assert auto_login.uses_mailcatcher_api("onet") is True
    assert auto_login.uses_mailcatcher_api("gazeta") is True
    assert auto_login.uses_mailcatcher_api("mailcom") is True
    assert auto_login.uses_mailcatcher_api("171mail") is False


def _write_credential(config_dir: Path, name: str, content: bytes, mode: int) -> None:
    path = config_dir / name
    path.write_bytes(content)
    path.chmod(mode)


def _credential_state(config_dir: Path, name: str) -> tuple[bytes, int] | None:
    path = config_dir / name
    if not path.exists():
        return None
    return path.read_bytes(), stat.S_IMODE(path.stat().st_mode)


def _install_cdp_login(monkeypatch, callback) -> None:
    module = types.ModuleType("cdp_login")
    module.cdp_login = callback
    monkeypatch.setitem(sys.modules, "cdp_login", module)


async def test_171mail_exception_restores_credentials_before_cdp(tmp_path, monkeypatch):
    _write_credential(tmp_path, ".claude.json", b"old-claude", 0o640)
    _write_credential(tmp_path, ".credentials.json", b"old-oauth", 0o600)

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

    async def failed_trigger(*_args):
        raise auto_login.MailServiceError("mailbox unavailable")

    monkeypatch.setattr(auto_login.httpx, "AsyncClient", lambda **_kwargs: _Client())
    monkeypatch.setattr(auto_login, "_trigger_send", failed_trigger)

    ok = await auto_login.perform_login(
        email="user@example.com",
        token_171="171mail-token",
        config_dir=str(tmp_path),
        provider="171mail",
    )

    assert ok is False
    assert _credential_state(tmp_path, ".claude.json") == (b"old-claude", 0o640)
    assert _credential_state(tmp_path, ".credentials.json") == (b"old-oauth", 0o600)


async def test_perform_login_false_restores_credentials_content_and_mode(tmp_path, monkeypatch):
    _write_credential(tmp_path, ".claude.json", b"old-claude", 0o640)
    _write_credential(tmp_path, ".credentials.json", b"old-oauth", 0o600)

    async def failed_login(**kwargs):
        config_dir = Path(kwargs["config_dir"])
        _write_credential(config_dir, ".claude.json", b"partial-claude", 0o666)
        _write_credential(config_dir, ".credentials.json", b"partial-oauth", 0o644)
        return {"success": False}

    _install_cdp_login(monkeypatch, failed_login)

    ok = await auto_login.perform_login(
        email="user@onet.pl",
        token_171="platform-query-token",
        config_dir=str(tmp_path),
        provider="onet",
    )

    assert ok is False
    assert _credential_state(tmp_path, ".claude.json") == (b"old-claude", 0o640)
    assert _credential_state(tmp_path, ".credentials.json") == (b"old-oauth", 0o600)


async def test_perform_login_false_leaves_no_partial_credentials_when_none_existed(
    tmp_path, monkeypatch,
):
    async def failed_login(**kwargs):
        config_dir = Path(kwargs["config_dir"])
        _write_credential(config_dir, ".claude.json", b"partial-claude", 0o600)
        _write_credential(config_dir, ".credentials.json", b"partial-oauth", 0o600)
        return None

    _install_cdp_login(monkeypatch, failed_login)

    ok = await auto_login.perform_login(
        email="user@gazeta.pl",
        token_171="platform-query-token",
        config_dir=str(tmp_path),
        provider="gazeta",
    )

    assert ok is False
    assert _credential_state(tmp_path, ".claude.json") is None
    assert _credential_state(tmp_path, ".credentials.json") is None


async def test_perform_login_exception_restores_exact_previous_state(tmp_path, monkeypatch):
    _write_credential(tmp_path, ".claude.json", b"old-claude", 0o600)
    _write_credential(tmp_path, ".credentials.json", b"old-oauth", 0o640)

    async def crashing_login(**kwargs):
        config_dir = Path(kwargs["config_dir"])
        _write_credential(config_dir, ".claude.json", b"partial-claude", 0o666)
        _write_credential(config_dir, ".credentials.json", b"partial-oauth", 0o666)
        raise RuntimeError("browser crashed")

    _install_cdp_login(monkeypatch, crashing_login)

    ok = await auto_login.perform_login(
        email="user@onet.pl",
        token_171="platform-query-token",
        config_dir=str(tmp_path),
        provider="onet",
    )

    assert ok is False
    assert _credential_state(tmp_path, ".claude.json") == (b"old-claude", 0o600)
    assert _credential_state(tmp_path, ".credentials.json") == (b"old-oauth", 0o640)


async def test_perform_login_success_keeps_new_credentials_and_platform_token(
    tmp_path, monkeypatch,
):
    _write_credential(tmp_path, ".claude.json", b"old-claude", 0o600)
    _write_credential(tmp_path, ".credentials.json", b"old-oauth", 0o600)
    captured: dict = {}

    async def successful_login(**kwargs):
        captured.update(kwargs)
        config_dir = Path(kwargs["config_dir"])
        _write_credential(config_dir, ".claude.json", b"new-claude", 0o640)
        _write_credential(config_dir, ".credentials.json", b"new-oauth", 0o600)
        return {"success": True}

    _install_cdp_login(monkeypatch, successful_login)

    ok = await auto_login.perform_login(
        email="user@onet.pl",
        token_171="platform-query-token",
        config_dir=str(tmp_path),
        provider="onet",
    )

    assert ok is True
    assert captured["token"] == "platform-query-token"
    assert captured["mail_provider"] == "onet"
    assert _credential_state(tmp_path, ".claude.json") == (b"new-claude", 0o640)
    assert _credential_state(tmp_path, ".credentials.json") == (b"new-oauth", 0o600)
