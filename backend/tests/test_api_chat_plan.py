"""Tests for Chat and Plan API endpoints."""
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.task import Task
from backend.models.instance import Instance


# === Chat tests ===


@pytest.mark.asyncio
async def test_chat_history_not_found(client):
    resp = await client.get("/api/tasks/9999/chat/history")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_chat_history_empty(client):
    create_resp = await client.post("/api/tasks", json={
        "title": "T", "description": "d", "target_repo": "/tmp",
    })
    task_id = create_resp.json()["id"]
    resp = await client.get(f"/api/tasks/{task_id}/chat/history")
    assert resp.status_code == 200
    assert resp.json() == []


async def _create_task_with_tools(client, session_factory):
    """Helper: create task + insert tool_use/tool_result log entries."""
    from backend.models.log_entry import LogEntry
    create_resp = await client.post("/api/tasks", json={
        "title": "T", "description": "d", "target_repo": "/tmp",
    })
    task_id = create_resp.json()["id"]
    async with session_factory() as db:
        db.add(LogEntry(
            instance_id=1, task_id=task_id, event_type="tool_use",
            role="assistant", tool_name="Edit",
            tool_input='{"file_path": "/tmp/test.py", "old_string": "foo", "new_string": "bar"}',
            is_error=False,
        ))
        db.add(LogEntry(
            instance_id=1, task_id=task_id, event_type="tool_result",
            role="assistant", tool_name="Edit",
            tool_output="File updated successfully",
            is_error=False,
        ))
        await db.commit()
    return task_id


@pytest.mark.asyncio
async def test_chat_history_compact_returns_summary(client, session_factory):
    """Default compact mode: tool_input is a summary, tool_output is null."""
    task_id = await _create_task_with_tools(client, session_factory)
    resp = await client.get(f"/api/tasks/{task_id}/chat/history")
    assert resp.status_code == 200
    msgs = resp.json()
    assert len(msgs) == 2

    # tool_use: summary extracted from file_path
    assert msgs[0]["event_type"] == "tool_use"
    assert msgs[0]["tool_name"] == "Edit"
    assert msgs[0]["tool_input"] == "/tmp/test.py"

    # tool_result: output stripped in compact mode
    assert msgs[1]["event_type"] == "tool_result"
    assert msgs[1]["tool_output"] is None


@pytest.mark.asyncio
async def test_chat_history_full_returns_tool_fields(client, session_factory):
    """compact=false: full tool_input/tool_output returned."""
    task_id = await _create_task_with_tools(client, session_factory)
    resp = await client.get(f"/api/tasks/{task_id}/chat/history?compact=false")
    assert resp.status_code == 200
    msgs = resp.json()
    assert len(msgs) == 2

    assert msgs[0]["event_type"] == "tool_use"
    assert msgs[0]["tool_input"] is not None
    assert "file_path" in msgs[0]["tool_input"]

    assert msgs[1]["event_type"] == "tool_result"
    assert msgs[1]["tool_output"] == "File updated successfully"


@pytest.mark.asyncio
async def test_message_detail_endpoint(client, session_factory):
    """Detail endpoint returns full tool_input/tool_output for a single message."""
    task_id = await _create_task_with_tools(client, session_factory)

    # Get compact history first to find message ids
    resp = await client.get(f"/api/tasks/{task_id}/chat/history")
    msgs = resp.json()
    tool_use_id = msgs[0]["id"]
    tool_result_id = msgs[1]["id"]

    # Fetch detail for tool_use
    resp = await client.get(f"/api/tasks/{task_id}/chat/{tool_use_id}/detail")
    assert resp.status_code == 200
    detail = resp.json()
    assert "file_path" in detail["tool_input"]

    # Fetch detail for tool_result
    resp = await client.get(f"/api/tasks/{task_id}/chat/{tool_result_id}/detail")
    assert resp.status_code == 200
    detail = resp.json()
    assert detail["tool_output"] == "File updated successfully"


@pytest.mark.asyncio
async def test_message_detail_not_found(client):
    """Detail for nonexistent message returns 404."""
    create_resp = await client.post("/api/tasks", json={
        "title": "T", "description": "d", "target_repo": "/tmp",
    })
    task_id = create_resp.json()["id"]
    resp = await client.get(f"/api/tasks/{task_id}/chat/99999/detail")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_chat_send_no_session(client):
    """Sending chat to a task with no session should return 400."""
    create_resp = await client.post("/api/tasks", json={
        "title": "T", "description": "d", "target_repo": "/tmp",
    })
    task_id = create_resp.json()["id"]
    resp = await client.post(f"/api/tasks/{task_id}/chat", json={"message": "hello"})
    assert resp.status_code == 400
    assert "session" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_chat_send_task_not_found(client):
    resp = await client.post("/api/tasks/9999/chat", json={"message": "hello"})
    assert resp.status_code == 404


# === Plan tests ===


@pytest.mark.asyncio
async def test_plan_approve_not_plan_review(client):
    """Approving a task not in plan_review state should return 400."""
    create_resp = await client.post("/api/tasks", json={
        "title": "T", "description": "d", "target_repo": "/tmp",
    })
    task_id = create_resp.json()["id"]
    resp = await client.post(f"/api/tasks/{task_id}/plan/approve")
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_plan_reject_not_plan_review(client):
    """Rejecting a task not in plan_review state should return 400."""
    create_resp = await client.post("/api/tasks", json={
        "title": "T", "description": "d", "target_repo": "/tmp",
    })
    task_id = create_resp.json()["id"]
    resp = await client.post(f"/api/tasks/{task_id}/plan/reject")
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_plan_approve_success(client, session_factory):
    """Approving a plan-mode task in plan_review state should succeed."""
    create_resp = await client.post("/api/tasks", json={
        "title": "Plan Task", "description": "d", "target_repo": "/tmp", "mode": "plan",
    })
    task_id = create_resp.json()["id"]

    # Set task to plan_review state directly in DB
    async with session_factory() as db:
        await db.execute(
            update(Task).where(Task.id == task_id).values(
                status="plan_review", plan_content="Here is my plan..."
            )
        )
        await db.commit()

    resp = await client.post(f"/api/tasks/{task_id}/plan/approve")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "pending"
    assert data["plan_approved"] is True


@pytest.mark.asyncio
async def test_plan_reject_success(client, session_factory):
    """Rejecting a plan-mode task in plan_review state should cancel it."""
    create_resp = await client.post("/api/tasks", json={
        "title": "Plan Task", "description": "d", "target_repo": "/tmp", "mode": "plan",
    })
    task_id = create_resp.json()["id"]

    async with session_factory() as db:
        await db.execute(
            update(Task).where(Task.id == task_id).values(
                status="plan_review", plan_content="Here is my plan..."
            )
        )
        await db.commit()

    resp = await client.post(f"/api/tasks/{task_id}/plan/reject")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "cancelled"
    assert data["plan_approved"] is False


@pytest.mark.asyncio
async def test_plan_approve_not_found(client):
    resp = await client.post("/api/tasks/9999/plan/approve")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_plan_reject_not_found(client):
    resp = await client.post("/api/tasks/9999/plan/reject")
    assert resp.status_code == 404


# === Chat send extra tests ===


async def _create_task_with_session(client, session_factory, **extra_fields):
    """Helper: create a task and set session_id + target_repo in DB."""
    create_resp = await client.post("/api/tasks", json={
        "title": "Chat Task", "description": "d", "target_repo": "/tmp",
    })
    task_id = create_resp.json()["id"]
    values = {"session_id": "test-session-123", **extra_fields}
    async with session_factory() as db:
        await db.execute(update(Task).where(Task.id == task_id).values(**values))
        await db.commit()
    return task_id


@pytest.mark.asyncio
async def test_chat_send_no_idle_instance(client, session_factory):
    """Task has session but no idle instances exist."""
    task_id = await _create_task_with_session(client, session_factory)

    mock_im = MagicMock()
    mock_im.processes = {}
    mock_broadcaster = MagicMock()
    mock_broadcaster.broadcast = AsyncMock()

    with patch("backend.main.instance_manager", mock_im), \
         patch("backend.main.broadcaster", mock_broadcaster):
        resp = await client.post(f"/api/tasks/{task_id}/chat", json={"message": "hi"})
    assert resp.status_code == 400
    assert "no idle instance" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_chat_send_task_being_processed(client, session_factory):
    """Task has session but an instance is currently processing it."""
    task_id = await _create_task_with_session(client, session_factory)

    # Create an instance that's "running" this task
    async with session_factory() as db:
        inst = Instance(name="busy-inst", status="idle", current_task_id=task_id)
        db.add(inst)
        await db.commit()
        await db.refresh(inst)
        inst_id = inst.id

    # Mock a process with returncode=None (still running) for this instance
    mock_proc = MagicMock()
    mock_proc.returncode = None
    mock_im = MagicMock()
    mock_im.processes = {inst_id: mock_proc}

    with patch("backend.main.instance_manager", mock_im):
        resp = await client.post(f"/api/tasks/{task_id}/chat", json={"message": "hi"})
    assert resp.status_code == 409
    assert "currently being processed" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_chat_send_cwd_uses_last_cwd(client, session_factory):
    """When last_cwd exists, uses it as cwd."""
    task_id = await _create_task_with_session(
        client, session_factory,
        last_cwd="/tmp",  # /tmp exists
    )

    # Create an idle instance
    async with session_factory() as db:
        inst = Instance(name="idle-inst", status="idle")
        db.add(inst)
        await db.commit()

    mock_im = MagicMock()
    mock_im.processes = {}
    mock_im.launch = AsyncMock(return_value=42)
    mock_broadcaster = MagicMock()
    mock_broadcaster.broadcast = AsyncMock()

    with patch("backend.main.instance_manager", mock_im), \
         patch("backend.main.broadcaster", mock_broadcaster):
        resp = await client.post(f"/api/tasks/{task_id}/chat", json={"message": "followup"})
    assert resp.status_code == 200
    mock_im.launch.assert_awaited_once()
    call_kwargs = mock_im.launch.call_args
    assert call_kwargs.kwargs.get("cwd") == "/tmp" or call_kwargs[1].get("cwd") == "/tmp"


@pytest.mark.asyncio
async def test_chat_send_cwd_not_found(client, session_factory):
    """When cwd doesn't exist -> 400."""
    task_id = await _create_task_with_session(
        client, session_factory,
        last_cwd="/nonexistent/a",
    )

    # Create an idle instance
    async with session_factory() as db:
        inst = Instance(name="idle-inst-2", status="idle")
        db.add(inst)
        await db.commit()

    mock_im = MagicMock()
    mock_im.processes = {}
    mock_broadcaster = MagicMock()
    mock_broadcaster.broadcast = AsyncMock()

    with patch("backend.main.instance_manager", mock_im), \
         patch("backend.main.broadcaster", mock_broadcaster):
        resp = await client.post(f"/api/tasks/{task_id}/chat", json={"message": "hi"})
    assert resp.status_code == 400
    assert "directory" in resp.json()["detail"].lower()


# === Chat with image_paths ===


@pytest.mark.asyncio
async def test_chat_send_with_image_paths_appends_to_prompt(client, session_factory):
    """When image_paths are provided, prompt passed to launch includes image list."""
    task_id = await _create_task_with_session(
        client, session_factory,
        last_cwd="/tmp",
    )

    async with session_factory() as db:
        inst = Instance(name="idle-img-inst", status="idle")
        db.add(inst)
        await db.commit()

    mock_im = MagicMock()
    mock_im.processes = {}
    mock_im.launch = AsyncMock(return_value=99)
    mock_broadcaster = MagicMock()
    mock_broadcaster.broadcast = AsyncMock()

    with patch("backend.main.instance_manager", mock_im), \
         patch("backend.main.broadcaster", mock_broadcaster):
        resp = await client.post(
            f"/api/tasks/{task_id}/chat",
            json={"message": "check this", "image_paths": ["/uploads/img1.png", "/uploads/img2.jpg"]},
        )
    assert resp.status_code == 200

    # Verify launch was called with a prompt that includes the image paths
    call_kwargs = mock_im.launch.call_args
    prompt_used = call_kwargs.kwargs.get("prompt") or call_kwargs[1].get("prompt") or call_kwargs[0][1]
    assert "/uploads/img1.png" in prompt_used
    assert "/uploads/img2.jpg" in prompt_used
    assert "Read" in prompt_used  # the instruction to use the Read tool


@pytest.mark.asyncio
async def test_chat_send_without_image_paths_plain_prompt(client, session_factory):
    """When no image_paths are provided, prompt is just the message text."""
    task_id = await _create_task_with_session(
        client, session_factory,
        last_cwd="/tmp",
    )

    async with session_factory() as db:
        inst = Instance(name="idle-plain-inst", status="idle")
        db.add(inst)
        await db.commit()

    mock_im = MagicMock()
    mock_im.processes = {}
    mock_im.launch = AsyncMock(return_value=100)
    mock_broadcaster = MagicMock()
    mock_broadcaster.broadcast = AsyncMock()

    with patch("backend.main.instance_manager", mock_im), \
         patch("backend.main.broadcaster", mock_broadcaster):
        resp = await client.post(
            f"/api/tasks/{task_id}/chat",
            json={"message": "plain message"},
        )
    assert resp.status_code == 200

    call_kwargs = mock_im.launch.call_args
    prompt_used = call_kwargs.kwargs.get("prompt") or call_kwargs[1].get("prompt") or call_kwargs[0][1]
    assert prompt_used == "plain message"


@pytest.mark.asyncio
async def test_chat_send_with_image_paths_stores_original_message(client, session_factory):
    """LogEntry content stores the original user message (without image instruction)."""
    from backend.models.log_entry import LogEntry
    from sqlalchemy import select

    task_id = await _create_task_with_session(
        client, session_factory,
        last_cwd="/tmp",
    )

    async with session_factory() as db:
        inst = Instance(name="idle-log-inst", status="idle")
        db.add(inst)
        await db.commit()

    mock_im = MagicMock()
    mock_im.processes = {}
    mock_im.launch = AsyncMock(return_value=101)
    mock_broadcaster = MagicMock()
    mock_broadcaster.broadcast = AsyncMock()

    with patch("backend.main.instance_manager", mock_im), \
         patch("backend.main.broadcaster", mock_broadcaster):
        await client.post(
            f"/api/tasks/{task_id}/chat",
            json={"message": "my message", "image_paths": ["/uploads/z.png"]},
        )

    async with session_factory() as db:
        result = await db.execute(
            select(LogEntry)
            .where(LogEntry.task_id == task_id, LogEntry.event_type == "user_message")
        )
        log = result.scalar_one()

    # Stored content should be the clean user message, not the augmented prompt
    assert log.content == "my message"


# === Chat model/effort resolution tests ===


@pytest.mark.asyncio
async def test_chat_send_uses_task_model_not_instance_model(client, session_factory):
    """Chat resume should use task.model (the model that created the session),
    not the instance's model."""
    task_id = await _create_task_with_session(
        client, session_factory,
        last_cwd="/tmp",
        model="claude-opus-4-6",  # task was created with opus 4.6
    )

    # Create an idle instance with a DIFFERENT model
    async with session_factory() as db:
        inst = Instance(name="inst-diff-model", status="idle", model="claude-opus-4-7")
        db.add(inst)
        await db.commit()

    mock_im = MagicMock()
    mock_im.processes = {}
    mock_im.launch = AsyncMock(return_value=42)
    mock_broadcaster = MagicMock()
    mock_broadcaster.broadcast = AsyncMock()

    with patch("backend.main.instance_manager", mock_im), \
         patch("backend.main.broadcaster", mock_broadcaster):
        resp = await client.post(f"/api/tasks/{task_id}/chat", json={"message": "hi"})
    assert resp.status_code == 200

    call_kwargs = mock_im.launch.call_args.kwargs
    # Should use task's model, not instance's
    assert call_kwargs["model"] == "claude-opus-4-6"


@pytest.mark.asyncio
async def test_chat_send_falls_back_to_instance_model_when_task_has_none(client, session_factory):
    """When task.model is None, chat resume should fall back to instance.model."""
    task_id = await _create_task_with_session(
        client, session_factory,
        last_cwd="/tmp",
        # no model set on task
    )

    async with session_factory() as db:
        inst = Instance(name="inst-fallback", status="idle", model="claude-sonnet-4-6")
        db.add(inst)
        await db.commit()

    mock_im = MagicMock()
    mock_im.processes = {}
    mock_im.launch = AsyncMock(return_value=43)
    mock_broadcaster = MagicMock()
    mock_broadcaster.broadcast = AsyncMock()

    with patch("backend.main.instance_manager", mock_im), \
         patch("backend.main.broadcaster", mock_broadcaster):
        resp = await client.post(f"/api/tasks/{task_id}/chat", json={"message": "hi"})
    assert resp.status_code == 200

    call_kwargs = mock_im.launch.call_args.kwargs
    assert call_kwargs["model"] == "claude-sonnet-4-6"


@pytest.mark.asyncio
async def test_chat_send_effort_uses_task_effort_over_instance(client, session_factory):
    """Chat resume effort_level should follow: task → instance → settings default."""
    task_id = await _create_task_with_session(
        client, session_factory,
        last_cwd="/tmp",
        effort_level="high",  # task-level effort
    )

    async with session_factory() as db:
        inst = Instance(name="inst-effort", status="idle", effort_level="low")
        db.add(inst)
        await db.commit()

    mock_im = MagicMock()
    mock_im.processes = {}
    mock_im.launch = AsyncMock(return_value=44)
    mock_broadcaster = MagicMock()
    mock_broadcaster.broadcast = AsyncMock()

    with patch("backend.main.instance_manager", mock_im), \
         patch("backend.main.broadcaster", mock_broadcaster):
        resp = await client.post(f"/api/tasks/{task_id}/chat", json={"message": "hi"})
    assert resp.status_code == 200

    call_kwargs = mock_im.launch.call_args.kwargs
    # Task's effort should take priority over instance's
    assert call_kwargs["effort_level"] == "high"


@pytest.mark.asyncio
async def test_chat_send_effort_falls_back_to_instance(client, session_factory):
    """When task has no effort_level, should use instance's effort_level."""
    task_id = await _create_task_with_session(
        client, session_factory,
        last_cwd="/tmp",
        # no effort_level on task
    )

    async with session_factory() as db:
        inst = Instance(name="inst-effort-fb", status="idle", effort_level="max")
        db.add(inst)
        await db.commit()

    mock_im = MagicMock()
    mock_im.processes = {}
    mock_im.launch = AsyncMock(return_value=45)
    mock_broadcaster = MagicMock()
    mock_broadcaster.broadcast = AsyncMock()

    with patch("backend.main.instance_manager", mock_im), \
         patch("backend.main.broadcaster", mock_broadcaster):
        resp = await client.post(f"/api/tasks/{task_id}/chat", json={"message": "hi"})
    assert resp.status_code == 200

    call_kwargs = mock_im.launch.call_args.kwargs
    assert call_kwargs["effort_level"] == "max"
