"""Tests for Task API endpoints."""
import pytest
import pytest_asyncio
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
async def test_create_task_without_model_returns_null(client):
    """Task created without model field has model=None."""
    resp = await client.post("/api/tasks", json={
        "title": "No model", "description": "d", "target_repo": "/tmp",
    })
    assert resp.status_code == 201
    assert resp.json()["model"] is None


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
async def test_create_task_without_effort_level_returns_null(client):
    """Task created without effort_level has effort_level=None."""
    resp = await client.post("/api/tasks", json={
        "title": "No effort", "description": "d", "target_repo": "/tmp",
    })
    assert resp.status_code == 201
    assert resp.json()["effort_level"] is None


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
