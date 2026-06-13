"""Tests for Worker management API + provisioner state machine."""
import asyncio
import socket
from unittest.mock import AsyncMock

import pytest

import backend.main as main_module
from backend.models.worker import Worker
from backend.services.worker_provisioner import BootstrapError, WorkerProvisioner


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


async def test_create_worker_auto_name_and_background_task(client, fake_provisioner):
    resp = await client.post(
        "/api/workers",
        json={"accounts": [{"email": "a@x.com", "password": "p"}]},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["name"] == f"test-manager-worker-{data['id']}"
    assert data["status"] == "creating"
    assert data["accounts"] == [{"email": "a@x.com", "status": "pending"}]
    await asyncio.sleep(0)  # 让 create_task 跑起来
    fake_provisioner.create_worker.assert_called_once()
    kwargs = fake_provisioner.create_worker.call_args.kwargs
    assert kwargs["accounts"] == [{"email": "a@x.com", "password": "p"}]
    assert kwargs["adopt_instance_id"] is None


async def test_create_worker_adopt_instance(client, fake_provisioner):
    resp = await client.post(
        "/api/workers",
        json={"accounts": [], "adopt_instance_id": "i-abc", "name": "custom"},
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "custom"
    await asyncio.sleep(0)
    assert fake_provisioner.create_worker.call_args.kwargs["adopt_instance_id"] == "i-abc"


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


async def test_provisioner_adopt_happy_path(db_factory, session_factory):
    wid = await _insert_worker(session_factory, status="creating")
    prov = WorkerProvisioner(db_factory=db_factory, cloud=FakeCloud(), broadcaster=None)
    prov._bootstrap = AsyncMock()

    await prov.create_worker(wid, accounts=[], adopt_instance_id="i-adopt")

    async with session_factory() as db:
        w = await db.get(Worker, wid)
    assert w.status == "ready"
    assert w.cloud_instance_id == "i-adopt"
    assert w.private_ip == "10.0.0.9"
    assert w.adopted is True
    assert w.last_heartbeat is not None


async def test_provisioner_bootstrap_failure_records_step(db_factory, session_factory):
    wid = await _insert_worker(session_factory, status="creating")
    prov = WorkerProvisioner(db_factory=db_factory, cloud=FakeCloud(), broadcaster=None)
    prov._bootstrap = AsyncMock(side_effect=BootstrapError("ccm-deploy", "rsync failed"))

    await prov.create_worker(wid, accounts=[], adopt_instance_id="i-adopt")

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


async def test_provisioner_destroy_adopted_only_stops(db_factory, session_factory):
    wid = await _insert_worker(
        session_factory, status="ready", cloud_instance_id="i-x", adopted=True
    )
    cloud = FakeCloud()
    prov = WorkerProvisioner(db_factory=db_factory, cloud=cloud, broadcaster=None)
    await prov.destroy_worker(wid)
    async with session_factory() as db:
        assert (await db.get(Worker, wid)).status == "terminated"
    assert ("stop", "i-x") in cloud.calls
    assert ("terminate", "i-x") not in cloud.calls


async def test_provisioner_destroy_created_terminates(db_factory, session_factory):
    wid = await _insert_worker(
        session_factory, status="ready", cloud_instance_id="i-x", adopted=False
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
