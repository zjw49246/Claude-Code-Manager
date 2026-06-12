"""PTY 权限透传 — CC 权限请求 → 前端卡片 → 用户回包 → BridgeHub。

覆盖：
- _handle_pty_permission_request：落库 LogEntry + 广播 permission_request + 登记 pending
- resolve_pty_permission：回包 bridge、广播 permission_resolved；未知/过期返回 False
- POST /api/tasks/{id}/permissions/{request_id} 端点（allow/deny/410/400）
"""
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select

from backend.models.task import Task
from backend.models.log_entry import LogEntry
from backend.services.instance_manager import InstanceManager


class _FakeBroadcaster:
    def __init__(self):
        self.events = []

    async def broadcast(self, channel, data):
        self.events.append((channel, data))


def _make_im(db_factory):
    im = InstanceManager.__new__(InstanceManager)
    im.db_factory = db_factory
    im.broadcaster = _FakeBroadcaster()
    im._pty_permissions = {}
    im._pty_backend = None
    im._loop = None
    return im


REQUEST = {
    "request_id": "perm-1",
    "tool_name": "Bash",
    "description": "运行 rm -rf /tmp/x",
    "input_preview": "rm -rf /tmp/x",
}


@pytest.mark.asyncio
async def test_permission_request_logged_and_broadcast(db_factory, db_session):
    im = _make_im(db_factory)
    task = Task(title="t", description="d", session_id="sess-1", instance_id=3)
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    await im._handle_pty_permission_request("sess-1", REQUEST)

    # pending 登记
    assert "perm-1" in im._pty_permissions
    assert im._pty_permissions["perm-1"]["task_id"] == task.id

    # LogEntry 落库
    entry = (
        await db_session.execute(
            select(LogEntry).where(
                LogEntry.task_id == task.id,
                LogEntry.event_type == "permission_request",
            )
        )
    ).scalars().first()
    assert entry is not None
    assert entry.tool_name == "Bash"
    assert json.loads(entry.raw_json)["request_id"] == "perm-1"

    # 广播了卡片事件
    channels = [c for c, _ in im.broadcaster.events]
    payloads = [d for _, d in im.broadcaster.events]
    assert f"task:{task.id}" in channels
    assert payloads[0]["event_type"] == "permission_request"
    assert payloads[0]["request_id"] == "perm-1"
    assert payloads[0]["timeout_seconds"] == 120


@pytest.mark.asyncio
async def test_permission_request_unknown_session_no_broadcast(db_factory):
    im = _make_im(db_factory)
    await im._handle_pty_permission_request("no-such-session", REQUEST)
    # 登记了（万一 task 是后绑定的）但无广播
    assert im.broadcaster.events == []


@pytest.mark.asyncio
async def test_resolve_permission_roundtrip(db_factory, db_session):
    im = _make_im(db_factory)
    task = Task(title="t", description="d", session_id="sess-2", instance_id=1)
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    await im._handle_pty_permission_request("sess-2", REQUEST)
    im.broadcaster.events.clear()

    fake_backend = MagicMock()
    fake_backend._bridge.resolve_permission = MagicMock(return_value=True)
    im._pty_backend = fake_backend

    ok = await im.resolve_pty_permission("perm-1", "allow")
    assert ok is True
    fake_backend._bridge.resolve_permission.assert_called_once_with(
        "sess-2", "perm-1", "allow"
    )
    # pending 已清除，二次回包失败
    assert await im.resolve_pty_permission("perm-1", "allow") is False

    # 广播 resolved
    assert any(
        d["event_type"] == "permission_resolved" and d["behavior"] == "allow"
        for _, d in im.broadcaster.events
    )


@pytest.mark.asyncio
async def test_resolve_unknown_or_expired(db_factory):
    im = _make_im(db_factory)
    assert await im.resolve_pty_permission("nope", "allow") is False

    import time
    im._pty_permissions["old"] = {
        "session_id": "s", "task_id": None, "tool_name": "Bash",
        "expires_at": time.monotonic() - 1,
    }
    assert await im.resolve_pty_permission("old", "deny") is False


# ------------------------------------------------------------- API endpoint


async def _create_task(client, session_factory, session_id="sess-api"):
    resp = await client.post("/api/tasks", json={"title": "t", "description": "d"})
    task_id = resp.json()["id"]
    async with session_factory() as db:
        t = await db.get(Task, task_id)
        t.session_id = session_id
        await db.commit()
    return task_id


@pytest.mark.asyncio
async def test_permission_endpoint_allow(client, session_factory):
    task_id = await _create_task(client, session_factory)
    mock_im = MagicMock()
    mock_im.resolve_pty_permission = AsyncMock(return_value=True)
    with patch("backend.main.instance_manager", mock_im):
        resp = await client.post(
            f"/api/tasks/{task_id}/permissions/perm-9", json={"behavior": "allow"}
        )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "behavior": "allow"}
    mock_im.resolve_pty_permission.assert_awaited_once_with("perm-9", "allow")


@pytest.mark.asyncio
async def test_permission_endpoint_expired_410(client, session_factory):
    task_id = await _create_task(client, session_factory)
    mock_im = MagicMock()
    mock_im.resolve_pty_permission = AsyncMock(return_value=False)
    with patch("backend.main.instance_manager", mock_im):
        resp = await client.post(
            f"/api/tasks/{task_id}/permissions/perm-x", json={"behavior": "deny"}
        )
    assert resp.status_code == 410


@pytest.mark.asyncio
async def test_permission_endpoint_validates_behavior(client, session_factory):
    task_id = await _create_task(client, session_factory)
    resp = await client.post(
        f"/api/tasks/{task_id}/permissions/perm-y", json={"behavior": "maybe"}
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_permission_endpoint_task_not_found(client):
    resp = await client.post(
        "/api/tasks/999999/permissions/perm-z", json={"behavior": "allow"}
    )
    assert resp.status_code == 404
