"""Tests for InstanceManager — subprocess lifecycle management."""
import asyncio
import json
import os
import signal
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from backend.services.instance_manager import InstanceManager
from backend.models.instance import Instance
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
async def test_launch_codex_provider_command(db_factory):
    """launch(provider='codex') constructs codex exec command."""
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

    with patch("backend.services.instance_manager.asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=mock_proc) as mock_exec:
        await im.launch(instance_id=inst_id, prompt="do stuff", cwd="/tmp", provider="codex")

    cmd_args = mock_exec.call_args[0]
    assert cmd_args[1] == "exec"
    assert "--json" in cmd_args
    assert "--dangerously-bypass-approvals-and-sandbox" in cmd_args
    assert "do stuff" in cmd_args
    # Should NOT have Claude-specific flags
    assert "--output-format" not in cmd_args
    assert "--verbose" not in cmd_args
    await asyncio.sleep(0.1)


@pytest.mark.asyncio
async def test_launch_codex_no_thinking_budget_env(db_factory):
    """launch(provider='codex', thinking_budget=N) does NOT set MAX_THINKING_TOKENS."""
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
