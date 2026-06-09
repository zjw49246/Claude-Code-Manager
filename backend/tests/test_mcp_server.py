"""Tests for MCP Skills Server — tool registration and HTTP calls."""
import json
import pytest
from unittest.mock import AsyncMock, patch, MagicMock

import backend.mcp.ccm_skills_server as mcp_mod


@pytest.fixture(autouse=True)
def _set_mcp_globals():
    mcp_mod._TASK_ID = 42
    mcp_mod._API_BASE = "http://localhost:9999"
    yield
    mcp_mod._TASK_ID = 0
    mcp_mod._API_BASE = "http://localhost:8000"


def test_mcp_server_tools_registered():
    tools = mcp_mod.mcp._tool_manager._tools
    names = set(tools.keys())
    assert "create_monitor" in names
    assert "check_monitors" in names
    assert "stop_monitor" in names


def test_api_url():
    assert mcp_mod._api_url("/monitor-sessions") == "http://localhost:9999/api/tasks/42/monitor-sessions"
    assert mcp_mod._api_url("/monitor-sessions/5") == "http://localhost:9999/api/tasks/42/monitor-sessions/5"


@pytest.mark.asyncio
async def test_create_monitor_success():
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"id": 7}
    mock_resp.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_resp)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("backend.mcp.ccm_skills_server.httpx.AsyncClient", return_value=mock_client):
        result = await mcp_mod.create_monitor("build progress", "tail -f build.log", 60, 10)

    data = json.loads(result)
    assert data["success"] is True
    assert data["monitor_id"] == 7
    assert data["status"] == "created"


@pytest.mark.asyncio
async def test_check_monitors_returns_sessions():
    mock_resp = MagicMock()
    mock_resp.json.return_value = [
        {"id": 1, "description": "test", "status": "running", "checks_done": 3, "max_checks": 50, "last_summary": "ok"},
    ]
    mock_resp.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_resp)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("backend.mcp.ccm_skills_server.httpx.AsyncClient", return_value=mock_client):
        result = await mcp_mod.check_monitors()

    data = json.loads(result)
    assert data["success"] is True
    assert len(data["monitors"]) == 1
    assert data["monitors"][0]["monitor_id"] == 1


@pytest.mark.asyncio
async def test_check_monitors_empty():
    mock_resp = MagicMock()
    mock_resp.json.return_value = []
    mock_resp.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_resp)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("backend.mcp.ccm_skills_server.httpx.AsyncClient", return_value=mock_client):
        result = await mcp_mod.check_monitors()

    data = json.loads(result)
    assert data["success"] is True
    assert data["monitors"] == []
    assert "没有活跃" in data["message"]


@pytest.mark.asyncio
async def test_stop_monitor_success():
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.delete = AsyncMock(return_value=mock_resp)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("backend.mcp.ccm_skills_server.httpx.AsyncClient", return_value=mock_client):
        result = await mcp_mod.stop_monitor(5)

    data = json.loads(result)
    assert data["success"] is True
    assert data["status"] == "cancelled"


@pytest.mark.asyncio
async def test_create_monitor_api_error():
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(side_effect=Exception("Connection refused"))
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("backend.mcp.ccm_skills_server.httpx.AsyncClient", return_value=mock_client):
        result = await mcp_mod.create_monitor("test")

    data = json.loads(result)
    assert data["success"] is False
    assert "Connection refused" in data["error"]


@pytest.mark.asyncio
async def test_check_monitors_api_error():
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(side_effect=Exception("timeout"))
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("backend.mcp.ccm_skills_server.httpx.AsyncClient", return_value=mock_client):
        result = await mcp_mod.check_monitors()

    data = json.loads(result)
    assert data["success"] is False
    assert "timeout" in data["error"]


@pytest.mark.asyncio
async def test_stop_monitor_api_error():
    mock_client = AsyncMock()
    mock_client.delete = AsyncMock(side_effect=Exception("not found"))
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    with patch("backend.mcp.ccm_skills_server.httpx.AsyncClient", return_value=mock_client):
        result = await mcp_mod.stop_monitor(99)

    data = json.loads(result)
    assert data["success"] is False
    assert "not found" in data["error"]
