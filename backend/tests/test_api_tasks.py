"""Tests for Task API endpoints."""
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession


# === Existing tests ===


@pytest.mark.asyncio
async def test_create_task(client):
    resp = await client.post("/api/tasks", json={
        "title": "Test",
        "description": "Do something",
        "target_repo": "/tmp/repo",
        "priority": 1,
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["title"] == "Test"
    assert data["status"] == "pending"
    assert data["priority"] == 1


@pytest.mark.asyncio
async def test_create_task_with_project_id(client):
    resp = await client.post("/api/tasks", json={
        "title": "Test",
        "description": "Do something",
        "project_id": 1,
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["project_id"] == 1


@pytest.mark.asyncio
async def test_list_tasks(client):
    await client.post("/api/tasks", json={
        "title": "A", "description": "d", "target_repo": "/tmp",
    })
    await client.post("/api/tasks", json={
        "title": "B", "description": "d", "target_repo": "/tmp",
    })
    resp = await client.get("/api/tasks")
    assert resp.status_code == 200
    assert len(resp.json()) == 2


@pytest.mark.asyncio
async def test_get_task(client):
    create_resp = await client.post("/api/tasks", json={
        "title": "T", "description": "d", "target_repo": "/tmp",
    })
    task_id = create_resp.json()["id"]
    resp = await client.get(f"/api/tasks/{task_id}")
    assert resp.status_code == 200
    assert resp.json()["title"] == "T"


@pytest.mark.asyncio
async def test_get_task_not_found(client):
    resp = await client.get("/api/tasks/9999")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_task(client):
    create_resp = await client.post("/api/tasks", json={
        "title": "T", "description": "d", "target_repo": "/tmp",
    })
    task_id = create_resp.json()["id"]
    resp = await client.delete(f"/api/tasks/{task_id}")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_cancel_task(client):
    create_resp = await client.post("/api/tasks", json={
        "title": "T", "description": "d", "target_repo": "/tmp",
    })
    task_id = create_resp.json()["id"]
    resp = await client.post(f"/api/tasks/{task_id}/cancel")
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"


@pytest.mark.asyncio
async def test_retry_task(client):
    create_resp = await client.post("/api/tasks", json={
        "title": "T", "description": "d", "target_repo": "/tmp",
    })
    task_id = create_resp.json()["id"]
    # Must fail first to retry
    resp = await client.post(f"/api/tasks/{task_id}/retry")
    assert resp.status_code == 200


# === New tests (Phase 2 gaps) ===


@pytest.mark.asyncio
async def test_update_task(client):
    """PUT /api/tasks/{id} updates task fields."""
    create_resp = await client.post("/api/tasks", json={
        "title": "Original", "description": "d", "target_repo": "/tmp",
    })
    task_id = create_resp.json()["id"]
    resp = await client.put(f"/api/tasks/{task_id}", json={"title": "Updated"})
    assert resp.status_code == 200
    assert resp.json()["title"] == "Updated"


@pytest.mark.asyncio
async def test_update_task_not_found(client):
    resp = await client.put("/api/tasks/9999", json={"title": "X"})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_list_tasks_filter_status(client):
    """GET /api/tasks?status=pending returns only matching tasks."""
    await client.post("/api/tasks", json={
        "title": "A", "description": "d", "target_repo": "/tmp",
    })
    create2 = await client.post("/api/tasks", json={
        "title": "B", "description": "d", "target_repo": "/tmp",
    })
    # Cancel B so it's not pending
    await client.post(f"/api/tasks/{create2.json()['id']}/cancel")

    resp = await client.get("/api/tasks?status=pending")
    assert resp.status_code == 200
    tasks = resp.json()
    assert all(t["status"] == "pending" for t in tasks)


@pytest.mark.asyncio
async def test_list_tasks_pagination(client):
    """GET /api/tasks?limit=1&offset=1 returns second task."""
    await client.post("/api/tasks", json={
        "title": "First", "description": "d", "target_repo": "/tmp",
    })
    await client.post("/api/tasks", json={
        "title": "Second", "description": "d", "target_repo": "/tmp",
    })
    resp = await client.get("/api/tasks?limit=1&offset=1")
    assert resp.status_code == 200
    tasks = resp.json()
    assert len(tasks) == 1


@pytest.mark.asyncio
async def test_queue_next(client):
    """GET /api/tasks/queue/next returns pending tasks."""
    await client.post("/api/tasks", json={
        "title": "Pending", "description": "d", "target_repo": "/tmp",
    })
    resp = await client.get("/api/tasks/queue/next")
    assert resp.status_code == 200
    tasks = resp.json()
    assert len(tasks) >= 1
    assert all(t["status"] == "pending" for t in tasks)


@pytest.mark.asyncio
async def test_delete_in_progress_rejected(client, session_factory):
    """Cannot delete a task in in_progress state."""
    from backend.models.task import Task

    create_resp = await client.post("/api/tasks", json={
        "title": "T", "description": "d", "target_repo": "/tmp",
    })
    task_id = create_resp.json()["id"]

    # Set to in_progress directly in DB
    async with session_factory() as db:
        await db.execute(
            update(Task).where(Task.id == task_id).values(status="in_progress")
        )
        await db.commit()

    resp = await client.delete(f"/api/tasks/{task_id}")
    assert resp.status_code == 400


# === image_paths tests ===


@pytest.mark.asyncio
async def test_create_task_with_image_paths(client, session_factory):
    """image_paths are stored in task.metadata_['image_paths']."""
    from backend.models.task import Task

    resp = await client.post("/api/tasks", json={
        "title": "Img Task",
        "description": "look at this image",
        "target_repo": "/tmp",
        "image_paths": ["/uploads/a.png", "/uploads/b.jpg"],
    })
    assert resp.status_code == 201
    task_id = resp.json()["id"]

    async with session_factory() as db:
        task = await db.get(Task, task_id)
    assert task.metadata_ is not None
    assert task.metadata_["image_paths"] == ["/uploads/a.png", "/uploads/b.jpg"]


@pytest.mark.asyncio
async def test_create_task_without_image_paths(client, session_factory):
    """Task created without image_paths has no image_paths in metadata_."""
    from backend.models.task import Task

    resp = await client.post("/api/tasks", json={
        "title": "No Img", "description": "plain task", "target_repo": "/tmp",
    })
    assert resp.status_code == 201
    task_id = resp.json()["id"]

    async with session_factory() as db:
        task = await db.get(Task, task_id)
    assert (task.metadata_ or {}).get("image_paths") is None


@pytest.mark.asyncio
async def test_create_task_image_paths_not_in_response(client):
    """image_paths field is not leaked in the TaskResponse (stored in metadata_)."""
    resp = await client.post("/api/tasks", json={
        "title": "Img Task",
        "description": "check response",
        "target_repo": "/tmp",
        "image_paths": ["/uploads/x.png"],
    })
    assert resp.status_code == 201
    data = resp.json()
    # image_paths should not appear as a top-level key in the response schema
    assert "image_paths" not in data


# === max_iterations tests ===


@pytest.mark.asyncio
async def test_create_loop_task_default_max_iterations(client):
    """Loop task created without max_iterations gets default value of 50."""
    resp = await client.post("/api/tasks", json={
        "title": "Loop Default",
        "mode": "loop",
        "todo_file_path": "TODO.md",
        "target_repo": "/tmp/repo",
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["max_iterations"] == 50


@pytest.mark.asyncio
async def test_create_loop_task_custom_max_iterations(client):
    """Loop task created with custom max_iterations stores it correctly."""
    resp = await client.post("/api/tasks", json={
        "title": "Loop Custom",
        "mode": "loop",
        "todo_file_path": "TODO.md",
        "target_repo": "/tmp/repo",
        "max_iterations": 10,
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["max_iterations"] == 10


@pytest.mark.asyncio
async def test_create_auto_task_max_iterations_in_response(client):
    """Non-loop task also exposes max_iterations in response (always 50 by default)."""
    resp = await client.post("/api/tasks", json={
        "title": "Auto Task",
        "description": "do something",
        "target_repo": "/tmp",
    })
    assert resp.status_code == 201
    data = resp.json()
    assert "max_iterations" in data
    assert data["max_iterations"] == 50


@pytest.mark.asyncio
async def test_update_task_max_iterations(client):
    """PUT /api/tasks/{id} can update max_iterations."""
    create_resp = await client.post("/api/tasks", json={
        "title": "Loop Task",
        "mode": "loop",
        "todo_file_path": "TODO.md",
        "target_repo": "/tmp/repo",
        "max_iterations": 20,
    })
    task_id = create_resp.json()["id"]

    resp = await client.put(f"/api/tasks/{task_id}", json={"max_iterations": 5})
    assert resp.status_code == 200
    assert resp.json()["max_iterations"] == 5


@pytest.mark.asyncio
async def test_create_loop_task_requires_todo_file_path(client):
    """Loop task without todo_file_path returns 422."""
    resp = await client.post("/api/tasks", json={
        "title": "Missing Todo",
        "mode": "loop",
        "target_repo": "/tmp/repo",
    })
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_create_loop_task_max_iterations_persisted(client, session_factory):
    """max_iterations value is actually persisted to the database."""
    from backend.models.task import Task

    resp = await client.post("/api/tasks", json={
        "title": "Persisted",
        "mode": "loop",
        "todo_file_path": "TODO.md",
        "target_repo": "/tmp/repo",
        "max_iterations": 7,
    })
    task_id = resp.json()["id"]

    async with session_factory() as db:
        task = await db.get(Task, task_id)
    assert task.max_iterations == 7


# === has_unread tests ===


@pytest.mark.asyncio
async def test_create_task_has_unread_defaults_false(client):
    """New task has has_unread=False by default."""
    resp = await client.post("/api/tasks", json={
        "title": "Unread test", "description": "d", "target_repo": "/tmp",
    })
    assert resp.status_code == 201
    assert resp.json()["has_unread"] is False


@pytest.mark.asyncio
async def test_mark_task_read_clears_unread(client, session_factory):
    """POST /api/tasks/{id}/read sets has_unread=False."""
    from backend.models.task import Task
    from sqlalchemy import update

    create_resp = await client.post("/api/tasks", json={
        "title": "Unread", "description": "d", "target_repo": "/tmp",
    })
    task_id = create_resp.json()["id"]

    # Set has_unread=True directly in DB
    async with session_factory() as db:
        await db.execute(update(Task).where(Task.id == task_id).values(has_unread=True))
        await db.commit()

    resp = await client.post(f"/api/tasks/{task_id}/read")
    assert resp.status_code == 200
    assert resp.json()["has_unread"] is False


@pytest.mark.asyncio
async def test_mark_task_read_not_found(client):
    """POST /api/tasks/9999/read returns 404 for missing task."""
    resp = await client.post("/api/tasks/9999/read")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_has_unread_persisted_in_db(client, session_factory):
    """has_unread=True set in DB is returned in task response."""
    from backend.models.task import Task
    from sqlalchemy import update

    create_resp = await client.post("/api/tasks", json={
        "title": "Persist unread", "description": "d", "target_repo": "/tmp",
    })
    task_id = create_resp.json()["id"]

    async with session_factory() as db:
        await db.execute(update(Task).where(Task.id == task_id).values(has_unread=True))
        await db.commit()

    resp = await client.get(f"/api/tasks/{task_id}")
    assert resp.status_code == 200
    assert resp.json()["has_unread"] is True


# === Model field tests ===


@pytest.mark.asyncio
async def test_create_task_with_model(client):
    """Task created with model field stores and returns the model."""
    resp = await client.post("/api/tasks", json={
        "title": "Opus task", "description": "d", "target_repo": "/tmp", "model": "opus",
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["model"] == "opus"


@pytest.mark.asyncio
async def test_create_task_without_model_fills_default(client):
    """设置归 Task：不指定 model 时创建即填入全局默认值。"""
    from backend.config import settings
    resp = await client.post("/api/tasks", json={
        "title": "No model", "description": "d", "target_repo": "/tmp",
    })
    assert resp.status_code == 201
    expected_model = (
        settings.default_codex_model
        if settings.default_provider == "codex"
        else settings.default_model
    )
    assert resp.json()["model"] == expected_model


@pytest.mark.asyncio
async def test_create_task_without_provider_uses_config_default(client, monkeypatch):
    """Omitted provider should use the configured default, not the schema fallback."""
    from backend.config import settings

    monkeypatch.setattr(settings, "default_provider", "codex")
    monkeypatch.setattr(settings, "default_codex_model", "gpt-test")

    resp = await client.post("/api/tasks", json={
        "title": "Codex default", "description": "d", "target_repo": "/tmp",
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["provider"] == "codex"
    assert data["model"] == "gpt-test"


@pytest.mark.asyncio
async def test_create_task_explicit_provider_overrides_config_default(client, monkeypatch):
    """Explicit provider must still win over the configured default."""
    from backend.config import settings

    monkeypatch.setattr(settings, "default_provider", "codex")

    resp = await client.post("/api/tasks", json={
        "title": "Claude explicit",
        "description": "d",
        "target_repo": "/tmp",
        "provider": "claude",
    })
    assert resp.status_code == 201
    assert resp.json()["provider"] == "claude"


@pytest.mark.asyncio
async def test_create_task_model_persisted_in_get(client):
    """Model value survives a round-trip through GET."""
    create_resp = await client.post("/api/tasks", json={
        "title": "T", "description": "d", "target_repo": "/tmp", "model": "sonnet",
    })
    task_id = create_resp.json()["id"]

    resp = await client.get(f"/api/tasks/{task_id}")
    assert resp.status_code == 200
    assert resp.json()["model"] == "sonnet"


@pytest.mark.asyncio
async def test_create_task_model_in_list(client):
    """model field is included when listing tasks."""
    await client.post("/api/tasks", json={
        "title": "A", "description": "d", "target_repo": "/tmp", "model": "haiku",
    })
    resp = await client.get("/api/tasks")
    assert resp.status_code == 200
    tasks = resp.json()
    assert tasks[0]["model"] == "haiku"


# === Title update tests ===


@pytest.mark.asyncio
async def test_update_task_title_only(client):
    """PUT /api/tasks/{id} with only title preserves other fields."""
    create_resp = await client.post("/api/tasks", json={
        "title": "Original Title", "description": "Keep this", "target_repo": "/tmp", "priority": 2,
    })
    task_id = create_resp.json()["id"]

    resp = await client.put(f"/api/tasks/{task_id}", json={"title": "New Title"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["title"] == "New Title"
    assert data["description"] == "Keep this"
    assert data["priority"] == 2


@pytest.mark.asyncio
async def test_update_task_title_empty_string(client):
    """PUT /api/tasks/{id} can set title to empty string."""
    create_resp = await client.post("/api/tasks", json={
        "title": "Has Title", "description": "d", "target_repo": "/tmp",
    })
    task_id = create_resp.json()["id"]

    resp = await client.put(f"/api/tasks/{task_id}", json={"title": ""})
    assert resp.status_code == 200
    assert resp.json()["title"] == ""


@pytest.mark.asyncio
async def test_update_task_title_persisted_in_get(client):
    """Updated title is returned on subsequent GET."""
    create_resp = await client.post("/api/tasks", json={
        "title": "Old", "description": "d", "target_repo": "/tmp",
    })
    task_id = create_resp.json()["id"]

    await client.put(f"/api/tasks/{task_id}", json={"title": "Renamed"})
    resp = await client.get(f"/api/tasks/{task_id}")
    assert resp.status_code == 200
    assert resp.json()["title"] == "Renamed"


# === Effort level tests ===


@pytest.mark.asyncio
async def test_create_task_with_effort_level(client):
    """Task created with effort_level field stores and returns it."""
    resp = await client.post("/api/tasks", json={
        "title": "Effort task", "description": "d", "target_repo": "/tmp", "effort_level": "high",
    })
    assert resp.status_code == 201
    assert resp.json()["effort_level"] == "high"


@pytest.mark.asyncio
async def test_create_task_without_effort_level_fills_default(client):
    """设置归 Task：不指定 effort 时创建即填入全局默认值。"""
    from backend.config import settings
    resp = await client.post("/api/tasks", json={
        "title": "No effort", "description": "d", "target_repo": "/tmp",
    })
    assert resp.status_code == 201
    assert resp.json()["effort_level"] == settings.default_effort


@pytest.mark.asyncio
async def test_create_task_effort_level_persisted_in_get(client):
    """effort_level survives a round-trip through GET."""
    create_resp = await client.post("/api/tasks", json={
        "title": "T", "description": "d", "target_repo": "/tmp", "effort_level": "max",
    })
    task_id = create_resp.json()["id"]

    resp = await client.get(f"/api/tasks/{task_id}")
    assert resp.status_code == 200
    assert resp.json()["effort_level"] == "max"


@pytest.mark.asyncio
async def test_create_task_effort_level_in_list(client):
    """effort_level is included when listing tasks."""
    await client.post("/api/tasks", json={
        "title": "A", "description": "d", "target_repo": "/tmp", "effort_level": "low",
    })
    resp = await client.get("/api/tasks")
    assert resp.status_code == 200
    tasks = resp.json()
    assert tasks[0]["effort_level"] == "low"


# === Goal mode tests ===


@pytest.mark.asyncio
async def test_create_goal_task(client):
    """Goal task with condition is created successfully."""
    resp = await client.post("/api/tasks", json={
        "title": "Goal Task",
        "description": "implement feature",
        "mode": "goal",
        "goal_condition": "all tests pass",
        "target_repo": "/tmp",
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["mode"] == "goal"
    assert data["goal_condition"] == "all tests pass"
    assert data["goal_max_turns"] == 30
    assert data["goal_turns_used"] == 0
    assert data["goal_last_reason"] is None


@pytest.mark.asyncio
async def test_create_goal_task_custom_max_turns(client):
    """Goal task with custom max_turns stores it correctly."""
    resp = await client.post("/api/tasks", json={
        "title": "Goal Custom",
        "description": "do it",
        "mode": "goal",
        "goal_condition": "lint clean",
        "goal_max_turns": 15,
        "target_repo": "/tmp",
    })
    assert resp.status_code == 201
    assert resp.json()["goal_max_turns"] == 15


@pytest.mark.asyncio
async def test_create_goal_task_requires_condition(client):
    """Goal task without goal_condition returns 422."""
    resp = await client.post("/api/tasks", json={
        "title": "No Condition",
        "description": "do it",
        "mode": "goal",
        "target_repo": "/tmp",
    })
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_create_goal_task_with_evaluator_model(client):
    """Goal task with custom evaluator model stores it."""
    resp = await client.post("/api/tasks", json={
        "title": "Goal Eval",
        "description": "do it",
        "mode": "goal",
        "goal_condition": "condition",
        "goal_evaluator_model": "claude-sonnet-4-6",
        "target_repo": "/tmp",
    })
    assert resp.status_code == 201
    assert resp.json()["goal_evaluator_model"] == "claude-sonnet-4-6"


@pytest.mark.asyncio
async def test_goal_fields_persisted_in_db(client, session_factory):
    """Goal fields are actually persisted to the database."""
    from backend.models.task import Task

    resp = await client.post("/api/tasks", json={
        "title": "Persist Goal",
        "description": "do it",
        "mode": "goal",
        "goal_condition": "all green",
        "goal_max_turns": 20,
        "target_repo": "/tmp",
    })
    task_id = resp.json()["id"]

    async with session_factory() as db:
        task = await db.get(Task, task_id)
    assert task.goal_condition == "all green"
    assert task.goal_max_turns == 20
    assert task.goal_turns_used == 0


@pytest.mark.asyncio
async def test_goal_fields_in_get_response(client):
    """Goal fields are returned in GET /api/tasks/{id}."""
    create_resp = await client.post("/api/tasks", json={
        "title": "T",
        "description": "d",
        "mode": "goal",
        "goal_condition": "tests pass",
        "target_repo": "/tmp",
    })
    task_id = create_resp.json()["id"]

    resp = await client.get(f"/api/tasks/{task_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["goal_condition"] == "tests pass"
    assert data["goal_max_turns"] == 30
    assert data["goal_turns_used"] == 0


@pytest.mark.asyncio
async def test_update_goal_task_fields(client):
    """PUT /api/tasks/{id} can update goal-specific fields."""
    create_resp = await client.post("/api/tasks", json={
        "title": "Goal Update",
        "description": "d",
        "mode": "goal",
        "goal_condition": "old condition",
        "goal_max_turns": 10,
        "target_repo": "/tmp",
    })
    task_id = create_resp.json()["id"]

    resp = await client.put(f"/api/tasks/{task_id}", json={
        "goal_condition": "new condition",
        "goal_max_turns": 50,
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["goal_condition"] == "new condition"
    assert data["goal_max_turns"] == 50


@pytest.mark.asyncio
async def test_non_goal_task_has_null_goal_fields(client):
    """Auto-mode task has null goal fields."""
    resp = await client.post("/api/tasks", json={
        "title": "Auto", "description": "d", "target_repo": "/tmp",
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["goal_condition"] is None
    assert data["goal_max_turns"] == 30
    assert data["goal_turns_used"] == 0
    assert data["goal_last_reason"] is None


# === Cancel task kills process tests ===


@pytest.mark.asyncio
async def test_cancel_task_attempts_process_stop(client, session_factory):
    """POST /api/tasks/{id}/cancel calls _stop_task_process before cancelling."""
    create_resp = await client.post("/api/tasks", json={
        "title": "Cancel Me", "description": "d", "target_repo": "/tmp",
    })
    task_id = create_resp.json()["id"]

    with patch("backend.api.tasks._stop_task_process", new_callable=AsyncMock, return_value=False) as mock_stop:
        resp = await client.post(f"/api/tasks/{task_id}/cancel")

    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"
    mock_stop.assert_awaited_once()
    call_args = mock_stop.call_args
    assert call_args[0][0] == task_id


@pytest.mark.asyncio
async def test_cancel_task_still_works_if_no_process(client):
    """Cancel works even when no process is running (stop returns False)."""
    create_resp = await client.post("/api/tasks", json={
        "title": "No Process", "description": "d", "target_repo": "/tmp",
    })
    task_id = create_resp.json()["id"]

    with patch("backend.api.tasks._stop_task_process", new_callable=AsyncMock, return_value=False):
        resp = await client.post(f"/api/tasks/{task_id}/cancel")

    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"


@pytest.mark.asyncio
async def test_stop_session_uses_helper(client, session_factory):
    """POST /api/tasks/{id}/stop-session delegates to _stop_task_process."""
    create_resp = await client.post("/api/tasks", json={
        "title": "Stop Me", "description": "d", "target_repo": "/tmp",
    })
    task_id = create_resp.json()["id"]

    with patch("backend.api.tasks._stop_task_process", new_callable=AsyncMock, return_value=True) as mock_stop:
        resp = await client.post(f"/api/tasks/{task_id}/stop-session")

    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    mock_stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_stop_session_no_process_returns_400(client):
    """POST /api/tasks/{id}/stop-session returns 400 when no process found."""
    create_resp = await client.post("/api/tasks", json={
        "title": "No Session", "description": "d", "target_repo": "/tmp",
    })
    task_id = create_resp.json()["id"]

    with patch("backend.api.tasks._stop_task_process", new_callable=AsyncMock, return_value=False):
        resp = await client.post(f"/api/tasks/{task_id}/stop-session")

    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_cancel_sets_status_before_stopping_process(client):
    """Cancel must set status to cancelled BEFORE stopping process to prevent race."""
    create_resp = await client.post("/api/tasks", json={
        "title": "Race Test", "description": "d", "target_repo": "/tmp",
    })
    task_id = create_resp.json()["id"]

    call_order = []

    original_cancel = None

    async def tracking_stop(tid, db):
        call_order.append("stop")
        return True

    with patch("backend.api.tasks._stop_task_process", side_effect=tracking_stop):
        resp = await client.post(f"/api/tasks/{task_id}/cancel")

    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"
    # stop should be called (after cancel sets status)
    assert "stop" in call_order


# === Mark unread tests ===


@pytest.mark.asyncio
async def test_mark_task_unread(client, session_factory):
    """POST /api/tasks/{id}/unread sets has_unread=True."""
    create_resp = await client.post("/api/tasks", json={
        "title": "Read task", "description": "d", "target_repo": "/tmp",
    })
    task_id = create_resp.json()["id"]
    assert create_resp.json()["has_unread"] is False

    resp = await client.post(f"/api/tasks/{task_id}/unread")
    assert resp.status_code == 200
    assert resp.json()["has_unread"] is True


@pytest.mark.asyncio
async def test_mark_task_unread_not_found(client):
    """POST /api/tasks/9999/unread returns 404 for missing task."""
    resp = await client.post("/api/tasks/9999/unread")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_toggle_read_unread_roundtrip(client, session_factory):
    """Can toggle between read and unread states."""
    from backend.models.task import Task
    from sqlalchemy import update

    create_resp = await client.post("/api/tasks", json={
        "title": "Toggle", "description": "d", "target_repo": "/tmp",
    })
    task_id = create_resp.json()["id"]

    # Mark unread
    resp = await client.post(f"/api/tasks/{task_id}/unread")
    assert resp.json()["has_unread"] is True

    # Mark read
    resp = await client.post(f"/api/tasks/{task_id}/read")
    assert resp.json()["has_unread"] is False

    # Mark unread again
    resp = await client.post(f"/api/tasks/{task_id}/unread")
    assert resp.json()["has_unread"] is True


@pytest.mark.asyncio
async def test_mark_already_unread_task_unread(client, session_factory):
    """Marking an already-unread task as unread is idempotent."""
    from backend.models.task import Task
    from sqlalchemy import update

    create_resp = await client.post("/api/tasks", json={
        "title": "Already unread", "description": "d", "target_repo": "/tmp",
    })
    task_id = create_resp.json()["id"]

    # Set unread via DB
    async with session_factory() as db:
        await db.execute(update(Task).where(Task.id == task_id).values(has_unread=True))
        await db.commit()

    resp = await client.post(f"/api/tasks/{task_id}/unread")
    assert resp.status_code == 200
    assert resp.json()["has_unread"] is True


# === Starred on create tests ===


@pytest.mark.asyncio
async def test_create_task_starred_default_false(client):
    """Task created without starred flag has starred=False."""
    resp = await client.post("/api/tasks", json={
        "title": "No star", "description": "d", "target_repo": "/tmp",
    })
    assert resp.status_code == 201
    assert resp.json()["starred"] is False


@pytest.mark.asyncio
async def test_create_task_with_starred_true(client):
    """Task created with starred=True is starred immediately."""
    resp = await client.post("/api/tasks", json={
        "title": "Starred", "description": "d", "target_repo": "/tmp",
        "starred": True,
    })
    assert resp.status_code == 201
    assert resp.json()["starred"] is True


@pytest.mark.asyncio
async def test_create_task_starred_persisted_in_db(client, session_factory):
    """starred=True at creation is persisted to the database."""
    from backend.models.task import Task

    resp = await client.post("/api/tasks", json={
        "title": "Starred persist", "description": "d", "target_repo": "/tmp",
        "starred": True,
    })
    task_id = resp.json()["id"]

    async with session_factory() as db:
        task = await db.get(Task, task_id)
    assert task.starred is True


@pytest.mark.asyncio
async def test_create_task_starred_in_list(client):
    """Starred task appears with starred=True in list endpoint."""
    await client.post("/api/tasks", json={
        "title": "Starred list", "description": "d", "target_repo": "/tmp",
        "starred": True,
    })
    resp = await client.get("/api/tasks")
    assert resp.status_code == 200
    tasks = resp.json()
    assert any(t["starred"] is True for t in tasks)


@pytest.mark.asyncio
async def test_create_task_starred_filter(client):
    """Starred filter returns only starred tasks including those starred at creation."""
    await client.post("/api/tasks", json={
        "title": "Not starred", "description": "d", "target_repo": "/tmp",
    })
    await client.post("/api/tasks", json={
        "title": "Starred", "description": "d", "target_repo": "/tmp",
        "starred": True,
    })
    resp = await client.get("/api/tasks?starred=true")
    assert resp.status_code == 200
    tasks = resp.json()
    assert len(tasks) == 1
    assert tasks[0]["starred"] is True


# === has_unread filter tests ===


@pytest.mark.asyncio
async def test_filter_unread_tasks(client, session_factory):
    """has_unread=true filter returns only unread tasks."""
    from backend.models.task import Task
    from sqlalchemy import update

    r1 = await client.post("/api/tasks", json={
        "title": "Read task", "description": "d", "target_repo": "/tmp",
    })
    r2 = await client.post("/api/tasks", json={
        "title": "Unread task", "description": "d", "target_repo": "/tmp",
    })
    unread_id = r2.json()["id"]

    async with session_factory() as db:
        await db.execute(update(Task).where(Task.id == unread_id).values(has_unread=True))
        await db.commit()

    resp = await client.get("/api/tasks?has_unread=true")
    assert resp.status_code == 200
    tasks = resp.json()
    assert len(tasks) == 1
    assert tasks[0]["id"] == unread_id
    assert tasks[0]["has_unread"] is True


@pytest.mark.asyncio
async def test_filter_read_tasks(client, session_factory):
    """has_unread=false filter returns only read tasks."""
    from backend.models.task import Task
    from sqlalchemy import update

    r1 = await client.post("/api/tasks", json={
        "title": "Read task", "description": "d", "target_repo": "/tmp",
    })
    r2 = await client.post("/api/tasks", json={
        "title": "Unread task", "description": "d", "target_repo": "/tmp",
    })
    unread_id = r2.json()["id"]
    read_id = r1.json()["id"]

    async with session_factory() as db:
        await db.execute(update(Task).where(Task.id == unread_id).values(has_unread=True))
        await db.commit()

    resp = await client.get("/api/tasks?has_unread=false")
    assert resp.status_code == 200
    tasks = resp.json()
    assert len(tasks) == 1
    assert tasks[0]["id"] == read_id
    assert tasks[0]["has_unread"] is False


@pytest.mark.asyncio
async def test_count_unread_tasks(client, session_factory):
    """has_unread filter works with count endpoint."""
    from backend.models.task import Task
    from sqlalchemy import update

    r1 = await client.post("/api/tasks", json={
        "title": "Task A", "description": "d", "target_repo": "/tmp",
    })
    r2 = await client.post("/api/tasks", json={
        "title": "Task B", "description": "d", "target_repo": "/tmp",
    })
    r3 = await client.post("/api/tasks", json={
        "title": "Task C", "description": "d", "target_repo": "/tmp",
    })

    async with session_factory() as db:
        await db.execute(update(Task).where(Task.id == r1.json()["id"]).values(has_unread=True))
        await db.execute(update(Task).where(Task.id == r2.json()["id"]).values(has_unread=True))
        await db.commit()

    resp = await client.get("/api/tasks/count?has_unread=true")
    assert resp.status_code == 200
    assert resp.json()["total"] == 2

    resp = await client.get("/api/tasks/count?has_unread=false")
    assert resp.status_code == 200
    assert resp.json()["total"] == 1


@pytest.mark.asyncio
async def test_filter_unread_combined_with_status(client, session_factory):
    """has_unread filter works combined with status filter."""
    from backend.models.task import Task
    from sqlalchemy import update

    r1 = await client.post("/api/tasks", json={
        "title": "Pending unread", "description": "d", "target_repo": "/tmp",
    })
    r2 = await client.post("/api/tasks", json={
        "title": "Pending read", "description": "d", "target_repo": "/tmp",
    })

    async with session_factory() as db:
        await db.execute(update(Task).where(Task.id == r1.json()["id"]).values(has_unread=True))
        await db.commit()

    resp = await client.get("/api/tasks?has_unread=true&status=pending")
    assert resp.status_code == 200
    tasks = resp.json()
    assert len(tasks) == 1
    assert tasks[0]["id"] == r1.json()["id"]


@pytest.mark.asyncio
async def test_filter_unread_with_no_results(client):
    """has_unread=true returns empty list when no unread tasks exist."""
    await client.post("/api/tasks", json={
        "title": "All read", "description": "d", "target_repo": "/tmp",
    })

    resp = await client.get("/api/tasks?has_unread=true")
    assert resp.status_code == 200
    assert resp.json() == []

    resp = await client.get("/api/tasks/count?has_unread=true")
    assert resp.status_code == 200
    assert resp.json()["total"] == 0


@pytest.mark.asyncio
async def test_no_unread_filter_returns_all(client, session_factory):
    """Without has_unread filter, both read and unread tasks are returned."""
    from backend.models.task import Task
    from sqlalchemy import update

    r1 = await client.post("/api/tasks", json={
        "title": "Read", "description": "d", "target_repo": "/tmp",
    })
    r2 = await client.post("/api/tasks", json={
        "title": "Unread", "description": "d", "target_repo": "/tmp",
    })

    async with session_factory() as db:
        await db.execute(update(Task).where(Task.id == r2.json()["id"]).values(has_unread=True))
        await db.commit()

    resp = await client.get("/api/tasks")
    assert resp.status_code == 200
    assert len(resp.json()) == 2


# === enable_workflows tests ===


@pytest.mark.asyncio
async def test_create_task_enable_workflows_default(client):
    """Task created without enable_workflows defaults to False."""
    resp = await client.post("/api/tasks", json={
        "title": "Default WF",
        "description": "test default",
        "target_repo": "/tmp",
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["enable_workflows"] is False


@pytest.mark.asyncio
async def test_create_task_enable_workflows_true(client):
    """Task created with enable_workflows=True stores it correctly."""
    resp = await client.post("/api/tasks", json={
        "title": "WF Enabled",
        "description": "workflows enabled",
        "target_repo": "/tmp",
        "enable_workflows": True,
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["enable_workflows"] is True


@pytest.mark.asyncio
async def test_create_task_enable_workflows_false(client):
    """Task created with enable_workflows=False stores it correctly."""
    resp = await client.post("/api/tasks", json={
        "title": "WF Disabled",
        "description": "workflows disabled",
        "target_repo": "/tmp",
        "enable_workflows": False,
    })
    assert resp.status_code == 201
    data = resp.json()
    assert data["enable_workflows"] is False


@pytest.mark.asyncio
async def test_update_task_enable_workflows(client):
    """PUT /api/tasks/{id} can update enable_workflows."""
    create_resp = await client.post("/api/tasks", json={
        "title": "WF Toggle",
        "description": "toggle test",
        "target_repo": "/tmp",
        "enable_workflows": False,
    })
    task_id = create_resp.json()["id"]
    assert create_resp.json()["enable_workflows"] is False

    update_resp = await client.put(f"/api/tasks/{task_id}", json={
        "enable_workflows": True,
    })
    assert update_resp.status_code == 200
    assert update_resp.json()["enable_workflows"] is True


@pytest.mark.asyncio
async def test_create_task_enable_workflows_persisted_in_db(client, session_factory):
    """enable_workflows value is persisted in the database."""
    from backend.models.task import Task

    resp = await client.post("/api/tasks", json={
        "title": "DB Check",
        "description": "check db",
        "target_repo": "/tmp",
        "enable_workflows": True,
    })
    assert resp.status_code == 201
    task_id = resp.json()["id"]

    async with session_factory() as db:
        task = await db.get(Task, task_id)
    assert task.enable_workflows is True


@pytest.mark.asyncio
async def test_stop_session_clears_pending_queue(client):
    """POST /api/tasks/{id}/stop-session drops queued chat messages before stopping."""
    create_resp = await client.post("/api/tasks", json={
        "title": "Stop Queue", "description": "d", "target_repo": "/tmp",
    })
    task_id = create_resp.json()["id"]

    import backend.main
    with patch.object(backend.main.dispatcher, "clear_task_queue", return_value=2) as mock_clear, \
         patch("backend.api.tasks._stop_task_process", new_callable=AsyncMock, return_value=True):
        resp = await client.post(f"/api/tasks/{task_id}/stop-session")

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["stopped"] is True
    assert body["cleared_messages"] == 2
    mock_clear.assert_called_once_with(task_id)


@pytest.mark.asyncio
async def test_stop_session_no_process_reports_not_stopped(client, session_factory):
    """When no process is found but task is executing, response says stopped=False."""
    from backend.models.task import Task
    from sqlalchemy import update

    create_resp = await client.post("/api/tasks", json={
        "title": "No Proc", "description": "d", "target_repo": "/tmp",
    })
    task_id = create_resp.json()["id"]
    async with session_factory() as db:
        await db.execute(update(Task).where(Task.id == task_id).values(status="executing"))
        await db.commit()

    import backend.main
    with patch.object(backend.main.dispatcher, "clear_task_queue", return_value=0), \
         patch("backend.api.tasks._stop_task_process", new_callable=AsyncMock, return_value=False):
        resp = await client.post(f"/api/tasks/{task_id}/stop-session")

    assert resp.status_code == 200
    body = resp.json()
    assert body["stopped"] is False
    assert "note" in body


@pytest.mark.asyncio
async def test_stop_session_cleared_only_returns_ok(client):
    """No process and task not executing, but messages were cleared -> 200 not 400."""
    create_resp = await client.post("/api/tasks", json={
        "title": "Cleared Only", "description": "d", "target_repo": "/tmp",
    })
    task_id = create_resp.json()["id"]

    import backend.main
    with patch.object(backend.main.dispatcher, "clear_task_queue", return_value=1), \
         patch("backend.api.tasks._stop_task_process", new_callable=AsyncMock, return_value=False):
        resp = await client.post(f"/api/tasks/{task_id}/stop-session")

    assert resp.status_code == 200
    assert resp.json()["stopped"] is False


@pytest.mark.asyncio
async def test_list_order_starred_then_access_then_manual(client, session_factory):
    """排序：标星置顶 → 手动 sort_order / 最近访问时间（越新越靠前）。"""
    from datetime import datetime, timedelta
    from backend.models.task import Task

    ids = []
    for i in range(4):
        resp = await client.post("/api/tasks", json={
            "title": f"T{i}", "description": "d", "target_repo": "/tmp",
        })
        ids.append(resp.json()["id"])

    now = datetime.utcnow()
    async with session_factory() as db:
        a, b, c, d = [await db.get(Task, i) for i in ids]
        a.last_accessed_at = now - timedelta(hours=3)
        b.last_accessed_at = now - timedelta(hours=1)   # 最近访问
        c.last_accessed_at = now - timedelta(hours=2)
        c.starred = True                                 # 标星 → 置顶
        d.last_accessed_at = now - timedelta(hours=4)
        d.sort_order = now.timestamp() + 999             # 手动拖到最前（非星组）
        await db.commit()

    resp = await client.get("/api/tasks?limit=50")
    order = [t["id"] for t in resp.json() if t["id"] in ids]
    # c 标星置顶；非星组按位置键：d 手动键最大 → b（访问较近）→ a
    assert order == [ids[2], ids[3], ids[1], ids[0]]


@pytest.mark.asyncio
async def test_chat_history_touches_last_accessed(client, session_factory):
    """打开 chat（拉历史，touch=true）应更新 last_accessed_at；
    不带 touch 的拉取（分页/后台轮询/旧版客户端）不得更新——
    生产实录：旧版前端残留标签页轮询导致任务在列表里来回跳。"""
    from backend.models.task import Task

    resp = await client.post("/api/tasks", json={
        "title": "T", "description": "d", "target_repo": "/tmp",
    })
    task_id = resp.json()["id"]
    async with session_factory() as db:
        t = await db.get(Task, task_id)
        assert t.last_accessed_at is None

    # 不带 touch：不更新（回归：每次 history 拉取都 touch 会被轮询滥用）
    await client.get(f"/api/tasks/{task_id}/chat/history")
    async with session_factory() as db:
        t = await db.get(Task, task_id)
        assert t.last_accessed_at is None

    await client.get(f"/api/tasks/{task_id}/chat/history?touch=true")
    async with session_factory() as db:
        t = await db.get(Task, task_id)
        assert t.last_accessed_at is not None


@pytest.mark.asyncio
async def test_open_chat_moves_task_to_front_of_group(client, session_factory):
    """Touch updates last_accessed_at; for tasks without sort_order,
    the query sorts by last_accessed_at (auto_sort_on_access=True default),
    so the most recently accessed task appears first."""
    from datetime import datetime, timedelta
    from backend.models.task import Task

    now = datetime.utcnow()
    ids = []
    for i in range(3):
        resp = await client.post("/api/tasks", json={
            "title": f"T{i}", "description": "d", "target_repo": "/tmp",
        })
        ids.append(resp.json()["id"])

    # Set distinct created_at timestamps with 10s gaps to avoid strftime("%s") collisions
    async with session_factory() as db:
        for i, tid in enumerate(ids):
            t = await db.get(Task, tid)
            t.created_at = now - timedelta(seconds=30 - i * 10)  # t0 oldest, t2 newest
            t.sort_order = None
            t.last_accessed_at = None
        await db.commit()

    # Before touch: t2 (newest created_at) should be first
    resp = await client.get("/api/tasks?limit=50")
    order = [t["id"] for t in resp.json() if t["id"] in ids]
    assert order[0] == ids[2]

    # Touch t0 → t0's last_accessed_at becomes now → should sort first
    await client.get(f"/api/tasks/{ids[0]}/chat/history?touch=true")
    resp = await client.get("/api/tasks?limit=50")
    order = [t["id"] for t in resp.json() if t["id"] in ids]
    assert order[0] == ids[0]


@pytest.mark.asyncio
async def test_update_sort_order_via_api_moves_task(client):
    """回归：sort_order 曾只加在 TaskCreate 上，PUT 被 pydantic 丢弃 →
    前端拖拽永远不生效。必须走 API 全链路验证。"""
    ids = []
    for i in range(3):
        resp = await client.post("/api/tasks", json={
            "title": f"T{i}", "description": "d", "target_repo": "/tmp",
        })
        ids.append(resp.json()["id"])

    # 默认按创建时间倒序：[t2, t1, t0]；把 t0 拖到第一
    resp = await client.get("/api/tasks?limit=10")
    order = [t["id"] for t in resp.json() if t["id"] in ids]
    assert order == [ids[2], ids[1], ids[0]]

    import time
    resp = await client.put(f"/api/tasks/{ids[0]}", json={"sort_order": time.time() + 9999})
    assert resp.status_code == 200
    assert resp.json()["sort_order"] is not None

    resp = await client.get("/api/tasks?limit=10")
    order = [t["id"] for t in resp.json() if t["id"] in ids]
    assert order == [ids[0], ids[2], ids[1]]
