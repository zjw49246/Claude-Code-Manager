"""Phase 3 测试：TaskMigrator 状态机 / PUT 触发迁移 / 销毁批量迁回。"""
from unittest.mock import AsyncMock

import pytest

import backend.main as main_module
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
