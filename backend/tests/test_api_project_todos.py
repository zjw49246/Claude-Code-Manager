"""Tests for project todo API endpoints."""
import pytest
import pytest_asyncio
from sqlalchemy import func, select

from backend.models.project import Project
from backend.models.project_todo import ProjectTodo


@pytest_asyncio.fixture
async def project_id(session_factory):
    async with session_factory() as session:
        project = Project(name="todo-proj", has_remote=False, status="ready")
        session.add(project)
        await session.commit()
        await session.refresh(project)
        return project.id


@pytest.mark.asyncio
async def test_project_todo_lifecycle(client, project_id):
    resp = await client.get(f"/api/projects/{project_id}/todos")
    assert resp.status_code == 200
    assert resp.json() == []

    resp = await client.post(
        f"/api/projects/{project_id}/todos",
        json={"title": "  Refactor auth  ", "prompt": "  Inspect auth module first.  "},
    )
    assert resp.status_code == 201
    todo = resp.json()
    assert todo["title"] == "Refactor auth"
    assert todo["prompt"] == "Inspect auth module first."
    assert todo["status"] == "open"
    assert todo["sort_order"] == 100

    resp = await client.patch(
        f"/api/projects/{project_id}/todos/{todo['id']}",
        json={"title": "Refactor auth plan", "prompt": "Write a plan.", "status": "done"},
    )
    assert resp.status_code == 200
    updated = resp.json()
    assert updated["title"] == "Refactor auth plan"
    assert updated["prompt"] == "Write a plan."
    assert updated["status"] == "done"

    resp = await client.delete(f"/api/projects/{project_id}/todos/{todo['id']}")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True

    resp = await client.get(f"/api/projects/{project_id}/todos")
    assert resp.status_code == 200
    assert resp.json() == []

    resp = await client.get(f"/api/projects/{project_id}/todos?include_archived=true")
    assert resp.status_code == 200
    archived = resp.json()
    assert len(archived) == 1
    assert archived[0]["status"] == "archived"


@pytest.mark.asyncio
async def test_project_todo_rejects_blank_fields(client, project_id):
    resp = await client.post(
        f"/api/projects/{project_id}/todos",
        json={"title": "   ", "prompt": "Do work"},
    )
    assert resp.status_code == 400

    resp = await client.post(
        f"/api/projects/{project_id}/todos",
        json={"title": "Do work", "prompt": "   "},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_project_todos_require_existing_project(client):
    resp = await client.get("/api/projects/9999/todos")
    assert resp.status_code == 404

    resp = await client.post(
        "/api/projects/9999/todos",
        json={"title": "Missing", "prompt": "Missing project"},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_project_removes_project_todos(client, project_id, session_factory):
    resp = await client.post(
        f"/api/projects/{project_id}/todos",
        json={"title": "Clean up", "prompt": "Remove with project"},
    )
    assert resp.status_code == 201

    resp = await client.delete(f"/api/projects/{project_id}")
    assert resp.status_code == 200

    async with session_factory() as session:
        count = await session.scalar(
            select(func.count()).select_from(ProjectTodo).where(ProjectTodo.project_id == project_id)
        )
    assert count == 0
