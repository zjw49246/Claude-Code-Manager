"""Protocol regression tests for the persistent Codex app-server backend."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.services.codex_app_server import (
    CodexAppServer,
    CodexAppServerError,
    CodexTurnProcess,
)


@pytest.mark.asyncio
async def test_start_turn_uses_native_resume_and_turn_start():
    server = CodexAppServer("codex")
    server._process = SimpleNamespace(pid=4321, returncode=None)
    server.ensure_started = AsyncMock()
    server._request = AsyncMock(side_effect=[
        {"thread": {"id": "thread-123"}},
        {"turn": {"id": "turn-456"}},
    ])

    process, thread_id = await server.start_turn(
        prompt="continue",
        cwd="/tmp",
        model="gpt-5.6-luna",
        effort="max",
        resume_session_id="thread-123",
        git_env={"GIT_AUTHOR_NAME": "CCM"},
        task_id=9,
    )

    assert thread_id == "thread-123"
    resume_call, turn_call = server._request.await_args_list
    assert resume_call.args[0] == "thread/resume"
    assert resume_call.args[1]["threadId"] == "thread-123"
    assert resume_call.args[1]["approvalPolicy"] == "never"
    assert resume_call.args[1]["sandbox"] == "danger-full-access"
    assert resume_call.args[1]["config"]["shell_environment_policy"]["set"] == {
        "GIT_AUTHOR_NAME": "CCM"
    }
    assert turn_call.args[0] == "turn/start"
    assert turn_call.args[1]["effort"] == "max"
    assert turn_call.args[1]["model"] == "gpt-5.6-luna"

    first = json.loads((await process.stdout.readline()).decode())
    assert first == {"type": "thread.started", "thread_id": "thread-123"}


@pytest.mark.asyncio
async def test_steer_turn_targets_the_active_turn():
    server = CodexAppServer("codex")
    server._process = SimpleNamespace(pid=4321, returncode=None)
    server.ensure_started = AsyncMock()
    server._request = AsyncMock(side_effect=[
        {"thread": {"id": "thread-1"}},
        {"turn": {"id": "turn-1"}},
        {"turnId": "turn-1"},
    ])
    await server.start_turn(
        prompt="work", cwd="/tmp", model="gpt-5.5", effort="low",
        resume_session_id=None, git_env=None, task_id=1,
    )

    assert await server.steer_turn("thread-1", "focus on the failing test") is True
    steer_call = server._request.await_args_list[2]
    assert steer_call.args == (
        "turn/steer",
        {
            "threadId": "thread-1",
            "expectedTurnId": "turn-1",
            "input": [{"type": "text", "text": "focus on the failing test"}],
        },
    )


@pytest.mark.asyncio
async def test_steer_turn_without_active_context_does_not_send_rpc():
    server = CodexAppServer("codex")
    server._process = SimpleNamespace(pid=4321, returncode=None)
    server._request = AsyncMock()

    assert await server.steer_turn("thread-gone", "too late") is False
    server._request.assert_not_awaited()


@pytest.mark.asyncio
async def test_steer_turn_protocol_rejection_is_a_normal_false_result():
    server = CodexAppServer("codex")
    server._process = SimpleNamespace(pid=4321, returncode=None)
    server.ensure_started = AsyncMock()
    server._request = AsyncMock(side_effect=[
        {"thread": {"id": "thread-1"}},
        {"turn": {"id": "turn-1"}},
        CodexAppServerError("active turn changed"),
    ])
    await server.start_turn(
        prompt="work", cwd="/tmp", model="gpt-5.5", effort="low",
        resume_session_id=None, git_env=None, task_id=1,
    )

    assert await server.steer_turn("thread-1", "late input") is False


@pytest.mark.asyncio
async def test_notifications_stream_delta_and_finish_process():
    server = CodexAppServer("codex")
    server._process = SimpleNamespace(pid=4321, returncode=None)
    server.ensure_started = AsyncMock()
    server._request = AsyncMock(side_effect=[
        {"thread": {"id": "thread-1"}},
        {"turn": {"id": "turn-1"}},
    ])
    process, _ = await server.start_turn(
        prompt="hi", cwd="/tmp", model="gpt-5.5", effort="low",
        resume_session_id=None, git_env=None, task_id=1,
    )
    # Consume the synthetic thread.started line.
    await process.stdout.readline()

    server._handle_notification("item/agentMessage/delta", {
        "threadId": "thread-1", "turnId": "turn-1",
        "itemId": "msg-1", "delta": "Hel",
    })
    server._handle_notification("item/completed", {
        "threadId": "thread-1", "turnId": "turn-1",
        "item": {"type": "agentMessage", "id": "msg-1", "text": "Hello"},
    })
    server._handle_notification("thread/tokenUsage/updated", {
        "threadId": "thread-1", "turnId": "turn-1",
        "tokenUsage": {"last": {
            "inputTokens": 100, "cachedInputTokens": 80, "outputTokens": 5,
        }},
    })
    server._handle_notification("turn/completed", {
        "threadId": "thread-1",
        "turn": {"id": "turn-1", "status": "completed", "error": None},
    })

    lines = []
    while True:
        line = await process.stdout.readline()
        if not line:
            break
        lines.append(json.loads(line))
    assert lines[0] == {
        "type": "item.agent_message.delta", "delta": "Hel", "item_id": "msg-1"
    }
    assert lines[1]["type"] == "item.completed"
    assert lines[1]["item"]["type"] == "agent_message"
    assert lines[2] == {
        "type": "turn.completed",
        "usage": {"input_tokens": 100, "cached_input_tokens": 80, "output_tokens": 5},
    }
    assert await process.wait() == 0


@pytest.mark.asyncio
async def test_interleaved_notifications_are_isolated_by_turn():
    """Concurrent tasks must never receive another thread's output."""
    server = CodexAppServer("codex")
    server._process = SimpleNamespace(pid=4321, returncode=None)
    server.ensure_started = AsyncMock()
    server._request = AsyncMock(side_effect=[
        {"thread": {"id": "thread-a"}},
        {"turn": {"id": "turn-a"}},
        {"thread": {"id": "thread-b"}},
        {"turn": {"id": "turn-b"}},
    ])
    process_a, _ = await server.start_turn(
        prompt="a", cwd="/tmp", model="gpt-5.5", effort="low",
        resume_session_id=None, git_env=None, task_id=1,
    )
    process_b, _ = await server.start_turn(
        prompt="b", cwd="/tmp", model="gpt-5.5", effort="low",
        resume_session_id=None, git_env=None, task_id=2,
    )
    await process_a.stdout.readline()
    await process_b.stdout.readline()

    # Deliberately deliver B before A, as happens under real concurrent turns.
    for thread, turn, item, text in (
        ("thread-b", "turn-b", "msg-b", "B"),
        ("thread-a", "turn-a", "msg-a", "A"),
    ):
        server._handle_notification("item/agentMessage/delta", {
            "threadId": thread, "turnId": turn, "itemId": item, "delta": text,
        })
        server._handle_notification("item/completed", {
            "threadId": thread, "turnId": turn,
            "item": {"type": "agentMessage", "id": item, "text": text},
        })
        server._handle_notification("turn/completed", {
            "threadId": thread,
            "turn": {"id": turn, "status": "completed", "error": None},
        })

    async def read_all(process):
        rows = []
        while line := await process.stdout.readline():
            rows.append(json.loads(line))
        return rows

    rows_a, rows_b = await asyncio.gather(read_all(process_a), read_all(process_b))
    assert [row.get("delta") for row in rows_a if "delta" in row] == ["A"]
    assert [row.get("delta") for row in rows_b if "delta" in row] == ["B"]
    assert rows_a[1]["item"]["text"] == "A"
    assert rows_b[1]["item"]["text"] == "B"
    assert await process_a.wait() == await process_b.wait() == 0


@pytest.mark.asyncio
async def test_reader_exit_fails_pending_requests_and_active_turns():
    """A crashed shared process must unblock every waiter instead of hanging."""
    server = CodexAppServer("codex")
    server._process = SimpleNamespace(pid=4321, returncode=None)
    server.ensure_started = AsyncMock()
    server._request = AsyncMock(side_effect=[
        {"thread": {"id": "thread-1"}},
        {"turn": {"id": "turn-1"}},
    ])
    turn_process, _ = await server.start_turn(
        prompt="hi", cwd="/tmp", model="gpt-5.5", effort="low",
        resume_session_id=None, git_env=None, task_id=1,
    )
    await turn_process.stdout.readline()
    pending = asyncio.get_running_loop().create_future()
    server._pending[99] = pending

    stdout = asyncio.StreamReader()
    stdout.feed_eof()
    fake_process = SimpleNamespace(
        stdout=stdout,
        wait=AsyncMock(return_value=1),
    )
    await server._read_loop(fake_process)

    assert await turn_process.wait() == 1
    assert not server._contexts_by_thread
    assert not server._contexts_by_turn
    assert not server._pending
    with pytest.raises(CodexAppServerError, match="exited unexpectedly"):
        await pending


def test_normalize_app_server_command_item():
    normalized = CodexAppServer._normalize_item({
        "type": "commandExecution",
        "id": "cmd-1",
        "command": "pwd",
        "aggregatedOutput": "/tmp\n",
        "exitCode": 0,
        "status": "completed",
    })
    assert normalized["type"] == "command_execution"
    assert normalized["aggregated_output"] == "/tmp\n"
    assert normalized["exit_code"] == 0


@pytest.mark.asyncio
async def test_turn_process_interrupt_is_nonblocking_and_completes():
    interrupted = asyncio.Event()

    async def interrupt():
        interrupted.set()

    process = CodexTurnProcess(1, interrupt)
    process.send_signal(2)
    await asyncio.wait_for(interrupted.wait(), timeout=1)
    process.finish(130)
    assert await process.wait() == 130


@pytest.mark.asyncio
async def test_reader_delivers_json_rpc_response_to_pending_request():
    """A protocol response must resolve exactly one waiting request."""
    stdout = asyncio.StreamReader()
    stdout.feed_data(b'{"id":7,"result":{"ok":true}}\n')
    stdout.feed_eof()
    fake_process = SimpleNamespace(
        stdout=stdout,
        wait=AsyncMock(return_value=0),
    )
    server = CodexAppServer("codex")
    pending = asyncio.get_running_loop().create_future()
    server._pending[7] = pending

    await server._read_loop(fake_process)

    assert pending.result() == {"id": 7, "result": {"ok": True}}


@pytest.mark.asyncio
async def test_server_requests_use_protocol_specific_approval_shapes():
    server = CodexAppServer("codex")
    server._write = AsyncMock()

    await server._handle_server_request({
        "id": 1,
        "method": "item/commandExecution/requestApproval",
        "params": {},
    })
    await server._handle_server_request({
        "id": 2,
        "method": "item/permissions/requestApproval",
        "params": {"permissions": {"network": {"enabled": True}}},
    })
    await server._handle_server_request({
        "id": 3,
        "method": "execCommandApproval",
        "params": {},
    })

    assert server._write.await_args_list[0].args[0] == {
        "id": 1, "result": {"decision": "accept"}
    }
    assert server._write.await_args_list[1].args[0] == {
        "id": 2,
        "result": {
            "permissions": {"network": {"enabled": True}},
            "scope": "turn",
        },
    }
    assert server._write.await_args_list[2].args[0] == {
        "id": 3, "result": {"decision": "approved"}
    }
