"""Integration contracts for provider-aware Worker account management.

These tests keep all traffic local and mocked.  They specifically lock down the
boundary where the Manager sends Codex credentials through SSH stdin to the
Worker-local CCM API, rather than duplicating Codex login state machinery.
"""

from __future__ import annotations

import asyncio
import base64
import json
import shlex
from types import SimpleNamespace
from unittest.mock import AsyncMock, call

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

import backend.api.deps as api_deps
import backend.api.workers as workers_api
import backend.main as main_module
import backend.services.worker_provisioner as worker_provisioner_module
from backend.models.worker import Worker
from backend.services.worker_provisioner import WorkerProvisioner


async def _insert_worker(session_factory, **fields) -> Worker:
    fields.setdefault("status", "ready")
    fields.setdefault("private_ip", "10.0.0.9")
    fields.setdefault("ssh_user", "ubuntu")
    fields.setdefault("ssh_key_path", "/tmp/worker-key")
    fields.setdefault("auth_token", "worker-auth-token")
    async with session_factory() as db:
        worker = Worker(name="codex-worker", **fields)
        db.add(worker)
        await db.commit()
        await db.refresh(worker)
        return worker


async def _drain_worker_background_tasks() -> None:
    while workers_api._background_tasks:
        await asyncio.gather(*tuple(workers_api._background_tasks))


async def test_login_codex_account_posts_credentials_and_polls_to_success(
    db_factory, monkeypatch,
):
    provisioner = WorkerProvisioner(db_factory, cloud=object())
    worker = Worker(
        name="remote-worker",
        private_ip="10.0.0.9",
        ssh_user="ubuntu",
        auth_token="worker-token",
        ccm_port=8000,
    )
    local_api = AsyncMock(side_effect=[
        {"status": "running", "account_id": "codex-7"},
        {"status": "finalizing"},
        {"status": "success"},
    ])
    provisioner.worker_local_api = local_api
    sleep = AsyncMock()
    monkeypatch.setattr(worker_provisioner_module.asyncio, "sleep", sleep)

    account_id = await provisioner.login_codex_account(
        worker,
        {
            "email": "codex+worker@example.com",
            "token": "  mailbox-token  ",
            "password": "  exact OpenAI password  ",
            "login_method": "mailcatcher",
        },
    )

    assert account_id == "codex-7"
    assert local_api.await_args_list == [
        call(
            worker,
            "POST",
            "/api/codex-pool/add",
            payload={
                "email": "codex+worker@example.com",
                "token": "mailbox-token",
                "password": "  exact OpenAI password  ",
                "login_method": "mailcatcher",
            },
            timeout=45,
        ),
        call(
            worker,
            "GET",
            "/api/codex-pool/add/codex%2Bworker%40example.com",
            timeout=30,
        ),
        call(
            worker,
            "GET",
            "/api/codex-pool/add/codex%2Bworker%40example.com",
            timeout=30,
        ),
    ]
    assert sleep.await_count == 2


async def test_ensure_codex_account_reuses_existing_slot_without_add(db_factory):
    provisioner = WorkerProvisioner(db_factory, cloud=object())
    worker = Worker(
        name="remote-worker",
        private_ip="10.0.0.9",
        ssh_user="ubuntu",
        auth_token="worker-token",
        ccm_port=8000,
    )
    provisioner.worker_local_api = AsyncMock(side_effect=[
        {
            "accounts": [{
                "id": "codex-1",
                "email": "codex@example.com",
                "enabled": True,
            }],
        },
        {"logged_in": True, "email": "codex@example.com"},
    ])
    provisioner.login_codex_account = AsyncMock()

    account_id = await provisioner.ensure_codex_account(worker, {
        "account_id": "codex-1",
        "email": "codex@example.com",
        "token": "mail-token",
        "password": "password",
    })

    assert account_id == "codex-1"
    provisioner.login_codex_account.assert_not_awaited()
    assert provisioner.worker_local_api.await_args_list == [
        call(worker, "GET", "/api/codex-pool/status", timeout=30),
        call(
            worker,
            "GET",
            "/api/codex-pool/accounts/codex-1/verify?live=true",
            timeout=30,
        ),
    ]


async def test_ensure_codex_account_relogs_existing_broken_slot(
    db_factory, monkeypatch,
):
    provisioner = WorkerProvisioner(db_factory, cloud=object())
    worker = Worker(
        name="remote-worker",
        private_ip="10.0.0.9",
        ssh_user="ubuntu",
        auth_token="worker-token",
        ccm_port=8000,
    )
    provisioner.worker_local_api = AsyncMock(side_effect=[
        {"accounts": [{"id": "codex-1", "email": "codex@example.com"}]},
        {"logged_in": False, "detail": "auth.json missing"},
        {"status": "running", "attempt_id": "retry-attempt"},
        {"status": "success"},
    ])
    sleep = AsyncMock()
    monkeypatch.setattr(worker_provisioner_module.asyncio, "sleep", sleep)

    account_id = await provisioner.ensure_codex_account(worker, {
        "account_id": "codex-1",
        "email": "codex@example.com",
        "token": "mail-token",
        "password": "password",
    })

    assert account_id == "codex-1"
    assert provisioner.worker_local_api.await_args_list[-2:] == [
        call(
            worker,
            "POST",
            "/api/codex-pool/accounts/codex-1/relogin",
            timeout=45,
        ),
        call(
            worker,
            "GET",
            "/api/codex-pool/accounts/codex-1/relogin",
            timeout=30,
        ),
    ]


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (
            {
                "status": "awaiting_otp",
                "attempt_id": "attempt-1",
                "challenge_id": "challenge-1",
            },
            "人工输入邮箱验证码",
        ),
        ({"status": "failed", "detail": "OAuth callback failed"}, "OAuth callback failed"),
    ],
)
async def test_login_codex_account_surfaces_otp_and_terminal_failure(
    db_factory, response, message,
):
    provisioner = WorkerProvisioner(db_factory, cloud=object())
    worker = Worker(
        name="remote-worker",
        private_ip="10.0.0.9",
        ssh_user="ubuntu",
        auth_token="worker-token",
        ccm_port=8000,
    )
    provisioner.worker_local_api = AsyncMock(return_value=response)

    with pytest.raises(RuntimeError, match=message):
        await provisioner.login_codex_account(
            worker,
            {
                "email": "codex@example.com",
                "token": "mailbox-token",
                "password": "",
                "login_method": "171mail",
            },
        )

    if response["status"] == "awaiting_otp":
        assert provisioner.worker_local_api.await_count == 2
        assert provisioner.worker_local_api.await_args_list[-1] == call(
            worker,
            "DELETE",
            "/api/codex-pool/login-attempts/attempt-1",
            timeout=45,
        )
    else:
        provisioner.worker_local_api.assert_awaited_once()


async def test_step_account_login_keeps_codex_and_historical_claude_slots_independent(
    db_factory, session_factory,
):
    worker = await _insert_worker(session_factory, status="creating", accounts=[])
    provisioner = WorkerProvisioner(db_factory, cloud=object())
    provisioner._log = AsyncMock()
    provisioner.ensure_codex_account = AsyncMock(return_value="codex-4")
    ssh = AsyncMock()
    ssh.run.side_effect = [(0, "uploaded"), (0, "claude login ok")]
    codex_password = "  opaque password  "
    codex_token = "codex-mailbox-token"

    await provisioner._step_account_login(
        ssh,
        worker.id,
        [
            {
                "email": "codex@example.com",
                "provider": "codex",
                "token": codex_token,
                "password": codex_password,
                "login_method": "mailcatcher",
            },
            {
                # Missing provider is a historical Claude-only record. Codex
                # must not consume or renumber its independent Claude slot.
                "email": "legacy-claude@example.com",
                "token": "claude-mail-token",
                "login_method": "onet",
            },
        ],
    )

    codex_call = provisioner.ensure_codex_account.await_args
    assert codex_call.args[0].id == worker.id
    assert codex_call.args[1] == {
        "email": "codex@example.com",
        "provider": "codex",
        "token": codex_token,
        "password": codex_password,
        "login_method": "mailcatcher",
    }

    assert ssh.run.await_count == 2
    upload_command = ssh.run.await_args_list[0].args[0]
    upload_argv = shlex.split(upload_command)
    encoded_script = upload_argv[upload_argv.index("%s") + 1]
    login_script = base64.b64decode(encoded_script).decode()
    login_argv = shlex.split(login_script[login_script.index("uv run "):])
    assert login_argv[login_argv.index("--add-to-pool") + 1] == "default"

    async with session_factory() as db:
        persisted = await db.get(Worker, worker.id)
    assert persisted.accounts == [
        {
            "email": "codex@example.com",
            "token": codex_token,
            "password": codex_password,
            "provider": "codex",
            "login_method": "mailcatcher",
            "status": "logged_in",
            "account_id": "codex-4",
        },
        {
            "email": "legacy-claude@example.com",
            "token": "claude-mail-token",
            "password": "",
            "provider": "claude",
            "login_method": "onet",
            "status": "logged_in",
            "account_id": "default",
        },
    ]


async def test_worker_local_api_sends_bearer_and_payload_only_through_ssh_stdin(
    db_factory,
):
    provisioner = WorkerProvisioner(db_factory, cloud=object())
    worker = Worker(
        name="remote-worker",
        private_ip="10.0.0.9",
        ssh_user="ubuntu",
        ssh_key_path="/tmp/worker-key",
        auth_token="worker-auth-token",
        ccm_port=8123,
    )
    ssh = AsyncMock()
    ssh.run_with_input.return_value = (0, '{"ok": true, "status": "running"}')
    provisioner._ssh = lambda _worker: ssh
    payload = {
        "email": "stdin-only@example.com",
        "token": "stdin-only-mailbox-token",
        "password": "stdin-only-openai-password",
        "login_method": "171mail",
    }

    result = await provisioner.worker_local_api(
        worker,
        "POST",
        "/api/codex-pool/add",
        payload=payload,
        timeout=41,
    )

    assert result == {"ok": True, "status": "running"}
    ssh.run.assert_not_awaited()
    ssh.run_with_input.assert_awaited_once()
    command, input_data = ssh.run_with_input.await_args.args
    envelope = json.loads(input_data)
    assert envelope == {
        "url": "http://127.0.0.1:8123/api/codex-pool/add",
        "method": "POST",
        "timeout": 41,
        "auth_token": "worker-auth-token",
        "has_payload": True,
        "payload": payload,
    }
    assert all(str(value) not in command for value in payload.values())
    assert "worker-auth-token" not in command
    assert "/api/codex-pool/add" not in command
    assert command.startswith("python3 -c ")
    assert "ProxyHandler({})" in command
    assert ssh.run_with_input.await_args.kwargs == {
        "timeout": 46,
        "sensitive": True,
    }


class _JSONResponse:
    def __init__(self, body: dict, status_code: int = 200):
        self._body = body
        self.status_code = status_code
        self.text = json.dumps(body)

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "remote failure",
                request=httpx.Request("GET", "http://worker"),
                response=httpx.Response(self.status_code),
            )


async def test_worker_codex_pool_status_usage_and_delete_use_codex_paths(
    client, session_factory, monkeypatch,
):
    worker = await _insert_worker(session_factory, accounts=[{
        "email": "deleted@example.com",
        "provider": "codex",
        "token": "mail-token-that-must-be-erased",
        "password": "password-that-must-be-erased",
        "login_method": "mailcatcher",
        "status": "logged_in",
        "account_id": "codex+1",
    }])
    requests: list[tuple[str, str, dict]] = []

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            self.timeout = kwargs.get("timeout")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url, **kwargs):
            requests.append(("GET", url, kwargs))
            kind = "usage" if "/usage" in url else "status"
            return _JSONResponse({"kind": kind, "accounts": []})

        async def delete(self, url, **kwargs):
            requests.append(("DELETE", url, kwargs))
            return _JSONResponse({"ok": True})

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)

    status = await client.get(f"/api/workers/{worker.id}/pool?provider=codex")
    usage = await client.get(f"/api/workers/{worker.id}/pool/usage?provider=codex")
    deleted = await client.delete(
        f"/api/workers/{worker.id}/pool/codex%2B1?provider=codex"
    )

    assert status.status_code == 200
    assert status.json()["kind"] == "status"
    assert usage.status_code == 200
    assert usage.json()["kind"] == "usage"
    assert deleted.status_code == 200
    assert deleted.json() == {"ok": True}
    assert [(method, url) for method, url, _ in requests] == [
        ("GET", "http://10.0.0.9:8000/api/codex-pool/status"),
        ("GET", "http://10.0.0.9:8000/api/codex-pool/usage?force=true"),
        ("DELETE", "http://10.0.0.9:8000/api/codex-pool/accounts/codex%2B1"),
    ]
    for _method, _url, kwargs in requests:
        assert kwargs["headers"] == {
            "Authorization": "Bearer worker-auth-token"
        }

    async with session_factory() as db:
        persisted = await db.get(Worker, worker.id)
        assert persisted.accounts == []
        persisted.status = "error"
        await db.commit()

    # A later bootstrap retry must not resurrect the remotely deleted account
    # or retain its token/password in Manager DB.
    provisioner = AsyncMock()
    monkeypatch.setattr(main_module, "worker_provisioner", provisioner)
    retried = await client.post(f"/api/workers/{worker.id}/retry")
    assert retried.status_code == 200
    await worker_provisioner_module.asyncio.sleep(0)
    provisioner.create_worker.assert_awaited_once_with(worker.id, accounts=[])


async def test_dynamic_codex_add_coalesces_case_insensitive_active_email(
    client, session_factory, monkeypatch,
):
    worker = await _insert_worker(session_factory, accounts=[])
    provisioner = WorkerProvisioner(session_factory, cloud=object())
    login_entered = asyncio.Event()
    release_login = asyncio.Event()

    async def blocked_login(_worker, _account, **_kwargs):
        login_entered.set()
        await release_login.wait()
        return "codex-1"

    provisioner.ensure_codex_account = AsyncMock(side_effect=blocked_login)
    monkeypatch.setattr(main_module, "worker_provisioner", provisioner)
    state_key = f"{worker.id}:codex:same.email@example.com"
    workers_api._worker_login_state.pop(state_key, None)

    first = await client.post(
        f"/api/workers/{worker.id}/pool/add",
        json={
            "email": "Same.Email@Example.com",
            "provider": "codex",
            "token": "first-mail-token",
        },
    )
    await asyncio.wait_for(login_entered.wait(), timeout=1)
    second = await client.post(
        f"/api/workers/{worker.id}/pool/add",
        json={
            "email": "same.email@example.com",
            "provider": "codex",
            "token": "must-not-overwrite-first-token",
        },
    )

    try:
        assert first.status_code == 200, first.text
        assert second.status_code == 200, second.text
        assert second.json()["status"] == "running"
        provisioner.ensure_codex_account.assert_awaited_once()
    finally:
        release_login.set()
        await _drain_worker_background_tasks()

    async with session_factory() as db:
        persisted = await db.get(Worker, worker.id)
    assert persisted.accounts == [{
        "email": "Same.Email@Example.com",
        "provider": "codex",
        "token": "first-mail-token",
        "password": "",
        "login_method": "",
        "account_id": "codex-1",
        "status": "logged_in",
    }]
    workers_api._worker_login_state.pop(state_key, None)


@pytest.mark.parametrize("login_status", ["running", "cancelling"])
async def test_worker_account_delete_rejects_active_codex_login(
    client, session_factory, monkeypatch, login_status,
):
    account = {
        "email": "active-delete@example.com",
        "provider": "codex",
        "token": "mail-token",
        "password": "",
        "login_method": "",
        "account_id": "codex-8",
        "status": "pending",
    }
    worker = await _insert_worker(session_factory, accounts=[account])
    state_key = f"{worker.id}:codex:active-delete@example.com"
    workers_api._worker_login_state[state_key] = {
        "status": login_status,
        "provider": "codex",
        "attempt_id": "active-delete-attempt",
    }
    remote_delete = AsyncMock()
    monkeypatch.setattr(workers_api, "_worker_http_request", remote_delete)

    try:
        response = await client.delete(
            f"/api/workers/{worker.id}/pool/codex-8?provider=codex"
        )
    finally:
        workers_api._worker_login_state.pop(state_key, None)

    assert response.status_code == 409
    assert "登录仍在进行中" in response.json()["detail"]
    remote_delete.assert_not_awaited()
    async with session_factory() as db:
        assert (await db.get(Worker, worker.id)).accounts == [account]


async def test_remote_terminal_login_stays_active_until_failure_is_persisted(
    client, session_factory, monkeypatch,
):
    worker = await _insert_worker(session_factory, accounts=[])
    provisioner = WorkerProvisioner(session_factory, cloud=object())
    remote_terminal_published = asyncio.Event()
    release_failure = asyncio.Event()

    async def terminal_then_block(_worker, _account, **kwargs):
        await kwargs["on_status"]({
            "status": "failed",
            "attempt_id": "terminal-race-attempt",
            "detail": "remote browser failed",
        })
        remote_terminal_published.set()
        await release_failure.wait()
        raise RuntimeError("remote browser failed")

    provisioner.ensure_codex_account = AsyncMock(side_effect=terminal_then_block)
    remote_delete = AsyncMock()
    monkeypatch.setattr(main_module, "worker_provisioner", provisioner)
    monkeypatch.setattr(workers_api, "_worker_http_request", remote_delete)
    state_key = f"{worker.id}:codex:terminal-race@example.com"
    workers_api._worker_login_state.pop(state_key, None)

    added = await client.post(
        f"/api/workers/{worker.id}/pool/add",
        json={
            "email": "terminal-race@example.com",
            "provider": "codex",
            "token": "terminal-race-mail-token",
        },
    )
    await asyncio.wait_for(remote_terminal_published.wait(), timeout=1)
    blocked_delete = await client.delete(
        f"/api/workers/{worker.id}/pool/codex-1?provider=codex"
    )

    try:
        assert added.status_code == 200, added.text
        assert workers_api._worker_login_state[state_key]["status"] == "finalizing"
        assert blocked_delete.status_code == 409
        remote_delete.assert_not_awaited()
    finally:
        release_failure.set()
        await _drain_worker_background_tasks()

    assert workers_api._worker_login_state[state_key]["status"] == "failed"
    async with session_factory() as db:
        persisted = await db.get(Worker, worker.id)
    assert persisted.accounts == [{
        "email": "terminal-race@example.com",
        "provider": "codex",
        "token": "terminal-race-mail-token",
        "password": "",
        "login_method": "",
        "status": "failed",
    }]
    workers_api._worker_login_state.pop(state_key, None)


async def test_cancelled_dynamic_codex_login_blocks_immediate_second_ensure(
    client, session_factory, monkeypatch,
):
    worker = await _insert_worker(session_factory, accounts=[])
    provisioner = WorkerProvisioner(session_factory, cloud=object())
    login_entered = asyncio.Event()
    release_login = asyncio.Event()

    async def awaiting_cancel(_worker, _account, **kwargs):
        await kwargs["on_status"]({
            "status": "awaiting_otp",
            "attempt_id": "cancel-attempt",
            "challenge_id": "cancel-challenge",
        })
        login_entered.set()
        await release_login.wait()
        raise RuntimeError("remote login cancelled")

    provisioner.ensure_codex_account = AsyncMock(side_effect=awaiting_cancel)
    provisioner.worker_local_api = AsyncMock(return_value={
        "ok": True,
        "status": "cancelled",
    })
    monkeypatch.setattr(main_module, "worker_provisioner", provisioner)
    state_key = f"{worker.id}:codex:cancel-retry@example.com"
    workers_api._worker_login_state.pop(state_key, None)

    first = await client.post(
        f"/api/workers/{worker.id}/pool/add",
        json={
            "email": "cancel-retry@example.com",
            "provider": "codex",
            "token": "first-mail-token",
        },
    )
    await asyncio.wait_for(login_entered.wait(), timeout=1)
    cancelled = await client.delete(
        f"/api/workers/{worker.id}/pool/login-attempts/cancel-attempt"
    )
    immediate_retry = await client.post(
        f"/api/workers/{worker.id}/pool/add",
        json={
            "email": "CANCEL-RETRY@example.com",
            "provider": "codex",
            "token": "must-not-start-a-second-login",
        },
    )

    try:
        assert first.status_code == 200, first.text
        assert cancelled.status_code == 200, cancelled.text
        assert immediate_retry.status_code == 200, immediate_retry.text
        assert immediate_retry.json()["status"] == "cancelling"
        provisioner.ensure_codex_account.assert_awaited_once()
    finally:
        release_login.set()
        await _drain_worker_background_tasks()

    assert workers_api._worker_login_state[state_key]["status"] == "failed"
    async with session_factory() as db:
        persisted = await db.get(Worker, worker.id)
    assert persisted.accounts == [{
        "email": "cancel-retry@example.com",
        "provider": "codex",
        "token": "first-mail-token",
        "password": "",
        "login_method": "",
        "status": "failed",
    }]
    workers_api._worker_login_state.pop(state_key, None)


async def test_destroyed_worker_rejects_late_dynamic_codex_persistence(
    client, session_factory, monkeypatch,
):
    worker = await _insert_worker(
        session_factory,
        accounts=[],
        cloud_instance_id="i-active-login",
    )
    cloud = AsyncMock()
    provisioner = WorkerProvisioner(session_factory, cloud=cloud)
    login_entered = asyncio.Event()
    release_login = asyncio.Event()

    async def late_remote_success(_worker, _account, **_kwargs):
        login_entered.set()
        await release_login.wait()
        return "codex-late"

    provisioner.ensure_codex_account = AsyncMock(side_effect=late_remote_success)
    monkeypatch.setattr(main_module, "worker_provisioner", provisioner)
    state_key = f"{worker.id}:codex:destroy-race@example.com"
    workers_api._worker_login_state.pop(state_key, None)

    response = await client.post(
        f"/api/workers/{worker.id}/pool/add",
        json={
            "email": "destroy-race@example.com",
            "provider": "codex",
            "token": "mail-token-must-not-return",
            "password": "openai-password-must-not-return",
        },
    )
    await asyncio.wait_for(login_entered.wait(), timeout=1)
    await provisioner.destroy_worker(worker.id)

    async with session_factory() as db:
        after_destroy = await db.get(Worker, worker.id)
    try:
        assert response.status_code == 200, response.text
        assert after_destroy.status == "terminated"
        assert after_destroy.auth_token is None
        assert after_destroy.accounts == [{
            "email": "destroy-race@example.com",
            "provider": "codex",
            "status": "pending",
        }]
    finally:
        release_login.set()
        await _drain_worker_background_tasks()

    async with session_factory() as db:
        after_late_callback = await db.get(Worker, worker.id)
    assert after_late_callback.status == "terminated"
    assert after_late_callback.auth_token is None
    assert after_late_callback.accounts == after_destroy.accounts
    serialized_accounts = json.dumps(after_late_callback.accounts)
    assert "mail-token-must-not-return" not in serialized_accounts
    assert "openai-password-must-not-return" not in serialized_accounts
    assert workers_api._worker_login_state[state_key]["status"] == "failed"
    assert "persistence rejected while terminated" in (
        workers_api._worker_login_state[state_key]["detail"]
    )
    workers_api._worker_login_state.pop(state_key, None)


async def test_destroy_winning_after_account_snapshot_rejects_stale_secret_write(
    session_factory, monkeypatch,
):
    worker = await _insert_worker(
        session_factory,
        accounts=[],
        cloud_instance_id="i-account-write-race",
    )
    cloud = AsyncMock()
    provisioner = WorkerProvisioner(session_factory, cloud=cloud)
    snapshot_released = asyncio.Event()
    release_stale_write = asyncio.Event()
    original_rollback = AsyncSession.rollback

    async def rollback_with_barrier(session):
        await original_rollback(session)
        task = asyncio.current_task()
        if task is not None and task.get_name() == "stale-account-persist":
            snapshot_released.set()
            await release_stale_write.wait()

    monkeypatch.setattr(AsyncSession, "rollback", rollback_with_barrier)
    account = {
        "email": "destroy-write-race@example.com",
        "provider": "codex",
        "token": "mail-token-must-stay-erased",
        "password": "password-must-stay-erased",
        "login_method": "",
    }
    persist_task = asyncio.create_task(
        workers_api._persist_worker_account_state(
            provisioner,
            worker.id,
            account,
            status="failed",
        ),
        name="stale-account-persist",
    )
    await asyncio.wait_for(snapshot_released.wait(), timeout=1)
    await provisioner.destroy_worker(worker.id)
    release_stale_write.set()

    with pytest.raises(RuntimeError, match="rejected while terminated"):
        await persist_task

    async with session_factory() as db:
        persisted = await db.get(Worker, worker.id)
    assert persisted.status == "terminated"
    assert persisted.accounts == []
    serialized = json.dumps(persisted.accounts)
    assert "mail-token-must-stay-erased" not in serialized
    assert "password-must-stay-erased" not in serialized


async def test_destroy_winning_after_delete_snapshot_rejects_stale_account_write(
    session_factory, monkeypatch,
):
    accounts = [
        {
            "email": "delete-me@example.com",
            "provider": "codex",
            "token": "deleted-mail-token",
            "password": "deleted-password",
            "login_method": "",
            "account_id": "codex-1",
            "status": "logged_in",
        },
        {
            "email": "survivor@example.com",
            "provider": "codex",
            "token": "survivor-mail-token-must-stay-erased",
            "password": "survivor-password-must-stay-erased",
            "login_method": "",
            "account_id": "codex-2",
            "status": "logged_in",
        },
    ]
    worker = await _insert_worker(
        session_factory,
        accounts=accounts,
        cloud_instance_id="i-delete-write-race",
    )
    cloud = AsyncMock()
    provisioner = WorkerProvisioner(session_factory, cloud=cloud)
    snapshot_released = asyncio.Event()
    release_stale_delete = asyncio.Event()
    original_rollback = AsyncSession.rollback
    barrier_used = False

    async def rollback_with_barrier(session):
        nonlocal barrier_used
        await original_rollback(session)
        task = asyncio.current_task()
        if (
            not barrier_used
            and task is not None
            and task.get_name() == "stale-account-delete"
        ):
            barrier_used = True
            snapshot_released.set()
            await release_stale_delete.wait()

    monkeypatch.setattr(AsyncSession, "rollback", rollback_with_barrier)
    remote_delete = AsyncMock()
    monkeypatch.setattr(workers_api, "_worker_http_request", remote_delete)
    request = SimpleNamespace(
        state=SimpleNamespace(user_id=None, user_role="super_admin")
    )
    async with session_factory() as route_db:
        delete_task = asyncio.create_task(
            workers_api.delete_worker_account(
                worker.id,
                request,
                "codex-1",
                provider="codex",
                db=route_db,
            ),
            name="stale-account-delete",
        )
        await asyncio.wait_for(snapshot_released.wait(), timeout=1)
        try:
            await provisioner.destroy_worker(worker.id)
        finally:
            release_stale_delete.set()
        with pytest.raises(HTTPException) as rejected:
            await delete_task

    assert rejected.value.status_code == 409
    remote_delete.assert_not_awaited()
    async with session_factory() as db:
        persisted = await db.get(Worker, worker.id)
    assert persisted.status == "terminated"
    serialized = json.dumps(persisted.accounts)
    assert "survivor-mail-token-must-stay-erased" not in serialized
    assert "survivor-password-must-stay-erased" not in serialized
    assert persisted.accounts == [
        {
            "email": "delete-me@example.com",
            "provider": "codex",
            "status": "logged_in",
            "account_id": "codex-1",
        },
        {
            "email": "survivor@example.com",
            "provider": "codex",
            "status": "logged_in",
            "account_id": "codex-2",
        },
    ]


async def test_delete_holds_admission_until_remote_slot_is_gone(
    client, session_factory, monkeypatch,
):
    worker = await _insert_worker(
        session_factory,
        accounts=[{
            "email": "replace-after-delete@example.com",
            "provider": "codex",
            "token": "old-mail-token",
            "password": "old-password",
            "login_method": "",
            "account_id": "codex-1",
            "status": "logged_in",
        }],
    )
    provisioner = WorkerProvisioner(session_factory, cloud=object())
    provisioner.ensure_codex_account = AsyncMock(return_value="codex-1")
    monkeypatch.setattr(main_module, "worker_provisioner", provisioner)
    remote_delete_entered = asyncio.Event()
    release_remote_delete = asyncio.Event()

    async def blocked_remote_delete(*_args, **_kwargs):
        remote_delete_entered.set()
        await release_remote_delete.wait()
        return _JSONResponse({"ok": True})

    remote_delete = AsyncMock(side_effect=blocked_remote_delete)
    monkeypatch.setattr(workers_api, "_worker_http_request", remote_delete)
    state_key = f"{worker.id}:codex:replace-after-delete@example.com"
    workers_api._worker_login_state.pop(state_key, None)

    delete_task = asyncio.create_task(client.delete(
        f"/api/workers/{worker.id}/pool/codex-1?provider=codex"
    ))
    await asyncio.wait_for(remote_delete_entered.wait(), timeout=1)
    add_task = asyncio.create_task(client.post(
        f"/api/workers/{worker.id}/pool/add",
        json={
            "email": "replace-after-delete@example.com",
            "provider": "codex",
            "token": "new-mail-token",
            "password": "new-password",
        },
    ))
    await asyncio.sleep(0)

    try:
        assert not add_task.done()
        provisioner.ensure_codex_account.assert_not_awaited()
    finally:
        release_remote_delete.set()

    deleted, added = await asyncio.gather(delete_task, add_task)
    await _drain_worker_background_tasks()
    assert deleted.status_code == 200, deleted.text
    assert added.status_code == 200, added.text
    provisioner.ensure_codex_account.assert_awaited_once()
    async with session_factory() as db:
        persisted = await db.get(Worker, worker.id)
    assert persisted.accounts == [{
        "email": "replace-after-delete@example.com",
        "provider": "codex",
        "token": "new-mail-token",
        "password": "new-password",
        "login_method": "",
        "account_id": "codex-1",
        "status": "logged_in",
    }]
    workers_api._worker_login_state.pop(state_key, None)


async def test_dynamic_codex_add_resumes_persisted_pending_credentials(
    client, session_factory, monkeypatch,
):
    persisted_account = {
        "email": "pending@example.com",
        "provider": "codex",
        "token": "known-good-mail-token",
        "password": "known-good-openai-password",
        "login_method": "mailcatcher",
        "account_id": "codex-4",
        "status": "pending",
    }
    worker = await _insert_worker(
        session_factory,
        accounts=[persisted_account],
    )
    provisioner = WorkerProvisioner(session_factory, cloud=object())
    provisioner.ensure_codex_account = AsyncMock(return_value="codex-4")
    monkeypatch.setattr(main_module, "worker_provisioner", provisioner)
    state_key = f"{worker.id}:codex:pending@example.com"
    workers_api._worker_login_state.pop(state_key, None)

    response = await client.post(
        f"/api/workers/{worker.id}/pool/add",
        json={
            "email": "PENDING@example.com",
            "provider": "codex",
            "token": "replacement-form-token",
            "password": "replacement-form-password",
        },
    )
    await _drain_worker_background_tasks()

    assert response.status_code == 200, response.text
    submitted_account = provisioner.ensure_codex_account.await_args.args[1]
    assert submitted_account["token"] == "known-good-mail-token"
    assert submitted_account["password"] == "known-good-openai-password"
    assert submitted_account["account_id"] == "codex-4"
    async with session_factory() as db:
        persisted = await db.get(Worker, worker.id)
    assert persisted.accounts == [{
        **persisted_account,
        "status": "logged_in",
    }]
    workers_api._worker_login_state.pop(state_key, None)


async def test_dynamic_codex_add_rejects_already_logged_in_email(
    client, session_factory, monkeypatch,
):
    account = {
        "email": "existing@example.com",
        "provider": "codex",
        "token": "existing-mail-token",
        "password": "existing-openai-password",
        "login_method": "mailcatcher",
        "account_id": "codex-2",
        "status": "logged_in",
    }
    worker = await _insert_worker(session_factory, accounts=[account])
    provisioner = WorkerProvisioner(session_factory, cloud=object())
    provisioner.ensure_codex_account = AsyncMock()
    monkeypatch.setattr(main_module, "worker_provisioner", provisioner)
    state_key = f"{worker.id}:codex:existing@example.com"
    workers_api._worker_login_state.pop(state_key, None)

    response = await client.post(
        f"/api/workers/{worker.id}/pool/add",
        json={
            "email": "EXISTING@example.com",
            "provider": "codex",
            "token": "new-token",
        },
    )

    assert response.status_code == 409
    assert "已在 Worker 号池" in response.json()["detail"]
    provisioner.ensure_codex_account.assert_not_awaited()
    assert state_key not in workers_api._worker_login_state
    async with session_factory() as db:
        assert (await db.get(Worker, worker.id)).accounts == [account]


async def test_dynamic_codex_remote_success_is_failed_when_local_commit_fails(
    client, session_factory, monkeypatch,
):
    worker = await _insert_worker(session_factory, accounts=[])
    provisioner = WorkerProvisioner(session_factory, cloud=object())

    async def remote_success(_worker, _account, **kwargs):
        await kwargs["on_status"]({
            "status": "success",
            "account_id": "codex-9",
        })
        return "codex-9"

    provisioner.ensure_codex_account = AsyncMock(side_effect=remote_success)
    monkeypatch.setattr(main_module, "worker_provisioner", provisioner)
    original_persist = workers_api._persist_worker_account_state

    async def fail_final_commit(*args, **kwargs):
        if kwargs.get("status") == "logged_in":
            raise RuntimeError("manager account commit failed")
        return await original_persist(*args, **kwargs)

    monkeypatch.setattr(
        workers_api,
        "_persist_worker_account_state",
        fail_final_commit,
    )
    state_key = f"{worker.id}:codex:commit-failure@example.com"
    workers_api._worker_login_state.pop(state_key, None)

    response = await client.post(
        f"/api/workers/{worker.id}/pool/add",
        json={
            "email": "commit-failure@example.com",
            "provider": "codex",
            "token": "mail-token",
        },
    )
    await _drain_worker_background_tasks()
    status = await client.get(
        f"/api/workers/{worker.id}/pool/add/commit-failure%40example.com"
        "?provider=codex"
    )

    assert response.status_code == 200, response.text
    assert status.status_code == 200
    assert status.json()["status"] == "failed"
    assert "manager account commit failed" in status.json()["detail"]
    async with session_factory() as db:
        persisted = await db.get(Worker, worker.id)
    assert persisted.accounts == [{
        "email": "commit-failure@example.com",
        "provider": "codex",
        "token": "mail-token",
        "password": "",
        "login_method": "",
        "account_id": "codex-9",
        "status": "failed",
    }]
    workers_api._worker_login_state.pop(state_key, None)


async def test_worker_account_delete_remote_404_still_clears_local_credentials(
    client, session_factory, monkeypatch,
):
    worker = await _insert_worker(session_factory, accounts=[{
        "email": "already-absent@example.com",
        "provider": "codex",
        "token": "mail-token-that-must-be-erased",
        "password": "password-that-must-be-erased",
        "login_method": "mailcatcher",
        "status": "logged_in",
        "account_id": "codex-3",
    }])

    class MissingAccountClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def delete(self, _url, **_kwargs):
            return _JSONResponse({"detail": "not found"}, status_code=404)

    monkeypatch.setattr(httpx, "AsyncClient", MissingAccountClient)

    response = await client.delete(
        f"/api/workers/{worker.id}/pool/codex-3?provider=codex"
    )

    assert response.status_code == 200, response.text
    assert response.json() == {"ok": True, "already_absent": True}
    async with session_factory() as db:
        assert (await db.get(Worker, worker.id)).accounts == []


async def test_worker_add_status_requires_worker_access(
    client, session_factory, monkeypatch,
):
    worker = await _insert_worker(session_factory, owner_user_id=42)
    state_key = f"{worker.id}:codex:private@example.com"
    workers_api._worker_login_state[state_key] = {
        "status": "failed",
        "detail": "private login detail",
    }
    access_guard = AsyncMock(
        side_effect=HTTPException(status_code=403, detail="No access to this worker")
    )
    monkeypatch.setattr(api_deps, "require_worker_access", access_guard)
    try:
        response = await client.get(
            f"/api/workers/{worker.id}/pool/add/private%40example.com"
            "?provider=codex"
        )
    finally:
        workers_api._worker_login_state.pop(state_key, None)

    assert response.status_code == 403
    assert response.json()["detail"] == "No access to this worker"
    access_guard.assert_awaited_once()
