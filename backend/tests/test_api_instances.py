"""Tests for Instance and Dispatcher API endpoints."""
from contextlib import asynccontextmanager

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from backend.models.instance import Instance
from backend.models.log_entry import LogEntry


@asynccontextmanager
async def _open_task_start_guard():
    yield


def _make_mock_instance_manager(is_running_val=False, launch_pid=12345, stop_val=True):
    mock = MagicMock()
    mock.is_running = MagicMock(return_value=is_running_val)
    mock.launch = AsyncMock(return_value=launch_pid)
    mock.stop = AsyncMock(return_value=stop_val)
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
    return mock


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
async def test_delete_instance_not_found(client):
    mock_im = _make_mock_instance_manager()
    with patch("backend.main.instance_manager", mock_im):
        resp = await client.delete("/api/instances/9999")
    assert resp.status_code == 404


# === Stop ===


@pytest.mark.asyncio
async def test_stop_instance_success(client):
    mock_im = _make_mock_instance_manager(stop_val=True)
    with patch("backend.main.instance_manager", mock_im):
        resp = await client.post("/api/instances/1/stop")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_stop_instance_not_running(client):
    mock_im = _make_mock_instance_manager(stop_val=False)
    with patch("backend.main.instance_manager", mock_im):
        resp = await client.post("/api/instances/1/stop")
    assert resp.status_code == 400


# === Run ===


@pytest.mark.asyncio
async def test_run_with_prompt(client):
    create_resp = await client.post("/api/instances", json={"name": "runner"})
    inst_id = create_resp.json()["id"]
    mock_im = _make_mock_instance_manager(is_running_val=False, launch_pid=999)
    with patch("backend.main.instance_manager", mock_im):
        resp = await client.post(f"/api/instances/{inst_id}/run?prompt=hello")
    assert resp.status_code == 200
    assert resp.json()["pid"] == 999
    assert mock_im.launch.await_args.kwargs["config_dir"] is None
    assert mock_im.launch.await_args.kwargs["resume_session_id"] is None


@pytest.mark.asyncio
async def test_run_already_running(client):
    create_resp = await client.post("/api/instances", json={"name": "busy"})
    inst_id = create_resp.json()["id"]
    mock_im = _make_mock_instance_manager(is_running_val=True)
    with patch("backend.main.instance_manager", mock_im):
        resp = await client.post(f"/api/instances/{inst_id}/run?prompt=hello")
    assert resp.status_code == 400
    assert "already running" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_run_no_prompt_no_task(client):
    create_resp = await client.post("/api/instances", json={"name": "empty"})
    inst_id = create_resp.json()["id"]
    mock_im = _make_mock_instance_manager(is_running_val=False)
    with patch("backend.main.instance_manager", mock_im):
        resp = await client.post(f"/api/instances/{inst_id}/run")
    assert resp.status_code == 400
    assert "task_id or prompt" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_run_with_task_id(client):
    """Run instance with a task_id instead of prompt."""
    # Create instance and task
    inst_resp = await client.post("/api/instances", json={"name": "task-runner"})
    inst_id = inst_resp.json()["id"]
    task_resp = await client.post("/api/tasks", json={
        "title": "T", "description": "Do work", "target_repo": "/tmp",
        "provider": "claude",
    })
    task_id = task_resp.json()["id"]

    mock_im = _make_mock_instance_manager(is_running_val=False, launch_pid=111)
    mock_dispatcher = MagicMock()
    mock_dispatcher.task_start_guard = MagicMock(
        side_effect=_open_task_start_guard
    )
    mock_dispatcher._resolve_resume_config_dir = AsyncMock(
        return_value="/pool/claude-2"
    )
    with (
        patch("backend.main.instance_manager", mock_im),
        patch("backend.main.dispatcher", mock_dispatcher),
    ):
        resp = await client.post(f"/api/instances/{inst_id}/run?task_id={task_id}")
    assert resp.status_code == 200
    assert resp.json()["pid"] == 111
    mock_dispatcher._resolve_resume_config_dir.assert_awaited_once_with(
        None,
        "claude",
        task_id=task_id,
    )
    assert mock_im.launch.await_args.kwargs["config_dir"] == "/pool/claude-2"
    assert mock_im.launch.await_args.kwargs["resume_session_id"] is None


@pytest.mark.asyncio
async def test_run_with_task_id_rejected_during_maintenance(client):
    from backend.services.dispatcher import TaskStartPausedError

    inst_resp = await client.post("/api/instances", json={"name": "paused-runner"})
    inst_id = inst_resp.json()["id"]
    task_resp = await client.post("/api/tasks", json={
        "title": "Paused", "description": "Do not start", "target_repo": "/tmp",
    })
    task_id = task_resp.json()["id"]

    @asynccontextmanager
    async def paused_guard():
        raise TaskStartPausedError("maintenance")
        yield

    mock_im = _make_mock_instance_manager(is_running_val=False)
    mock_dispatcher = MagicMock()
    mock_dispatcher._resolve_resume_config_dir = AsyncMock(return_value=None)
    mock_dispatcher.task_start_guard = MagicMock(side_effect=paused_guard)
    with (
        patch("backend.main.instance_manager", mock_im),
        patch("backend.main.dispatcher", mock_dispatcher),
    ):
        resp = await client.post(f"/api/instances/{inst_id}/run?task_id={task_id}")

    assert resp.status_code == 409
    mock_im.launch.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_codex_task_resumes_on_dispatcher_bound_home(client):
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
    mock_dispatcher = MagicMock()
    mock_dispatcher.task_start_guard = MagicMock(
        side_effect=_open_task_start_guard
    )
    mock_dispatcher._resolve_resume_config_dir = AsyncMock(
        return_value="/pool/codex-2"
    )
    with (
        patch("backend.main.instance_manager", mock_im),
        patch("backend.main.dispatcher", mock_dispatcher),
    ):
        resp = await client.post(f"/api/instances/{inst_id}/run?task_id={task_id}")

    assert resp.status_code == 200
    mock_dispatcher._resolve_resume_config_dir.assert_awaited_once_with(
        "thread-manual-1",
        "codex",
        task_id=task_id,
    )
    launch_kwargs = mock_im.launch.await_args.kwargs
    assert launch_kwargs["provider"] == "codex"
    assert launch_kwargs["config_dir"] == "/pool/codex-2"
    assert launch_kwargs["resume_session_id"] == "thread-manual-1"


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
    assert resp.status_code == 200
    mock_rl.start.assert_awaited_once_with(inst_id)


@pytest.mark.asyncio
async def test_ralph_stop(client):
    mock_rl = _make_mock_ralph_loop()
    with patch("backend.main.ralph_loop", mock_rl):
        resp = await client.post("/api/instances/1/ralph/stop")
    assert resp.status_code == 200
    mock_rl.stop.assert_awaited_once_with(1)


@pytest.mark.asyncio
async def test_ralph_status(client):
    mock_rl = _make_mock_ralph_loop(running=True)
    with patch("backend.main.ralph_loop", mock_rl):
        resp = await client.get("/api/instances/1/ralph/status")
    assert resp.status_code == 200
    assert resp.json()["running"] is True
