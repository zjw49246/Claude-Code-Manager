"""Tests for Instance and Dispatcher API endpoints."""
import asyncio
import json
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.config import settings
from backend.models.instance import Instance
from backend.models.log_entry import LogEntry
from backend.models.task import Task

def _make_mock_instance_manager(is_running_val=False, launch_pid=12345, stop_val=True):
    mock = MagicMock()
    mock.is_running = MagicMock(return_value=is_running_val)
    mock.launch = AsyncMock(return_value=launch_pid)
    mock.stop = AsyncMock(return_value=stop_val)
    lifecycle_locks: dict[int, asyncio.Lock] = {}
    mock._instance_lifecycle_lock = MagicMock(
        side_effect=lambda instance_id: lifecycle_locks.setdefault(
            instance_id, asyncio.Lock()
        )
    )
    mock.processes = {}
    return mock


def _make_mock_ralph_loop(running=False):
    mock = MagicMock()
    mock.start = AsyncMock()
    mock.stop = AsyncMock()
    mock.is_running = MagicMock(return_value=running)
    return mock


def _make_mock_dispatcher():
    mock = MagicMock()
    mock.start = AsyncMock()
    mock.stop = AsyncMock()
    mock.status = MagicMock(return_value={"running": True, "active_tasks": {}})
    mock._instance_claim_lock = asyncio.Lock()
    mock._instance_claim_owners = {}
    mock._active_local_instance_ids = MagicMock(return_value=set())
    mock._ensure_instances = AsyncMock()
    return mock


async def _assign_running_task(session_factory, instance_id: int) -> int:
    async with session_factory() as db:
        task = Task(title="running task", description="owner", status="executing")
        db.add(task)
        await db.flush()
        instance = await db.get(Instance, instance_id)
        instance.status = "running"
        instance.pid = 12345
        instance.current_task_id = task.id
        task.instance_id = instance_id
        await db.commit()
        return task.id


# === CRUD ===


@pytest.mark.asyncio
async def test_list_instances_empty(client):
    resp = await client.get("/api/instances")
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_create_instance(client):
    resp = await client.post("/api/instances", json={"name": "worker-1"})
    assert resp.status_code == 201
    data = resp.json()
    assert data["name"] == "worker-1"
    assert data["status"] == "idle"


@pytest.mark.asyncio
async def test_create_instance_normalizes_and_validates_name(client):
    response = await client.post("/api/instances", json={"name": "  worker  "})
    assert response.status_code == 201
    assert response.json()["name"] == "worker"
    blank = await client.post("/api/instances", json={"name": "   "})
    assert blank.status_code == 422


@pytest.mark.asyncio
async def test_create_instance_enforces_live_capacity(client):
    original_cap = settings.max_concurrent_instances
    settings.max_concurrent_instances = 1
    try:
        first = await client.post("/api/instances", json={"name": "worker-1"})
        second = await client.post("/api/instances", json={"name": "worker-2"})
    finally:
        settings.max_concurrent_instances = original_cap

    assert first.status_code == 201
    assert second.status_code == 409
    assert "capacity limit" in second.json()["detail"].lower()


@pytest.mark.asyncio
async def test_terminal_instance_does_not_consume_live_capacity(
    client, session_factory
):
    original_cap = settings.max_concurrent_instances
    settings.max_concurrent_instances = 1
    try:
        first = await client.post("/api/instances", json={"name": "old-worker"})
        async with session_factory() as db:
            instance = await db.get(Instance, first.json()["id"])
            instance.status = "error"
            await db.commit()
        replacement = await client.post(
            "/api/instances", json={"name": "replacement"}
        )
    finally:
        settings.max_concurrent_instances = original_cap

    assert replacement.status_code == 201


@pytest.mark.asyncio
async def test_concurrent_instance_creates_cannot_exceed_capacity(client):
    original_cap = settings.max_concurrent_instances
    settings.max_concurrent_instances = 1
    try:
        responses = await asyncio.gather(
            client.post("/api/instances", json={"name": "worker-a"}),
            client.post("/api/instances", json={"name": "worker-b"}),
        )
    finally:
        settings.max_concurrent_instances = original_cap

    assert sorted(response.status_code for response in responses) == [201, 409]


@pytest.mark.asyncio
async def test_get_instance(client):
    create_resp = await client.post("/api/instances", json={"name": "w"})
    inst_id = create_resp.json()["id"]
    resp = await client.get(f"/api/instances/{inst_id}")
    assert resp.status_code == 200
    assert resp.json()["name"] == "w"


@pytest.mark.asyncio
async def test_get_instance_not_found(client):
    resp = await client.get("/api/instances/9999")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_instance(client):
    create_resp = await client.post("/api/instances", json={"name": "del-me"})
    inst_id = create_resp.json()["id"]
    mock_im = _make_mock_instance_manager(is_running_val=False)
    with patch("backend.main.instance_manager", mock_im):
        resp = await client.delete(f"/api/instances/{inst_id}")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


@pytest.mark.asyncio
async def test_cleanup_reloads_generation_after_waiting_for_lifecycle_lock(
    client,
    session_factory,
):
    """A MySQL RR snapshot must not delete a slot reused before lock entry."""

    async with session_factory() as db:
        instance = Instance(name="cleanup-aba", status="error")
        db.add(instance)
        await db.commit()
        instance_id = instance.id

    async def finish_new_generation(_instance_id):
        assert _instance_id == instance_id
        async with session_factory() as db:
            current = await db.get(Instance, instance_id)
            current.status = "idle"
            current.started_at = datetime(2026, 7, 23, 1, 2, 3)
            await db.commit()

    mock_im = _make_mock_instance_manager(is_running_val=False)
    mock_rl = _make_mock_ralph_loop()
    mock_rl.stop.side_effect = finish_new_generation
    mock_dispatcher = _make_mock_dispatcher()
    with (
        patch("backend.main.instance_manager", mock_im),
        patch("backend.main.ralph_loop", mock_rl),
        patch("backend.main.dispatcher", mock_dispatcher),
    ):
        response = await client.delete("/api/instances/cleanup")

    assert response.status_code == 200, response.text
    assert response.json()["deleted"] == 0
    assert response.json()["skipped_running"] == [instance_id]
    async with session_factory() as db:
        current = await db.get(Instance, instance_id)
        assert current is not None
        assert current.status == "idle"
        assert current.started_at == datetime(2026, 7, 23, 1, 2, 3)


@pytest.mark.asyncio
async def test_exact_instance_delete_rejects_changed_generation(
    session_factory,
):
    from backend.api.instances import _delete_exact_instance_generation

    async with session_factory() as db:
        instance = Instance(name="delete-cas", status="error")
        db.add(instance)
        await db.commit()
        instance_id = instance.id

    observed = Instance(
        id=instance_id,
        name="delete-cas",
        status="error",
        pid=None,
        current_task_id=None,
        started_at=None,
    )
    async with session_factory() as db:
        current = await db.get(Instance, instance_id)
        current.status = "idle"
        current.started_at = datetime(2026, 7, 23, 4, 5, 6)
        await db.commit()

    async with session_factory() as db:
        assert await _delete_exact_instance_generation(db, observed) is False
        await db.rollback()

    async with session_factory() as db:
        assert await db.get(Instance, instance_id) is not None


@pytest.mark.asyncio
async def test_delete_instance_reconciles_definitively_dead_pid(
    client, session_factory,
):
    async with session_factory() as db:
        instance = Instance(name="dead-orphan", status="error", pid=45671)
        db.add(instance)
        await db.commit()
        instance_id = instance.id

    mock_im = _make_mock_instance_manager(is_running_val=False)
    mock_rl = _make_mock_ralph_loop()
    with (
        patch("backend.main.instance_manager", mock_im),
        patch("backend.main.ralph_loop", mock_rl),
        patch(
            "backend.services.task_queue.os.kill",
            side_effect=ProcessLookupError,
        ) as probe,
    ):
        response = await client.delete(f"/api/instances/{instance_id}")

    assert response.status_code == 200, response.text
    probe.assert_called_once_with(45671, 0)
    async with session_factory() as db:
        assert await db.get(Instance, instance_id) is None


@pytest.mark.asyncio
async def test_delete_instance_preserves_pid_that_may_be_alive(
    client, session_factory,
):
    async with session_factory() as db:
        task = Task(title="live orphan", description="d", status="failed")
        db.add(task)
        await db.flush()
        instance = Instance(
            name="live-orphan",
            status="error",
            pid=45672,
            current_task_id=task.id,
        )
        db.add(instance)
        await db.commit()
        task_id, instance_id = task.id, instance.id

    mock_im = _make_mock_instance_manager(is_running_val=False)
    mock_rl = _make_mock_ralph_loop()
    with (
        patch("backend.main.instance_manager", mock_im),
        patch("backend.main.ralph_loop", mock_rl),
        patch("backend.services.task_queue.os.kill", return_value=None),
    ):
        response = await client.delete(f"/api/instances/{instance_id}")

    assert response.status_code == 409
    assert "may still be alive" in response.json()["detail"]
    async with session_factory() as db:
        instance = await db.get(Instance, instance_id)
        assert instance.pid == 45672
        assert instance.current_task_id == task_id


@pytest.mark.asyncio
async def test_delete_running_instance_is_rejected(client):
    created = await client.post("/api/instances", json={"name": "active"})
    inst_id = created.json()["id"]
    mock_im = _make_mock_instance_manager(is_running_val=True)
    mock_rl = _make_mock_ralph_loop()
    with (
        patch("backend.main.instance_manager", mock_im),
        patch("backend.main.ralph_loop", mock_rl),
    ):
        resp = await client.delete(f"/api/instances/{inst_id}")

    assert resp.status_code == 409
    assert "stop it before deleting" in resp.json()["detail"]
    mock_im.stop.assert_not_awaited()
    mock_rl.stop.assert_awaited_once_with(inst_id)


@pytest.mark.asyncio
async def test_delete_stops_legacy_ralph_before_locking_slot(client):
    created = await client.post("/api/instances", json={"name": "legacy"})
    inst_id = created.json()["id"]
    mock_im = _make_mock_instance_manager()
    mock_rl = _make_mock_ralph_loop(running=True)
    with (
        patch("backend.main.instance_manager", mock_im),
        patch("backend.main.ralph_loop", mock_rl),
    ):
        resp = await client.delete(f"/api/instances/{inst_id}")

    assert resp.status_code == 200
    mock_rl.stop.assert_awaited_once_with(inst_id)


@pytest.mark.asyncio
async def test_delete_rechecks_dispatcher_reservation_under_admission_lock(client):
    created = await client.post("/api/instances", json={"name": "reserved"})
    instance_id = created.json()["id"]
    mock_im = _make_mock_instance_manager()
    mock_rl = _make_mock_ralph_loop()
    mock_dispatcher = _make_mock_dispatcher()
    reached_admission = asyncio.Event()

    async def stop_legacy(_instance_id):
        reached_admission.set()

    mock_rl.stop.side_effect = stop_legacy
    await mock_dispatcher._instance_claim_lock.acquire()
    try:
        with (
            patch("backend.main.instance_manager", mock_im),
            patch("backend.main.ralph_loop", mock_rl),
            patch("backend.main.dispatcher", mock_dispatcher),
        ):
            request_task = asyncio.create_task(
                client.delete(f"/api/instances/{instance_id}")
            )
            await asyncio.wait_for(reached_admission.wait(), timeout=1)
            assert request_task.done() is False
            mock_dispatcher._instance_claim_owners[instance_id] = (
                object(),
                request_task,
            )
            mock_dispatcher._instance_claim_lock.release()
            response = await asyncio.wait_for(request_task, timeout=1)
    finally:
        if mock_dispatcher._instance_claim_lock.locked():
            mock_dispatcher._instance_claim_lock.release()

    assert response.status_code == 409
    assert "reserved for a task lifecycle" in response.json()["detail"]


@pytest.mark.asyncio
async def test_cleanup_only_deletes_inactive_terminal_instances(
    client, session_factory
):
    async with session_factory() as db:
        removable = Instance(name="old-error", status="error")
        active = Instance(name="inconsistent-error", status="error")
        db.add_all([removable, active])
        await db.commit()
        removable_id = removable.id
        active_id = active.id

    mock_im = _make_mock_instance_manager()
    mock_im.is_running.side_effect = lambda instance_id: instance_id == active_id
    mock_rl = _make_mock_ralph_loop()
    mock_dispatcher = _make_mock_dispatcher()
    mock_dispatcher.status.return_value = {"running": False}
    with (
        patch("backend.main.instance_manager", mock_im),
        patch("backend.main.ralph_loop", mock_rl),
        patch("backend.main.dispatcher", mock_dispatcher),
    ):
        response = await client.delete("/api/instances/cleanup")

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "deleted": 1,
        "skipped_running": [active_id],
    }
    async with session_factory() as db:
        assert await db.get(Instance, removable_id) is None
        assert await db.get(Instance, active_id) is not None
    mock_im.stop.assert_not_awaited()


@pytest.mark.asyncio
async def test_cleanup_skips_terminal_instance_reserved_for_launch(
    client, session_factory
):
    async with session_factory() as db:
        instance = Instance(name="reserved-terminal", status="error")
        db.add(instance)
        await db.commit()
        instance_id = instance.id

    mock_im = _make_mock_instance_manager()
    mock_rl = _make_mock_ralph_loop()
    mock_dispatcher = _make_mock_dispatcher()
    mock_dispatcher.status.return_value = {"running": False}
    mock_dispatcher._instance_claim_owners[instance_id] = (
        object(),
        None,
    )
    with (
        patch("backend.main.instance_manager", mock_im),
        patch("backend.main.ralph_loop", mock_rl),
        patch("backend.main.dispatcher", mock_dispatcher),
    ):
        response = await client.delete("/api/instances/cleanup")

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "deleted": 0,
        "skipped_running": [instance_id],
    }
    async with session_factory() as db:
        assert await db.get(Instance, instance_id) is not None


@pytest.mark.asyncio
async def test_delete_instance_not_found(client):
    mock_im = _make_mock_instance_manager()
    with patch("backend.main.instance_manager", mock_im):
        resp = await client.delete("/api/instances/9999")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_concurrent_deletes_are_serialized_without_500(client):
    created = await client.post("/api/instances", json={"name": "delete-race"})
    instance_id = created.json()["id"]
    mock_im = _make_mock_instance_manager()
    mock_rl = _make_mock_ralph_loop()
    with (
        patch("backend.main.instance_manager", mock_im),
        patch("backend.main.ralph_loop", mock_rl),
    ):
        responses = await asyncio.gather(
            client.delete(f"/api/instances/{instance_id}"),
            client.delete(f"/api/instances/{instance_id}"),
        )
    assert sorted(response.status_code for response in responses) == [200, 404]


@pytest.mark.asyncio
async def test_cleanup_refuses_terminal_row_with_dirty_owner_metadata(
    client, session_factory
):
    async with session_factory() as db:
        task = Task(title="orphan", description="unknown process")
        instance = Instance(name="dirty", status="error", pid=76543)
        db.add_all([task, instance])
        await db.flush()
        instance.current_task_id = task.id
        await db.commit()
        instance_id = instance.id

    mock_im = _make_mock_instance_manager(is_running_val=False)
    mock_rl = _make_mock_ralph_loop()
    mock_dispatcher = _make_mock_dispatcher()
    mock_dispatcher.status.return_value = {"running": False}
    with (
        patch("backend.main.instance_manager", mock_im),
        patch("backend.main.ralph_loop", mock_rl),
        patch("backend.main.dispatcher", mock_dispatcher),
        patch("backend.services.task_queue.os.kill", return_value=None) as probe,
    ):
        response = await client.delete("/api/instances/cleanup")

    assert response.status_code == 200
    assert response.json()["deleted"] == 0
    assert response.json()["skipped_running"] == [instance_id]
    async with session_factory() as db:
        instance = await db.get(Instance, instance_id)
        assert instance.pid == 76543
        assert instance.current_task_id == task.id
    probe.assert_called_once_with(76543, 0)


@pytest.mark.asyncio
async def test_cleanup_reconciles_dead_terminal_pid(
    client, session_factory,
):
    async with session_factory() as db:
        task = Task(title="dead orphan", description="d", status="failed")
        db.add(task)
        await db.flush()
        instance = Instance(
            name="dead-dirty",
            status="error",
            pid=76544,
            current_task_id=task.id,
        )
        db.add(instance)
        await db.commit()
        instance_id = instance.id

    mock_im = _make_mock_instance_manager(is_running_val=False)
    mock_rl = _make_mock_ralph_loop()
    mock_dispatcher = _make_mock_dispatcher()
    mock_dispatcher.status.return_value = {"running": False}
    with (
        patch("backend.main.instance_manager", mock_im),
        patch("backend.main.ralph_loop", mock_rl),
        patch("backend.main.dispatcher", mock_dispatcher),
        patch(
            "backend.services.task_queue.os.kill",
            side_effect=ProcessLookupError,
        ) as probe,
    ):
        response = await client.delete("/api/instances/cleanup")

    assert response.status_code == 200, response.text
    assert response.json()["deleted"] == 1
    assert response.json()["skipped_running"] == []
    probe.assert_called_once_with(76544, 0)
    async with session_factory() as db:
        assert await db.get(Instance, instance_id) is None


# === Stop ===


@pytest.mark.asyncio
async def test_stop_instance_success(client, session_factory):
    created = await client.post("/api/instances", json={"name": "running"})
    inst_id = created.json()["id"]
    task_id = await _assign_running_task(session_factory, inst_id)
    mock_im = _make_mock_instance_manager(stop_val=True)
    with patch("backend.main.instance_manager", mock_im):
        resp = await client.post(
            f"/api/instances/{inst_id}/stop",
            json={
                "expected_task_id": task_id,
                "expected_pid": 12345,
                "expected_started_at": None,
            },
        )
    assert resp.status_code == 200
    mock_im.stop.assert_awaited_once_with(
        inst_id,
        expected_task_id=task_id,
        expected_pid=12345,
        expected_started_at=None,
        terminal_consumer_timeout=30.0,
        consumer_cancel_timeout=10.0,
    )


@pytest.mark.asyncio
async def test_stop_instance_not_running(client, session_factory):
    created = await client.post("/api/instances", json={"name": "idle"})
    inst_id = created.json()["id"]
    task_id = await _assign_running_task(session_factory, inst_id)
    mock_im = _make_mock_instance_manager(stop_val=False)
    with patch("backend.main.instance_manager", mock_im):
        resp = await client.post(
            f"/api/instances/{inst_id}/stop",
            json={
                "expected_task_id": task_id,
                "expected_pid": 12345,
                "expected_started_at": None,
            },
        )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_stop_instance_rejects_stale_task_owner(client, session_factory):
    created = await client.post("/api/instances", json={"name": "reused"})
    inst_id = created.json()["id"]
    current_task_id = await _assign_running_task(session_factory, inst_id)
    mock_im = _make_mock_instance_manager(stop_val=True)
    with patch("backend.main.instance_manager", mock_im):
        resp = await client.post(
            f"/api/instances/{inst_id}/stop",
            json={
                "expected_task_id": current_task_id + 1,
                "expected_pid": 12345,
                "expected_started_at": None,
            },
        )
    assert resp.status_code == 409
    mock_im.stop.assert_not_awaited()


@pytest.mark.asyncio
async def test_stop_instance_rejects_stale_pid_or_start_generation(
    client,
    session_factory,
):
    created = await client.post("/api/instances", json={"name": "aba-reused"})
    instance_id = created.json()["id"]
    task_id = await _assign_running_task(session_factory, instance_id)
    started_at = datetime(2026, 4, 5, 6, 7, 8)
    async with session_factory() as db:
        instance = await db.get(Instance, instance_id)
        instance.started_at = started_at
        await db.commit()

    mock_im = _make_mock_instance_manager(stop_val=True)
    with patch("backend.main.instance_manager", mock_im):
        stale_pid = await client.post(
            f"/api/instances/{instance_id}/stop",
            json={
                "expected_task_id": task_id,
                "expected_pid": 54321,
                "expected_started_at": started_at.isoformat(),
            },
        )
        stale_start = await client.post(
            f"/api/instances/{instance_id}/stop",
            json={
                "expected_task_id": task_id,
                "expected_pid": 12345,
                "expected_started_at": datetime(
                    2026, 4, 5, 6, 7, 9
                ).isoformat(),
            },
        )

    assert stale_pid.status_code == 409
    assert stale_start.status_code == 409
    mock_im.stop.assert_not_awaited()


@pytest.mark.asyncio
async def test_stop_instance_fails_closed_when_legacy_ralph_owner_remains(
    client, session_factory
):
    created = await client.post("/api/instances", json={"name": "ralph-running"})
    inst_id = created.json()["id"]
    task_id = await _assign_running_task(session_factory, inst_id)
    mock_im = _make_mock_instance_manager(stop_val=False)
    mock_rl = _make_mock_ralph_loop(running=True)
    with (
        patch("backend.main.instance_manager", mock_im),
        patch("backend.main.ralph_loop", mock_rl),
    ):
        resp = await client.post(
            f"/api/instances/{inst_id}/stop",
            json={
                "expected_task_id": task_id,
                "expected_pid": 12345,
                "expected_started_at": None,
            },
        )

    assert resp.status_code == 409
    assert "cleanup could not be confirmed" in resp.json()["detail"]
    mock_rl.stop.assert_awaited_once_with(inst_id)
    mock_im.stop.assert_awaited_once_with(
        inst_id,
        expected_task_id=task_id,
        expected_pid=12345,
        expected_started_at=None,
        terminal_consumer_timeout=30.0,
        consumer_cancel_timeout=10.0,
    )


# === Run ===


@pytest.mark.asyncio
async def test_direct_run_with_prompt_is_retired(client):
    create_resp = await client.post("/api/instances", json={"name": "runner"})
    inst_id = create_resp.json()["id"]
    mock_im = _make_mock_instance_manager(is_running_val=False, launch_pid=999)
    with patch("backend.main.instance_manager", mock_im):
        resp = await client.post(f"/api/instances/{inst_id}/run?prompt=hello")
    assert resp.status_code == 410
    assert "create or retry a task" in resp.json()["detail"].lower()
    mock_im.launch.assert_not_awaited()


@pytest.mark.asyncio
async def test_direct_run_is_retired_even_when_instance_is_busy(client):
    create_resp = await client.post("/api/instances", json={"name": "busy"})
    inst_id = create_resp.json()["id"]
    mock_im = _make_mock_instance_manager(is_running_val=True)
    with patch("backend.main.instance_manager", mock_im):
        resp = await client.post(f"/api/instances/{inst_id}/run?prompt=hello")
    assert resp.status_code == 410
    mock_im.launch.assert_not_awaited()


@pytest.mark.asyncio
async def test_direct_run_without_payload_is_retired(client):
    create_resp = await client.post("/api/instances", json={"name": "empty"})
    inst_id = create_resp.json()["id"]
    mock_im = _make_mock_instance_manager(is_running_val=False)
    with patch("backend.main.instance_manager", mock_im):
        resp = await client.post(f"/api/instances/{inst_id}/run")
    assert resp.status_code == 410
    mock_im.launch.assert_not_awaited()


@pytest.mark.asyncio
async def test_direct_run_with_task_id_cannot_bypass_task_queue(client):
    # Create instance and task
    inst_resp = await client.post("/api/instances", json={"name": "task-runner"})
    inst_id = inst_resp.json()["id"]
    task_resp = await client.post("/api/tasks", json={
        "title": "T", "description": "Do work", "target_repo": "/tmp",
        "provider": "claude",
    })
    task_id = task_resp.json()["id"]

    mock_im = _make_mock_instance_manager(is_running_val=False, launch_pid=111)
    with patch("backend.main.instance_manager", mock_im):
        resp = await client.post(f"/api/instances/{inst_id}/run?task_id={task_id}")
    assert resp.status_code == 410
    mock_im.launch.assert_not_awaited()


@pytest.mark.asyncio
async def test_direct_run_codex_task_is_also_retired(client):
    inst_resp = await client.post("/api/instances", json={"name": "codex-task-runner"})
    inst_id = inst_resp.json()["id"]
    task_resp = await client.post("/api/tasks", json={
        "title": "Codex task",
        "description": "Continue work",
        "target_repo": "/tmp",
        "provider": "codex",
        "session_id": "thread-manual-1",
    })
    task_id = task_resp.json()["id"]

    mock_im = _make_mock_instance_manager(is_running_val=False, launch_pid=222)
    with patch("backend.main.instance_manager", mock_im):
        resp = await client.post(f"/api/instances/{inst_id}/run?task_id={task_id}")

    assert resp.status_code == 410
    mock_im.launch.assert_not_awaited()


# === Logs ===


@pytest.mark.asyncio
async def test_get_logs(client, session_factory):
    # Create instance and some log entries
    inst_resp = await client.post("/api/instances", json={"name": "log-test"})
    inst_id = inst_resp.json()["id"]

    async with session_factory() as db:
        db.add(LogEntry(instance_id=inst_id, event_type="message", content="hello"))
        db.add(LogEntry(instance_id=inst_id, event_type="tool_use", tool_name="Edit"))
        await db.commit()

    resp = await client.get(f"/api/instances/{inst_id}/logs")
    assert resp.status_code == 200
    assert len(resp.json()) == 2
    assert [entry["event_type"] for entry in resp.json()] == [
        "tool_use",
        "message",
    ]


@pytest.mark.asyncio
async def test_get_logs_validates_pagination(client):
    created = await client.post("/api/instances", json={"name": "log-bounds"})
    inst_id = created.json()["id"]

    assert (
        await client.get(f"/api/instances/{inst_id}/logs?limit=0")
    ).status_code == 422
    assert (
        await client.get(f"/api/instances/{inst_id}/logs?limit=1001")
    ).status_code == 422
    assert (
        await client.get(f"/api/instances/{inst_id}/logs?offset=-1")
    ).status_code == 422
    assert (
        await client.get(f"/api/instances/{inst_id}/logs?after_id=-1")
    ).status_code == 422
    assert (
        await client.get(
            f"/api/instances/{inst_id}/logs?after_id=1&offset=1"
        )
    ).status_code == 422


@pytest.mark.asyncio
async def test_get_logs_after_id_returns_complete_ascending_cursor_pages(
    client, session_factory
):
    created = await client.post("/api/instances", json={"name": "log-cursor"})
    inst_id = created.json()["id"]

    async with session_factory() as db:
        entries = [
            LogEntry(
                instance_id=inst_id,
                event_type="message" if index != 2 else "tool_use",
                content=f"log-{index}",
            )
            for index in range(5)
        ]
        db.add_all(entries)
        await db.commit()
        entry_ids = [entry.id for entry in entries]

    first = await client.get(
        f"/api/instances/{inst_id}/logs?after_id={entry_ids[0]}&limit=2"
    )
    second = await client.get(
        f"/api/instances/{inst_id}/logs"
        f"?after_id={first.json()[-1]['id']}&limit=2"
    )

    assert first.status_code == 200
    assert [entry["id"] for entry in first.json()] == entry_ids[1:3]
    assert [entry["id"] for entry in second.json()] == entry_ids[3:5]

    filtered = await client.get(
        f"/api/instances/{inst_id}/logs"
        f"?after_id={entry_ids[0]}&event_type=message"
    )
    assert [entry["id"] for entry in filtered.json()] == [
        entry_ids[1],
        entry_ids[3],
        entry_ids[4],
    ]


@pytest.mark.asyncio
async def test_get_logs_exposes_item_id_without_raw_json(client, session_factory):
    created = await client.post("/api/instances", json={"name": "log-item-id"})
    inst_id = created.json()["id"]

    async with session_factory() as db:
        db.add(
            LogEntry(
                instance_id=inst_id,
                event_type="message",
                content="final",
                raw_json=json.dumps(
                    {
                        "type": "item.completed",
                        "item": {"id": "message-42", "type": "agent_message"},
                    }
                ),
            )
        )
        await db.commit()

    response = await client.get(f"/api/instances/{inst_id}/logs")
    assert response.status_code == 200
    assert response.json()[0]["item_id"] == "message-42"
    assert "raw_json" not in response.json()[0]


@pytest.mark.asyncio
async def test_get_logs_returns_404_for_unknown_instance(client):
    response = await client.get("/api/instances/9999/logs")
    assert response.status_code == 404


# === Dispatcher ===


@pytest.mark.asyncio
async def test_dispatcher_status(client):
    mock_disp = _make_mock_dispatcher()
    with patch("backend.main.dispatcher", mock_disp):
        resp = await client.get("/api/dispatcher/status")
    assert resp.status_code == 200
    assert "running" in resp.json()


@pytest.mark.asyncio
async def test_dispatcher_start(client):
    mock_disp = _make_mock_dispatcher()
    with patch("backend.main.dispatcher", mock_disp):
        resp = await client.post("/api/dispatcher/start")
    assert resp.status_code == 200
    mock_disp.start.assert_awaited_once()


@pytest.mark.asyncio
async def test_dispatcher_stop(client):
    mock_disp = _make_mock_dispatcher()
    with patch("backend.main.dispatcher", mock_disp):
        resp = await client.post("/api/dispatcher/stop")
    assert resp.status_code == 200
    mock_disp.stop.assert_awaited_once()


# === Ralph ===


@pytest.mark.asyncio
async def test_ralph_start(client):
    inst_resp = await client.post("/api/instances", json={"name": "ralph-test"})
    inst_id = inst_resp.json()["id"]
    mock_rl = _make_mock_ralph_loop()
    with patch("backend.main.ralph_loop", mock_rl):
        resp = await client.post(f"/api/instances/{inst_id}/ralph/start")
    assert resp.status_code == 410
    assert "global dispatcher" in resp.json()["detail"].lower()
    mock_rl.start.assert_not_awaited()


@pytest.mark.asyncio
async def test_ralph_stop(client):
    mock_rl = _make_mock_ralph_loop()
    with patch("backend.main.ralph_loop", mock_rl):
        resp = await client.post("/api/instances/1/ralph/stop")
    assert resp.status_code == 200
    mock_rl.stop.assert_awaited_once_with(1)


@pytest.mark.asyncio
async def test_ralph_stop_returns_409_when_loop_ignores_cancellation(client):
    mock_rl = _make_mock_ralph_loop()
    mock_rl.stop.return_value = False
    with patch("backend.main.ralph_loop", mock_rl):
        resp = await client.post("/api/instances/1/ralph/stop")
    assert resp.status_code == 409
    assert "did not stop" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_ralph_status(client):
    mock_rl = _make_mock_ralph_loop(running=True)
    with patch("backend.main.ralph_loop", mock_rl):
        resp = await client.get("/api/instances/1/ralph/status")
    assert resp.status_code == 200
    assert resp.json()["running"] is True
