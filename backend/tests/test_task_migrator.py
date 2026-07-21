"""Phase 3 测试：TaskMigrator 状态机 / PUT 触发迁移 / 销毁批量迁回。"""
from pathlib import PurePosixPath
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import update

import backend.main as main_module
import backend.services.task_migrator as task_migrator_module
from backend.models.task import Task
from backend.models.worker import Worker
from backend.services.task_migrator import MigrationError, TaskMigrator


class FakeRelay:
    def __init__(self):
        self.subscribed: list[tuple[int, int]] = []
        self.unsubscribed: list[tuple[int, int]] = []

    async def subscribe_task(self, worker, task_id):
        self.subscribed.append((worker.id, task_id))

    def unsubscribe_task(self, worker_id, task_id):
        self.unsubscribed.append((worker_id, task_id))


async def _mk_worker(session_factory, **fields) -> Worker:
    fields.setdefault("status", "ready")
    fields.setdefault("private_ip", "10.0.0.9")
    fields.setdefault("auth_token", "t")
    async with session_factory() as db:
        w = Worker(name=fields.pop("name", "w"), **fields)
        db.add(w)
        await db.commit()
        await db.refresh(w)
        return w


async def _mk_task(session_factory, **fields) -> Task:
    fields.setdefault("status", "completed")
    fields.setdefault("description", "d")
    async with session_factory() as db:
        t = Task(title="t", **fields)
        db.add(t)
        await db.commit()
        await db.refresh(t)
        return t


def _migrator(db_factory, relay=None) -> TaskMigrator:
    m = TaskMigrator(db_factory=db_factory, relay=relay or FakeRelay(), broadcaster=None)
    # 文件搬运全替身（不碰 SSH/磁盘）
    m._sync_workspace = AsyncMock()
    m._move_session = AsyncMock()
    m._move_codex_session = AsyncMock()
    m._sync_task_fields_from_worker = AsyncMock()
    m._ensure_worker_task = AsyncMock()
    return m


async def test_migrate_local_to_worker(db_factory, session_factory, monkeypatch):
    w = await _mk_worker(session_factory)
    t = await _mk_task(session_factory, session_id="sess-1")
    relay = FakeRelay()
    m = _migrator(db_factory, relay)
    proxy = AsyncMock()
    proxy.ensure_worker_project.return_value = 9
    monkeypatch.setattr(main_module, "worker_proxy", proxy)

    await m.migrate(t.id, w.id)

    async with session_factory() as db:
        task = await db.get(Task, t.id)
    assert task.worker_id == w.id
    assert task.status == "completed"  # 迁移后状态复原
    assert (w.id, t.id) in relay.subscribed
    m._move_session.assert_called_once()
    m._ensure_worker_task.assert_called_once()


async def test_migrate_worker_to_local(db_factory, session_factory):
    w = await _mk_worker(session_factory)
    t = await _mk_task(session_factory, worker_id=w.id, session_id="sess-1")
    relay = FakeRelay()
    m = _migrator(db_factory, relay)

    await m.migrate(t.id, None)

    async with session_factory() as db:
        task = await db.get(Task, t.id)
    assert task.worker_id is None
    assert (w.id, t.id) in relay.unsubscribed
    m._sync_task_fields_from_worker.assert_called_once()


async def test_migrate_rejects_executing(db_factory, session_factory):
    w = await _mk_worker(session_factory)
    t = await _mk_task(session_factory, status="executing")
    m = _migrator(db_factory)
    with pytest.raises(MigrationError, match="先停止"):
        await m.migrate(t.id, w.id)


async def test_migrate_rejects_in_progress(db_factory, session_factory):
    w = await _mk_worker(session_factory)
    t = await _mk_task(session_factory, status="in_progress")
    m = _migrator(db_factory)
    with pytest.raises(MigrationError, match="先停止"):
        await m.migrate(t.id, w.id)


async def test_migration_claim_cas_preserves_concurrent_dispatcher_claim(
    db_factory, session_factory, monkeypatch,
):
    """A state change during Worker validation must beat migration's CAS."""
    w = await _mk_worker(session_factory)
    t = await _mk_task(session_factory, status="pending")
    m = _migrator(db_factory)
    real_get_worker = m._get_worker

    async def claim_while_validating(worker_id):
        worker = await real_get_worker(worker_id)
        async with session_factory() as db:
            await db.execute(
                update(Task)
                .where(Task.id == t.id, Task.status == "pending")
                .values(status="in_progress")
            )
            await db.commit()
        return worker

    monkeypatch.setattr(m, "_get_worker", claim_while_validating)

    with pytest.raises(MigrationError, match="并发修改"):
        await m.migrate(t.id, w.id)

    async with session_factory() as db:
        task = await db.get(Task, t.id)
    assert task.status == "in_progress"
    assert task.worker_id is None
    m._sync_workspace.assert_not_called()


async def test_migrate_noop_when_already_there(db_factory, session_factory):
    t = await _mk_task(session_factory)  # 本机
    m = _migrator(db_factory)
    await m.migrate(t.id, None)  # 不抛错、无副作用
    m._move_session.assert_not_called()


async def test_migrate_rejects_unready_target(db_factory, session_factory):
    w = await _mk_worker(session_factory, status="stopped")
    t = await _mk_task(session_factory)
    m = _migrator(db_factory)
    with pytest.raises(MigrationError, match="不可用"):
        await m.migrate(t.id, w.id)


async def test_migrate_rejects_unready_source(db_factory, session_factory):
    w = await _mk_worker(session_factory, status="stopped")
    t = await _mk_task(session_factory, worker_id=w.id)
    m = _migrator(db_factory)
    with pytest.raises(MigrationError, match="源 Worker"):
        await m.migrate(t.id, None)


async def test_migrate_failure_restores_status(db_factory, session_factory, monkeypatch):
    w = await _mk_worker(session_factory)
    t = await _mk_task(session_factory, session_id="s", status="failed")
    m = _migrator(db_factory)
    m._move_session = AsyncMock(side_effect=RuntimeError("rsync down"))
    monkeypatch.setattr(main_module, "worker_proxy", AsyncMock())

    with pytest.raises(RuntimeError):
        await m.migrate(t.id, w.id)

    async with session_factory() as db:
        task = await db.get(Task, t.id)
    assert task.status == "failed"      # 复原
    assert task.worker_id is None       # 指针没切


async def test_migration_failure_does_not_overwrite_concurrent_status(
    db_factory, session_factory, monkeypatch,
):
    """Rollback is a CAS too: a concurrent cancellation must remain final."""
    w = await _mk_worker(session_factory)
    t = await _mk_task(session_factory, session_id="s", status="failed")
    m = _migrator(db_factory)

    async def cancel_then_fail(*_args):
        async with session_factory() as db:
            await db.execute(
                update(Task)
                .where(Task.id == t.id, Task.status == "migrating")
                .values(status="cancelled")
            )
            await db.commit()
        raise RuntimeError("rsync down")

    m._move_session = AsyncMock(side_effect=cancel_then_fail)
    monkeypatch.setattr(main_module, "worker_proxy", AsyncMock())

    with pytest.raises(RuntimeError, match="rsync down"):
        await m.migrate(t.id, w.id)

    async with session_factory() as db:
        task = await db.get(Task, t.id)
    assert task.status == "cancelled"
    assert task.worker_id is None


async def test_worker_task_import_is_one_inert_request(
    session_factory, monkeypatch,
):
    w = await _mk_worker(session_factory)
    t = await _mk_task(session_factory, session_id="s", status="completed")
    requests = []

    class Response:
        status_code = 201
        text = ""

        @staticmethod
        def json():
            return {"status": "cancelled"}

        @staticmethod
        def raise_for_status():
            return None

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url, *, headers, json):
            requests.append((url, headers, json))
            return Response()

    monkeypatch.setattr(
        task_migrator_module.httpx,
        "AsyncClient",
        lambda **_kwargs: Client(),
    )
    migrator = TaskMigrator(db_factory=None, relay=FakeRelay())

    await migrator._ensure_worker_task(w, t, worker_project_id=17)

    assert len(requests) == 1
    url, _headers, payload = requests[0]
    assert url.endswith("/api/tasks/migration-import")
    assert payload["id"] == t.id
    assert payload["project_id"] == 17


async def test_put_worker_id_triggers_migration(client, session_factory, monkeypatch):
    t = await _mk_task(session_factory)
    migrator = AsyncMock()
    monkeypatch.setattr(main_module, "task_migrator", migrator)

    resp = await client.put(f"/api/tasks/{t.id}", json={"worker_id": 7})
    assert resp.status_code == 200, resp.text
    migrator.migrate.assert_called_once_with(t.id, 7)

    # -1 = 切回本机；已在本机 → 不触发
    migrator.migrate.reset_mock()
    resp = await client.put(f"/api/tasks/{t.id}", json={"worker_id": -1})
    assert resp.status_code == 200
    migrator.migrate.assert_not_called()


async def test_put_migration_error_maps_409(client, session_factory, monkeypatch):
    t = await _mk_task(session_factory)
    migrator = AsyncMock()
    migrator.migrate.side_effect = MigrationError("先停止再切换")
    monkeypatch.setattr(main_module, "task_migrator", migrator)
    resp = await client.put(f"/api/tasks/{t.id}", json={"worker_id": 7})
    assert resp.status_code == 409


async def test_put_without_worker_id_unchanged(client, session_factory, monkeypatch):
    """常规字段更新不碰迁移逻辑。"""
    t = await _mk_task(session_factory)
    migrator = AsyncMock()
    monkeypatch.setattr(main_module, "task_migrator", migrator)
    resp = await client.put(f"/api/tasks/{t.id}", json={"title": "renamed"})
    assert resp.status_code == 200
    assert resp.json()["title"] == "renamed"
    migrator.migrate.assert_not_called()


async def test_destroy_migrates_tasks_back(db_factory, session_factory, monkeypatch):
    from backend.api.workers import _migrate_back_then_destroy
    w = await _mk_worker(session_factory)
    t1 = await _mk_task(session_factory, worker_id=w.id)
    t2 = await _mk_task(session_factory, worker_id=w.id)

    migrator = AsyncMock()
    # t2 迁移失败也不阻塞销毁
    async def _migrate(task_id, target):
        if task_id == t2.id:
            raise RuntimeError("boom")
        async with db_factory() as db:
            task = await db.get(Task, task_id)
            task.worker_id = None
            await db.commit()
    migrator.migrate.side_effect = _migrate
    relay = AsyncMock()
    prov = AsyncMock()
    monkeypatch.setattr(main_module, "task_migrator", migrator)
    monkeypatch.setattr(main_module, "worker_relay", relay)

    await _migrate_back_then_destroy(prov, w.id, db_factory=db_factory)

    async with session_factory() as db:
        a = await db.get(Task, t1.id)
        b = await db.get(Task, t2.id)
    assert a.worker_id is None
    assert b.worker_id is None  # 失败也切回指针
    assert "销毁迁移失败" in (b.error_message or "")
    prov.destroy_worker.assert_called_once_with(w.id)
    relay.stop_worker.assert_called_once_with(w.id)


# ---------------------------------------------------------------------------
# Codex session 搬运（rollout 文件在 ~/.codex/sessions/YYYY/MM/DD/）
# ---------------------------------------------------------------------------

async def test_migrate_codex_task_uses_codex_session_mover(db_factory, session_factory, monkeypatch):
    w = await _mk_worker(session_factory)
    t = await _mk_task(session_factory, session_id="019f0000-aaaa-bbbb-cccc-000000000001", provider="codex")
    m = _migrator(db_factory)
    proxy = AsyncMock()
    proxy.ensure_worker_project.return_value = 9
    monkeypatch.setattr(main_module, "worker_proxy", proxy)

    await m.migrate(t.id, w.id)

    m._move_codex_session.assert_called_once()
    m._move_session.assert_not_called()


async def test_migrate_claude_task_keeps_claude_session_mover(db_factory, session_factory, monkeypatch):
    w = await _mk_worker(session_factory)
    t = await _mk_task(session_factory, session_id="sess-claude", provider="claude")
    m = _migrator(db_factory)
    proxy = AsyncMock()
    proxy.ensure_worker_project.return_value = 9
    monkeypatch.setattr(main_module, "worker_proxy", proxy)

    await m.migrate(t.id, w.id)

    m._move_session.assert_called_once()
    m._move_codex_session.assert_not_called()


async def test_local_codex_session_glob_finds_rollout_file(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    sid = "019f0000-aaaa-bbbb-cccc-000000000002"
    day_dir = tmp_path / ".codex" / "sessions" / "2026" / "07" / "19"
    day_dir.mkdir(parents=True)
    f = day_dir / f"rollout-2026-07-19T01-02-03-{sid}.jsonl"
    f.write_text("{}")

    matches = TaskMigrator._local_codex_session_glob(sid)
    assert matches == [str(f)]
    # 不同 session id 不应命中
    assert TaskMigrator._local_codex_session_glob("other-id") == []


async def test_local_codex_session_glob_finds_account_specific_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    sid = "019f0000-aaaa-bbbb-cccc-000000000003"
    day_dir = tmp_path / ".codex-account-2" / "sessions" / "2026" / "07" / "20"
    day_dir.mkdir(parents=True)
    rollout = day_dir / f"rollout-2026-07-20T01-02-03-{sid}.jsonl"
    rollout.write_text("{}")

    assert TaskMigrator._local_codex_session_glob(sid) == [str(rollout)]
    root, relative = TaskMigrator._codex_sessions_root_and_relative(str(rollout))
    assert root == str(tmp_path / ".codex-account-2" / "sessions")
    assert relative == f"2026/07/20/{rollout.name}"
    assert ".." not in PurePosixPath(relative).parts


async def test_local_account_rollout_moves_to_safe_remote_relative_path(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    sid = "019f0000-aaaa-bbbb-cccc-000000000005"
    day_dir = tmp_path / ".codex-account-2" / "sessions" / "2026" / "07" / "20"
    day_dir.mkdir(parents=True)
    rollout = day_dir / f"rollout-2026-07-20T02-03-04-{sid}.jsonl"
    rollout.write_text("{}")

    fake_ssh = AsyncMock()
    destination = Worker(
        id=8,
        name="destination",
        status="ready",
        private_ip="10.0.0.8",
        auth_token="t",
        ssh_user="ubuntu",
    )
    migrator = TaskMigrator(db_factory=None, relay=FakeRelay())
    monkeypatch.setattr(migrator, "_ssh", lambda worker: fake_ssh)

    await migrator._move_codex_session(None, destination, sid)

    expected = (
        "/home/ubuntu/.codex/sessions/2026/07/20/"
        f"rollout-2026-07-20T02-03-04-{sid}.jsonl"
    )
    fake_ssh.copy_file.assert_awaited_once_with(str(rollout), expected)
    assert ".." not in PurePosixPath(expected).parts


async def test_local_codex_migration_selects_copy_with_complete_history(
    tmp_path, monkeypatch,
):
    """Rotation copies remain in old homes; the longest proven prefix wins."""
    monkeypatch.setenv("HOME", str(tmp_path))
    sid = "019f0000-aaaa-bbbb-cccc-000000000006"
    old_dir = tmp_path / ".codex" / "sessions" / "2026" / "07" / "20"
    new_dir = tmp_path / ".codex-codex-3" / "sessions" / "2026" / "07" / "21"
    old_dir.mkdir(parents=True)
    new_dir.mkdir(parents=True)
    old = old_dir / f"rollout-old-{sid}.jsonl"
    newest = new_dir / f"rollout-new-{sid}.jsonl"
    old.write_bytes(b"turn-1\n")
    newest.write_bytes(b"turn-1\nturn-2\n")

    destination = Worker(
        id=9,
        name="destination",
        status="ready",
        private_ip="10.0.0.9",
        auth_token="t",
        ssh_user="ubuntu",
    )
    fake_ssh = AsyncMock()
    migrator = TaskMigrator(db_factory=None, relay=FakeRelay())
    monkeypatch.setattr(migrator, "_ssh", lambda worker: fake_ssh)

    await migrator._move_codex_session(None, destination, sid)

    assert fake_ssh.copy_file.await_args.args[0] == str(newest)


def test_codex_migration_refuses_divergent_account_copies(tmp_path):
    first = tmp_path / "first.jsonl"
    second = tmp_path / "second.jsonl"
    first.write_bytes(b"same-prefix\nA\n")
    second.write_bytes(b"same-prefix\nB\n")

    with pytest.raises(MigrationError, match="分叉 rollout"):
        TaskMigrator._select_authoritative_codex_rollout(
            [str(first), str(second)]
        )


async def test_remote_codex_session_uses_matched_account_sessions_root(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("HOME", str(tmp_path))
    sid = "019f0000-aaaa-bbbb-cccc-000000000004"
    remote_file = (
        f"/home/ubuntu/.codex-account-3/sessions/2026/07/20/"
        f"rollout-2026-07-20T01-02-03-{sid}.jsonl"
    )

    class FakeSSH:
        def __init__(self):
            self.commands = []

        async def run(self, command):
            self.commands.append(command)
            return 0, remote_file + "\n"

        async def rsync_from(self, remote_path, local_path, delete=False):
            assert remote_path == remote_file
            assert delete is False
            with open(local_path, "w", encoding="utf-8") as stream:
                stream.write("{}")

    source = Worker(
        id=7,
        name="source",
        status="ready",
        private_ip="10.0.0.7",
        auth_token="t",
        ssh_user="ubuntu",
    )
    fake_ssh = FakeSSH()
    migrator = TaskMigrator(db_factory=None, relay=FakeRelay())
    monkeypatch.setattr(migrator, "_ssh", lambda worker: fake_ssh)

    await migrator._move_codex_session(source, None, sid)

    target = (
        tmp_path / ".codex" / "sessions" / "2026" / "07" / "20"
        / f"rollout-2026-07-20T01-02-03-{sid}.jsonl"
    )
    assert target.read_text() == "{}"
    assert "find ~/.codex*/sessions" in fake_ssh.commands[0]


async def test_remote_codex_session_downloads_all_copies_and_uses_complete_one(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("HOME", str(tmp_path))
    sid = "019f0000-aaaa-bbbb-cccc-000000000007"
    old_remote = (
        f"/home/ubuntu/.codex/sessions/2026/07/20/rollout-old-{sid}.jsonl"
    )
    new_remote = (
        f"/home/ubuntu/.codex-codex-3/sessions/2026/07/21/"
        f"rollout-new-{sid}.jsonl"
    )

    class MultiCopySSH:
        async def run(self, _command):
            return 0, f"{old_remote}\n{new_remote}\n"

        async def rsync_from(self, remote_path, local_path, delete=False):
            assert delete is False
            content = b"turn-1\n" if remote_path == old_remote else b"turn-1\nturn-2\n"
            with open(local_path, "wb") as stream:
                stream.write(content)

    source = Worker(
        id=7,
        name="source",
        status="ready",
        private_ip="10.0.0.7",
        auth_token="t",
        ssh_user="ubuntu",
    )
    fake_ssh = MultiCopySSH()
    migrator = TaskMigrator(db_factory=None, relay=FakeRelay())
    monkeypatch.setattr(migrator, "_ssh", lambda worker: fake_ssh)

    await migrator._move_codex_session(source, None, sid)

    target = (
        tmp_path / ".codex" / "sessions" / "2026" / "07" / "21"
        / f"rollout-new-{sid}.jsonl"
    )
    assert target.read_bytes() == b"turn-1\nturn-2\n"
