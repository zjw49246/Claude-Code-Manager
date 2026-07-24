import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.mark.asyncio
async def test_dispatcher_shutdown_error_propagates_after_other_cleanup(
    monkeypatch,
):
    import backend.main as main

    dispatcher_error = RuntimeError("dispatcher generation survived")
    dispatcher = MagicMock()
    dispatcher.shutdown = AsyncMock(side_effect=dispatcher_error)

    pty_backend = MagicMock()
    pty_backend.shutdown = AsyncMock()
    instance_manager = MagicMock()
    instance_manager._pty_backend = pty_backend
    instance_manager.shutdown_codex_app_server = AsyncMock()

    watcher = MagicMock()
    heartbeat = MagicMock()
    worker_health = MagicMock()
    upload_cleanup = MagicMock()
    backup = MagicMock()

    monkeypatch.setattr(main, "dispatcher", dispatcher)
    monkeypatch.setattr(main, "instance_manager", instance_manager)
    monkeypatch.setattr(main, "sub_agent_watcher", watcher)

    with pytest.raises(RuntimeError, match="generation survived"):
        await main._shutdown_runtime_services(
            heartbeat_task=heartbeat,
            worker_health_task=worker_health,
            upload_cleanup_task=upload_cleanup,
            backup_svc=backup,
        )

    heartbeat.cancel.assert_called_once_with()
    worker_health.cancel.assert_called_once_with()
    upload_cleanup.cancel.assert_called_once_with()
    pty_backend.shutdown.assert_awaited_once_with()
    instance_manager.shutdown_codex_app_server.assert_awaited_once_with()
    watcher.stop.assert_called_once_with()
    backup.stop.assert_called_once_with()
    assert dispatcher.shutdown.await_count == 2


@pytest.mark.asyncio
async def test_dispatcher_shutdown_retry_can_recover_after_transport_cleanup(
    monkeypatch,
):
    import backend.main as main

    dispatcher = MagicMock()
    dispatcher.shutdown = AsyncMock(
        side_effect=[RuntimeError("spawn still settling"), None]
    )
    pty_backend = MagicMock(shutdown=AsyncMock())
    instance_manager = MagicMock()
    instance_manager._pty_backend = pty_backend
    instance_manager.shutdown_codex_app_server = AsyncMock()
    watcher = MagicMock()

    monkeypatch.setattr(main, "dispatcher", dispatcher)
    monkeypatch.setattr(main, "instance_manager", instance_manager)
    monkeypatch.setattr(main, "sub_agent_watcher", watcher)

    upload_cleanup = MagicMock()
    await main._shutdown_runtime_services(
        heartbeat_task=None,
        worker_health_task=None,
        upload_cleanup_task=upload_cleanup,
        backup_svc=None,
    )

    assert dispatcher.shutdown.await_count == 2
    pty_backend.shutdown.assert_awaited_once_with()
    instance_manager.shutdown_codex_app_server.assert_awaited_once_with()
    upload_cleanup.cancel.assert_called_once_with()
    watcher.stop.assert_called_once_with()


@pytest.mark.asyncio
async def test_shutdown_awaits_cancelled_background_tasks(monkeypatch):
    import backend.main as main

    dispatcher = MagicMock(shutdown=AsyncMock())
    instance_manager = MagicMock()
    instance_manager._pty_backend = None
    instance_manager.shutdown_codex_app_server = AsyncMock()
    watcher = MagicMock()
    monkeypatch.setattr(main, "dispatcher", dispatcher)
    monkeypatch.setattr(main, "instance_manager", instance_manager)
    monkeypatch.setattr(main, "sub_agent_watcher", watcher)

    finalized = [asyncio.Event() for _ in range(3)]

    async def background(done):
        try:
            await asyncio.Event().wait()
        finally:
            done.set()

    tasks = [
        asyncio.create_task(background(done))
        for done in finalized
    ]
    await asyncio.sleep(0)

    await main._shutdown_runtime_services(
        heartbeat_task=tasks[0],
        worker_health_task=tasks[1],
        upload_cleanup_task=tasks[2],
        backup_svc=None,
    )

    assert all(task.done() for task in tasks)
    assert all(done.is_set() for done in finalized)
