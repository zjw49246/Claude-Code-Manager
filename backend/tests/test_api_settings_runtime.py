"""Tests for /api/settings/runtime — frontend PTY mode toggle."""
import pytest


@pytest.mark.asyncio
async def test_get_runtime_settings(client):
    resp = await client.get("/api/settings/runtime")
    assert resp.status_code == 200
    data = resp.json()
    assert "use_pty_mode" in data
    assert "pty_available" in data


@pytest.mark.asyncio
async def test_toggle_pty_mode_roundtrip(client):
    from backend.main import instance_manager

    try:
        resp = await client.put(
            "/api/settings/runtime", json={"use_pty_mode": True}
        )
        assert resp.status_code == 200
        body = resp.json()
        # claude_pty installed in dev venv -> enable succeeds
        assert body["pty_available"] is True
        assert body["use_pty_mode"] is True
        assert instance_manager.pty_mode_enabled is True

        resp = await client.put(
            "/api/settings/runtime", json={"use_pty_mode": False}
        )
        assert resp.json()["use_pty_mode"] is False
        assert instance_manager.pty_mode_enabled is False

        # GET reflects current state
        resp = await client.get("/api/settings/runtime")
        assert resp.json()["use_pty_mode"] is False
    finally:
        instance_manager.set_pty_mode(False)
