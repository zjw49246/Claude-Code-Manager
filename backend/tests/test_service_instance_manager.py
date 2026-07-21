"""Tests for InstanceManager — subprocess lifecycle management."""
import asyncio
import json
import os
import signal
import time
import pytest
from sqlalchemy import select
from unittest.mock import AsyncMock, MagicMock, call, patch

from backend.services.instance_manager import InstanceManager
from backend.services.claude_pool import ClaudePool
from backend.services.codex_pool import CodexPool
from backend.services.codex_app_server import (
    CodexAppServerBusyError,
    CodexThreadHomeMismatchError,
)
from backend.config import settings
from backend.models.instance import Instance
from backend.models.log_entry import LogEntry
from backend.models.task import Task


@pytest.fixture(autouse=True)
def _no_pty_no_skills(monkeypatch):
    """Disable PTY mode and mock discover_skills for all tests in this module."""
    monkeypatch.setattr("backend.config.settings.use_pty_mode", False)
    with patch("backend.services.skill_loader.discover_skills", return_value={}), \
         patch("backend.services.skill_loader.build_skill_prompt_file", return_value=""), \
         patch("backend.services.skill_loader.get_skill_disallowed_tools", return_value=[]):
        yield


def test_parse_codex_agent_message():
    im = InstanceManager(MagicMock(), MagicMock())
    event = im._parse_codex_line(json.dumps({
        "type": "item.completed",
        "item": {"id": "item_1", "type": "agent_message", "text": "Done"},
    }))

    assert event["event_type"] == "message"
    assert event["role"] == "assistant"
    assert event["content"] == "Done"
    assert event["is_error"] is False


@pytest.mark.asyncio
async def test_pty_quota_event_retains_reset_metadata_for_post_turn_switch(
    db_factory,
):
    async with db_factory() as db:
        inst = Instance(name="pty-quota-event")
        task = Task(title="pty quota", status="executing", provider="claude")
        db.add_all([inst, task])
        await db.commit()
        await db.refresh(inst)
        await db.refresh(task)

    info = {
        "status": "allowed_warning",
        "rateLimitType": "seven_day",
        "utilization": 0.91,
        "resetsAt": 1_800_000_000,
    }
    im = InstanceManager(db_factory, MagicMock(broadcast=AsyncMock()))
    await im._process_event(inst.id, task.id, {
        "event_type": "rate_limit_event",
        "role": None,
        "content": None,
        "raw_json": None,
        "is_error": False,
        "rate_limit_info": info,
    })

    assert im.pty_rate_limit_seen(inst.id)
    assert im.pty_rate_limit_info(inst.id) == info
    im.clear_pty_rate_limit(inst.id)
    assert not im.pty_rate_limit_seen(inst.id)
    assert im.pty_rate_limit_info(inst.id) is None


@pytest.mark.asyncio
async def test_codex_app_server_delta_is_broadcast_but_not_persisted(db_factory):
    """Streaming improves TTFT without turning every token into a DB row."""
    async with db_factory() as db:
        inst = Instance(name="delta-inst")
        task = Task(title="delta-task", status="executing", provider="codex")
        db.add_all([inst, task])
        await db.commit()
        await db.refresh(inst)
        await db.refresh(task)

    broadcaster = MagicMock()
    broadcaster.broadcast = AsyncMock()
    im = InstanceManager(db_factory, broadcaster)
    await im._process_event(inst.id, task.id, {
        "event_type": "message_delta",
        "role": "assistant",
        "content": "Hel",
        "item_id": "msg-1",
        "raw_json": '{"large":"payload"}',
        "is_error": False,
    })

    async with db_factory() as db:
        rows = (await db.execute(
            select(LogEntry).where(LogEntry.task_id == task.id)
        )).scalars().all()
    assert rows == []
    assert broadcaster.broadcast.await_args_list == [
        ((f"instance:{inst.id}", {
            "event_type": "message_delta", "role": "assistant",
            "content": "Hel", "item_id": "msg-1", "is_error": False,
        }),),
        ((f"task:{task.id}", {
            "event_type": "message_delta", "role": "assistant",
            "content": "Hel", "item_id": "msg-1", "is_error": False,
        }),),
    ]


def test_parse_codex_command_started():
    im = InstanceManager(MagicMock(), MagicMock())
    event = im._parse_codex_line(json.dumps({
        "type": "item.started",
        "item": {
            "id": "item_2",
            "type": "command_execution",
            "command": "npm run build",
            "status": "in_progress",
        },
    }))

    assert event["event_type"] == "tool_use"
    assert event["role"] == "assistant"
    assert event["tool_name"] == "Shell"
    assert json.loads(event["tool_input"]) == {"command": "npm run build"}
    assert event["content"] is None


def test_parse_codex_command_completed():
    im = InstanceManager(MagicMock(), MagicMock())
    event = im._parse_codex_line(json.dumps({
        "type": "item.completed",
        "item": {
            "id": "item_3",
            "type": "command_execution",
            "command": "git status",
            "aggregated_output": "nothing to commit\n",
            "exit_code": 0,
            "status": "completed",
        },
    }))

    assert event["event_type"] == "tool_result"
    assert event["role"] == "tool"
    assert event["tool_name"] == "Shell"
    assert json.loads(event["tool_input"]) == {"command": "git status"}
    assert event["tool_output"] == "nothing to commit\n"
    assert event["content"] is None
    assert event["is_error"] is False


def test_parse_codex_turn_completed_usage():
    im = InstanceManager(MagicMock(), MagicMock())
    event = im._parse_codex_line(json.dumps({
        "type": "turn.completed",
        "usage": {
            "input_tokens": 100,
            "cached_input_tokens": 40,
            "output_tokens": 20,
            "reasoning_output_tokens": 5,
        },
    }))

    assert event["event_type"] == "system_event"
    assert event["context_usage"] == {
        "input_tokens": 60,
        "cache_read_input_tokens": 40,
        "cache_creation_input_tokens": 0,
        "output_tokens": 20,
        "total_input_tokens": 100,
    }
    assert event["content"] == "turn.completed"


def test_parse_codex_thread_started_session_id():
    im = InstanceManager(MagicMock(), MagicMock())
    event = im._parse_codex_line(json.dumps({
        "type": "thread.started",
        "thread_id": "test-thread-123",
    }))

    assert event["event_type"] == "system_event"
    assert event["content"] == "thread.started"
    assert event["session_id"] == "test-thread-123"


def test_parse_codex_app_server_message_delta():
    im = InstanceManager(MagicMock(), MagicMock())
    event = im._parse_codex_line(json.dumps({
        "type": "item.agent_message.delta",
        "item_id": "msg-1",
        "delta": "Hel",
    }))

    assert event["event_type"] == "message_delta"
    assert event["role"] == "assistant"
    assert event["content"] == "Hel"
    assert event["item_id"] == "msg-1"


# === _build_command tests ===


def test_build_command_claude_basic():
    im = InstanceManager(MagicMock(), MagicMock())
    cmd = im._build_command(provider="claude", prompt="do stuff", model=None, resume_session_id=None, effort_level=None)
    assert cmd[0] == "claude"
    assert "-p" in cmd
    assert "do stuff" in cmd
    assert "--dangerously-skip-permissions" in cmd
    assert "--output-format" in cmd
    assert "--verbose" in cmd


def test_build_command_claude_with_resume_and_model():
    im = InstanceManager(MagicMock(), MagicMock())
    cmd = im._build_command(provider="claude", prompt="follow up", model="opus", resume_session_id="sess-1", effort_level="high")
    assert "--resume" in cmd
    assert "sess-1" in cmd
    assert "--model" in cmd
    assert "opus" in cmd
    assert "--effort" in cmd
    assert "high" in cmd


def test_build_command_codex_basic():
    im = InstanceManager(MagicMock(), MagicMock())
    cmd = im._build_command(provider="codex", prompt="do stuff", model=None, resume_session_id=None, effort_level=None)
    assert cmd[1] == "exec"
    assert "--json" in cmd
    assert "--dangerously-bypass-approvals-and-sandbox" in cmd
    assert "do stuff" in cmd
    assert "resume" not in cmd


def test_build_command_codex_with_resume():
    im = InstanceManager(MagicMock(), MagicMock())
    cmd = im._build_command(provider="codex", prompt="continue", model="gpt-5.5", resume_session_id="thread-abc", effort_level=None)
    assert cmd[1] == "exec"
    assert cmd[2] == "resume"
    assert "--model" in cmd
    assert "gpt-5.5" in cmd
    assert "thread-abc" in cmd
    assert "continue" in cmd


def test_build_command_codex_default_model_not_passed():
    im = InstanceManager(MagicMock(), MagicMock())
    cmd = im._build_command(provider="codex", prompt="hi", model="default", resume_session_id=None, effort_level=None)
    assert "--model" not in cmd


def test_build_command_unsupported_provider():
    im = InstanceManager(MagicMock(), MagicMock())
    with pytest.raises(ValueError, match="Unsupported CLI provider"):
        im._build_command(provider="unknown", prompt="hi", model=None, resume_session_id=None, effort_level=None)


# === Codex parser edge cases ===


def test_parse_codex_malformed_json():
    im = InstanceManager(MagicMock(), MagicMock())
    event = im._parse_codex_line("this is not json")
    assert event["event_type"] == "message"
    assert event["content"] == "this is not json"
    assert event["is_error"] is False


def test_parse_codex_error_event():
    im = InstanceManager(MagicMock(), MagicMock())
    event = im._parse_codex_line(json.dumps({
        "type": "error",
        "message": "rate limit exceeded",
    }))
    assert event["event_type"] == "system_event"
    assert event["is_error"] is True
    assert "rate limit exceeded" in event["content"]


def test_parse_codex_heartbeat_returns_none():
    """Heartbeat-like events with no meaningful content return None."""
    im = InstanceManager(MagicMock(), MagicMock())
    result = im._parse_codex_line(json.dumps({
        "type": "heartbeat",
    }))
    assert result is None


def test_parse_codex_unknown_event_with_content():
    """Unknown event type with content is preserved as system_event."""
    im = InstanceManager(MagicMock(), MagicMock())
    event = im._parse_codex_line(json.dumps({
        "type": "custom.event",
        "content": "something happened",
    }))
    assert event is not None
    assert event["event_type"] == "system_event"
    assert event["content"] == "something happened"


def test_parse_codex_command_with_nonzero_exit():
    """Command execution with non-zero exit code sets is_error=True."""
    im = InstanceManager(MagicMock(), MagicMock())
    event = im._parse_codex_line(json.dumps({
        "type": "item.completed",
        "item": {
            "type": "command_execution",
            "command": "npm test",
            "exit_code": 1,
            "status": "failed",
            "aggregated_output": "test failed",
        },
    }))
    assert event["event_type"] == "tool_result"
    assert event["is_error"] is True


def test_parse_codex_session_id_from_nested_session():
    """Session ID extracted from nested session.id field."""
    im = InstanceManager(MagicMock(), MagicMock())
    event = im._parse_codex_line(json.dumps({
        "type": "session.started",
        "session": {"id": "nested-sess-456"},
    }))
    assert event is not None
    assert event.get("session_id") == "nested-sess-456"


def _make_mock_process(pid=12345, returncode=0):
    """Create a mock asyncio subprocess."""
    proc = MagicMock()
    proc.pid = pid
    proc.returncode = returncode

    # stdout: readline returns empty bytes (EOF immediately)
    async def readline():
        return b""
    proc.stdout = MagicMock()
    proc.stdout.readline = readline

    # stderr
    async def read_stderr():
        return b""
    proc.stderr = MagicMock()
    proc.stderr.read = read_stderr

    # wait
    proc.wait = AsyncMock(return_value=returncode)
    proc.terminate = MagicMock()
    proc.kill = MagicMock()

    return proc


@pytest.mark.asyncio
async def test_launch_creates_subprocess(db_factory):
    """launch() calls create_subprocess_exec with correct args."""
    # Create instance in DB
    async with db_factory() as db:
        inst = Instance(name="test-inst")
        db.add(inst)
        await db.commit()
        await db.refresh(inst)
        inst_id = inst.id

    mock_proc = _make_mock_process()
    broadcaster = MagicMock()
    broadcaster.broadcast = AsyncMock()
    im = InstanceManager(db_factory, broadcaster)

    with patch("backend.services.instance_manager.asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=mock_proc) as mock_exec:
        pid = await im.launch(instance_id=inst_id, prompt="hello", cwd="/tmp")

    assert pid == 12345
    mock_exec.assert_awaited_once()
    cmd_args = mock_exec.call_args[0]
    assert "-p" in cmd_args
    assert "hello" in cmd_args
    assert "--dangerously-skip-permissions" in cmd_args
    # Wait for consumer task to finish
    await asyncio.sleep(0.1)


@pytest.mark.asyncio
async def test_launch_with_resume(db_factory):
    """launch() with resume_session_id includes --resume flag."""
    async with db_factory() as db:
        inst = Instance(name="resume-inst")
        db.add(inst)
        await db.commit()
        await db.refresh(inst)
        inst_id = inst.id

    mock_proc = _make_mock_process()
    broadcaster = MagicMock()
    broadcaster.broadcast = AsyncMock()
    im = InstanceManager(db_factory, broadcaster)

    with patch("backend.services.instance_manager.asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=mock_proc):
        await im.launch(instance_id=inst_id, prompt="followup", cwd="/tmp", resume_session_id="sess-123")

    call_args = im.processes  # just verify no error
    await asyncio.sleep(0.1)


@pytest.mark.asyncio
async def test_launch_with_model(db_factory):
    """launch() with model param includes --model flag."""
    async with db_factory() as db:
        inst = Instance(name="model-inst")
        db.add(inst)
        await db.commit()
        await db.refresh(inst)
        inst_id = inst.id

    mock_proc = _make_mock_process()
    broadcaster = MagicMock()
    broadcaster.broadcast = AsyncMock()
    im = InstanceManager(db_factory, broadcaster)

    with patch("backend.services.instance_manager.asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=mock_proc) as mock_exec:
        await im.launch(instance_id=inst_id, prompt="hi", cwd="/tmp", model="opus")

    cmd_args = mock_exec.call_args[0]
    assert "--model" in cmd_args
    assert "opus" in cmd_args
    await asyncio.sleep(0.1)


@pytest.mark.asyncio
async def test_launch_updates_db(db_factory):
    """After launch, Instance status is 'running' in DB."""
    async with db_factory() as db:
        inst = Instance(name="db-inst")
        db.add(inst)
        await db.commit()
        await db.refresh(inst)
        inst_id = inst.id

    mock_proc = _make_mock_process()
    broadcaster = MagicMock()
    broadcaster.broadcast = AsyncMock()
    im = InstanceManager(db_factory, broadcaster)

    with patch("backend.services.instance_manager.asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=mock_proc):
        await im.launch(instance_id=inst_id, prompt="hi", cwd="/tmp")

    # Check DB state (before consumer finishes)
    async with db_factory() as db:
        inst = await db.get(Instance, inst_id)
        assert inst.status == "running"
        assert inst.pid == 12345
    await asyncio.sleep(0.1)


@pytest.mark.asyncio
async def test_launch_saves_cwd(db_factory):
    """After launch with task_id, Task.last_cwd is set."""
    async with db_factory() as db:
        inst = Instance(name="cwd-inst")
        db.add(inst)
        task = Task(title="t", description="d")
        db.add(task)
        await db.commit()
        await db.refresh(inst)
        await db.refresh(task)
        inst_id = inst.id
        task_id = task.id

    mock_proc = _make_mock_process()
    broadcaster = MagicMock()
    broadcaster.broadcast = AsyncMock()
    im = InstanceManager(db_factory, broadcaster)

    with patch("backend.services.instance_manager.asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=mock_proc):
        await im.launch(instance_id=inst_id, prompt="hi", task_id=task_id, cwd="/my/repo")

    async with db_factory() as db:
        task = await db.get(Task, task_id)
        assert task.last_cwd == "/my/repo"
    await asyncio.sleep(0.1)


@pytest.mark.asyncio
async def test_launch_unsets_claude_env(db_factory):
    """Environment passed to subprocess excludes CLAUDECODE/CLAUDE_CODE."""
    async with db_factory() as db:
        inst = Instance(name="env-inst")
        db.add(inst)
        await db.commit()
        await db.refresh(inst)
        inst_id = inst.id

    mock_proc = _make_mock_process()
    broadcaster = MagicMock()
    broadcaster.broadcast = AsyncMock()
    im = InstanceManager(db_factory, broadcaster)

    with patch.dict(os.environ, {"CLAUDECODE": "1", "CLAUDE_CODE": "1"}, clear=False), \
         patch("backend.services.instance_manager.asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=mock_proc) as mock_exec:
        await im.launch(instance_id=inst_id, prompt="hi", cwd="/tmp")

    call_kwargs = mock_exec.call_args[1]
    env = call_kwargs["env"]
    assert "CLAUDECODE" not in env
    assert "CLAUDE_CODE" not in env
    await asyncio.sleep(0.1)


@pytest.mark.asyncio
async def test_default_claude_launch_clears_stale_instance_account_home(db_factory):
    async with db_factory() as db:
        inst = Instance(name="default-home-inst")
        db.add(inst)
        await db.commit()
        await db.refresh(inst)
        inst_id = inst.id

    mock_proc = _make_mock_process()
    broadcaster = MagicMock(broadcast=AsyncMock())
    im = InstanceManager(db_factory, broadcaster)
    im._config_dirs[inst_id] = "/tmp/previous-claude-account"

    with patch(
        "backend.services.instance_manager.asyncio.create_subprocess_exec",
        new_callable=AsyncMock,
        return_value=mock_proc,
    ):
        await im.launch(
            instance_id=inst_id,
            prompt="use default account",
            cwd="/tmp",
            provider="claude",
            config_dir=None,
        )

    assert im.get_config_dir(inst_id) is None
    await asyncio.sleep(0.1)


@pytest.mark.asyncio
async def test_launch_with_thinking_budget_sets_env(db_factory):
    """launch(thinking_budget=N) injects MAX_THINKING_TOKENS=N into subprocess env."""
    async with db_factory() as db:
        inst = Instance(name="thinking-inst")
        db.add(inst)
        await db.commit()
        await db.refresh(inst)
        inst_id = inst.id

    mock_proc = _make_mock_process()
    broadcaster = MagicMock()
    broadcaster.broadcast = AsyncMock()
    im = InstanceManager(db_factory, broadcaster)

    with patch("backend.services.instance_manager.asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=mock_proc) as mock_exec:
        await im.launch(instance_id=inst_id, prompt="hi", cwd="/tmp", thinking_budget=12000)

    env = mock_exec.call_args[1]["env"]
    assert env.get("MAX_THINKING_TOKENS") == "12000"
    await asyncio.sleep(0.1)


@pytest.mark.asyncio
async def test_launch_without_thinking_budget_omits_env(db_factory):
    """launch() without thinking_budget leaves MAX_THINKING_TOKENS unset."""
    async with db_factory() as db:
        inst = Instance(name="no-thinking-inst")
        db.add(inst)
        await db.commit()
        await db.refresh(inst)
        inst_id = inst.id

    mock_proc = _make_mock_process()
    broadcaster = MagicMock()
    broadcaster.broadcast = AsyncMock()
    im = InstanceManager(db_factory, broadcaster)

    # Make sure the env var isn't already set in the test environment
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("MAX_THINKING_TOKENS", None)
        with patch("backend.services.instance_manager.asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=mock_proc) as mock_exec:
            await im.launch(instance_id=inst_id, prompt="hi", cwd="/tmp")

    env = mock_exec.call_args[1]["env"]
    assert "MAX_THINKING_TOKENS" not in env
    await asyncio.sleep(0.1)


@pytest.mark.asyncio
async def test_launch_with_zero_thinking_budget_omits_env(db_factory):
    """thinking_budget=0 is treated as 'no budget' (CLI default)."""
    async with db_factory() as db:
        inst = Instance(name="zero-budget-inst")
        db.add(inst)
        await db.commit()
        await db.refresh(inst)
        inst_id = inst.id

    mock_proc = _make_mock_process()
    broadcaster = MagicMock()
    broadcaster.broadcast = AsyncMock()
    im = InstanceManager(db_factory, broadcaster)

    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("MAX_THINKING_TOKENS", None)
        with patch("backend.services.instance_manager.asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=mock_proc) as mock_exec:
            await im.launch(instance_id=inst_id, prompt="hi", cwd="/tmp", thinking_budget=0)

    env = mock_exec.call_args[1]["env"]
    assert "MAX_THINKING_TOKENS" not in env
    await asyncio.sleep(0.1)


@pytest.mark.asyncio
async def test_launch_with_effort_level(db_factory):
    """launch(effort_level='high') includes --effort high in command."""
    async with db_factory() as db:
        inst = Instance(name="effort-inst")
        db.add(inst)
        await db.commit()
        await db.refresh(inst)
        inst_id = inst.id

    mock_proc = _make_mock_process()
    broadcaster = MagicMock()
    broadcaster.broadcast = AsyncMock()
    im = InstanceManager(db_factory, broadcaster)

    with patch("backend.services.instance_manager.asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=mock_proc) as mock_exec:
        await im.launch(instance_id=inst_id, prompt="hi", cwd="/tmp", effort_level="high")

    cmd_args = mock_exec.call_args[0]
    assert "--effort" in cmd_args
    idx = cmd_args.index("--effort")
    assert cmd_args[idx + 1] == "high"
    await asyncio.sleep(0.1)


@pytest.mark.asyncio
async def test_launch_codex_provider_command(db_factory, monkeypatch, tmp_path):
    """launch(provider='codex') constructs codex exec command."""
    monkeypatch.setattr(settings, "codex_app_server_enabled", False)
    async with db_factory() as db:
        inst = Instance(name="codex-inst")
        db.add(inst)
        await db.commit()
        await db.refresh(inst)
        inst_id = inst.id

    mock_proc = _make_mock_process()
    broadcaster = MagicMock()
    broadcaster.broadcast = AsyncMock()
    im = InstanceManager(db_factory, broadcaster)

    codex_home = tmp_path / "codex-account"
    with patch("backend.services.instance_manager.asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=mock_proc) as mock_exec:
        await im.launch(
            instance_id=inst_id, prompt="do stuff", cwd="/tmp",
            provider="codex", config_dir=str(codex_home),
        )

    cmd_args = mock_exec.call_args[0]
    assert cmd_args[1] == "exec"
    assert "--json" in cmd_args
    assert "--dangerously-bypass-approvals-and-sandbox" in cmd_args
    assert "do stuff" in cmd_args
    # Should NOT have Claude-specific flags
    assert "--output-format" not in cmd_args
    assert "--verbose" not in cmd_args
    expected_home = str(codex_home.resolve())
    assert mock_exec.call_args.kwargs["env"]["CODEX_HOME"] == expected_home
    assert im.get_config_dir(inst_id) == expected_home
    assert codex_home.stat().st_mode & 0o777 == 0o700
    await asyncio.sleep(0.1)


@pytest.mark.asyncio
async def test_launch_codex_prefers_persistent_app_server(
    db_factory, monkeypatch, tmp_path,
):
    monkeypatch.setattr(settings, "codex_app_server_enabled", True)
    async with db_factory() as db:
        inst = Instance(name="codex-app-inst")
        db.add(inst)
        await db.commit()
        await db.refresh(inst)

    im = InstanceManager(db_factory, MagicMock())
    im._launch_codex_app_server = AsyncMock(return_value=4321)
    codex_home = tmp_path / "codex-account"
    with patch(
        "backend.services.instance_manager.asyncio.create_subprocess_exec",
        new_callable=AsyncMock,
    ) as exec_mock:
        pid = await im.launch(
            instance_id=inst.id, prompt="hi", cwd="/tmp", provider="codex",
            resume_session_id="thread-1", config_dir=str(codex_home),
        )

    assert pid == 4321
    im._launch_codex_app_server.assert_awaited_once()
    assert im._launch_codex_app_server.await_args.kwargs["resume_session_id"] == "thread-1"
    assert im._launch_codex_app_server.await_args.kwargs["config_dir"] == str(
        codex_home.resolve()
    )
    exec_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_launch_codex_falls_back_to_exec_when_app_server_fails(
    db_factory, monkeypatch, tmp_path,
):
    monkeypatch.setattr(settings, "codex_app_server_enabled", True)
    async with db_factory() as db:
        inst = Instance(name="codex-fallback-inst")
        db.add(inst)
        await db.commit()
        await db.refresh(inst)

    mock_proc = _make_mock_process()
    broadcaster = MagicMock()
    broadcaster.broadcast = AsyncMock()
    im = InstanceManager(db_factory, broadcaster)
    im._launch_codex_app_server = AsyncMock(side_effect=RuntimeError("bad protocol"))
    codex_home = tmp_path / "codex-fallback-home"
    with patch(
        "backend.services.instance_manager.asyncio.create_subprocess_exec",
        new_callable=AsyncMock,
        return_value=mock_proc,
    ) as exec_mock:
        await im.launch(
            instance_id=inst.id, prompt="fallback", cwd="/tmp", provider="codex",
            config_dir=str(codex_home),
        )

    assert exec_mock.await_args.args[1] == "exec"
    assert "fallback" in exec_mock.await_args.args
    assert exec_mock.await_args.kwargs["env"]["CODEX_HOME"] == str(
        codex_home.resolve()
    )
    await asyncio.sleep(0.1)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "launch_error",
    [
        asyncio.TimeoutError(),
        CodexAppServerBusyError("account busy"),
        CodexThreadHomeMismatchError("wrong owner"),
    ],
    ids=["timeout", "busy", "owner-mismatch"],
)
async def test_launch_codex_does_not_fallback_when_replay_is_unsafe(
    db_factory, monkeypatch, tmp_path, launch_error,
):
    monkeypatch.setattr(settings, "codex_app_server_enabled", True)
    async with db_factory() as db:
        inst = Instance(name="codex-no-fallback-inst")
        db.add(inst)
        await db.commit()
        await db.refresh(inst)

    im = InstanceManager(db_factory, MagicMock())
    im._launch_codex_app_server = AsyncMock(side_effect=launch_error)
    with patch(
        "backend.services.instance_manager.asyncio.create_subprocess_exec",
        new_callable=AsyncMock,
    ) as exec_mock:
        with pytest.raises(type(launch_error)):
            await im.launch(
                instance_id=inst.id,
                prompt="must not replay",
                cwd="/tmp",
                provider="codex",
                config_dir=str(tmp_path / "codex-home"),
            )

    exec_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_launch_codex_app_server_routes_turn_to_canonical_home(
    db_factory, tmp_path,
):
    async with db_factory() as db:
        inst = Instance(name="codex-registry-inst")
        task = Task(title="registry-task", status="executing", provider="codex")
        db.add_all([inst, task])
        await db.commit()
        await db.refresh(inst)
        await db.refresh(task)

    process = _make_mock_process(pid=7654)
    registry = MagicMock()
    registry.start_turn = AsyncMock(return_value=(process, "thread-home"))
    codex_home = tmp_path / "account-home"
    im = InstanceManager(db_factory, MagicMock(broadcast=AsyncMock()))

    with patch(
        "backend.services.codex_app_server.CodexAppServerRegistry",
        return_value=registry,
    ) as registry_cls:
        pid = await im._launch_codex_app_server(
            instance_id=inst.id,
            prompt="work",
            task_id=task.id,
            cwd="/tmp",
            model="gpt-5.5",
            resume_session_id="thread-home",
            loop_iteration=None,
            git_env=None,
            effort_level="high",
            chat_initiated=True,
            config_dir=str(codex_home.resolve()),
            enable_workflows=False,
            enabled_skills=None,
        )

    assert pid == 7654
    registry_cls.assert_called_once()
    assert registry.start_turn.await_args.kwargs["codex_home"] == str(
        codex_home.resolve()
    )
    assert im.get_config_dir(inst.id) == str(codex_home.resolve())
    assert im._launch_params[inst.id]["config_dir"] == str(codex_home.resolve())
    await asyncio.sleep(0.1)


@pytest.mark.asyncio
async def test_codex_registry_lifecycle_facades_delegate():
    registry = MagicMock()
    registry.begin_home_maintenance = AsyncMock(return_value=True)
    registry.end_home_maintenance = AsyncMock()
    registry.rebind_thread = AsyncMock()
    im = InstanceManager(MagicMock(), MagicMock())
    im._codex_app_server = registry

    assert await im.shutdown_codex_app_server_home(
        "/tmp/codex-a", require_idle=True,
    ) is True
    await im.rebind_codex_thread(
        "thread-1",
        source_codex_home="/tmp/codex-a",
        target_codex_home="/tmp/codex-b",
    )
    await im.begin_codex_app_server_home_maintenance("/tmp/codex-b")
    await im.end_codex_app_server_home_maintenance("/tmp/codex-b")

    assert registry.begin_home_maintenance.await_args_list[0].kwargs == {
        "require_idle": True,
    }
    assert registry.begin_home_maintenance.await_args_list[0].args == (
        "/tmp/codex-a",
    )
    registry.end_home_maintenance.assert_any_await("/tmp/codex-a")
    registry.begin_home_maintenance.assert_any_await(
        "/tmp/codex-b", require_idle=True,
    )
    registry.end_home_maintenance.assert_any_await("/tmp/codex-b")
    registry.rebind_thread.assert_awaited_once_with(
        "thread-1",
        source_codex_home="/tmp/codex-a",
        target_codex_home="/tmp/codex-b",
    )


@pytest.mark.asyncio
async def test_codex_maintenance_rejects_active_exec_turn(tmp_path):
    home = str((tmp_path / "codex-a").resolve())
    im = InstanceManager(MagicMock(), MagicMock())
    im.processes[7] = MagicMock(returncode=None)
    im._codex_exec_homes[7] = home

    with pytest.raises(CodexAppServerBusyError, match="active exec turn"):
        await im.begin_codex_home_maintenance(home, require_idle=True)

    assert home not in im._codex_home_maintenance
    assert im._codex_app_server is None


@pytest.mark.asyncio
async def test_codex_maintenance_blocks_exec_launch_even_without_app_server(
    db_factory, monkeypatch, tmp_path,
):
    monkeypatch.setattr(settings, "codex_app_server_enabled", False)
    async with db_factory() as db:
        inst = Instance(name="codex-maintenance-inst")
        db.add(inst)
        await db.commit()
        await db.refresh(inst)

    home = str((tmp_path / "codex-a").resolve())
    im = InstanceManager(db_factory, MagicMock())
    assert await im.begin_codex_home_maintenance(home) is False
    try:
        with patch(
            "backend.services.instance_manager.asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
        ) as exec_mock:
            with pytest.raises(CodexAppServerBusyError, match="under maintenance"):
                await im.launch(
                    instance_id=inst.id,
                    prompt="must wait",
                    cwd="/tmp",
                    provider="codex",
                    config_dir=home,
                )
        exec_mock.assert_not_awaited()
    finally:
        await im.end_codex_home_maintenance(home)

    assert home not in im._codex_home_maintenance


@pytest.mark.asyncio
async def test_codex_maintenance_reservation_creates_registry_before_first_turn(
    tmp_path,
):
    registry = MagicMock()
    registry.begin_home_maintenance = AsyncMock(return_value=False)
    registry.end_home_maintenance = AsyncMock()
    im = InstanceManager(MagicMock(), MagicMock())

    def ensure_registry():
        im._codex_app_server = registry
        return registry

    im._ensure_codex_app_server_registry = MagicMock(side_effect=ensure_registry)
    home = str((tmp_path / "codex-first").resolve())

    assert await im.begin_codex_home_maintenance(home) is False
    assert home in im._codex_home_maintenance
    await im.end_codex_home_maintenance(home)

    im._ensure_codex_app_server_registry.assert_called_once_with()
    registry.begin_home_maintenance.assert_awaited_once_with(
        home, require_idle=True,
    )
    registry.end_home_maintenance.assert_awaited_once_with(home)


@pytest.mark.asyncio
async def test_codex_registry_legacy_rebind_facade_still_delegates():
    registry = MagicMock()
    registry.rebind_thread = AsyncMock()
    im = InstanceManager(MagicMock(), MagicMock())
    im._codex_app_server = registry

    await im.rebind_codex_app_server_thread(
        "thread-legacy",
        source_codex_home="/tmp/codex-a",
        target_codex_home="/tmp/codex-b",
    )

    registry.rebind_thread.assert_awaited_once_with(
        "thread-legacy",
        source_codex_home="/tmp/codex-a",
        target_codex_home="/tmp/codex-b",
    )


@pytest.mark.asyncio
async def test_codex_shutdown_home_uses_idle_maintenance_gate():
    registry = MagicMock()
    registry.begin_home_maintenance = AsyncMock(return_value=True)
    registry.end_home_maintenance = AsyncMock()
    im = InstanceManager(MagicMock(), MagicMock())
    im._codex_app_server = registry

    assert await im.shutdown_codex_app_server_home(
        "/tmp/codex-a", require_idle=True,
    ) is True
    registry.begin_home_maintenance.assert_awaited_once_with(
        "/tmp/codex-a", require_idle=True,
    )
    registry.end_home_maintenance.assert_awaited_once_with("/tmp/codex-a")


@pytest.mark.asyncio
async def test_codex_chat_pool_rotation_delegates_to_dispatcher_and_relaunches(
    db_factory, tmp_path,
):
    async with db_factory() as db:
        task = Task(
            title="rotate-codex",
            status="executing",
            provider="codex",
            session_id="thread-rotate",
            last_cwd="/tmp",
        )
        db.add(task)
        await db.commit()
        await db.refresh(task)

    new_home = str((tmp_path / "codex-b").resolve())
    dispatcher = MagicMock()
    dispatcher._check_rate_limit_and_rotate = AsyncMock(return_value={
        "config_dir": new_home,
        "session_id": "thread-rotate",
        "excluded": {"codex-a"},
    })
    im = InstanceManager(db_factory, MagicMock(broadcast=AsyncMock()))
    im._launch_params[7] = {
        "provider": "codex",
        "prompt": "continue the task",
        "model": "gpt-5.5",
        "git_env": {},
        "effort_level": "high",
    }
    im.get_recent_log_contents = AsyncMock(return_value=[])
    im.launch = AsyncMock(return_value=999)

    with patch("backend.main.dispatcher", dispatcher):
        rotated = await im._try_chat_pool_rotation(
            7, task.id, 1, "You've hit your usage limit",
        )

    assert rotated is True
    assert dispatcher._check_rate_limit_and_rotate.await_args.args == (
        7, task.id, 1,
    )
    combined = dispatcher._check_rate_limit_and_rotate.await_args.kwargs["combined"]
    assert "usage limit" in combined
    launch_kwargs = im.launch.await_args.kwargs
    assert launch_kwargs["provider"] == "codex"
    assert launch_kwargs["config_dir"] == new_home
    assert launch_kwargs["resume_session_id"] == "thread-rotate"
    assert launch_kwargs["prompt"] == "continue the task"


@pytest.mark.asyncio
async def test_codex_chat_pool_rotation_replays_fresh_prompt_without_session(
    db_factory, tmp_path,
):
    """Fresh/compact-retry turns rotate by starting a new thread in the new home."""

    async with db_factory() as db:
        task = Task(
            title="rotate-fresh-codex",
            status="executing",
            provider="codex",
            session_id=None,
            last_cwd="/tmp",
        )
        db.add(task)
        await db.commit()
        await db.refresh(task)

    new_home = str((tmp_path / "codex-b").resolve())
    dispatcher = MagicMock()
    dispatcher._check_rate_limit_and_rotate = AsyncMock(return_value={
        "config_dir": new_home,
        "session_id": None,
        "excluded": {"codex-a"},
    })
    compact_prompt = "[Context compacted]\nsummary\n\n[Message]\ncontinue"
    im = InstanceManager(db_factory, MagicMock(broadcast=AsyncMock()))
    im._launch_params[7] = {
        "provider": "codex",
        "prompt": compact_prompt,
        "model": "gpt-5.5",
        "git_env": {},
        "effort_level": "high",
    }
    im.get_recent_log_contents = AsyncMock(return_value=[])
    im.launch = AsyncMock(return_value=999)

    with patch("backend.main.dispatcher", dispatcher):
        rotated = await im._try_chat_pool_rotation(
            7, task.id, 1, "You've hit your usage limit",
        )

    assert rotated is True
    launch_kwargs = im.launch.await_args.kwargs
    assert launch_kwargs["provider"] == "codex"
    assert launch_kwargs["config_dir"] == new_home
    assert launch_kwargs["resume_session_id"] is None
    assert launch_kwargs["prompt"] == compact_prompt


@pytest.mark.asyncio
async def test_claude_soft_quota_switch_migrates_before_reset_cooldown(
    db_factory, tmp_path,
):
    source = tmp_path / "claude-a"
    target = tmp_path / "claude-b"
    session_id = "quota-session"
    rollout = source / "projects" / "encoded-cwd" / f"{session_id}.jsonl"
    rollout.parent.mkdir(parents=True)
    rollout.write_text('{"type":"user"}\n')
    config = tmp_path / "claude-pool.json"
    config.write_text(json.dumps({"accounts": [
        {"id": "claude-a", "config_dir": str(source), "enabled": True},
        {"id": "claude-b", "config_dir": str(target), "enabled": True},
    ]}))
    pool = ClaudePool(config_path=config, cooldown_seconds=60)
    pool.fetch_usage = AsyncMock(return_value=[
        {"id": "claude-a", "usage": {"five_hour": {"utilization": 95}}},
        {"id": "claude-b", "usage": {"seven_day": {"utilization": 20}}},
    ])

    async with db_factory() as db:
        task = Task(
            title="claude quota", provider="claude", status="executing",
            session_id=session_id,
        )
        db.add(task)
        await db.commit()
        await db.refresh(task)

    broadcaster = MagicMock(broadcast=AsyncMock())
    im = InstanceManager(db_factory, broadcaster)
    im._config_dirs[7] = str(source)
    dispatcher = MagicMock(pool=pool, codex_pool=None)
    reset_at = time.time() + 3600

    with patch("backend.main.dispatcher", dispatcher):
        switched = await im._try_proactive_pool_switch(
            7,
            task.id,
            rate_limit_info={
                "status": "allowed_warning",
                "rateLimitType": "five_hour",
                "utilization": 0.95,
                "resetsAt": reset_at,
            },
        )

    assert switched is True
    migrated = target / "projects" / "encoded-cwd" / f"{session_id}.jsonl"
    assert migrated.exists()
    assert migrated.stat().st_ino == rollout.stat().st_ino
    assert pool.is_in_cooldown(str(source))
    assert pool._cooldowns["claude-a"] >= reset_at - 2
    assert im.get_config_dir(7) == str(target)


@pytest.mark.asyncio
@pytest.mark.parametrize("migration_exists", [True, False])
async def test_claude_soft_quota_no_usable_target_or_migration_failure_does_not_cool(
    db_factory, tmp_path, migration_exists,
):
    source = tmp_path / "claude-a"
    target = tmp_path / "claude-b"
    session_id = "quota-stays"
    if migration_exists:
        rollout = source / "projects" / "encoded-cwd" / f"{session_id}.jsonl"
        rollout.parent.mkdir(parents=True)
        rollout.write_text("{}\n")
    config = tmp_path / "claude-pool.json"
    config.write_text(json.dumps({"accounts": [
        {"id": "claude-a", "config_dir": str(source), "enabled": True},
        {"id": "claude-b", "config_dir": str(target), "enabled": True},
    ]}))
    pool = ClaudePool(config_path=config, cooldown_seconds=60)
    if migration_exists:
        # Both accounts are known-high, so selection must stop before migration.
        pool.fetch_usage = AsyncMock(return_value=[
            {"id": "claude-a", "usage": {"five_hour": {"utilization": 95}}},
            {"id": "claude-b", "usage": {"seven_day": {"utilization": 90}}},
        ])
    else:
        # A usable target exists, but the session copy itself fails.
        pool.fetch_usage = AsyncMock(return_value=[
            {"id": "claude-a", "usage": {"five_hour": {"utilization": 95}}},
            {"id": "claude-b", "usage": {"seven_day": {"utilization": 10}}},
        ])

    async with db_factory() as db:
        task = Task(
            title="claude quota stays", provider="claude", status="executing",
            session_id=session_id,
        )
        db.add(task)
        await db.commit()
        await db.refresh(task)

    im = InstanceManager(db_factory, MagicMock(broadcast=AsyncMock()))
    im._config_dirs[7] = str(source)
    dispatcher = MagicMock(pool=pool, codex_pool=None)
    with patch("backend.main.dispatcher", dispatcher):
        switched = await im._try_proactive_pool_switch(
            7,
            task.id,
            rate_limit_info={
                "status": "allowed_warning",
                "rateLimitType": "seven_day",
                "utilization": 0.95,
                "resetsAt": time.time() + 86400,
            },
        )

    assert switched is False
    assert not pool.is_in_cooldown(str(source))
    assert im.get_config_dir(7) == str(source)


@pytest.mark.asyncio
async def test_codex_soft_quota_switch_migrates_rebinds_and_updates_binding(
    db_factory, tmp_path,
):
    source = tmp_path / "codex-a"
    target = tmp_path / "codex-b"
    session_id = "thread-quota"
    rollout = (
        source / "sessions" / "2026" / "07" / "21"
        / f"rollout-2026-07-21T00-00-00-{session_id}.jsonl"
    )
    rollout.parent.mkdir(parents=True)
    rollout.write_text("{}\n")
    config = tmp_path / "codex-pool.json"
    config.write_text(json.dumps({"accounts": [
        {"id": "codex-a", "codex_home": str(source), "enabled": True},
        {"id": "codex-b", "codex_home": str(target), "enabled": True},
    ]}))
    pool = CodexPool(config_path=config)
    pool.select_quota_alternative = AsyncMock(return_value=str(target.resolve()))
    reset_at = time.time() + 7200
    pool._quota_cache = {
        "codex-a": {
            "id": "codex-a",
            "quota": {
                "primary_used_percent": 95,
                "primary_resets_at": time.time() + 300,
                "secondary_used_percent": 90,
                "secondary_resets_at": reset_at,
            },
        }
    }

    async with db_factory() as db:
        task = Task(
            title="codex quota", provider="codex", status="executing",
            session_id=session_id, metadata_={"codex_account_id": "codex-a"},
        )
        db.add(task)
        await db.commit()
        await db.refresh(task)

    im = InstanceManager(db_factory, MagicMock(broadcast=AsyncMock()))
    im._config_dirs[7] = str(source.resolve())
    im.rebind_codex_thread = AsyncMock()
    dispatcher = MagicMock(pool=None, codex_pool=pool)
    dispatcher._set_codex_task_binding = AsyncMock()

    with patch("backend.main.dispatcher", dispatcher):
        switched = await im._try_proactive_pool_switch(7, task.id)

    assert switched is True
    migrated = (
        target / "sessions" / "2026" / "07" / "21"
        / f"rollout-2026-07-21T00-00-00-{session_id}.jsonl"
    )
    assert migrated.exists()
    im.rebind_codex_thread.assert_awaited_once_with(
        session_id,
        source_codex_home=str(source.resolve()),
        target_codex_home=str(target.resolve()),
    )
    dispatcher._set_codex_task_binding.assert_awaited_once_with(
        task.id, "codex-b"
    )
    assert pool.is_in_cooldown(str(source))
    assert pool._cooldowns["codex-a"] >= reset_at - 2
    assert im.get_config_dir(7) == str(target.resolve())


@pytest.mark.asyncio
@pytest.mark.parametrize("rollback_fails", [False, True])
async def test_codex_soft_quota_binding_failure_rolls_back_owner_without_cooldown(
    db_factory, tmp_path, rollback_fails,
):
    source = tmp_path / "codex-binding-old"
    target = tmp_path / "codex-binding-new"
    session_id = "thread-binding-rollback"
    rollout = (
        source / "sessions" / "2026" / "07" / "21"
        / f"rollout-2026-07-21T00-00-00-{session_id}.jsonl"
    )
    rollout.parent.mkdir(parents=True)
    rollout.write_text("{}\n")
    config = tmp_path / "codex-binding-pool.json"
    config.write_text(json.dumps({"accounts": [
        {"id": "codex-old", "codex_home": str(source), "enabled": True},
        {"id": "codex-new", "codex_home": str(target), "enabled": True},
    ]}))
    pool = CodexPool(config_path=config)
    pool.select_quota_alternative = AsyncMock(return_value=str(target.resolve()))
    pool._quota_cache = {
        "codex-old": {
            "id": "codex-old",
            "quota": {
                "primary_used_percent": 95,
                "primary_resets_at": time.time() + 3600,
            },
        }
    }

    async with db_factory() as db:
        task = Task(
            title="codex binding rollback", provider="codex", status="executing",
            session_id=session_id, metadata_={"codex_account_id": "codex-old"},
        )
        db.add(task)
        await db.commit()
        await db.refresh(task)

    im = InstanceManager(db_factory, MagicMock(broadcast=AsyncMock()))
    im._config_dirs[7] = str(source.resolve())
    im.rebind_codex_thread = AsyncMock(
        side_effect=[None, RuntimeError("rollback busy")]
        if rollback_fails else None
    )
    im.clear_codex_thread_owner_for_recovery = AsyncMock(return_value=True)
    dispatcher = MagicMock(pool=None, codex_pool=pool)
    dispatcher._set_codex_task_binding = AsyncMock(
        side_effect=RuntimeError("database unavailable")
    )

    with patch("backend.main.dispatcher", dispatcher):
        switched = await im._try_proactive_pool_switch(7, task.id)

    assert switched is False
    assert im.rebind_codex_thread.await_args_list == [
        call(
            session_id,
            source_codex_home=str(source.resolve()),
            target_codex_home=str(target.resolve()),
        ),
        call(
            session_id,
            source_codex_home=str(target.resolve()),
            target_codex_home=str(source.resolve()),
        ),
    ]
    assert not pool.is_in_cooldown(str(source))
    assert im.get_config_dir(7) == str(source.resolve())
    if rollback_fails:
        im.clear_codex_thread_owner_for_recovery.assert_awaited_once_with(
            session_id,
            expected_codex_home=str(target.resolve()),
        )
    else:
        im.clear_codex_thread_owner_for_recovery.assert_not_awaited()
    async with db_factory() as db:
        persisted = await db.get(Task, task.id)
        assert persisted.metadata_["codex_account_id"] == "codex-old"


@pytest.mark.asyncio
async def test_codex_soft_quota_rebind_failure_keeps_old_home_available(
    db_factory, tmp_path,
):
    source = tmp_path / "codex-rebind-old"
    target = tmp_path / "codex-rebind-new"
    session_id = "thread-rebind-fails"
    rollout = (
        source / "sessions" / "2026" / "07" / "21"
        / f"rollout-2026-07-21T00-00-00-{session_id}.jsonl"
    )
    rollout.parent.mkdir(parents=True)
    rollout.write_text("{}\n")
    config = tmp_path / "codex-rebind-pool.json"
    config.write_text(json.dumps({"accounts": [
        {"id": "codex-old", "codex_home": str(source), "enabled": True},
        {"id": "codex-new", "codex_home": str(target), "enabled": True},
    ]}))
    pool = CodexPool(config_path=config)
    pool.select_quota_alternative = AsyncMock(return_value=str(target.resolve()))
    pool._quota_cache = {
        "codex-old": {
            "id": "codex-old",
            "quota": {
                "primary_used_percent": 95,
                "primary_resets_at": time.time() + 3600,
            },
        }
    }

    async with db_factory() as db:
        task = Task(
            title="codex rebind failure", provider="codex", status="executing",
            session_id=session_id, metadata_={"codex_account_id": "codex-old"},
        )
        db.add(task)
        await db.commit()
        await db.refresh(task)

    im = InstanceManager(db_factory, MagicMock(broadcast=AsyncMock()))
    im._config_dirs[7] = str(source.resolve())
    im.rebind_codex_thread = AsyncMock(side_effect=RuntimeError("target busy"))
    dispatcher = MagicMock(pool=None, codex_pool=pool)
    dispatcher._set_codex_task_binding = AsyncMock()

    with patch("backend.main.dispatcher", dispatcher):
        switched = await im._try_proactive_pool_switch(7, task.id)

    assert switched is False
    dispatcher._set_codex_task_binding.assert_not_awaited()
    assert not pool.is_in_cooldown(str(source))
    assert im.get_config_dir(7) == str(source.resolve())


@pytest.mark.asyncio
async def test_codex_chat_routing_error_requeues_prompt_and_cleans_failed_turn(
    db_factory,
):
    from backend.services.dispatcher import (
        CodexAccountRoutingError,
        PRIORITY_USER,
    )

    async with db_factory() as db:
        task = Task(
            title="route-retry-codex",
            status="executing",
            provider="codex",
            session_id="thread-route",
            last_cwd="/tmp",
        )
        inst = Instance(name="route-retry-inst", status="running")
        db.add_all([task, inst])
        await db.flush()
        inst.current_task_id = task.id
        await db.commit()
        await db.refresh(task)
        await db.refresh(inst)

    dispatcher = MagicMock()
    dispatcher._check_rate_limit_and_rotate = AsyncMock(side_effect=
        CodexAccountRoutingError(
            "rollout migration is temporarily unavailable", retry_after=5,
        )
    )
    dispatcher.enqueue_message = AsyncMock()
    broadcaster = MagicMock(broadcast=AsyncMock())
    im = InstanceManager(db_factory, broadcaster)
    process = _make_mock_process(returncode=1)
    im.processes[inst.id] = process
    im._launch_params[inst.id] = {
        "provider": "codex",
        "prompt": "preserve this exact user prompt",
        "model": "gpt-5.5",
    }
    im.get_recent_log_contents = AsyncMock(return_value=[])

    with patch("backend.main.dispatcher", dispatcher):
        await im._consume_output(
            inst.id,
            task.id,
            process,
            chat_initiated=True,
            provider="codex",
        )

    dispatcher.enqueue_message.assert_awaited_once_with(
        task_id=task.id,
        prompt="preserve this exact user prompt",
        priority=PRIORITY_USER,
        source="routing_retry",
    )
    async with db_factory() as db:
        refreshed_task = await db.get(Task, task.id)
        refreshed_inst = await db.get(Instance, inst.id)
    assert refreshed_task.status == "failed"
    assert refreshed_inst.status == "error"
    assert inst.id not in im.processes


@pytest.mark.asyncio
async def test_codex_transient_replacement_busy_requeues_exact_prompt(
    db_factory, monkeypatch,
):
    import backend.services.claude_pool as claude_pool_module

    async with db_factory() as db:
        task = Task(
            title="transient replacement busy",
            status="executing",
            provider="codex",
            session_id="thread-transient-busy",
            last_cwd="/tmp",
        )
        db.add(task)
        await db.commit()
        await db.refresh(task)

    dispatcher = MagicMock()
    dispatcher.enqueue_message = AsyncMock()
    broadcaster = MagicMock(broadcast=AsyncMock())
    im = InstanceManager(db_factory, broadcaster)
    im._config_dirs[7] = "/tmp/codex-a"
    im._launch_params[7] = {
        "provider": "codex",
        "prompt": "preserve transient prompt",
        "model": "gpt-5.5",
    }
    im.get_recent_log_contents = AsyncMock(return_value=[])
    im.launch = AsyncMock(
        side_effect=CodexAppServerBusyError("account under maintenance")
    )
    monkeypatch.setattr(
        claude_pool_module, "transient_retry_delay", lambda *_args: 0,
    )

    with patch("backend.main.dispatcher", dispatcher):
        launched = await im._try_chat_transient_retry(
            7, task.id, 1, "request timed out",
        )

    assert launched is False
    dispatcher.enqueue_message.assert_awaited_once_with(
        task_id=task.id,
        prompt="preserve transient prompt",
        priority=0,
        source="routing_retry",
    )


@pytest.mark.asyncio
async def test_codex_pool_replacement_busy_requeues_exact_prompt(db_factory):
    async with db_factory() as db:
        task = Task(
            title="pool replacement busy",
            status="executing",
            provider="codex",
            session_id="thread-pool-busy",
            last_cwd="/tmp",
        )
        db.add(task)
        await db.commit()
        await db.refresh(task)

    dispatcher = MagicMock()
    dispatcher._check_rate_limit_and_rotate = AsyncMock(return_value={
        "config_dir": "/tmp/codex-b",
        "session_id": task.session_id,
    })
    dispatcher.enqueue_message = AsyncMock()
    broadcaster = MagicMock(broadcast=AsyncMock())
    im = InstanceManager(db_factory, broadcaster)
    im._launch_params[7] = {
        "provider": "codex",
        "prompt": "preserve rotation prompt",
        "model": "gpt-5.5",
    }
    im.get_recent_log_contents = AsyncMock(return_value=[])
    im.launch = AsyncMock(
        side_effect=CodexThreadHomeMismatchError("thread is being rebound")
    )

    with patch("backend.main.dispatcher", dispatcher):
        launched = await im._try_chat_pool_rotation(
            7, task.id, 1, "You've hit your usage limit",
        )

    assert launched is False
    dispatcher.enqueue_message.assert_awaited_once_with(
        task_id=task.id,
        prompt="preserve rotation prompt",
        priority=0,
        source="routing_retry",
    )


@pytest.mark.asyncio
async def test_launch_codex_no_thinking_budget_env(db_factory, monkeypatch):
    """launch(provider='codex', thinking_budget=N) does NOT set MAX_THINKING_TOKENS."""
    monkeypatch.setattr(settings, "codex_app_server_enabled", False)
    async with db_factory() as db:
        inst = Instance(name="codex-think-inst")
        db.add(inst)
        await db.commit()
        await db.refresh(inst)
        inst_id = inst.id

    mock_proc = _make_mock_process()
    broadcaster = MagicMock()
    broadcaster.broadcast = AsyncMock()
    im = InstanceManager(db_factory, broadcaster)

    with patch("backend.services.instance_manager.asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=mock_proc) as mock_exec:
        await im.launch(instance_id=inst_id, prompt="hi", cwd="/tmp", provider="codex", thinking_budget=12000)

    env = mock_exec.call_args[1]["env"]
    assert "MAX_THINKING_TOKENS" not in env
    await asyncio.sleep(0.1)


@pytest.mark.asyncio
async def test_launch_without_effort_level_omits_flag(db_factory):
    """launch() without effort_level does not include --effort."""
    async with db_factory() as db:
        inst = Instance(name="no-effort-inst")
        db.add(inst)
        await db.commit()
        await db.refresh(inst)
        inst_id = inst.id

    mock_proc = _make_mock_process()
    broadcaster = MagicMock()
    broadcaster.broadcast = AsyncMock()
    im = InstanceManager(db_factory, broadcaster)

    with patch("backend.services.instance_manager.asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=mock_proc) as mock_exec:
        await im.launch(instance_id=inst_id, prompt="hi", cwd="/tmp")

    cmd_args = mock_exec.call_args[0]
    assert "--effort" not in cmd_args
    await asyncio.sleep(0.1)


@pytest.mark.asyncio
async def test_stop_terminates(db_factory):
    """stop() sends SIGINT first and updates DB status."""
    async with db_factory() as db:
        inst = Instance(name="stop-inst", status="running")
        db.add(inst)
        await db.commit()
        await db.refresh(inst)
        inst_id = inst.id

    mock_proc = MagicMock()
    mock_proc.returncode = None  # Still running
    mock_proc.terminate = MagicMock()
    mock_proc.send_signal = MagicMock()
    mock_proc.wait = AsyncMock(return_value=0)
    mock_proc.kill = MagicMock()

    broadcaster = MagicMock()
    broadcaster.broadcast = AsyncMock()
    im = InstanceManager(db_factory, broadcaster)
    im.processes[inst_id] = mock_proc

    # After SIGINT, wait() succeeds — set returncode
    async def fake_wait():
        mock_proc.returncode = 0
        return 0
    mock_proc.wait = fake_wait

    result = await im.stop(inst_id)
    assert result is True
    mock_proc.send_signal.assert_called_once_with(signal.SIGINT)

    async with db_factory() as db:
        inst = await db.get(Instance, inst_id)
        assert inst.status == "idle"


@pytest.mark.asyncio
async def test_stop_kills_on_timeout(db_factory):
    """stop() sends SIGKILL after timeout."""
    async with db_factory() as db:
        inst = Instance(name="kill-inst", status="running")
        db.add(inst)
        await db.commit()
        await db.refresh(inst)
        inst_id = inst.id

    mock_proc = MagicMock()
    mock_proc.returncode = None
    mock_proc.terminate = MagicMock()
    mock_proc.kill = MagicMock()

    # After kill, wait() succeeds
    async def post_kill_wait():
        mock_proc.returncode = -9
        return -9

    mock_proc.wait = post_kill_wait

    broadcaster = MagicMock()
    broadcaster.broadcast = AsyncMock()
    im = InstanceManager(db_factory, broadcaster)
    im.processes[inst_id] = mock_proc

    # wait_for raises TimeoutError (simulating process not responding to SIGTERM)
    with patch("backend.services.instance_manager.asyncio.wait_for", side_effect=asyncio.TimeoutError):
        result = await im.stop(inst_id)

    assert result is True
    mock_proc.terminate.assert_called_once()
    mock_proc.kill.assert_called_once()


@pytest.mark.asyncio
async def test_is_running():
    """is_running checks process returncode."""
    broadcaster = MagicMock()
    im = InstanceManager(MagicMock(), broadcaster)

    # No process
    assert im.is_running(1) is False

    # Process with returncode=None (still running)
    mock_proc = MagicMock()
    mock_proc.returncode = None
    im.processes[1] = mock_proc
    assert im.is_running(1) is True

    # Process with returncode=0 (finished)
    mock_proc.returncode = 0
    assert im.is_running(1) is False


@pytest.mark.asyncio
async def test_process_event_broadcasts_context_usage(db_factory):
    """_process_event broadcasts a separate context_usage event when present."""
    async with db_factory() as db:
        from backend.models.instance import Instance
        inst = Instance(name="ctx-inst")
        db.add(inst)
        await db.commit()
        await db.refresh(inst)
        inst_id = inst.id

    broadcaster = MagicMock()
    broadcaster.broadcast = AsyncMock()
    im = InstanceManager(db_factory, broadcaster)

    usage = {
        "input_tokens": 10,
        "cache_read_input_tokens": 500,
        "cache_creation_input_tokens": 200,
        "output_tokens": 20,
        "total_input_tokens": 710,
        "context_window": 200000,
    }
    event = {
        "event_type": "message",
        "role": "assistant",
        "content": "Hello",
        "tool_name": None,
        "tool_input": None,
        "tool_output": None,
        "raw_json": "{}",
        "is_error": False,
        "timestamp": "2024-01-01T00:00:00",
        "context_usage": usage,
    }

    await im._process_event(inst_id, None, event)

    # Should have broadcast the main event + context_usage event
    calls = broadcaster.broadcast.call_args_list
    # context_usage event not broadcast when task_id is None (no task channel)
    # Verify main event was broadcast to instance channel
    instance_broadcasts = [c for c in calls if c[0][0] == f"instance:{inst_id}"]
    assert len(instance_broadcasts) >= 1
    # context_usage key should be stripped from main broadcast
    main_data = instance_broadcasts[0][0][1]
    assert "context_usage" not in main_data


@pytest.mark.asyncio
async def test_process_event_broadcasts_context_usage_to_task(db_factory):
    """_process_event broadcasts context_usage event to task channel when task_id set."""
    async with db_factory() as db:
        from backend.models.instance import Instance
        from backend.models.task import Task
        inst = Instance(name="ctx-task-inst")
        db.add(inst)
        await db.commit()
        await db.refresh(inst)
        inst_id = inst.id

        task = Task(title="ctx task")
        db.add(task)
        await db.commit()
        await db.refresh(task)
        task_id = task.id

    broadcaster = MagicMock()
    broadcaster.broadcast = AsyncMock()
    im = InstanceManager(db_factory, broadcaster)

    usage = {
        "input_tokens": 5,
        "cache_read_input_tokens": 100,
        "cache_creation_input_tokens": 50,
        "output_tokens": 10,
        "total_input_tokens": 155,
        "context_window": 1000000,
    }
    event = {
        "event_type": "message",
        "role": "assistant",
        "content": "Hello",
        "tool_name": None,
        "tool_input": None,
        "tool_output": None,
        "raw_json": "{}",
        "is_error": False,
        "timestamp": "2024-01-01T00:00:00",
        "context_usage": usage,
    }

    await im._process_event(inst_id, task_id, event)

    calls = broadcaster.broadcast.call_args_list
    # Find context_usage broadcast to task channel
    ctx_calls = [
        c for c in calls
        if c[0][0] == f"task:{task_id}" and c[0][1].get("event_type") == "context_usage"
    ]
    assert len(ctx_calls) == 1
    ctx_data = ctx_calls[0][0][1]
    assert ctx_data["total_input_tokens"] == 155
    assert ctx_data["context_window"] == 1000000
    assert ctx_data["input_tokens"] == 5


@pytest.mark.asyncio
async def test_process_event_sets_has_unread_on_assistant_message(db_factory):
    """_process_event sets has_unread=True on task when assistant message event arrives."""
    async with db_factory() as db:
        inst = Instance(name="unread-inst")
        db.add(inst)
        task = Task(title="unread task", description="d")
        db.add(task)
        await db.commit()
        await db.refresh(inst)
        await db.refresh(task)
        inst_id = inst.id
        task_id = task.id

    broadcaster = MagicMock()
    broadcaster.broadcast = AsyncMock()
    im = InstanceManager(db_factory, broadcaster)

    event = {
        "event_type": "message",
        "role": "assistant",
        "content": "Here is my response",
        "tool_name": None,
        "tool_input": None,
        "tool_output": None,
        "raw_json": "{}",
        "is_error": False,
    }

    await im._process_event(inst_id, task_id, event)

    async with db_factory() as db:
        task = await db.get(Task, task_id)
    assert task.has_unread is True


@pytest.mark.asyncio
async def test_process_event_keeps_short_complete_codex_reply(db_factory):
    """Codex answers like 'OK' are final items, not Claude stream fragments."""
    from sqlalchemy import func, select
    from backend.models.log_entry import LogEntry

    async with db_factory() as db:
        inst = Instance(name="short-codex-reply-inst")
        db.add(inst)
        await db.commit()
        await db.refresh(inst)
        inst_id = inst.id

    broadcaster = MagicMock()
    broadcaster.broadcast = AsyncMock()
    im = InstanceManager(db_factory, broadcaster)
    raw = json.dumps({
        "type": "item.completed",
        "item": {"id": "msg-1", "type": "agent_message", "text": "OK"},
    })
    event = im._parse_codex_line(raw)

    await im._process_event(inst_id, None, event)

    async with db_factory() as db:
        count = await db.scalar(
            select(func.count()).select_from(LogEntry).where(
                LogEntry.instance_id == inst_id,
                LogEntry.content == "OK",
            )
        )
    assert count == 1


@pytest.mark.asyncio
async def test_process_event_sets_has_unread_on_result(db_factory):
    """_process_event sets has_unread=True on task when result event arrives."""
    async with db_factory() as db:
        inst = Instance(name="unread-result-inst")
        db.add(inst)
        task = Task(title="result task", description="d")
        db.add(task)
        await db.commit()
        await db.refresh(inst)
        await db.refresh(task)
        inst_id = inst.id
        task_id = task.id

    broadcaster = MagicMock()
    broadcaster.broadcast = AsyncMock()
    im = InstanceManager(db_factory, broadcaster)

    event = {
        "event_type": "result",
        "role": "assistant",
        "content": "Task completed",
        "tool_name": None,
        "tool_input": None,
        "tool_output": None,
        "raw_json": "{}",
        "is_error": False,
    }

    await im._process_event(inst_id, task_id, event)

    async with db_factory() as db:
        task = await db.get(Task, task_id)
    assert task.has_unread is True


@pytest.mark.asyncio
async def test_process_event_does_not_set_has_unread_for_user_message(db_factory):
    """_process_event does NOT set has_unread for user role messages."""
    async with db_factory() as db:
        inst = Instance(name="user-msg-inst")
        db.add(inst)
        task = Task(title="user msg task", description="d")
        db.add(task)
        await db.commit()
        await db.refresh(inst)
        await db.refresh(task)
        inst_id = inst.id
        task_id = task.id

    broadcaster = MagicMock()
    broadcaster.broadcast = AsyncMock()
    im = InstanceManager(db_factory, broadcaster)

    event = {
        "event_type": "message",
        "role": "user",
        "content": "User says hello",
        "tool_name": None,
        "tool_input": None,
        "tool_output": None,
        "raw_json": "{}",
        "is_error": False,
    }

    await im._process_event(inst_id, task_id, event)

    async with db_factory() as db:
        task = await db.get(Task, task_id)
    assert task.has_unread is False


@pytest.mark.asyncio
async def test_process_event_does_not_set_has_unread_for_tool_use(db_factory):
    """_process_event does NOT set has_unread for tool_use events."""
    async with db_factory() as db:
        inst = Instance(name="tool-inst")
        db.add(inst)
        task = Task(title="tool task", description="d")
        db.add(task)
        await db.commit()
        await db.refresh(inst)
        await db.refresh(task)
        inst_id = inst.id
        task_id = task.id

    broadcaster = MagicMock()
    broadcaster.broadcast = AsyncMock()
    im = InstanceManager(db_factory, broadcaster)

    event = {
        "event_type": "tool_use",
        "role": "assistant",
        "content": None,
        "tool_name": "Bash",
        "tool_input": '{"command": "ls"}',
        "tool_output": None,
        "raw_json": "{}",
        "is_error": False,
    }

    await im._process_event(inst_id, task_id, event)

    async with db_factory() as db:
        task = await db.get(Task, task_id)
    assert task.has_unread is False


# === loop_iteration broadcast tests ===


@pytest.mark.asyncio
async def test_process_event_broadcasts_loop_iteration(db_factory):
    """_process_event includes loop_iteration in broadcast data when provided."""
    async with db_factory() as db:
        inst = Instance(name="loop-iter-inst")
        db.add(inst)
        task = Task(title="loop task", description="d")
        db.add(task)
        await db.commit()
        await db.refresh(inst)
        await db.refresh(task)
        inst_id = inst.id
        task_id = task.id

    broadcaster = MagicMock()
    broadcaster.broadcast = AsyncMock()
    im = InstanceManager(db_factory, broadcaster)

    event = {
        "event_type": "message",
        "role": "assistant",
        "content": "Working on item 3",
        "tool_name": None,
        "tool_input": None,
        "tool_output": None,
        "raw_json": "{}",
        "is_error": False,
    }

    await im._process_event(inst_id, task_id, event, loop_iteration=2)

    calls = broadcaster.broadcast.call_args_list
    task_broadcasts = [c for c in calls if c[0][0] == f"task:{task_id}"]
    assert len(task_broadcasts) >= 1
    broadcast_data = task_broadcasts[0][0][1]
    assert broadcast_data["loop_iteration"] == 2


@pytest.mark.asyncio
async def test_process_event_omits_loop_iteration_when_none(db_factory):
    """_process_event does not add loop_iteration to broadcast when it is None."""
    async with db_factory() as db:
        inst = Instance(name="no-loop-inst")
        db.add(inst)
        task = Task(title="auto task", description="d")
        db.add(task)
        await db.commit()
        await db.refresh(inst)
        await db.refresh(task)
        inst_id = inst.id
        task_id = task.id

    broadcaster = MagicMock()
    broadcaster.broadcast = AsyncMock()
    im = InstanceManager(db_factory, broadcaster)

    event = {
        "event_type": "message",
        "role": "assistant",
        "content": "Hello",
        "tool_name": None,
        "tool_input": None,
        "tool_output": None,
        "raw_json": "{}",
        "is_error": False,
    }

    await im._process_event(inst_id, task_id, event)

    calls = broadcaster.broadcast.call_args_list
    task_broadcasts = [c for c in calls if c[0][0] == f"task:{task_id}"]
    assert len(task_broadcasts) >= 1
    broadcast_data = task_broadcasts[0][0][1]
    assert "loop_iteration" not in broadcast_data


@pytest.mark.asyncio
async def test_process_event_broadcasts_loop_iteration_zero(db_factory):
    """_process_event includes loop_iteration=0 in broadcast (first iteration)."""
    async with db_factory() as db:
        inst = Instance(name="loop-zero-inst")
        db.add(inst)
        task = Task(title="loop task zero", description="d")
        db.add(task)
        await db.commit()
        await db.refresh(inst)
        await db.refresh(task)
        inst_id = inst.id
        task_id = task.id

    broadcaster = MagicMock()
    broadcaster.broadcast = AsyncMock()
    im = InstanceManager(db_factory, broadcaster)

    event = {
        "event_type": "tool_use",
        "role": "assistant",
        "content": None,
        "tool_name": "Read",
        "tool_input": '{"file_path": "TODO.md"}',
        "tool_output": None,
        "raw_json": "{}",
        "is_error": False,
    }

    await im._process_event(inst_id, task_id, event, loop_iteration=0)

    calls = broadcaster.broadcast.call_args_list
    task_broadcasts = [c for c in calls if c[0][0] == f"task:{task_id}"]
    assert len(task_broadcasts) >= 1
    broadcast_data = task_broadcasts[0][0][1]
    assert broadcast_data["loop_iteration"] == 0


# === chat_initiated flag tests ===


@pytest.mark.asyncio
async def test_successful_codex_consumer_checks_quota_for_every_turn(db_factory):
    async with db_factory() as db:
        inst = Instance(name="codex-quota-consumer")
        task = Task(
            title="codex quota consumer",
            status="executing",
            provider="codex",
            session_id="thread-consumer",
        )
        db.add_all([inst, task])
        await db.commit()
        await db.refresh(inst)
        await db.refresh(task)

    process = _make_mock_process(returncode=0)
    im = InstanceManager(db_factory, MagicMock(broadcast=AsyncMock()))
    im.processes[inst.id] = process
    im._try_proactive_pool_switch = AsyncMock(return_value=False)

    await im._consume_output(
        inst.id,
        task.id,
        process,
        chat_initiated=False,
        provider="codex",
    )

    im._try_proactive_pool_switch.assert_awaited_once_with(
        inst.id,
        task.id,
        rate_limit_info=None,
    )


@pytest.mark.asyncio
async def test_consume_output_chat_initiated_restores_task_status(db_factory):
    """When chat_initiated=True, consumer marks task as completed on process exit."""
    async with db_factory() as db:
        inst = Instance(name="chat-init-inst")
        db.add(inst)
        task = Task(title="chat task", description="d", status="executing")
        db.add(task)
        await db.commit()
        await db.refresh(inst)
        await db.refresh(task)
        inst_id = inst.id
        task_id = task.id

    mock_proc = _make_mock_process(returncode=0)
    broadcaster = MagicMock()
    broadcaster.broadcast = AsyncMock()
    im = InstanceManager(db_factory, broadcaster)
    im.processes[inst_id] = mock_proc

    await im._consume_output(inst_id, task_id, mock_proc, chat_initiated=True)

    async with db_factory() as db:
        task = await db.get(Task, task_id)
    assert task.status == "completed"


@pytest.mark.asyncio
async def test_consume_output_dispatcher_does_not_restore_task_status(db_factory):
    """When chat_initiated=False (dispatcher), consumer does NOT mark task completed."""
    async with db_factory() as db:
        inst = Instance(name="dispatch-inst")
        db.add(inst)
        task = Task(title="dispatch task", description="d", status="executing")
        db.add(task)
        await db.commit()
        await db.refresh(inst)
        await db.refresh(task)
        inst_id = inst.id
        task_id = task.id

    mock_proc = _make_mock_process(returncode=0)
    broadcaster = MagicMock()
    broadcaster.broadcast = AsyncMock()
    im = InstanceManager(db_factory, broadcaster)
    im.processes[inst_id] = mock_proc

    await im._consume_output(inst_id, task_id, mock_proc, chat_initiated=False)

    async with db_factory() as db:
        task = await db.get(Task, task_id)
    assert task.status == "executing"


@pytest.mark.asyncio
async def test_consume_output_default_does_not_restore_task_status(db_factory):
    """Default launch (no chat_initiated) does NOT mark task completed — same as dispatcher."""
    async with db_factory() as db:
        inst = Instance(name="default-inst")
        db.add(inst)
        task = Task(title="default task", description="d", status="executing")
        db.add(task)
        await db.commit()
        await db.refresh(inst)
        await db.refresh(task)
        inst_id = inst.id
        task_id = task.id

    mock_proc = _make_mock_process(returncode=0)
    broadcaster = MagicMock()
    broadcaster.broadcast = AsyncMock()
    im = InstanceManager(db_factory, broadcaster)
    im.processes[inst_id] = mock_proc

    await im._consume_output(inst_id, task_id, mock_proc)

    async with db_factory() as db:
        task = await db.get(Task, task_id)
    assert task.status == "executing"


@pytest.mark.asyncio
async def test_consume_output_chat_initiated_error_marks_failed(db_factory):
    """When chat_initiated=True and process exits with error, task is marked failed."""
    async with db_factory() as db:
        inst = Instance(name="chat-err-inst")
        db.add(inst)
        task = Task(title="chat error task", description="d", status="executing")
        db.add(task)
        await db.commit()
        await db.refresh(inst)
        await db.refresh(task)
        inst_id = inst.id
        task_id = task.id

    mock_proc = _make_mock_process(returncode=1)
    broadcaster = MagicMock()
    broadcaster.broadcast = AsyncMock()
    im = InstanceManager(db_factory, broadcaster)
    im.processes[inst_id] = mock_proc

    await im._consume_output(inst_id, task_id, mock_proc, chat_initiated=True)

    async with db_factory() as db:
        task = await db.get(Task, task_id)
    assert task.status == "failed"
    assert task.error_message is not None


@pytest.mark.asyncio
async def test_consume_output_chat_initiated_interrupt_marks_completed(db_factory):
    """When chat_initiated=True and process is interrupted (SIGINT), task is marked completed."""
    async with db_factory() as db:
        inst = Instance(name="chat-int-inst")
        db.add(inst)
        task = Task(title="chat interrupt task", description="d", status="executing")
        db.add(task)
        await db.commit()
        await db.refresh(inst)
        await db.refresh(task)
        inst_id = inst.id
        task_id = task.id

    mock_proc = _make_mock_process(returncode=-2)  # SIGINT
    broadcaster = MagicMock()
    broadcaster.broadcast = AsyncMock()
    im = InstanceManager(db_factory, broadcaster)
    im.processes[inst_id] = mock_proc

    await im._consume_output(inst_id, task_id, mock_proc, chat_initiated=True)

    async with db_factory() as db:
        task = await db.get(Task, task_id)
    assert task.status == "completed"


@pytest.mark.asyncio
async def test_consume_output_chat_initiated_no_override_cancelled(db_factory):
    """Consumer does not override 'cancelled' status even for chat_initiated=True runs."""
    async with db_factory() as db:
        inst = Instance(name="chat-cancel-inst")
        db.add(inst)
        task = Task(title="cancelled chat task", description="d", status="cancelled")
        db.add(task)
        await db.commit()
        await db.refresh(inst)
        await db.refresh(task)
        inst_id = inst.id
        task_id = task.id

    mock_proc = _make_mock_process(returncode=0)
    broadcaster = MagicMock()
    broadcaster.broadcast = AsyncMock()
    im = InstanceManager(db_factory, broadcaster)
    im.processes[inst_id] = mock_proc

    await im._consume_output(inst_id, task_id, mock_proc, chat_initiated=True)

    async with db_factory() as db:
        task = await db.get(Task, task_id)
    assert task.status == "cancelled"


# === enable_workflows tests ===


def test_build_command_claude_enable_workflows_default():
    """_build_command defaults to enable_workflows=False, adding --disallowedTools Workflow."""
    im = InstanceManager(MagicMock(), MagicMock())
    cmd = im._build_command(provider="claude", prompt="hi", model=None, resume_session_id=None, effort_level=None)
    assert "--disallowedTools" in cmd
    idx = cmd.index("--disallowedTools")
    assert cmd[idx + 1] == "Workflow"


def test_build_command_claude_enable_workflows_true():
    """_build_command with enable_workflows=True does NOT include --disallowedTools."""
    im = InstanceManager(MagicMock(), MagicMock())
    cmd = im._build_command(provider="claude", prompt="hi", model=None, resume_session_id=None, effort_level=None, enable_workflows=True)
    assert "--disallowedTools" not in cmd


def test_build_command_claude_enable_workflows_false():
    """_build_command with enable_workflows=False includes --disallowedTools Workflow."""
    im = InstanceManager(MagicMock(), MagicMock())
    cmd = im._build_command(provider="claude", prompt="hi", model=None, resume_session_id=None, effort_level=None, enable_workflows=False)
    assert "--disallowedTools" in cmd
    idx = cmd.index("--disallowedTools")
    assert cmd[idx + 1] == "Workflow"


def test_build_command_codex_ignores_enable_workflows():
    """Codex provider does not include --disallowedTools regardless of enable_workflows."""
    im = InstanceManager(MagicMock(), MagicMock())
    cmd = im._build_command(provider="codex", prompt="hi", model=None, resume_session_id=None, effort_level=None, enable_workflows=False)
    assert "--disallowedTools" not in cmd


@pytest.mark.asyncio
async def test_launch_enable_workflows_false_includes_flag(db_factory):
    """launch(enable_workflows=False) generates command with --disallowedTools Workflow."""
    async with db_factory() as db:
        inst = Instance(name="wf-disabled-inst")
        db.add(inst)
        await db.commit()
        await db.refresh(inst)
        inst_id = inst.id

    mock_proc = _make_mock_process()
    broadcaster = MagicMock()
    broadcaster.broadcast = AsyncMock()
    im = InstanceManager(db_factory, broadcaster)

    with patch("backend.services.instance_manager.asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=mock_proc) as mock_exec:
        await im.launch(instance_id=inst_id, prompt="hi", cwd="/tmp", enable_workflows=False)

    cmd_args = mock_exec.call_args[0]
    assert "--disallowedTools" in cmd_args
    idx = cmd_args.index("--disallowedTools")
    assert cmd_args[idx + 1] == "Workflow"
    await asyncio.sleep(0.1)


@pytest.mark.asyncio
async def test_launch_enable_workflows_true_omits_flag(db_factory):
    """launch(enable_workflows=True) generates command without --disallowedTools."""
    async with db_factory() as db:
        inst = Instance(name="wf-enabled-inst")
        db.add(inst)
        await db.commit()
        await db.refresh(inst)
        inst_id = inst.id

    mock_proc = _make_mock_process()
    broadcaster = MagicMock()
    broadcaster.broadcast = AsyncMock()
    im = InstanceManager(db_factory, broadcaster)

    with patch("backend.services.instance_manager.asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=mock_proc) as mock_exec:
        await im.launch(instance_id=inst_id, prompt="hi", cwd="/tmp", enable_workflows=True)

    cmd_args = mock_exec.call_args[0]
    assert "--disallowedTools" not in cmd_args
    await asyncio.sleep(0.1)


@pytest.mark.asyncio
async def test_launch_default_disables_workflows(db_factory):
    """launch() without explicit enable_workflows defaults to False (workflows disabled)."""
    async with db_factory() as db:
        inst = Instance(name="wf-default-inst")
        db.add(inst)
        await db.commit()
        await db.refresh(inst)
        inst_id = inst.id

    mock_proc = _make_mock_process()
    broadcaster = MagicMock()
    broadcaster.broadcast = AsyncMock()
    im = InstanceManager(db_factory, broadcaster)

    with patch("backend.services.instance_manager.asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=mock_proc) as mock_exec:
        await im.launch(instance_id=inst_id, prompt="hi", cwd="/tmp")

    cmd_args = mock_exec.call_args[0]
    assert "--disallowedTools" in cmd_args
    await asyncio.sleep(0.1)


@pytest.mark.asyncio
async def test_launch_chat_initiated_stores_enable_workflows_in_params(db_factory):
    """chat_initiated launch stores enable_workflows in _launch_params for pool rotation."""
    async with db_factory() as db:
        inst = Instance(name="params-wf-inst")
        db.add(inst)
        task = Task(title="params task", description="d")
        db.add(task)
        await db.commit()
        await db.refresh(inst)
        await db.refresh(task)
        inst_id = inst.id
        task_id = task.id

    mock_proc = _make_mock_process()
    broadcaster = MagicMock()
    broadcaster.broadcast = AsyncMock()
    im = InstanceManager(db_factory, broadcaster)

    with patch("backend.services.instance_manager.asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=mock_proc):
        await im.launch(instance_id=inst_id, prompt="hi", task_id=task_id, cwd="/tmp", chat_initiated=True, enable_workflows=True)

    assert inst_id in im._launch_params
    assert im._launch_params[inst_id]["enable_workflows"] is True
    await asyncio.sleep(0.1)


@pytest.mark.asyncio
async def test_launch_chat_initiated_stores_enable_workflows_false_in_params(db_factory):
    """chat_initiated launch stores enable_workflows=False in _launch_params."""
    async with db_factory() as db:
        inst = Instance(name="params-wf-false-inst")
        db.add(inst)
        task = Task(title="params task", description="d")
        db.add(task)
        await db.commit()
        await db.refresh(inst)
        await db.refresh(task)
        inst_id = inst.id
        task_id = task.id

    mock_proc = _make_mock_process()
    broadcaster = MagicMock()
    broadcaster.broadcast = AsyncMock()
    im = InstanceManager(db_factory, broadcaster)

    with patch("backend.services.instance_manager.asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=mock_proc):
        await im.launch(instance_id=inst_id, prompt="hi", task_id=task_id, cwd="/tmp", chat_initiated=True, enable_workflows=False)

    assert im._launch_params[inst_id]["enable_workflows"] is False
    await asyncio.sleep(0.1)


@pytest.mark.asyncio
async def test_launch_non_chat_does_not_store_params(db_factory):
    """Non-chat launch does not store _launch_params."""
    async with db_factory() as db:
        inst = Instance(name="no-params-inst")
        db.add(inst)
        await db.commit()
        await db.refresh(inst)
        inst_id = inst.id

    mock_proc = _make_mock_process()
    broadcaster = MagicMock()
    broadcaster.broadcast = AsyncMock()
    im = InstanceManager(db_factory, broadcaster)

    with patch("backend.services.instance_manager.asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=mock_proc):
        await im.launch(instance_id=inst_id, prompt="hi", cwd="/tmp", enable_workflows=True)

    assert inst_id not in im._launch_params
    await asyncio.sleep(0.1)


# ---------- PTY mode wiring (use_pty_mode flag) ----------

class _FakeDB:
    def __init__(self):
        self.executed = []

    async def execute(self, stmt):
        self.executed.append(stmt)
        return MagicMock(rowcount=1)

    async def commit(self):
        pass

    async def get(self, model, pk):
        inst = MagicMock()
        inst.current_task_id = None
        return inst


class _FakeDBFactory:
    def __init__(self):
        self.db = _FakeDB()

    def __call__(self):
        return self

    async def __aenter__(self):
        return self.db

    async def __aexit__(self, *exc):
        return False


def test_pty_backend_disabled_by_default():
    im = InstanceManager(MagicMock(), MagicMock())
    assert im._pty_backend is None


@pytest.mark.asyncio
async def test_launch_delegates_to_pty_backend_for_claude():
    im = InstanceManager(_FakeDBFactory(), MagicMock())
    calls = {}

    class FakeBackend:
        async def launch_for_ccm(self, **kwargs):
            calls.update(kwargs)
            im.processes[kwargs["instance_id"]] = MagicMock(pid=4242)
            return "sess-1"

    im._pty_backend = FakeBackend()
    im._pty_enabled = True
    pid = await im.launch(
        instance_id=7, prompt="do it", task_id=3, cwd="/w",
        model="default", provider="claude",
    )
    assert pid == 4242
    assert calls["instance_id"] == 7
    assert calls["prompt"] == "do it"
    assert calls["model"] is None  # "default" normalized away
    assert calls["cwd"] == "/w"


@pytest.mark.asyncio
async def test_launch_pty_ignores_codex_provider():
    im = InstanceManager(_FakeDBFactory(), MagicMock())

    class ExplodingBackend:
        async def launch_for_ccm(self, **kwargs):
            raise AssertionError("PTY backend must not be used for codex")

    im._pty_backend = ExplodingBackend()
    fake_proc = MagicMock(pid=1)
    fake_proc.stdout = None
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=fake_proc)):
        with patch.object(im, "_consume_output", new=AsyncMock()):
            await im.launch(
                instance_id=1, prompt="x", provider="codex", cwd="/w",
            )
    assert im.processes[1] is fake_proc


@pytest.mark.asyncio
async def test_stop_uses_pty_backend_for_managed_instance():
    from claude_pty.adapters.ccm import _PTYProcessProxy

    im = InstanceManager(_FakeDBFactory(), MagicMock())
    im.broadcaster.broadcast = AsyncMock()
    proxy = _PTYProcessProxy()
    im.processes[5] = proxy
    stopped = []

    class FakeBackend:
        _sessions = {5: object()}

        async def stop(self, instance_id):
            stopped.append(instance_id)
            proxy.complete(0)

    im._pty_backend = FakeBackend()
    ok = await im.stop(5)
    assert ok is True
    assert stopped == [5]
    assert 5 not in im.processes


# ---------- runtime PTY mode toggle ----------

def test_set_pty_mode_runtime_toggle():
    im = InstanceManager(MagicMock(), MagicMock())
    assert im.pty_mode_enabled is False

    # enable: lazy-creates backend (claude_pty installed in dev venv)
    assert im.set_pty_mode(True) is True
    assert im.pty_mode_enabled is True
    assert im._pty_backend is not None
    backend = im._pty_backend

    # disable: flag off, backend retained for in-flight sessions
    assert im.set_pty_mode(False) is False
    assert im.pty_mode_enabled is False
    assert im._pty_backend is backend

    # re-enable reuses the same backend
    assert im.set_pty_mode(True) is True
    assert im._pty_backend is backend


@pytest.mark.asyncio
async def test_launch_respects_disabled_pty_mode():
    """With a backend present but mode disabled, claude goes through -p."""
    im = InstanceManager(_FakeDBFactory(), MagicMock())

    class ExplodingBackend:
        async def launch_for_ccm(self, **kwargs):
            raise AssertionError("PTY backend must not be used when disabled")

    im._pty_backend = ExplodingBackend()
    im._pty_enabled = False  # toggled off at runtime

    fake_proc = MagicMock(pid=1)
    fake_proc.stdout = None
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=fake_proc)):
        with patch.object(im, "_consume_output", new=AsyncMock()):
            await im.launch(instance_id=1, prompt="x", provider="claude", cwd="/w")
    assert im.processes[1] is fake_proc


@pytest.mark.asyncio
async def test_release_pty_session():
    im = InstanceManager(MagicMock(), MagicMock())
    # no backend -> no-op
    await im.release_pty_session("sid-x")

    class FakePool:
        removed = []

        async def remove(self, sid):
            FakePool.removed.append(sid)

    class FakeBackend:
        _pool = FakePool()

    im._pty_backend = FakeBackend()
    await im.release_pty_session("sid-x")
    assert FakePool.removed == ["sid-x"]
    await im.release_pty_session("")  # empty -> no-op
    assert FakePool.removed == ["sid-x"]


# ---------------------------------------------------------------------------
# Transient-overload turn-scoped flag (auto wait + retry on transient 429)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_process_event_sets_transient_flag_on_overload_error(db_factory):
    """An is_error event with the transient-429 wording flips the turn flag."""
    async with db_factory() as db:
        inst = Instance(name="transient-inst")
        db.add(inst)
        task = Task(title="t", description="d")
        db.add(task)
        await db.commit()
        await db.refresh(inst)
        await db.refresh(task)
        inst_id, task_id = inst.id, task.id

    broadcaster = MagicMock()
    broadcaster.broadcast = AsyncMock()
    im = InstanceManager(db_factory, broadcaster)

    assert im.transient_error_seen(inst_id) is False
    await im._process_event(inst_id, task_id, {
        "event_type": "result",
        "role": "assistant",
        "content": ("API Error: Server is temporarily limiting requests "
                    "(not your usage limit) · Rate limited"),
        "is_error": True,
        "raw_json": "{}",
    })
    assert im.transient_error_seen(inst_id) is True


@pytest.mark.asyncio
async def test_process_event_usage_limit_does_not_set_transient_flag(db_factory):
    """A genuine usage-limit banner must rotate, not set the transient flag."""
    async with db_factory() as db:
        inst = Instance(name="usage-inst")
        db.add(inst)
        task = Task(title="t", description="d")
        db.add(task)
        await db.commit()
        await db.refresh(inst)
        await db.refresh(task)
        inst_id, task_id = inst.id, task.id

    broadcaster = MagicMock()
    broadcaster.broadcast = AsyncMock()
    im = InstanceManager(db_factory, broadcaster)

    await im._process_event(inst_id, task_id, {
        "event_type": "result",
        "role": "assistant",
        "content": "You've hit your limit · resets 5pm (UTC)",
        "is_error": True,
        "raw_json": "{}",
    })
    assert im.transient_error_seen(inst_id) is False


@pytest.mark.asyncio
async def test_process_event_codex_usage_text_never_sets_claude_pty_limit_flag(
    db_factory,
):
    """Codex usage wording overlaps Claude regex but Codex is never PTY-managed."""
    async with db_factory() as db:
        inst = Instance(name="codex-usage-inst")
        task = Task(title="t", description="d", provider="codex")
        db.add_all([inst, task])
        await db.commit()
        await db.refresh(inst)
        await db.refresh(task)
        inst_id, task_id = inst.id, task.id

    broadcaster = MagicMock()
    broadcaster.broadcast = AsyncMock()
    im = InstanceManager(db_factory, broadcaster)
    im._launch_params[inst_id] = {"provider": "codex"}

    await im._process_event(inst_id, task_id, {
        "event_type": "result",
        "role": "assistant",
        "content": "You've hit your usage limit for Codex",
        "is_error": True,
        "raw_json": "{}",
    })

    assert im.transient_error_seen(inst_id) is False
    assert im.pty_rate_limit_seen(inst_id) is False


@pytest.mark.asyncio
async def test_process_event_clean_event_leaves_flag_unset(db_factory):
    async with db_factory() as db:
        inst = Instance(name="clean-inst")
        db.add(inst)
        task = Task(title="t", description="d")
        db.add(task)
        await db.commit()
        await db.refresh(inst)
        await db.refresh(task)
        inst_id, task_id = inst.id, task.id

    broadcaster = MagicMock()
    broadcaster.broadcast = AsyncMock()
    im = InstanceManager(db_factory, broadcaster)

    await im._process_event(inst_id, task_id, {
        "event_type": "message",
        "role": "assistant",
        "content": "All done, tests pass.",
        "is_error": False,
        "raw_json": "{}",
    })
    assert im.transient_error_seen(inst_id) is False


@pytest.mark.asyncio
async def test_process_event_orphan_overload_does_not_set_transient_flag(db_factory):
    """A REPLAYED transient-429 error (orphan / autonomous) must NOT re-flag.

    On resume PTY re-reads the JSONL and yields the previous turn's own
    api_error as an `orphan` event. If that re-set the turn flag,
    transient_error_seen() would stay True across a clean resume, so the host
    keeps "retrying" a turn that already succeeded and finally marks the task
    failed (the recover-then-failed bug). Only the CURRENT turn's live events
    count.
    """
    async with db_factory() as db:
        inst = Instance(name="orphan-inst")
        db.add(inst)
        task = Task(title="t", description="d")
        db.add(task)
        await db.commit()
        await db.refresh(inst)
        await db.refresh(task)
        inst_id, task_id = inst.id, task.id

    broadcaster = MagicMock()
    broadcaster.broadcast = AsyncMock()
    im = InstanceManager(db_factory, broadcaster)

    overload = ("API Error: Server is temporarily limiting requests "
                "(not your usage limit) · Rate limited")

    # Stale backlog from the previous turn, replayed on resume → must be ignored.
    await im._process_event(inst_id, task_id, {
        "event_type": "result",
        "role": "assistant",
        "content": overload,
        "is_error": True,
        "orphan": True,
        "raw_json": "{}",
    })
    assert im.transient_error_seen(inst_id) is False

    # A background sub-agent turn's error is likewise not this turn's signal.
    await im._process_event(inst_id, task_id, {
        "event_type": "result",
        "role": "assistant",
        "content": overload,
        "is_error": True,
        "autonomous": True,
        "raw_json": "{}",
    })
    assert im.transient_error_seen(inst_id) is False


@pytest.mark.asyncio
async def test_launch_resets_transient_flag(db_factory):
    """A new launch() clears the previous turn's transient flag."""
    async with db_factory() as db:
        inst = Instance(name="reset-inst")
        db.add(inst)
        await db.commit()
        await db.refresh(inst)
        inst_id = inst.id

    mock_proc = _make_mock_process()
    broadcaster = MagicMock()
    broadcaster.broadcast = AsyncMock()
    im = InstanceManager(db_factory, broadcaster)
    im._transient_seen.add(inst_id)  # pretend prior turn hit overload

    with patch("backend.services.instance_manager.asyncio.create_subprocess_exec",
               new_callable=AsyncMock, return_value=mock_proc):
        await im.launch(instance_id=inst_id, prompt="hi", cwd="/tmp")

    assert im.transient_error_seen(inst_id) is False
    await asyncio.sleep(0.1)


# === Reactivation guard (completed → executing) ===
# 复活块只认前台 turn 的活事件：orphan（PTY resume 回放）和 autonomous
# （后台子 agent turn）没有收尾路径，翻回 executing 后没人再标回 completed。


def _status_change_payloads(broadcaster):
    return [
        c.args[1]
        for c in broadcaster.broadcast.await_args_list
        if len(c.args) > 1 and isinstance(c.args[1], dict)
        and c.args[1].get("event") == "status_change"
    ]


async def _make_completed_task(db_factory, name):
    async with db_factory() as db:
        inst = Instance(name=name)
        task = Task(description="reactivation test", status="completed")
        db.add_all([inst, task])
        await db.commit()
        await db.refresh(inst)
        await db.refresh(task)
        return inst.id, task.id


@pytest.mark.asyncio
async def test_process_event_reactivates_completed_task(db_factory):
    """Foreground assistant output flips a completed task back to executing."""
    inst_id, task_id = await _make_completed_task(db_factory, "react-fg")
    broadcaster = MagicMock()
    broadcaster.broadcast = AsyncMock()
    im = InstanceManager(db_factory, broadcaster)

    await im._process_event(inst_id, task_id, {
        "event_type": "message",
        "role": "assistant",
        "content": "still working on the follow-up",
    })

    async with db_factory() as db:
        t = await db.get(Task, task_id)
        assert t.status == "executing"
    assert any(p.get("new_status") == "executing" for p in _status_change_payloads(broadcaster))


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", ["orphan", "autonomous"])
async def test_process_event_no_reactivate_on_stale_events(db_factory, flag):
    """orphan/autonomous events must NOT flip completed back to executing."""
    inst_id, task_id = await _make_completed_task(db_factory, f"react-{flag}")
    broadcaster = MagicMock()
    broadcaster.broadcast = AsyncMock()
    im = InstanceManager(db_factory, broadcaster)

    await im._process_event(inst_id, task_id, {
        "event_type": "message",
        "role": "assistant",
        "content": "replayed / background sub-agent output",
        flag: True,
    })

    async with db_factory() as db:
        t = await db.get(Task, task_id)
        assert t.status == "completed"
    assert _status_change_payloads(broadcaster) == []


# === GPT-5.6 per-model effort in codex command ===


def test_build_command_codex_gpt56_passes_max_effort():
    # 旧代码把 max 一律丢弃（"codex 无 max"），但 gpt-5.6-sol 支持 max
    im = InstanceManager(MagicMock(), MagicMock())
    cmd = im._build_command(provider="codex", prompt="hi", model="gpt-5.6-sol", resume_session_id=None, effort_level="max")
    assert 'model_reasoning_effort="max"' in cmd


def test_build_command_codex_gpt56_passes_ultra_effort():
    im = InstanceManager(MagicMock(), MagicMock())
    cmd = im._build_command(provider="codex", prompt="hi", model="gpt-5.6-terra", resume_session_id=None, effort_level="ultra")
    assert 'model_reasoning_effort="ultra"' in cmd


def test_build_command_codex_old_model_clamps_max_to_xhigh():
    # gpt-5.5 不支持 max：夹到 xhigh 而不是静默丢弃
    im = InstanceManager(MagicMock(), MagicMock())
    cmd = im._build_command(provider="codex", prompt="hi", model="gpt-5.5", resume_session_id=None, effort_level="max")
    assert 'model_reasoning_effort="xhigh"' in cmd


def test_build_command_codex_luna_clamps_ultra_to_max():
    im = InstanceManager(MagicMock(), MagicMock())
    cmd = im._build_command(provider="codex", prompt="hi", model="gpt-5.6-luna", resume_session_id=None, effort_level="ultra")
    assert 'model_reasoning_effort="max"' in cmd


def test_build_command_codex_supported_effort_still_passed():
    im = InstanceManager(MagicMock(), MagicMock())
    cmd = im._build_command(provider="codex", prompt="hi", model="gpt-5.5", resume_session_id=None, effort_level="high")
    assert 'model_reasoning_effort="high"' in cmd


# ---------------------------------------------------------------------------
# Codex event parsing — reasoning / file_change / mcp_tool_call / web_search /
# todo_list / error item / turn.failed（字段名来自 codex-rs rust-v0.144.6
# exec/src/exec_events.rs 实证）
# ---------------------------------------------------------------------------

def test_parse_codex_reasoning_becomes_thinking():
    im = InstanceManager(MagicMock(), MagicMock())
    event = im._parse_codex_line(json.dumps({
        "type": "item.completed",
        "item": {"id": "item_1", "type": "reasoning", "text": "Let me check the tests first."},
    }))

    assert event["event_type"] == "thinking"
    assert event["role"] == "assistant"
    assert event["content"] == "Let me check the tests first."


def test_parse_codex_empty_reasoning_skipped():
    im = InstanceManager(MagicMock(), MagicMock())
    assert im._parse_codex_line(json.dumps({
        "type": "item.completed",
        "item": {"id": "item_1", "type": "reasoning", "text": ""},
    })) is None


def test_parse_codex_file_change():
    im = InstanceManager(MagicMock(), MagicMock())
    event = im._parse_codex_line(json.dumps({
        "type": "item.completed",
        "item": {
            "id": "item_2",
            "type": "file_change",
            "changes": [{"path": "src/app.py", "kind": "update"},
                        {"path": "src/new.py", "kind": "add"}],
            "status": "completed",
        },
    }))

    assert event["event_type"] == "tool_result"
    assert event["tool_name"] == "FileChange"
    assert "update src/app.py" in event["tool_output"]
    assert "add src/new.py" in event["tool_output"]
    assert event["is_error"] is False


def test_parse_codex_file_change_failed_is_error():
    im = InstanceManager(MagicMock(), MagicMock())
    event = im._parse_codex_line(json.dumps({
        "type": "item.completed",
        "item": {"id": "i", "type": "file_change", "changes": [], "status": "failed"},
    }))
    assert event["is_error"] is True


def test_parse_codex_mcp_tool_call_started_and_completed():
    im = InstanceManager(MagicMock(), MagicMock())
    started = im._parse_codex_line(json.dumps({
        "type": "item.started",
        "item": {"id": "i", "type": "mcp_tool_call", "server": "ccm", "tool": "create_monitor",
                 "arguments": {"interval": 60}, "status": "in_progress"},
    }))
    assert started["event_type"] == "tool_use"
    assert started["tool_name"] == "ccm.create_monitor"
    assert json.loads(started["tool_input"]) == {"interval": 60}

    completed = im._parse_codex_line(json.dumps({
        "type": "item.completed",
        "item": {"id": "i", "type": "mcp_tool_call", "server": "ccm", "tool": "create_monitor",
                 "result": {"ok": True}, "status": "completed"},
    }))
    assert completed["event_type"] == "tool_result"
    assert completed["tool_name"] == "ccm.create_monitor"
    assert json.loads(completed["tool_output"]) == {"ok": True}
    assert completed["is_error"] is False


def test_parse_codex_mcp_tool_call_failed():
    im = InstanceManager(MagicMock(), MagicMock())
    event = im._parse_codex_line(json.dumps({
        "type": "item.completed",
        "item": {"id": "i", "type": "mcp_tool_call", "server": "ccm", "tool": "x",
                 "error": {"message": "boom"}, "status": "failed"},
    }))
    assert event["event_type"] == "tool_result"
    assert event["is_error"] is True
    assert "boom" in event["tool_output"]


def test_parse_codex_web_search():
    im = InstanceManager(MagicMock(), MagicMock())
    started = im._parse_codex_line(json.dumps({
        "type": "item.started",
        "item": {"id": "i", "type": "web_search", "query": "fastapi websocket"},
    }))
    assert started["event_type"] == "tool_use"
    assert started["tool_name"] == "WebSearch"

    completed = im._parse_codex_line(json.dumps({
        "type": "item.completed",
        "item": {"id": "i", "type": "web_search", "query": "fastapi websocket"},
    }))
    assert completed["event_type"] == "tool_result"
    assert "fastapi websocket" in completed["tool_output"]


def test_parse_codex_todo_list():
    im = InstanceManager(MagicMock(), MagicMock())
    event = im._parse_codex_line(json.dumps({
        "type": "item.updated",
        "item": {"id": "i", "type": "todo_list",
                 "items": [{"text": "write tests", "completed": True},
                           {"text": "run tests", "completed": False}]},
    }))
    assert event["event_type"] == "system_event"
    assert "✓ write tests" in event["content"]
    assert "○ run tests" in event["content"]


def test_parse_codex_error_item():
    im = InstanceManager(MagicMock(), MagicMock())
    event = im._parse_codex_line(json.dumps({
        "type": "item.completed",
        "item": {"id": "i", "type": "error", "message": "non-fatal oops"},
    }))
    assert event["event_type"] == "system_event"
    assert event["is_error"] is True
    assert event["content"] == "non-fatal oops"


def test_parse_codex_turn_failed_extracts_nested_message():
    # 实测形状（codex exec --json 认证失败捕获）：
    # {"type":"turn.failed","error":{"message":"..."}}
    im = InstanceManager(MagicMock(), MagicMock())
    event = im._parse_codex_line(json.dumps({
        "type": "turn.failed",
        "error": {"message": "stream disconnected before completion: transport error"},
    }))
    assert event["event_type"] == "system_event"
    assert event["is_error"] is True
    assert event["content"] == "stream disconnected before completion: transport error"


def test_parse_codex_file_change_started_is_tool_use():
    # 实测（CLI 0.144.6）file_change 也发 item.started——不映射会退化成
    # 一条 "in_progress" 噪音 system_event
    im = InstanceManager(MagicMock(), MagicMock())
    event = im._parse_codex_line(json.dumps({
        "type": "item.started",
        "item": {"id": "i", "type": "file_change",
                 "changes": [{"path": "probe.txt", "kind": "add"}],
                 "status": "in_progress"},
    }))
    assert event["event_type"] == "tool_use"
    assert event["tool_name"] == "FileChange"
    assert "probe.txt" in event["tool_input"]


@pytest.mark.asyncio
async def test_process_event_codex_window_backfill(db_factory):
    """codex 任务的 usage 不带 context_window → 按 codex 窗口表回填
    （gpt-5.6-terra = 272K，而不是 claude 的 200K 默认）。"""
    async with db_factory() as db:
        from backend.models.instance import Instance
        from backend.models.task import Task
        inst = Instance(name="codex-ctx-inst")
        db.add(inst)
        await db.commit()
        await db.refresh(inst)
        inst_id = inst.id

        task = Task(title="codex ctx", provider="codex", model="gpt-5.6-terra")
        db.add(task)
        await db.commit()
        await db.refresh(task)
        task_id = task.id

    broadcaster = MagicMock()
    broadcaster.broadcast = AsyncMock()
    im = InstanceManager(db_factory, broadcaster)

    event = {
        "event_type": "system_event",
        "role": None,
        "content": "turn.completed",
        "tool_name": None,
        "tool_input": None,
        "tool_output": None,
        "raw_json": "{}",
        "is_error": False,
        "timestamp": "2024-01-01T00:00:00",
        "context_usage": {
            "input_tokens": 5000,
            "cache_read_input_tokens": 30000,
            "cache_creation_input_tokens": 0,
            "output_tokens": 100,
            "total_input_tokens": 35000,
        },
    }
    await im._process_event(inst_id, task_id, event)

    ctx_calls = [
        c for c in broadcaster.broadcast.call_args_list
        if c[0][0] == f"task:{task_id}" and c[0][1].get("event_type") == "context_usage"
    ]
    assert len(ctx_calls) == 1
    assert ctx_calls[0][0][1]["context_window"] == 272_000

    # 落库的 usage 也带正确窗口（dispatcher 压缩阈值读的就是它）
    async with db_factory() as db:
        from backend.models.task import Task
        t = await db.get(Task, task_id)
        assert t.context_window_usage["context_window"] == 272_000
