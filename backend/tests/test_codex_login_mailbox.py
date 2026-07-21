from __future__ import annotations

from collections.abc import Iterable

import pytest

import scripts.codex_login as codex_login


class _Response:
    def __init__(self, payload: dict):
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class _MailClient:
    def __init__(self, responses: Iterable[dict], calls: list[dict]):
        self._responses = iter(responses)
        self._calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def get(self, url: str, **kwargs):
        self._calls.append({"url": url, **kwargs})
        return _Response(next(self._responses))


def _install_mail_client(monkeypatch, responses: list[dict]) -> dict:
    state: dict = {"timeouts": [], "calls": []}

    def factory(*_args, **kwargs):
        state["timeouts"].append(kwargs.get("timeout"))
        return _MailClient(responses, state["calls"])

    monkeypatch.setattr(codex_login.httpx, "AsyncClient", factory)
    monkeypatch.setattr(codex_login, "MAIL_POLL_INTERVAL", 0)
    return state


def _success(*, code: str = "654321", **timestamp_fields) -> dict:
    return {
        "code": 200,
        "message": "success",
        "data": {
            "subject": "Your OpenAI verification code",
            "code": code,
            **timestamp_fields,
        },
    }


def test_detect_mail_provider():
    assert codex_login.detect_mail_provider("user@onet.pl") == "onet"
    assert codex_login.detect_mail_provider("user@GAZETA.PL") == "gazeta"
    assert codex_login.detect_mail_provider("user@israelmail.com") == "171mail"


@pytest.mark.parametrize(
    "timestamp_fields",
    [
        {"date": "2023-11-14T22:13:30+00:00"},
        {"Date": "Tue, 14 Nov 2023 22:13:30 +0000"},
        {"subject": "Your OpenAI verification code | 2023-11-14 22:13:30"},
    ],
    ids=["mailcatcher-lowercase-date", "legacy-uppercase-Date", "subject-fallback"],
)
async def test_mailcatcher_accepts_only_fresh_timestamped_code(monkeypatch, timestamp_fields):
    state = _install_mail_client(monkeypatch, [_success(**timestamp_fields)])

    code = await codex_login.poll_verification_code(
        "platform-query-token",
        after_ts=1_699_999_990,
        timeout_s=1,
        email="user@onet.pl",
        provider="onet",
    )

    assert code == "654321"
    assert state["timeouts"] and state["timeouts"][0] >= 90
    assert state["calls"] == [{
        "url": codex_login.MAIL_DECODE_API,
        "params": {"token": "platform-query-token", "type": "gpt"},
    }]


async def test_mailcatcher_202_is_polled_until_200(monkeypatch):
    state = _install_mail_client(monkeypatch, [
        {"code": 202, "message": "processing"},
        _success(date="2023-11-14T22:13:30+00:00"),
    ])

    code = await codex_login.poll_verification_code(
        "platform-query-token",
        after_ts=1_699_999_990,
        timeout_s=1,
        provider="gazeta",
    )

    assert code == "654321"
    assert len(state["calls"]) == 2


async def test_mailcatcher_invalid_query_token_fails_immediately(monkeypatch):
    state = _install_mail_client(monkeypatch, [
        {"code": 401, "message": "invalid token"},
    ])

    with pytest.raises(RuntimeError, match="rejected.*token|token.*rejected"):
        await codex_login.poll_verification_code(
            "not-a-mailbox-password",
            after_ts=1_699_999_990,
            timeout_s=1,
            provider="onet",
        )

    assert len(state["calls"]) == 1


async def test_mailcatcher_stale_code_is_not_returned(monkeypatch):
    _install_mail_client(monkeypatch, [
        # Even a message one second before the login attempt is stale.  A
        # grace window here can reuse the previous attempt's still-valid OTP.
        _success(code="111111", date="2023-11-14T22:13:19+00:00"),
        _success(code="222222", date="2023-11-14T22:13:21+00:00"),
    ])

    code = await codex_login.poll_verification_code(
        "platform-query-token",
        after_ts=1_700_000_000,
        timeout_s=1,
        provider="onet",
    )

    assert code == "222222"


async def test_mailcatcher_undated_code_times_out_instead_of_being_reused(monkeypatch):
    _install_mail_client(monkeypatch, [_success(code="111111")])
    clock = iter([0.0, 0.0, 2.0])
    monkeypatch.setattr(codex_login.time, "time", lambda: next(clock))

    with pytest.raises(RuntimeError, match="No fresh OpenAI verification code"):
        await codex_login.poll_verification_code(
            "platform-query-token",
            after_ts=0.5,
            timeout_s=1,
            provider="gazeta",
        )
