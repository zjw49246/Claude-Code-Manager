"""Protocol regression tests for the persistent Codex app-server backend."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.services.codex_app_server import (
    CodexAppServer,
    CodexAppServerBusyError,
    CodexAppServerError,
    CodexAppServerRegistry,
    CodexThreadHomeMismatchError,
    CodexTurnProcess,
    normalize_codex_home,
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
async def test_second_turn_on_same_active_thread_is_typed_busy_error():
    server = CodexAppServer("codex")
    server._process = SimpleNamespace(pid=4321, returncode=None)
    server.ensure_started = AsyncMock()
    server._request = AsyncMock(side_effect=[
        {"thread": {"id": "thread-1"}},
        {"turn": {"id": "turn-1"}},
        {"thread": {"id": "thread-1"}},
    ])
    await server.start_turn(
        prompt="first", cwd="/tmp", model="gpt-5.5", effort="low",
        resume_session_id="thread-1", git_env=None, task_id=1,
    )

    with pytest.raises(CodexAppServerBusyError, match="already has an active turn"):
        await server.start_turn(
            prompt="second", cwd="/tmp", model="gpt-5.5", effort="low",
            resume_session_id="thread-1", git_env=None, task_id=1,
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


class _RegistryFakeServer:
    instances = []

    def __init__(self, binary, request_timeout=30.0, *, codex_home=None):
        self.binary = binary
        self.request_timeout = request_timeout
        self.codex_home = normalize_codex_home(codex_home)
        self.active_threads = set()
        self.known_threads = set()
        self.shutdown_count = 0
        self.steered = []
        type(self).instances.append(self)

    @property
    def has_active_turns(self):
        return bool(self.active_threads)

    def has_active_thread(self, thread_id):
        return thread_id in self.active_threads

    def knows_thread(self, thread_id):
        return thread_id in self.known_threads

    async def start_turn(self, **kwargs):
        thread_id = kwargs.get("resume_session_id") or f"thread-{kwargs['task_id']}"
        self.active_threads.add(thread_id)
        self.known_threads.add(thread_id)
        return MagicMock(terminate=MagicMock()), thread_id

    async def steer_turn(self, thread_id, content):
        self.steered.append((thread_id, content))
        return thread_id in self.active_threads

    async def shutdown(self):
        self.shutdown_count += 1
        self.active_threads.clear()


@pytest.fixture(autouse=False)
def reset_registry_fake_servers():
    _RegistryFakeServer.instances = []
    yield
    _RegistryFakeServer.instances = []


@pytest.mark.asyncio
async def test_registry_routes_each_canonical_home_to_one_server(
    tmp_path, reset_registry_fake_servers,
):
    registry = CodexAppServerRegistry("codex", request_timeout=7)
    home_a = tmp_path / "a" / ".." / "a"
    home_b = tmp_path / "b"

    with patch(
        "backend.services.codex_app_server.CodexAppServer",
        _RegistryFakeServer,
    ):
        _, thread_a = await registry.start_turn(
            codex_home=home_a, resume_session_id=None, task_id=1,
        )
        _, thread_b = await registry.start_turn(
            codex_home=home_b, resume_session_id=None, task_id=2,
        )
        assert await registry.steer_turn(thread_a, "a-only") is True

    assert thread_a == "thread-1"
    assert thread_b == "thread-2"
    assert len(_RegistryFakeServer.instances) == 2
    assert {server.codex_home for server in _RegistryFakeServer.instances} == {
        normalize_codex_home(home_a),
        normalize_codex_home(home_b),
    }
    server_a = next(
        server for server in _RegistryFakeServer.instances
        if server.codex_home == normalize_codex_home(home_a)
    )
    server_b = next(
        server for server in _RegistryFakeServer.instances
        if server.codex_home == normalize_codex_home(home_b)
    )
    assert server_a.steered == [(thread_a, "a-only")]
    assert server_b.steered == []


@pytest.mark.asyncio
async def test_registry_rejects_cross_home_resume_without_rebind(
    tmp_path, reset_registry_fake_servers,
):
    registry = CodexAppServerRegistry("codex")

    with patch(
        "backend.services.codex_app_server.CodexAppServer",
        _RegistryFakeServer,
    ):
        await registry.start_turn(
            codex_home=tmp_path / "a",
            resume_session_id="thread-owned",
            task_id=1,
        )
        with pytest.raises(CodexThreadHomeMismatchError, match="migrate and rebind"):
            await registry.start_turn(
                codex_home=tmp_path / "b",
                resume_session_id="thread-owned",
                task_id=1,
            )

    assert len(_RegistryFakeServer.instances) == 1


@pytest.mark.asyncio
async def test_registry_rebind_moves_resume_ownership_after_migration(
    tmp_path, reset_registry_fake_servers,
):
    registry = CodexAppServerRegistry("codex")
    home_a = tmp_path / "a"
    home_b = tmp_path / "b"

    with patch(
        "backend.services.codex_app_server.CodexAppServer",
        _RegistryFakeServer,
    ):
        await registry.start_turn(
            codex_home=home_a,
            resume_session_id="thread-migrated",
            task_id=1,
        )
        _RegistryFakeServer.instances[0].active_threads.clear()
        await registry.rebind_thread(
            "thread-migrated",
            source_codex_home=home_a,
            target_codex_home=home_b,
        )
        await registry.start_turn(
            codex_home=home_b,
            resume_session_id="thread-migrated",
            task_id=1,
        )
        with pytest.raises(CodexThreadHomeMismatchError):
            await registry.start_turn(
                codex_home=home_a,
                resume_session_id="thread-migrated",
                task_id=1,
            )

    assert len(_RegistryFakeServer.instances) == 2


@pytest.mark.asyncio
async def test_registry_recovery_clear_restores_db_authoritative_cold_route(
    tmp_path, reset_registry_fake_servers,
):
    registry = CodexAppServerRegistry("codex")
    old_home = normalize_codex_home(tmp_path / "old")
    new_home = normalize_codex_home(tmp_path / "new")
    thread_id = "thread-binding-failed"
    new_server = _RegistryFakeServer("codex", codex_home=new_home)
    new_server.known_threads.add(thread_id)
    registry._servers[new_home] = new_server
    registry._thread_owners[thread_id] = new_home

    assert await registry.clear_thread_owner_for_recovery(
        thread_id,
        expected_codex_home=new_home,
    )
    assert thread_id not in registry._thread_owners

    with patch(
        "backend.services.codex_app_server.CodexAppServer",
        _RegistryFakeServer,
    ):
        await registry.start_turn(
            codex_home=old_home,
            resume_session_id=thread_id,
            task_id=1,
        )

    assert registry._thread_owners[thread_id] == old_home


@pytest.mark.asyncio
async def test_registry_rebind_will_not_restart_target_during_start_rpc(tmp_path):
    """A cached target must not be shutdown under an admitted start/resume RPC."""

    entered = asyncio.Event()
    release = asyncio.Event()

    class BlockingTargetServer(_RegistryFakeServer):
        async def start_turn(self, **kwargs):
            entered.set()
            await release.wait()
            return await super().start_turn(**kwargs)

    registry = CodexAppServerRegistry("codex")
    source_home = normalize_codex_home(tmp_path / "source")
    target_home = normalize_codex_home(tmp_path / "target")
    source_server = _RegistryFakeServer("codex", codex_home=source_home)
    target_server = BlockingTargetServer("codex", codex_home=target_home)
    migrated_thread = "thread-migrated"
    target_server.known_threads.add(migrated_thread)
    registry._servers[source_home] = source_server
    registry._servers[target_home] = target_server
    registry._thread_owners[migrated_thread] = source_home

    start_task = asyncio.create_task(registry.start_turn(
        codex_home=target_home,
        resume_session_id=None,
        task_id=99,
    ))
    await entered.wait()
    try:
        with pytest.raises(CodexAppServerBusyError, match="request in flight"):
            await registry.rebind_thread(
                migrated_thread,
                source_codex_home=source_home,
                target_codex_home=target_home,
            )
        assert target_server.shutdown_count == 0
        assert registry._thread_owners[migrated_thread] == source_home
        assert target_home not in registry._draining
    finally:
        release.set()
        await start_task


@pytest.mark.asyncio
async def test_registry_rebind_rejects_source_resume_rpc_in_flight(tmp_path):
    entered = asyncio.Event()
    release = asyncio.Event()

    class BlockingSourceServer(_RegistryFakeServer):
        async def start_turn(self, **kwargs):
            entered.set()
            await release.wait()
            return await super().start_turn(**kwargs)

    registry = CodexAppServerRegistry("codex")
    source_home = normalize_codex_home(tmp_path / "source")
    target_home = normalize_codex_home(tmp_path / "target")
    thread_id = "thread-source-starting"
    source_server = BlockingSourceServer("codex", codex_home=source_home)
    registry._servers[source_home] = source_server
    registry._thread_owners[thread_id] = source_home

    start_task = asyncio.create_task(registry.start_turn(
        codex_home=source_home,
        resume_session_id=thread_id,
        task_id=1,
    ))
    await entered.wait()
    try:
        with pytest.raises(CodexAppServerBusyError, match="source account"):
            await registry.rebind_thread(
                thread_id,
                source_codex_home=source_home,
                target_codex_home=target_home,
            )
        assert registry._thread_owners[thread_id] == source_home
    finally:
        release.set()
        await start_task


@pytest.mark.asyncio
async def test_registry_rebind_reserves_thread_across_target_shutdown(tmp_path):
    entered_shutdown = asyncio.Event()
    release_shutdown = asyncio.Event()

    class BlockingShutdownServer(_RegistryFakeServer):
        async def shutdown(self):
            entered_shutdown.set()
            await release_shutdown.wait()
            await super().shutdown()

    registry = CodexAppServerRegistry("codex")
    source_home = normalize_codex_home(tmp_path / "source")
    target_home = normalize_codex_home(tmp_path / "target")
    thread_id = "thread-rebind-reserved"
    source_server = _RegistryFakeServer("codex", codex_home=source_home)
    target_server = BlockingShutdownServer("codex", codex_home=target_home)
    target_server.known_threads.add(thread_id)
    registry._servers[source_home] = source_server
    registry._servers[target_home] = target_server
    registry._thread_owners[thread_id] = source_home

    rebind = asyncio.create_task(registry.rebind_thread(
        thread_id,
        source_codex_home=source_home,
        target_codex_home=target_home,
    ))
    await entered_shutdown.wait()
    try:
        with pytest.raises(CodexAppServerBusyError, match="being rebound"):
            await registry.start_turn(
                codex_home=source_home,
                resume_session_id=thread_id,
                task_id=1,
            )
        with pytest.raises(CodexAppServerBusyError, match="rebind in flight"):
            await registry.begin_home_maintenance(source_home)
        assert registry._thread_owners[thread_id] == source_home
    finally:
        release_shutdown.set()
        await rebind

    assert registry._thread_owners[thread_id] == target_home
    assert thread_id not in registry._rebindings


@pytest.mark.asyncio
async def test_registry_maintenance_rejects_active_and_blocks_new_turns(
    tmp_path, reset_registry_fake_servers,
):
    registry = CodexAppServerRegistry("codex")
    home = tmp_path / "account"

    with patch(
        "backend.services.codex_app_server.CodexAppServer",
        _RegistryFakeServer,
    ):
        await registry.start_turn(
            codex_home=home, resume_session_id="thread-active", task_id=1,
        )
        with pytest.raises(CodexAppServerBusyError, match="active or starting"):
            await registry.begin_home_maintenance(home, require_idle=True)

        _RegistryFakeServer.instances[0].active_threads.clear()
        assert await registry.begin_home_maintenance(home) is True
        with pytest.raises(CodexAppServerBusyError, match="draining"):
            await registry.start_turn(
                codex_home=home, resume_session_id=None, task_id=2,
            )
        await registry.end_home_maintenance(home)
        _, thread_id = await registry.start_turn(
            codex_home=home, resume_session_id=None, task_id=2,
        )

    assert thread_id == "thread-2"
    assert len(_RegistryFakeServer.instances) == 2


@pytest.mark.asyncio
async def test_registry_maintenance_sees_start_rpc_in_flight(tmp_path):
    entered = asyncio.Event()
    release = asyncio.Event()

    class BlockingServer(_RegistryFakeServer):
        async def start_turn(self, **kwargs):
            entered.set()
            await release.wait()
            return await super().start_turn(**kwargs)

    BlockingServer.instances = []
    registry = CodexAppServerRegistry("codex")
    home = tmp_path / "account"

    with patch("backend.services.codex_app_server.CodexAppServer", BlockingServer):
        start_task = asyncio.create_task(registry.start_turn(
            codex_home=home, resume_session_id=None, task_id=1,
        ))
        await entered.wait()
        with pytest.raises(CodexAppServerBusyError, match="active or starting"):
            await registry.begin_home_maintenance(home, require_idle=True)
        release.set()
        await start_task
