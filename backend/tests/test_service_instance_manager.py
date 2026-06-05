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
