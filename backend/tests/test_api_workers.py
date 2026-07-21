"""Tests for Worker management API + provisioner state machine."""
import asyncio
import base64
import logging
import shlex
import socket
from unittest.mock import AsyncMock

import pytest

import backend.main as main_module
from backend.api.workers import _build_add_account_command
from backend.models.worker import Worker
from backend.services.worker_provisioner import (
    BootstrapError,
    WorkerProvisioner,
    _build_account_login_script,
    _build_script_upload_command,
)
from backend.services.ssh_executor import SSHExecutor


# === Fixtures ===


@pytest.fixture
def fake_provisioner(monkeypatch):
    prov = AsyncMock()
    prov.cloud = AsyncMock()
    prov.cloud.self_describe.return_value = {"name": "test-manager"}
    monkeypatch.setattr(main_module, "worker_provisioner", prov)
    return prov


async def _insert_worker(session_factory, **fields) -> int:
    fields.setdefault("status", "ready")
    async with session_factory() as db:
        worker = Worker(name="test-worker", **fields)
        db.add(worker)
        await db.commit()
        await db.refresh(worker)
        return worker.id


class FakeCloud:
    """最小 CloudProvider 替身。"""

    def __init__(self):
        self.calls = []

    async def self_describe(self):
        return {"instance_type": "t3.large"}

    async def describe_instance(self, iid):
        self.calls.append(("describe", iid))
        return {"instance_id": iid, "state": "stopped", "private_ip": "10.0.0.9",
                "public_ip": None, "name": "x"}

    async def create_instance(self, name, overrides=None):
        self.calls.append(("create", name))
        return "i-new123"

    async def wait_until_running(self, iid, timeout=300):
        self.calls.append(("wait", iid))
        return "10.0.0.9"

    async def stop_instance(self, iid):
        self.calls.append(("stop", iid))

    async def start_instance(self, iid):
        self.calls.append(("start", iid))

    async def terminate_instance(self, iid):
        self.calls.append(("terminate", iid))


# === API tests ===


async def test_list_workers_empty(client):
    resp = await client.get("/api/workers")
    assert resp.status_code == 200
    assert resp.json() == []


async def test_create_worker_disabled_returns_503(client, monkeypatch):
    monkeypatch.setattr(main_module, "worker_provisioner", None)
    resp = await client.post("/api/workers", json={"accounts": []})
    assert resp.status_code == 503


async def test_create_worker_auto_name_and_background_task(
    client, fake_provisioner, session_factory,
):
    resp = await client.post(
        "/api/workers",
        json={
            "name": "test-w",
            "accounts": [{
                "email": "a@x.com",
                "token": "tok123",
                "login_method": "onet",
            }],
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["name"] == "test-w"
    assert data["status"] == "creating"
    assert data["accounts"] == [{"email": "a@x.com", "status": "pending"}]
    await asyncio.sleep(0)  # 让 create_task 跑起来
    fake_provisioner.create_worker.assert_called_once()
    kwargs = fake_provisioner.create_worker.call_args.kwargs
    assert kwargs["accounts"] == [{
        "email": "a@x.com",
        "token": "tok123",
        "login_method": "onet",
    }]
    async with session_factory() as db:
        worker = await db.get(Worker, data["id"])
    assert worker.accounts == [{
        "email": "a@x.com",
        "token": "tok123",
        "login_method": "onet",
        "status": "pending",
    }]


@pytest.mark.parametrize("token", [None, "", " \n "])
async def test_create_worker_rejects_empty_account_token(
    client, fake_provisioner, token,
):
    resp = await client.post(
        "/api/workers",
        json={"name": "test-w", "accounts": [{"email": "a@x.com", "token": token}]},
    )
    assert resp.status_code == 400
    assert "token" in resp.json()["detail"]
    fake_provisioner.create_worker.assert_not_called()


async def test_stop_requires_ready(client, session_factory, fake_provisioner):
    wid = await _insert_worker(session_factory, status="stopped")
    resp = await client.post(f"/api/workers/{wid}/stop")
    assert resp.status_code == 409


async def test_stop_start_destroy_flow(client, session_factory, fake_provisioner, monkeypatch, db_factory):
    import backend.api.workers as workers_api
    # destroy 链路走 _migrate_back_then_destroy（Phase 3），mock 掉让它直接调 prov.destroy_worker
    async def _simple_destroy(prov, worker_id, db_factory_arg=None):
        await prov.destroy_worker(worker_id)
    monkeypatch.setattr(workers_api, "_migrate_back_then_destroy", _simple_destroy)
    wid = await _insert_worker(session_factory, status="ready")
    assert (await client.post(f"/api/workers/{wid}/stop")).status_code == 200
    await asyncio.sleep(0)
    fake_provisioner.stop_worker.assert_called_once_with(wid)

    async with session_factory() as db:
        (await db.get(Worker, wid)).status = "stopped"
        await db.commit()
    assert (await client.post(f"/api/workers/{wid}/start")).status_code == 200
    await asyncio.sleep(0)
    fake_provisioner.start_worker.assert_called_once_with(wid)

    assert (await client.post(f"/api/workers/{wid}/destroy")).status_code == 200
    # destroy 现在走 _migrate_back_then_destroy → prov.destroy_worker，
    # 需要更多 event loop ticks 让后台任务完成
    for _ in range(50):
        await asyncio.sleep(0)
    fake_provisioner.destroy_worker.assert_called_once_with(wid)


async def test_retry_only_from_error(client, session_factory, fake_provisioner):
    wid = await _insert_worker(session_factory, status="ready")
    assert (await client.post(f"/api/workers/{wid}/retry")).status_code == 409

    async with session_factory() as db:
        (await db.get(Worker, wid)).status = "error"
        await db.commit()
    assert (await client.post(f"/api/workers/{wid}/retry")).status_code == 200


async def test_retry_preserves_account_token_and_login_method(
    client, session_factory, fake_provisioner,
):
    wid = await _insert_worker(
        session_factory,
        status="error",
        accounts=[{
            "email": "onet@example.com",
            "token": "mailcatcher-token",
            "login_method": "onet",
            "status": "failed",
        }],
    )
    resp = await client.post(f"/api/workers/{wid}/retry")
    assert resp.status_code == 200, resp.text
    assert resp.json()["accounts"] == [{"email": "onet@example.com", "status": "failed"}]
    await asyncio.sleep(0)
    fake_provisioner.create_worker.assert_awaited_once_with(
        wid,
        accounts=[{
            "email": "onet@example.com",
            "token": "mailcatcher-token",
            "login_method": "onet",
        }],
    )


async def test_retry_missing_historical_token_fails_without_status_change(
    client, session_factory, fake_provisioner,
):
    wid = await _insert_worker(
        session_factory,
        status="error",
        accounts=[{"email": "legacy@example.com", "status": "failed"}],
    )
    resp = await client.post(f"/api/workers/{wid}/retry")
    assert resp.status_code == 409
    assert "缺少 token" in resp.json()["detail"]
    async with session_factory() as db:
        worker = await db.get(Worker, wid)
    assert worker.status == "error"
    fake_provisioner.create_worker.assert_not_called()


async def test_get_worker_and_logs(client, session_factory, fake_provisioner):
    wid = await _insert_worker(session_factory, bootstrap_log="[00:00:00] hi\n")
    resp = await client.get(f"/api/workers/{wid}")
    assert resp.status_code == 200
    resp = await client.get(f"/api/workers/{wid}/logs")
    assert resp.json()["bootstrap_log"] == "[00:00:00] hi\n"
    assert (await client.get("/api/workers/999")).status_code == 404


async def test_terminated_workers_hidden_from_list(client, session_factory):
    await _insert_worker(session_factory, status="terminated")
    wid = await _insert_worker(session_factory, status="ready")
    resp = await client.get("/api/workers")
    assert [w["id"] for w in resp.json()] == [wid]


# === Provisioner state machine tests（cloud/bootstrap 全替身，不碰网络） ===


def _flag_value(argv: list[str], flag: str) -> str:
    return argv[argv.index(flag) + 1]


def test_worker_login_commands_quote_untrusted_values():
    email = "mail+'; touch /tmp/pwn; #@example.com"
    token = "line-one\n$(touch /tmp/pwn) ' \" end"
    remote_dir = "/srv/ccm dir; touch /tmp/pwn"
    script = _build_account_login_script(
        remote_dir,
        email=email,
        token=token,
        slot="account-2",
        login_method="gazeta",
    )
    assert shlex.split(script.splitlines()[3]) == ["cd", remote_dir]
    login_argv = shlex.split(script[script.index("uv run "):])
    assert _flag_value(login_argv, "--email") == email
    assert _flag_value(login_argv, "--token") == token
    assert _flag_value(login_argv, "--add-to-pool") == "account-2"
    assert _flag_value(login_argv, "--login-method") == "gazeta"
    assert _flag_value(login_argv, "--config-dir") == "$CONFIG_DIR"

    upload = _build_script_upload_command(script, "/tmp/a script.sh")
    upload_argv = shlex.split(upload)
    encoded = upload_argv[upload_argv.index("%s") + 1]
    assert base64.b64decode(encoded).decode() == script
    assert email not in upload
    assert token not in upload
    assert "<<" not in upload

    add_command = _build_add_account_command(
        remote_dir,
        email=email,
        token=token,
        slot="default",
        login_method="onet",
    )
    pieces = add_command.split(" && ")
    assert shlex.split(pieces[0]) == ["cd", remote_dir]
    add_argv = shlex.split(pieces[-1])
    assert _flag_value(add_argv, "--email") == email
    assert _flag_value(add_argv, "--token") == token
    assert _flag_value(add_argv, "--login-method") == "onet"


async def test_sensitive_ssh_command_is_redacted_from_debug_log(monkeypatch, caplog):
    ssh = SSHExecutor(host="worker.internal", user="ubuntu", key_path="/tmp/test-key")
    monkeypatch.setattr(ssh, "_run_sync", lambda _command, _timeout: (0, "ok"))

    with caplog.at_level(logging.DEBUG, logger="backend.services.ssh_executor"):
        result = await ssh.run("login --token super-secret-token", sensitive=True)

    assert result == (0, "ok")
    assert "super-secret-token" not in caplog.text
    assert "sensitive command redacted" in caplog.text


async def test_provisioner_ccm_config_marks_embedded_auth_token_sensitive(
    db_factory, session_factory,
):
    wid = await _insert_worker(
        session_factory,
        status="creating",
        auth_token="worker-super-secret-token",
    )
    prov = WorkerProvisioner(db_factory=db_factory, cloud=FakeCloud(), broadcaster=None)
    ssh = AsyncMock()
    ssh.run.return_value = (0, "ok")

    await prov._step_ccm_config(ssh, wid)

    command = ssh.run.await_args.args[0]
    assert "worker-super-secret-token" in command
    assert ssh.run.await_args.kwargs["sensitive"] is True


async def test_provisioner_login_persists_credentials_and_onet_method(
    db_factory, session_factory,
):
    wid = await _insert_worker(session_factory, status="creating")
    prov = WorkerProvisioner(db_factory=db_factory, cloud=FakeCloud(), broadcaster=None)
    ssh = AsyncMock()
    ssh.run.side_effect = [(0, "uploaded"), (0, "login ok")]
    account = {
        "email": "user+'quote@example.com",
        "token": "secret\n'; echo injected",
        "login_method": "onet",
    }

    await prov._step_account_login(ssh, wid, [account])

    async with session_factory() as db:
        worker = await db.get(Worker, wid)
    assert worker.accounts == [{**account, "status": "logged_in"}]
    upload_command = ssh.run.await_args_list[0].args[0]
    assert ssh.run.await_args_list[0].kwargs["sensitive"] is True
    assert account["email"] not in upload_command
    assert account["token"] not in upload_command
    uploaded_argv = shlex.split(upload_command)
    uploaded_script = base64.b64decode(
        uploaded_argv[uploaded_argv.index("%s") + 1]
    ).decode()
    login_argv = shlex.split(uploaded_script[uploaded_script.index("uv run "):])
    assert _flag_value(login_argv, "--email") == account["email"]
    assert _flag_value(login_argv, "--token") == account["token"]
    assert _flag_value(login_argv, "--login-method") == "onet"


async def test_provisioner_login_rejects_empty_token_before_ssh(
    db_factory, session_factory,
):
    wid = await _insert_worker(session_factory, status="creating")
    prov = WorkerProvisioner(db_factory=db_factory, cloud=FakeCloud(), broadcaster=None)
    ssh = AsyncMock()
    with pytest.raises(BootstrapError, match="缺少 token"):
        await prov._step_account_login(
            ssh,
            wid,
            [{"email": "legacy@example.com", "token": "", "login_method": "onet"}],
        )
    ssh.run.assert_not_awaited()


async def test_provisioner_create_happy_path(db_factory, session_factory):
    wid = await _insert_worker(session_factory, status="creating")
    prov = WorkerProvisioner(db_factory=db_factory, cloud=FakeCloud(), broadcaster=None)
    prov._bootstrap = AsyncMock()

    await prov.create_worker(wid, accounts=[])

    async with session_factory() as db:
        w = await db.get(Worker, wid)
    assert w.status == "ready"
    assert w.cloud_instance_id == "i-new123"
    assert w.private_ip == "10.0.0.9"
    assert w.last_heartbeat is not None


async def test_provisioner_bootstrap_failure_records_step(db_factory, session_factory):
    wid = await _insert_worker(session_factory, status="creating")
    prov = WorkerProvisioner(db_factory=db_factory, cloud=FakeCloud(), broadcaster=None)
    prov._bootstrap = AsyncMock(side_effect=BootstrapError("ccm-deploy", "rsync failed"))

    await prov.create_worker(wid, accounts=[])

    async with session_factory() as db:
        w = await db.get(Worker, wid)
    assert w.status == "error"
    assert w.bootstrap_step == "ccm-deploy"
    assert "rsync failed" in w.bootstrap_error
    assert "FAILED" in w.bootstrap_log


async def test_provisioner_stop_and_start(db_factory, session_factory):
    wid = await _insert_worker(
        session_factory, status="ready", cloud_instance_id="i-x", private_ip="10.0.0.9"
    )
    cloud = FakeCloud()
    prov = WorkerProvisioner(db_factory=db_factory, cloud=cloud, broadcaster=None)
    prov._ssh = lambda w: AsyncMock()  # 跳过真实 SSH

    await prov.stop_worker(wid)
    async with session_factory() as db:
        assert (await db.get(Worker, wid)).status == "stopped"
    assert ("stop", "i-x") in cloud.calls

    prov._step_ssh_wait = AsyncMock()
    prov._step_health_check = AsyncMock()
    await prov.start_worker(wid)
    async with session_factory() as db:
        w = await db.get(Worker, wid)
    assert w.status == "ready"
    assert ("start", "i-x") in cloud.calls


async def test_provisioner_destroy_created_terminates(db_factory, session_factory):
    wid = await _insert_worker(
        session_factory, status="ready", cloud_instance_id="i-x"
    )
    cloud = FakeCloud()
    prov = WorkerProvisioner(db_factory=db_factory, cloud=cloud, broadcaster=None)
    await prov.destroy_worker(wid)
    assert ("terminate", "i-x") in cloud.calls


async def test_health_check_marks_error_and_recovers(db_factory, session_factory, monkeypatch):
    wid = await _insert_worker(
        session_factory, status="ready", private_ip="10.0.0.9", auth_token="t"
    )
    prov = WorkerProvisioner(db_factory=db_factory, cloud=FakeCloud(), broadcaster=None)

    class FailClient:
        def __init__(self, *a, **k): ...
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, *a, **k): raise ConnectionError("down")

    import backend.services.worker_provisioner as wp
    monkeypatch.setattr(wp.httpx, "AsyncClient", FailClient)
    fail_counts: dict[int, int] = {}
    for _ in range(3):
        await prov._health_check_once(fail_counts)
    async with session_factory() as db:
        w = await db.get(Worker, wid)
    assert w.status == "error"

    class OkResp:
        status_code = 200
        def raise_for_status(self): ...
        def json(self): return {"status": "ok", "commit": "abc123"}

    class OkClient(FailClient):
        async def get(self, *a, **k): return OkResp()

    monkeypatch.setattr(wp.httpx, "AsyncClient", OkClient)
    await prov._health_check_once(fail_counts)
    async with session_factory() as db:
        w = await db.get(Worker, wid)
    assert w.status == "ready"
    assert w.ccm_commit == "abc123"
    assert w.bootstrap_error is None


async def test_stop_worker_without_instance_goes_stopped(db_factory, session_factory):
    """bootstrap 在开机前失败的 worker：stop 不应卡死在 stopping。"""
    wid = await _insert_worker(session_factory, status="error", cloud_instance_id=None)
    prov = WorkerProvisioner(db_factory=db_factory, cloud=FakeCloud(), broadcaster=None)
    await prov.stop_worker(wid)
    async with session_factory() as db:
        assert (await db.get(Worker, wid)).status == "stopped"


async def test_stop_worker_failure_goes_error_not_stuck(db_factory, session_factory):
    wid = await _insert_worker(session_factory, status="ready", cloud_instance_id="i-x")
    cloud = FakeCloud()

    async def boom(iid):
        raise RuntimeError("ec2 down")

    cloud.stop_instance = boom
    prov = WorkerProvisioner(db_factory=db_factory, cloud=cloud, broadcaster=None)
    prov._ssh = lambda w: AsyncMock()
    await prov.stop_worker(wid)
    async with session_factory() as db:
        w = await db.get(Worker, wid)
    assert w.status == "error"
    assert "关机失败" in w.bootstrap_error


async def test_health_check_does_not_whitewash_bootstrap_error(db_factory, session_factory, monkeypatch):
    """bootstrap 失败（step 非 None）的 error 不能因服务恰好活着被自动洗白。"""
    wid = await _insert_worker(
        session_factory, status="error", private_ip="10.0.0.9", auth_token="t",
        bootstrap_step="account-login", bootstrap_error="全部账号登录失败",
    )
    prov = WorkerProvisioner(db_factory=db_factory, cloud=FakeCloud(), broadcaster=None)

    class OkResp:
        status_code = 200
        def raise_for_status(self): ...
        def json(self): return {"status": "ok", "commit": "abc"}

    class OkClient:
        def __init__(self, *a, **k): ...
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, *a, **k): return OkResp()

    import backend.services.worker_provisioner as wp
    monkeypatch.setattr(wp.httpx, "AsyncClient", OkClient)
    await prov._health_check_once({})
    async with session_factory() as db:
        w = await db.get(Worker, wid)
    assert w.status == "error"  # 不自动恢复
    assert w.bootstrap_error == "全部账号登录失败"


async def test_stop_endpoint_sets_transitional_status_sync(client, session_factory, fake_provisioner):
    """双击防护：第一发同步置 stopping，第二发 409。"""
    wid = await _insert_worker(session_factory, status="ready")
    r1 = await client.post(f"/api/workers/{wid}/stop")
    assert r1.status_code == 200
    assert r1.json()["status"] == "stopping"
    r2 = await client.post(f"/api/workers/{wid}/stop")
    assert r2.status_code == 409


async def test_git_head_commit_deploy_file_fallback(tmp_path):
    """rsync 部署不带 .git：git_head_commit 回退读 .deploy_commit。"""
    from backend.services.git_info import git_head_commit
    (tmp_path / ".deploy_commit").write_text("abc123def\n")
    assert git_head_commit(str(tmp_path)) == "abc123def"
    # 既无 git 也无文件 → ""
    assert git_head_commit(str(tmp_path / "nonexistent")) == ""
