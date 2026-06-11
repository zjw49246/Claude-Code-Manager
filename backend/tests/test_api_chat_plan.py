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


# === Chat send: enqueue contract ===
# POST /chat no longer launches an instance directly. It stores + broadcasts
# the user message and enqueues the prompt via dispatcher.enqueue_message;
# launch-time concerns (model/effort/cwd) moved to the dispatcher's
# _process_queued_message (tested below at the dispatcher level).


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


def _mock_dispatcher():
    d = MagicMock()
    d.enqueue_message = AsyncMock()
    return d


@pytest.mark.asyncio
async def test_chat_send_enqueues_message(client, session_factory):
    """Chat send returns 200 queued=True and enqueues via the dispatcher."""
    from backend.services.dispatcher import PRIORITY_USER

    task_id = await _create_task_with_session(client, session_factory)

    mock_d = _mock_dispatcher()
    mock_broadcaster = MagicMock()
    mock_broadcaster.broadcast = AsyncMock()

    with patch("backend.main.dispatcher", mock_d), \
         patch("backend.main.broadcaster", mock_broadcaster):
        resp = await client.post(f"/api/tasks/{task_id}/chat", json={"message": "hi"})

    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["queued"] is True
    assert data["session_id"] == "test-session-123"

    mock_d.enqueue_message.assert_awaited_once()
    kwargs = mock_d.enqueue_message.call_args.kwargs
    assert kwargs["task_id"] == task_id
    assert kwargs["prompt"] == "hi"
    assert kwargs["priority"] == PRIORITY_USER
    assert kwargs["source"] == "user"

    # User message broadcast to task channel before enqueue
    task_broadcasts = [
        c for c in mock_broadcaster.broadcast.call_args_list
        if c[0][0] == f"task:{task_id}" and c[0][1].get("event_type") == "user_message"
    ]
    assert len(task_broadcasts) == 1
    assert task_broadcasts[0][0][1]["content"] == "hi"


@pytest.mark.asyncio
async def test_chat_send_queues_even_when_task_busy(client, session_factory):
    """Busy/no-idle-instance states no longer 4xx at the endpoint — the
    message is queued and the dispatcher serializes processing."""
    task_id = await _create_task_with_session(client, session_factory)

    # An instance currently "running" this task — irrelevant to the endpoint now
    async with session_factory() as db:
        inst = Instance(name="busy-inst", status="idle", current_task_id=task_id)
        db.add(inst)
        await db.commit()

    mock_d = _mock_dispatcher()
    mock_broadcaster = MagicMock()
    mock_broadcaster.broadcast = AsyncMock()

    with patch("backend.main.dispatcher", mock_d), \
         patch("backend.main.broadcaster", mock_broadcaster):
        resp = await client.post(f"/api/tasks/{task_id}/chat", json={"message": "hi"})

    assert resp.status_code == 200
    assert resp.json()["queued"] is True
    mock_d.enqueue_message.assert_awaited_once()


# === Chat with image_paths ===


@pytest.mark.asyncio
async def test_chat_send_with_image_paths_appends_to_prompt(client, session_factory):
    """When image_paths are provided, the enqueued prompt includes the file list."""
    task_id = await _create_task_with_session(client, session_factory)

    mock_d = _mock_dispatcher()
    mock_broadcaster = MagicMock()
    mock_broadcaster.broadcast = AsyncMock()

    with patch("backend.main.dispatcher", mock_d), \
         patch("backend.main.broadcaster", mock_broadcaster):
        resp = await client.post(
            f"/api/tasks/{task_id}/chat",
            json={"message": "check this", "image_paths": ["/uploads/img1.png", "/uploads/img2.jpg"]},
        )
    assert resp.status_code == 200

    prompt_used = mock_d.enqueue_message.call_args.kwargs["prompt"]
    assert "/uploads/img1.png" in prompt_used
    assert "/uploads/img2.jpg" in prompt_used
    assert "Read" in prompt_used  # the instruction to use the Read tool


@pytest.mark.asyncio
async def test_chat_send_without_image_paths_plain_prompt(client, session_factory):
    """When no image_paths are provided, the enqueued prompt is just the message."""
    task_id = await _create_task_with_session(client, session_factory)

    mock_d = _mock_dispatcher()
    mock_broadcaster = MagicMock()
    mock_broadcaster.broadcast = AsyncMock()

    with patch("backend.main.dispatcher", mock_d), \
         patch("backend.main.broadcaster", mock_broadcaster):
        resp = await client.post(
            f"/api/tasks/{task_id}/chat",
            json={"message": "plain message"},
        )
    assert resp.status_code == 200
    assert mock_d.enqueue_message.call_args.kwargs["prompt"] == "plain message"


@pytest.mark.asyncio
async def test_chat_send_with_image_paths_stores_original_message(client, session_factory):
    """LogEntry content stores the original user message (without image instruction)."""
    from backend.models.log_entry import LogEntry
    from sqlalchemy import select

    task_id = await _create_task_with_session(client, session_factory)

    mock_d = _mock_dispatcher()
    mock_broadcaster = MagicMock()
    mock_broadcaster.broadcast = AsyncMock()

    with patch("backend.main.dispatcher", mock_d), \
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


# === Dispatcher: queued message → launch resolution (model/effort/cwd) ===


from backend.services.dispatcher import GlobalDispatcher, QueuedMessage, PRIORITY_USER


def _make_dispatcher(db_factory):
    mock_im = MagicMock()
    mock_im.processes = {}
    mock_im.launch = AsyncMock()
    mock_broadcaster = MagicMock()
    mock_broadcaster.broadcast = AsyncMock()
    return GlobalDispatcher(db_factory, mock_im, mock_broadcaster)


async def _seed_task_for_queue(db_factory, **task_fields):
    async with db_factory() as db:
        task = Task(
            title="t", description="d", status="completed", target_repo="/tmp",
            session_id="sess-1", **task_fields,
        )
        db.add(task)
        inst = Instance(name="idle-inst", status="idle")
        db.add(inst)
        await db.commit()
        await db.refresh(task)
        return task.id


def _queued(prompt="hi"):
    import time
    return QueuedMessage(
        priority=PRIORITY_USER, timestamp=time.monotonic(),
        prompt=prompt, source="user",
    )


@pytest.mark.asyncio
async def test_process_queued_message_uses_task_model(db_factory):
    """Queued message launch resumes with task.model (the model that created
    the session), not any instance-level model."""
    dispatcher = _make_dispatcher(db_factory)
    task_id = await _seed_task_for_queue(db_factory, model="claude-opus-4-6", last_cwd="/tmp")

    await dispatcher._process_queued_message(task_id, _queued())

    dispatcher.instance_manager.launch.assert_awaited_once()
    kwargs = dispatcher.instance_manager.launch.call_args.kwargs
    assert kwargs["model"] == "claude-opus-4-6"
    assert kwargs["resume_session_id"] == "sess-1"
    assert kwargs["prompt"] == "hi"
    assert kwargs["chat_initiated"] is True


@pytest.mark.asyncio
async def test_process_queued_message_effort_uses_task_effort(db_factory):
    """Queued message launch uses task.effort_level."""
    dispatcher = _make_dispatcher(db_factory)
    task_id = await _seed_task_for_queue(db_factory, effort_level="high", last_cwd="/tmp")

    await dispatcher._process_queued_message(task_id, _queued())

    kwargs = dispatcher.instance_manager.launch.call_args.kwargs
    assert kwargs["effort_level"] == "high"


@pytest.mark.asyncio
async def test_process_queued_message_cwd_uses_last_cwd(db_factory):
    """When last_cwd is set, the launch cwd uses it."""
    dispatcher = _make_dispatcher(db_factory)
    task_id = await _seed_task_for_queue(db_factory, last_cwd="/tmp/somewhere")

    await dispatcher._process_queued_message(task_id, _queued())

    kwargs = dispatcher.instance_manager.launch.call_args.kwargs
    assert kwargs["cwd"] == "/tmp/somewhere"


@pytest.mark.asyncio
async def test_process_queued_message_cwd_falls_back_to_target_repo(db_factory):
    """Without last_cwd, the launch cwd falls back to task.target_repo
    (the old endpoint-level 400-on-missing-cwd check no longer exists)."""
    dispatcher = _make_dispatcher(db_factory)
    task_id = await _seed_task_for_queue(db_factory, last_cwd=None)

    await dispatcher._process_queued_message(task_id, _queued())

    kwargs = dispatcher.instance_manager.launch.call_args.kwargs
    assert kwargs["cwd"] == "/tmp"


@pytest.mark.asyncio
async def test_chat_send_with_model_override(client, session_factory):
    """临时模型：body.model 透传为 enqueue 的 model_override，不落库。"""
    from backend.models.task import Task

    task_id = await _create_task_with_session(client, session_factory)

    mock_d = _mock_dispatcher()
    mock_broadcaster = MagicMock()
    mock_broadcaster.broadcast = AsyncMock()

    with patch("backend.main.dispatcher", mock_d), \
         patch("backend.main.broadcaster", mock_broadcaster):
        resp = await client.post(
            f"/api/tasks/{task_id}/chat",
            json={"message": "hard problem", "model": "claude-opus-4-8"},
        )

    assert resp.status_code == 200
    kwargs = mock_d.enqueue_message.call_args.kwargs
    assert kwargs["model_override"] == "claude-opus-4-8"

    # task.model 不被修改
    async with session_factory() as db:
        t = await db.get(Task, task_id)
        assert t.model != "claude-opus-4-8"


@pytest.mark.asyncio
async def test_update_task_model_persists(client, session_factory):
    """持久模型切换：PATCH/PUT task.model 生效。"""
    from backend.models.task import Task

    task_id = await _create_task_with_session(client, session_factory)
    resp = await client.put(
        f"/api/tasks/{task_id}", json={"model": "claude-sonnet-4-6"}
    )
    assert resp.status_code == 200
    async with session_factory() as db:
        t = await db.get(Task, task_id)
        assert t.model == "claude-sonnet-4-6"


@pytest.mark.asyncio
async def test_inject_requires_pty_mode(client, session_factory):
    """PTY 模式关闭时注入返回 400。"""
    task_id = await _create_task_with_session(client, session_factory)

    mock_im = MagicMock()
    mock_im.pty_mode_enabled = False
    with patch("backend.main.instance_manager", mock_im), \
         patch("backend.main.broadcaster", MagicMock(broadcast=AsyncMock())):
        resp = await client.post(
            f"/api/tasks/{task_id}/inject", json={"message": "hint"}
        )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_inject_delivers_to_pty_session(client, session_factory):
    """PTY 注入成功：调用 inject_pty_message 并广播 source=inject 的 user_message。"""
    from backend.models.task import Task

    task_id = await _create_task_with_session(client, session_factory)

    mock_im = MagicMock()
    mock_im.pty_mode_enabled = True
    mock_im.inject_pty_message = AsyncMock(return_value=True)
    mock_broadcaster = MagicMock()
    mock_broadcaster.broadcast = AsyncMock()

    with patch("backend.main.instance_manager", mock_im), \
         patch("backend.main.broadcaster", mock_broadcaster):
        resp = await client.post(
            f"/api/tasks/{task_id}/inject", json={"message": "focus on tests"}
        )

    assert resp.status_code == 200
    # 回归：chat 路径不更新 task.instance_id，必须按 session_id 定位 PTY 会话
    mock_im.inject_pty_message.assert_awaited_once_with(
        "test-session-123", "focus on tests"
    )
    casts = [c for c in mock_broadcaster.broadcast.call_args_list
             if c[0][1].get("source") == "inject"]
    assert len(casts) == 1


@pytest.mark.asyncio
async def test_inject_no_live_session_409(client, session_factory):
    """会话不存活时注入返回 409。"""
    from backend.models.task import Task

    task_id = await _create_task_with_session(client, session_factory)

    mock_im = MagicMock()
    mock_im.pty_mode_enabled = True
    mock_im.inject_pty_message = AsyncMock(return_value=False)
    with patch("backend.main.instance_manager", mock_im), \
         patch("backend.main.broadcaster", MagicMock(broadcast=AsyncMock())):
        resp = await client.post(
            f"/api/tasks/{task_id}/inject", json={"message": "x"}
        )
    assert resp.status_code == 409
