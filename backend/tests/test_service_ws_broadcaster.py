"""Tests for WebSocketBroadcaster."""
import json
import pytest
from unittest.mock import AsyncMock, MagicMock

from backend.services.ws_broadcaster import WebSocketBroadcaster


def _make_ws():
    ws = MagicMock()
    ws.send_text = AsyncMock()
    return ws


@pytest.mark.asyncio
async def test_subscribe():
    b = WebSocketBroadcaster()
    ws = _make_ws()
    await b.subscribe(ws, ["ch1"])
    assert ws in b.subscriptions["ch1"]


@pytest.mark.asyncio
async def test_subscribe_multiple_channels():
    b = WebSocketBroadcaster()
    ws = _make_ws()
    await b.subscribe(ws, ["ch1", "ch2", "ch3"])
    assert ws in b.subscriptions["ch1"]
    assert ws in b.subscriptions["ch2"]
    assert ws in b.subscriptions["ch3"]


@pytest.mark.asyncio
async def test_unsubscribe():
    b = WebSocketBroadcaster()
    ws = _make_ws()
    await b.subscribe(ws, ["ch1", "ch2"])
    await b.unsubscribe(ws)
    assert ws not in b.subscriptions.get("ch1", set())
    assert ws not in b.subscriptions.get("ch2", set())


@pytest.mark.asyncio
async def test_unsubscribe_cleans_empty_channels():
    b = WebSocketBroadcaster()
    ws = _make_ws()
    await b.subscribe(ws, ["ch1"])
    await b.unsubscribe(ws)
    assert "ch1" not in b.subscriptions


@pytest.mark.asyncio
async def test_broadcast_sends():
    b = WebSocketBroadcaster()
    ws1 = _make_ws()
    ws2 = _make_ws()
    await b.subscribe(ws1, ["events"])
    await b.subscribe(ws2, ["events"])

    await b.broadcast("events", {"type": "test"})

    expected = json.dumps({"channel": "events", "data": {"type": "test"}})
    ws1.send_text.assert_awaited_once_with(expected)
    ws2.send_text.assert_awaited_once_with(expected)


@pytest.mark.asyncio
async def test_broadcast_removes_dead_connections():
    b = WebSocketBroadcaster()
    ws_good = _make_ws()
    ws_dead = _make_ws()
    ws_dead.send_text.side_effect = Exception("connection closed")

    await b.subscribe(ws_good, ["events"])
    await b.subscribe(ws_dead, ["events"])

    await b.broadcast("events", {"type": "test"})

    # Dead ws should be removed
    assert ws_dead not in b.subscriptions.get("events", set())
    assert ws_good in b.subscriptions["events"]


@pytest.mark.asyncio
async def test_broadcast_no_subscribers():
    b = WebSocketBroadcaster()
    # Should not raise
    await b.broadcast("empty-channel", {"type": "test"})


@pytest.mark.asyncio
async def test_broadcast_survives_concurrent_unsubscribe():
    """send 悬挂期间并发退订不得炸掉 broadcast。

    2026-07-16 生产事故：前端 WS 连环 keepalive 超时断开，断连处理在
    broadcast 迭代中途改了活集合 → RuntimeError: Set changed size during
    iteration → create_monitor 返回 500 → 主 agent 重试建出重复 monitor。
    """
    b = WebSocketBroadcaster()
    ws1, ws2, extra = _make_ws(), _make_ws(), _make_ws()
    await b.subscribe(ws1, ["events"])
    await b.subscribe(ws2, ["events"])
    await b.subscribe(extra, ["events"])

    async def _drop_extra(_msg):
        await b.unsubscribe(extra)

    ws1.send_text = AsyncMock(side_effect=_drop_extra)
    ws2.send_text = AsyncMock(side_effect=_drop_extra)

    await b.broadcast("events", {"type": "test"})  # 修复前此处 RuntimeError

    assert ws1 in b.subscriptions.get("events", set())
    assert ws2 in b.subscriptions.get("events", set())
    assert extra not in b.subscriptions.get("events", set())
