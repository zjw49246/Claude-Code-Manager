"""Regression tests for generation-safe Task termination orchestration."""

import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.models.instance import Instance
from backend.models.monitor_session import MonitorSession
from backend.models.task import Task


@pytest.mark.asyncio
async def test_termination_cancellation_during_generation_commit_still_reaps_owner(
    db_factory,
):
    """Caller cancellation cannot strand a terminal Task with its old owner."""

    import backend.main
    import backend.services.task_termination as termination

    started_at = datetime.utcnow()
    async with db_factory() as db:
        task = Task(
            title="cancel-safe termination",
            description="test",
            status="executing",
            started_at=started_at,
        )
        db.add(task)
        await db.flush()
        instance = Instance(
            name="cancel-safe-owner",
            status="running",
            pid=54001,
            current_task_id=task.id,
            started_at=started_at,
        )
        db.add(instance)
        await db.flush()
        task.instance_id = instance.id
        await db.commit()
        task_id = task.id
        instance_id = instance.id

    generation_read_started = asyncio.Event()
    allow_generation_commit = asyncio.Event()
    real_read_completed_at = termination.read_persisted_task_completed_at

    async def pause_before_first_commit(read_task_id, db):
        completed_at = await real_read_completed_at(read_task_id, db)
        generation_read_started.set()
        await allow_generation_commit.wait()
        return completed_at

    async def stop_exact(stopped_instance_id, **kwargs):
        assert stopped_instance_id == instance_id
        assert kwargs["expected_task_id"] == task_id
        assert kwargs["expected_pid"] == 54001
        assert kwargs["expected_started_at"] == started_at
        async with db_factory() as db:
            owner = await db.get(Instance, instance_id)
            owner.status = "idle"
            owner.pid = None
            owner.current_task_id = None
            await db.commit()
        return True

    async with db_factory() as db:
        with (
            patch.object(
                backend.main.dispatcher,
                "abort_task_queue",
                new_callable=AsyncMock,
                return_value=0,
            ),
            patch.object(
                backend.main.instance_manager,
                "wait_for_task_launch_barrier",
                new_callable=AsyncMock,
                return_value=True,
            ),
            patch.object(
                backend.main.instance_manager,
                "stop",
                new_callable=AsyncMock,
                side_effect=stop_exact,
            ) as stop,
            patch.object(
                termination,
                "read_persisted_task_completed_at",
                side_effect=pause_before_first_commit,
            ),
            patch(
                "backend.services.task_events.broadcast_status_change",
                new_callable=AsyncMock,
            ) as publish,
        ):
            operation = asyncio.create_task(
                termination.terminate_local_task_generation(
                    task_id,
                    db,
                    reason="superseded",
                )
            )
            await generation_read_started.wait()
            operation.cancel()
            await asyncio.sleep(0)
            allow_generation_commit.set()
            with pytest.raises(asyncio.CancelledError):
                await operation

    stop.assert_awaited_once()
    publish.assert_awaited_once_with(task_id, "completed")
    async with db_factory() as db:
        task = await db.get(Task, task_id)
        instance = await db.get(Instance, instance_id)
        assert task.status == "completed"
        assert task.error_message == "superseded"
        assert instance.status == "idle"
        assert instance.pid is None
        assert instance.current_task_id is None


@pytest.mark.asyncio
async def test_local_termination_revalidates_authority_after_queue_abort(
    db_factory,
):
    """A local→Worker migration during abort cannot satisfy the local CAS."""

    import backend.main
    import backend.services.task_termination as termination

    async with db_factory() as db:
        task = Task(
            title="authority migration",
            description="test",
            status="pending",
        )
        db.add(task)
        await db.commit()
        task_id = task.id

    async def migrate_while_queue_settles(_task_id):
        async with db_factory() as db:
            task = await db.get(Task, task_id)
            task.worker_id = 91
            await db.commit()
        return 0

    async with db_factory() as db:
        with (
            patch.object(
                backend.main.dispatcher,
                "abort_task_queue",
                new_callable=AsyncMock,
                side_effect=migrate_while_queue_settles,
            ),
            patch.object(
                backend.main.instance_manager,
                "stop",
                new_callable=AsyncMock,
            ) as stop,
        ):
            with pytest.raises(
                termination.TaskGenerationTerminationConflict,
                match="changed execution authority",
            ):
                await termination.terminate_local_task_generation(
                    task_id,
                    db,
                    reason="superseded",
                )

    stop.assert_not_awaited()
    async with db_factory() as db:
        task = await db.get(Task, task_id)
        assert task.status == "pending"
        assert task.worker_id == 91


@pytest.mark.asyncio
async def test_local_termination_reconciles_conflict_as_terminal(db_factory):
    """Conflict is terminal and remains retryable for cleanup reconciliation."""

    import backend.main
    import backend.services.task_termination as termination

    async with db_factory() as db:
        task = Task(
            title="conflicted review",
            description="test",
            status="conflict",
        )
        db.add(task)
        await db.commit()
        task_id = task.id

    async with db_factory() as db:
        with (
            patch.object(
                backend.main.dispatcher,
                "abort_task_queue",
                new_callable=AsyncMock,
                return_value=0,
            ),
            patch(
                "backend.services.task_events.broadcast_status_change",
                new_callable=AsyncMock,
            ) as publish,
        ):
            result = await termination.terminate_local_task_generation(
                task_id,
                db,
                reason="superseded",
            )

    assert result.previous_status == "conflict"
    assert result.terminal_status == "conflict"
    assert result.transitioned is False
    publish.assert_not_awaited()


@pytest.mark.asyncio
async def test_internal_termination_endpoint_returns_exact_terminal_snapshot(
    client,
    session_factory,
):
    """Forwarded PR tags survive TaskCreate and authorize safe termination."""

    import backend.main

    created = await client.post(
        "/api/tasks",
        json={
            "title": "worker-facing termination",
            "description": "test",
            "tags": ["pr-review"],
        },
    )
    assert created.status_code == 201, created.text
    task_id = created.json()["id"]
    async with session_factory() as db:
        task = await db.get(Task, task_id)
        assert task.tags == ["pr-review"]
        task.status = "executing"
        await db.commit()

    with patch.object(
        backend.main.dispatcher,
        "abort_task_queue",
        new_callable=AsyncMock,
        return_value=0,
    ):
        response = await client.post(
            f"/api/tasks/{task_id}/terminate-generation",
            json={
                "expected_status": "executing",
                "expected_retry_count": 0,
                "expected_instance_id": None,
                "expected_started_at": None,
                "expected_completed_at": None,
            },
        )

    assert response.status_code == 200, response.text
    assert response.json()["id"] == task_id
    assert response.json()["status"] == "completed"
    assert response.json()["error_message"] == "Superseded by new PR push"
    assert response.json()["metadata_"]["pr_review_superseded"] is True


@pytest.mark.asyncio
async def test_termination_retries_cancelled_ccm_auxiliary_cleanup(db_factory):
    """A failed auxiliary reap remains discoverable on the next supersede."""

    import backend.main
    import backend.services.task_termination as termination

    async with db_factory() as db:
        task = Task(
            title="review with monitor",
            description="test",
            status="executing",
        )
        db.add(task)
        await db.flush()
        monitor = MonitorSession(
            task_id=task.id,
            agent_type="monitor",
            source="ccm",
            description="watch review",
            status="running",
        )
        db.add(monitor)
        await db.commit()
        task_id = task.id
        monitor_id = monitor.id

    stop_attempts = 0

    async def fail_once(session_id):
        nonlocal stop_attempts
        assert session_id == monitor_id
        stop_attempts += 1
        if stop_attempts == 1:
            raise RuntimeError("auxiliary group still alive")

    with (
        patch.object(
            backend.main.dispatcher,
            "abort_task_queue",
            new_callable=AsyncMock,
            return_value=0,
        ),
        patch.object(
            backend.main.dispatcher,
            "stop_monitor_session_process",
            new_callable=AsyncMock,
            side_effect=fail_once,
        ),
    ):
        async with db_factory() as db:
            with pytest.raises(
                termination.TaskAuxiliaryTerminationConflict
            ):
                await termination.terminate_local_task_generation(
                    task_id,
                    db,
                    reason="superseded",
                )

        async with db_factory() as db:
            task = await db.get(Task, task_id)
            monitor = await db.get(MonitorSession, monitor_id)
            assert task.status == "completed"
            assert monitor.status == "cancelled"

        # The retry includes already-cancelled CCM sessions, so retained exact
        # process evidence is not silently forgotten.
        async with db_factory() as db:
            result = await termination.terminate_local_task_generation(
                task_id,
                db,
                reason="superseded",
            )

    assert result.terminal_status == "completed"
    assert stop_attempts == 2


@pytest.mark.asyncio
async def test_queue_abort_failure_does_not_persist_supersede_gate(db_factory):
    """An unconfirmed queue abort leaves the active review recoverable."""

    import backend.main
    import backend.services.task_termination as termination
    from backend.services.dispatcher import TaskQueueAbortTimeoutError

    async with db_factory() as db:
        task = Task(
            title="abort timeout",
            description="test",
            status="executing",
            metadata_={"pr_review_id": 11},
        )
        db.add(task)
        await db.commit()
        task_id = task.id

    async with db_factory() as db:
        with patch.object(
            backend.main.dispatcher,
            "abort_task_queue",
            new_callable=AsyncMock,
            side_effect=TaskQueueAbortTimeoutError("still running"),
        ):
            with pytest.raises(termination.TaskQueueTerminationConflict):
                await termination.terminate_local_task_generation(
                    task_id,
                    db,
                    reason="superseded",
                )

    async with db_factory() as db:
        task = await db.get(Task, task_id)
        assert task.status == "executing"
        assert task.metadata_ == {"pr_review_id": 11}


@pytest.mark.asyncio
async def test_hidden_termination_rejects_stale_remote_generation_before_abort(
    client,
    session_factory,
):
    """GET→POST cannot terminate a Worker retry that won in the gap."""

    import backend.main

    created = await client.post(
        "/api/tasks",
        json={
            "title": "remote retry race",
            "description": "test",
            "tags": ["pr-review"],
        },
    )
    assert created.status_code == 201, created.text
    task_id = created.json()["id"]
    async with session_factory() as db:
        task = await db.get(Task, task_id)
        task.status = "pending"
        task.retry_count = 1
        await db.commit()

    with patch.object(
        backend.main.dispatcher,
        "abort_task_queue",
        new_callable=AsyncMock,
    ) as abort:
        response = await client.post(
            f"/api/tasks/{task_id}/terminate-generation",
            json={
                "expected_status": "executing",
                "expected_retry_count": 0,
                "expected_instance_id": None,
                "expected_started_at": None,
                "expected_completed_at": None,
            },
        )

    assert response.status_code == 409, response.text
    abort.assert_not_awaited()
    async with session_factory() as db:
        task = await db.get(Task, task_id)
        assert task.status == "pending"
        assert task.retry_count == 1
        assert (
            (task.metadata_ or {}).get("pr_review_superseded")
            is not True
        )


@pytest.mark.asyncio
async def test_superseded_marker_blocks_retry_defer_and_dequeue(db_factory):
    """Every local route back to runnable pending state honors the gate."""

    from backend.services.task_queue import TaskQueue

    async with db_factory() as db:
        retried = Task(
            title="blocked retry",
            description="test",
            status="completed",
            metadata_={"pr_review_superseded": True},
        )
        deferred = Task(
            title="blocked defer",
            description="test",
            status="executing",
            metadata_={"pr_review_superseded": True},
        )
        claimed = Task(
            title="blocked claim",
            description="test",
            status="pending",
            metadata_={"pr_review_superseded": True},
        )
        db.add_all([retried, deferred, claimed])
        await db.commit()
        retry_id = retried.id
        defer_id = deferred.id

        queue = TaskQueue(db)
        assert await queue.retry(retry_id) is None
        assert await queue.defer(defer_id, "backpressure") is False
        assert await queue.dequeue() is None

    async with db_factory() as db:
        retried = await db.get(Task, retry_id)
        deferred = await db.get(Task, defer_id)
        assert retried.status == "completed"
        assert retried.retry_count == 0
        assert deferred.status == "executing"


@pytest.mark.asyncio
async def test_superseded_manager_mirror_cannot_be_migrated(db_factory):
    """A Worker marker mirrored to Manager cannot be copied into an ungated Task."""

    from backend.services.task_migrator import (
        MigrationError,
        TaskMigrator,
        migration_task_generation,
    )

    async with db_factory() as db:
        task = Task(
            title="blocked migration",
            description="test",
            status="completed",
            metadata_={
                "pr_review_id": 17,
                "pr_review_superseded": True,
            },
        )
        db.add(task)
        await db.commit()
        task_id = task.id

    migrator = TaskMigrator(db_factory, AsyncMock())
    with pytest.raises(MigrationError):
        await migrator._claim_migration(migration_task_generation(task))

    async with db_factory() as db:
        task = await db.get(Task, task_id)
        assert task.status == "completed"


@pytest.mark.asyncio
async def test_worker_dispatch_excludes_superseded_pending_mirror(db_factory):
    """Even a malformed pending Manager mirror is never forwarded to a Worker."""

    import backend.main
    from backend.services.dispatcher import GlobalDispatcher

    async with db_factory() as db:
        task = Task(
            title="blocked worker dispatch",
            description="test",
            status="pending",
            worker_id=88,
            metadata_={"pr_review_superseded": True},
        )
        db.add(task)
        await db.commit()
        task_id = task.id

    proxy = MagicMock()
    proxy.forward_task_to_worker = AsyncMock()
    dispatcher = GlobalDispatcher(
        db_factory,
        MagicMock(),
        MagicMock(),
    )
    with patch.object(backend.main, "worker_proxy", proxy):
        await dispatcher._dispatch_worker_tasks()

    proxy.forward_task_to_worker.assert_not_awaited()
    async with db_factory() as db:
        task = await db.get(Task, task_id)
        assert task.status == "pending"


@pytest.mark.asyncio
async def test_queued_chat_drops_when_supersede_wins_final_launch_claim(
    db_factory,
):
    """A message admitted after abort cannot launch past the terminal marker."""

    from pathlib import Path
    from types import SimpleNamespace

    from sqlalchemy import update

    from backend.services.dispatcher import GlobalDispatcher, QueuedMessage

    async with db_factory() as db:
        task = Task(
            title="queued during supersede",
            description="test",
            status="completed",
            session_id="existing-session",
            metadata_={"pr_review_id": 41},
        )
        db.add(task)
        await db.commit()
        task_id = task.id

    instance_manager = MagicMock()
    instance_manager.is_running.return_value = False
    instance_manager.launch = AsyncMock()
    broadcaster = MagicMock()
    broadcaster.broadcast = AsyncMock()
    dispatcher = GlobalDispatcher(
        db_factory,
        instance_manager,
        broadcaster,
    )

    async def supersede_during_slot_reservation(db):
        # This is the post-abort/new-enqueue race: all Python prechecks saw the
        # old row, then synchronize commits the marker immediately before the
        # consumer's final atomic Task claim.
        await db.execute(
            update(Task)
            .where(Task.id == task_id)
            .values(
                metadata_={
                    "pr_review_id": 41,
                    "pr_review_superseded": True,
                }
            )
        )
        await db.commit()
        return SimpleNamespace(id=901), object()

    dispatcher._reserve_idle_instance = AsyncMock(
        side_effect=supersede_during_slot_reservation
    )
    dispatcher._resolve_resume_config_dir = AsyncMock(return_value=None)
    message = QueuedMessage(
        priority=0,
        timestamp=0,
        prompt="late queued message",
    )
    with patch(
        "backend.api.tasks._find_session_jsonl",
        return_value=Path("/tmp/existing-session.jsonl"),
    ):
        await dispatcher._process_queued_message(task_id, message)

    instance_manager.launch.assert_not_awaited()
    async with db_factory() as db:
        task = await db.get(Task, task_id)
        assert task.status == "completed"
        assert task.instance_id is None
        assert task.metadata_["pr_review_superseded"] is True
