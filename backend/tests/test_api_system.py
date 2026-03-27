"""Tests for System API endpoints."""
import pytest
from sqlalchemy import update

from backend.models.task import Task
from backend.models.instance import Instance


@pytest.mark.asyncio
async def test_health(client):
    resp = await client.get("/api/system/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_stats_empty(client):
    resp = await client.get("/api/system/stats")
    assert resp.status_code == 200
    data = resp.json()
    assert data["tasks"]["pending"] == 0
    assert data["tasks"]["completed"] == 0
    assert data["running_instances"] == 0


@pytest.mark.asyncio
async def test_stats_with_tasks(client, session_factory):
    # Create tasks in various statuses
    await client.post("/api/tasks", json={"title": "A", "description": "d", "target_repo": "/tmp"})
    await client.post("/api/tasks", json={"title": "B", "description": "d", "target_repo": "/tmp"})
    create3 = await client.post("/api/tasks", json={"title": "C", "description": "d", "target_repo": "/tmp"})
    # Cancel one to change its status
    await client.post(f"/api/tasks/{create3.json()['id']}/cancel")

    resp = await client.get("/api/system/stats")
    data = resp.json()
    assert data["tasks"]["pending"] == 2


@pytest.mark.asyncio
async def test_stats_running_instances(client, session_factory):
    # Create an instance with status="running"
    async with session_factory() as db:
        inst = Instance(name="worker-test", status="running")
        db.add(inst)
        await db.commit()

    resp = await client.get("/api/system/stats")
    data = resp.json()
    assert data["running_instances"] >= 1


# === /api/system/config tests ===


@pytest.mark.asyncio
async def test_config_returns_default_model(client):
    resp = await client.get("/api/system/config")
    assert resp.status_code == 200
    data = resp.json()
    assert "default_model" in data
    assert isinstance(data["default_model"], str)
    assert len(data["default_model"]) > 0


@pytest.mark.asyncio
async def test_config_returns_model_options_list(client):
    resp = await client.get("/api/system/config")
    assert resp.status_code == 200
    data = resp.json()
    assert "model_options" in data
    assert isinstance(data["model_options"], list)
    assert len(data["model_options"]) > 0


@pytest.mark.asyncio
async def test_config_model_options_no_empty_strings(client):
    """model_options should not contain empty strings."""
    resp = await client.get("/api/system/config")
    for opt in resp.json()["model_options"]:
        assert opt.strip() != ""


@pytest.mark.asyncio
async def test_config_reflects_settings(client):
    from unittest.mock import patch
    from backend.config import settings

    with patch.object(settings, "default_model", "haiku"), \
         patch.object(settings, "model_options", "haiku,sonnet"):
        resp = await client.get("/api/system/config")
    data = resp.json()
    assert data["default_model"] == "haiku"
    assert data["model_options"] == ["haiku", "sonnet"]
