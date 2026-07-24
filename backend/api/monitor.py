import asyncio
from weakref import WeakValueDictionary
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import case, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database import get_db
from backend.models.task import Task
from backend.models.monitor_session import MonitorSession, MonitorCheck
from backend.schemas.monitor_session import (
    MonitorSessionCreate,
    MonitorSessionResponse,
    MonitorCheckCreate,
    MonitorCheckResponse,
    MonitorCompleteRequest,
)

router = APIRouter(prefix="/api/tasks/{task_id}/monitor-sessions", tags=["monitor"])

MAX_CONCURRENT_MONITORS = 5
_monitor_admission_locks: WeakValueDictionary[int, asyncio.Lock] = (
    WeakValueDictionary()
)


def _monitor_admission_lock(task_id: int) -> asyncio.Lock:
    """Return the single-process SQLite admission lock for one Task."""

    lock = _monitor_admission_locks.get(task_id)
    if lock is None:
        lock = asyncio.Lock()
        _monitor_admission_locks[task_id] = lock
    return lock


async def _settle_shielded(operation: asyncio.Task) -> asyncio.CancelledError | None:
    """Delay caller cancellation until a lifecycle-critical operation settles."""

    delayed_cancellation: asyncio.CancelledError | None = None
    while not operation.done():
        try:
            await asyncio.shield(operation)
        except asyncio.CancelledError as exc:
            delayed_cancellation = exc
        except BaseException:
            break
    return delayed_cancellation


async def _mark_monitor_admission_failed(
    db: AsyncSession,
    task_id: int,
    session_id: int,
) -> None:
    await db.execute(
        update(MonitorSession)
        .where(
            MonitorSession.id == session_id,
            MonitorSession.task_id == task_id,
            MonitorSession.agent_type == "monitor",
            MonitorSession.source == "ccm",
            MonitorSession.status == "running",
        )
        .values(status="failed", completed_at=datetime.utcnow())
    )
    await db.commit()


async def _commit_and_admit_monitor(
    db: AsyncSession,
    task_id: int,
    session: MonitorSession,
    dispatcher,
) -> None:
    """Atomically settle DB commit and synchronous dispatcher registration."""

    committed = False

    async def commit_and_start() -> None:
        nonlocal committed
        await db.commit()
        committed = True
        # Deliberately no await after commit: registration becomes visible in
        # the same event-loop slice in which the durable row becomes visible.
        dispatcher.start_monitor_session(session)

    operation = asyncio.create_task(commit_and_start())
    delayed_cancellation = await _settle_shielded(operation)
    try:
        operation.result()
    except Exception as exc:
        if committed:
            cleanup = asyncio.create_task(
                _mark_monitor_admission_failed(db, task_id, session.id)
            )
            cleanup_cancellation = await _settle_shielded(cleanup)
            cleanup.result()
            delayed_cancellation = (
                delayed_cancellation or cleanup_cancellation
            )
        if delayed_cancellation is not None:
            raise delayed_cancellation
        if isinstance(exc, RuntimeError):
            raise HTTPException(503, str(exc)) from exc
        raise
    if delayed_cancellation is not None:
        raise delayed_cancellation


async def _monitor_session_or_error(
    db: AsyncSession,
    task_id: int,
    session_id: int,
) -> MonitorSession:
    db.expire_all()
    session = await db.get(MonitorSession, session_id)
    if session is None or session.task_id != task_id:
        raise HTTPException(404, "Monitor session not found")
    raise HTTPException(400, "Monitor session is not running")


@router.post("", response_model=MonitorSessionResponse)
async def create_monitor_session(
    task_id: int,
    body: MonitorSessionCreate,
    db: AsyncSession = Depends(get_db),
):
    # Route Worker tasks before taking a local row lock: the proxy is a network
    # await and must never hold the Manager's Task transaction open.
    task = await db.get(Task, task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    if task.worker_id is not None:
        # Worker task：monitor 子进程依赖 task 所在机器的文件系统（ps/tail/signal
        # file），必须在 worker 上跑。本地镜像行由 relay 的 monitor_session_created
        # 事件落库（带 remote_id），这里直接透传 worker 响应。
        from backend.main import worker_proxy
        if worker_proxy is None:
            raise HTTPException(503, "Worker 功能未启用")
        db.expunge(task)
        await db.rollback()
        return await worker_proxy.proxy_to_worker(
            task, "POST", f"/api/tasks/{task_id}/monitor-sessions",
            body=body.model_dump(),
        )

    async with _monitor_admission_lock(task_id):
        try:
            # End the routing read before waiting for a write barrier.
            # ``FOR UPDATE`` alone is ignored by SQLite. The keyed lock keeps
            # same-process cap checks ordered, while this no-op Task UPDATE
            # also serializes cancellation and other backend processes.
            await db.rollback()
            guarded = await db.execute(
                update(Task)
                .where(
                    Task.id == task_id,
                    Task.worker_id.is_(None),
                    Task.status.in_(("in_progress", "executing")),
                )
                .values(status=Task.status)
            )
            if not guarded.rowcount:
                await db.rollback()
                db.expire_all()
                task = await db.get(Task, task_id)
                if task is None:
                    raise HTTPException(404, "Task not found")
                if task.worker_id is not None:
                    from backend.main import worker_proxy
                    if worker_proxy is None:
                        raise HTTPException(503, "Worker 功能未启用")
                    db.expunge(task)
                    await db.rollback()
                    return await worker_proxy.proxy_to_worker(
                        task,
                        "POST",
                        f"/api/tasks/{task_id}/monitor-sessions",
                        body=body.model_dump(),
                    )
                raise HTTPException(
                    400,
                    "Cannot create monitor for inactive task",
                )
            db.expire_all()
            task = await db.get(Task, task_id)
            if task is None:
                raise HTTPException(404, "Task not found")
            # Monitor agents are currently hard-wired to Claude CLI.
            if (task.provider or "claude").lower() != "claude":
                raise HTTPException(
                    400,
                    "Monitor sub-agents are claude-only; this task runs on "
                    f"provider '{task.provider}'",
                )
            skills = task.enabled_skills or {}
            if not skills.get("monitor"):
                raise HTTPException(
                    403,
                    "Monitor skill not enabled for this task",
                )

            active_count = await db.scalar(
                select(func.count(MonitorSession.id)).where(
                    MonitorSession.task_id == task_id,
                    MonitorSession.agent_type == "monitor",
                    MonitorSession.source == "ccm",
                    MonitorSession.status == "running",
                )
            )
            if active_count >= MAX_CONCURRENT_MONITORS:
                raise HTTPException(
                    429,
                    "Too many active monitors "
                    f"({active_count}/{MAX_CONCURRENT_MONITORS}). "
                    "Stop an existing monitor first.",
                )

            from backend.main import dispatcher
            if getattr(dispatcher, "_shutting_down", False) is True:
                raise HTTPException(503, "Dispatcher is shutting down")

            ms = MonitorSession(
                task_id=task_id,
                agent_type="monitor",
                source="ccm",
                description=body.description,
                monitor_context=body.monitor_context,
                interval=body.interval,
                max_checks=body.max_checks,
                model=body.model,
            )
            db.add(ms)
            await db.flush()
            await _commit_and_admit_monitor(
                db,
                task_id,
                ms,
                dispatcher,
            )
        except BaseException:
            if db.in_transaction():
                await db.rollback()
            raise

    await dispatcher.broadcaster.broadcast(
        f"task:{task_id}",
        {"event": "monitor_session_created", "monitor_session_id": ms.id, "description": ms.description},
    )

    return ms


@router.get("", response_model=list[MonitorSessionResponse])
async def list_monitor_sessions(task_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(MonitorSession)
        .where(MonitorSession.task_id == task_id)
        .order_by(MonitorSession.created_at.desc())
    )
    return list(result.scalars().all())


@router.get("/{session_id}", response_model=MonitorSessionResponse)
async def get_monitor_session(
    task_id: int, session_id: int, db: AsyncSession = Depends(get_db),
):
    ms = await db.get(MonitorSession, session_id)
    if not ms or ms.task_id != task_id:
        raise HTTPException(404, "Monitor session not found")
    return ms


@router.delete("/{session_id}")
async def delete_monitor_session(
    task_id: int, session_id: int, db: AsyncSession = Depends(get_db),
):
    ms = await db.get(MonitorSession, session_id)
    if not ms or ms.task_id != task_id:
        raise HTTPException(404, "Monitor session not found")

    task = await db.get(Task, task_id)
    if task is not None and task.worker_id is not None:
        # 本地行是镜像（id 是 Manager 自增），worker 端要用 remote_id
        from backend.main import worker_proxy
        if worker_proxy is None:
            raise HTTPException(503, "Worker 功能未启用")
        if ms.remote_id is None:
            raise HTTPException(409, "该 monitor 缺少 worker 侧 id（remote_id），无法远程删除")
        result = await worker_proxy.proxy_to_worker(
            task, "DELETE", f"/api/tasks/{task_id}/monitor-sessions/{ms.remote_id}",
        )
        await db.execute(
            update(MonitorSession)
            .where(
                MonitorSession.id == session_id,
                MonitorSession.task_id == task_id,
                MonitorSession.status == "running",
            )
            .values(status="cancelled", completed_at=datetime.utcnow())
        )
        await db.commit()
        return result

    transitioned = await db.execute(
        update(MonitorSession)
        .where(
            MonitorSession.id == session_id,
            MonitorSession.task_id == task_id,
            MonitorSession.agent_type == "monitor",
            MonitorSession.source == "ccm",
            MonitorSession.status == "running",
        )
        .values(status="cancelled", completed_at=datetime.utcnow())
    )
    await db.commit()

    from backend.main import dispatcher
    await dispatcher.stop_monitor_session_process(session_id)

    from backend.services.mcp_config import cleanup_monitor_agent_mcp_config
    cleanup_monitor_agent_mcp_config(session_id)

    if transitioned.rowcount:
        await dispatcher.broadcaster.broadcast(
            f"task:{task_id}",
            {
                "event": "monitor_session_status",
                "monitor_session_id": session_id,
                "status": "cancelled",
            },
        )

    return {"ok": True}


@router.get("/{session_id}/checks", response_model=list[MonitorCheckResponse])
async def get_monitor_checks(
    task_id: int, session_id: int, db: AsyncSession = Depends(get_db),
):
    ms = await db.get(MonitorSession, session_id)
    if not ms or ms.task_id != task_id:
        raise HTTPException(404, "Monitor session not found")
    result = await db.execute(
        select(MonitorCheck)
        .where(MonitorCheck.monitor_session_id == session_id)
        .order_by(MonitorCheck.created_at.desc())
    )
    return list(result.scalars().all())


@router.post("/{session_id}/checks", response_model=MonitorCheckResponse)
async def create_monitor_check(
    task_id: int,
    session_id: int,
    body: MonitorCheckCreate,
    db: AsyncSession = Depends(get_db),
):
    """Sub-agent reports a status check via MCP tool."""
    import json as _json
    from backend.main import dispatcher
    from backend.models.log_entry import LogEntry

    next_check = MonitorSession.checks_done + 1
    reaches_limit = next_check >= MonitorSession.max_checks
    completed_at = datetime.utcnow()
    advanced = await db.execute(
        update(MonitorSession)
        .where(
            MonitorSession.id == session_id,
            MonitorSession.task_id == task_id,
            MonitorSession.agent_type == "monitor",
            MonitorSession.source == "ccm",
            MonitorSession.status == "running",
        )
        # MySQL evaluates assignments in a single-table UPDATE from left to
        # right. Keep both limit expressions ahead of ``checks_done`` so they
        # see the same pre-increment generation as PostgreSQL and SQLite.
        .ordered_values(
            (
                MonitorSession.status,
                case(
                    (reaches_limit, "completed"),
                    else_=MonitorSession.status,
                ),
            ),
            (
                MonitorSession.completed_at,
                case(
                    (reaches_limit, completed_at),
                    else_=MonitorSession.completed_at,
                ),
            ),
            (MonitorSession.checks_done, next_check),
            (MonitorSession.last_summary, body.summary),
        )
    )
    if not advanced.rowcount:
        await db.rollback()
        await _monitor_session_or_error(db, task_id, session_id)

    state = (
        await db.execute(
            select(
                MonitorSession.checks_done,
                MonitorSession.max_checks,
                MonitorSession.status,
            )
            .where(
                MonitorSession.id == session_id,
                MonitorSession.task_id == task_id,
            )
            .with_for_update()
        )
    ).one()
    new_checks_done, max_checks, persisted_status = state
    auto_complete = persisted_status == "completed"

    check = MonitorCheck(
        monitor_session_id=session_id,
        check_number=new_checks_done,
        status=body.status,
        summary=body.summary,
    )
    db.add(check)

    chat_injected = False
    if body.is_important and not auto_complete:
        monitor_log = LogEntry(
            instance_id=1,
            task_id=task_id,
            event_type="system_event",
            role="system",
            content=f"[Monitor #{session_id}] Check #{new_checks_done}: {body.summary}",
            raw_json=_json.dumps({"source": "monitor", "monitor_session_id": session_id,
                                  "check_number": new_checks_done, "is_important": body.is_important}),
            is_error=False,
        )
        db.add(monitor_log)

    if auto_complete:
        complete_log = LogEntry(
            instance_id=1,
            task_id=task_id,
            event_type="system_event",
            role="system",
            content=f"[Monitor #{session_id}] 监控完成: {body.summary}",
            raw_json=_json.dumps({"source": "monitor", "monitor_session_id": session_id,
                                  "check_number": new_checks_done, "is_important": True}),
            is_error=False,
        )
        db.add(complete_log)

    await db.commit()
    await db.refresh(check)

    if body.is_important and not auto_complete:
        from backend.services.dispatcher import PRIORITY_MONITOR_IMPORTANT
        report_prompt = (
            f"[Monitor #{session_id} 汇报] {body.summary}\n\n"
            "请向用户简要转达这个监控结果。"
        )
        await dispatcher.enqueue_message(
            task_id=task_id,
            prompt=report_prompt,
            priority=PRIORITY_MONITOR_IMPORTANT,
            source="monitor:report",
            user_message_text=f"[Monitor #{session_id}] {body.summary}",
            monitor_session_id=session_id,
        )
        chat_injected = True

    await dispatcher.broadcaster.broadcast(
        f"task:{task_id}",
        {
            "event": "monitor_check",
            "monitor_session_id": session_id,
            "check_number": new_checks_done,
            "status": body.status,
            "summary": body.summary,
            "is_important": body.is_important,
            "chat_injected": chat_injected,
            "source": "monitor",
        },
    )

    if auto_complete:
        await dispatcher.broadcaster.broadcast(
            f"task:{task_id}",
            {
                "event": "monitor_session_status",
                "monitor_session_id": session_id,
                "status": "completed",
            },
        )
        from backend.services.dispatcher import PRIORITY_MONITOR_COMPLETE

        complete_prompt = (
            f"[Monitor #{session_id} 完成] 已达最大检查次数"
            f"（{max_checks}次）。最后状态: {body.summary}\n\n"
            "请向用户简要转达监控结果。"
        )
        await dispatcher.enqueue_message(
            task_id=task_id,
            prompt=complete_prompt,
            priority=PRIORITY_MONITOR_COMPLETE,
            source="monitor:complete",
            user_message_text=f"[Monitor #{session_id}] 监控完成: {body.summary}",
            monitor_session_id=session_id,
        )
        # Kill the sub-agent process since it's no longer needed
        await dispatcher.stop_monitor_session_process(session_id)

    return check


@router.post("/{session_id}/complete")
async def complete_monitor_session(
    task_id: int,
    session_id: int,
    body: MonitorCompleteRequest,
    db: AsyncSession = Depends(get_db),
):
    """Sub-agent marks itself as complete."""
    completed = await db.execute(
        update(MonitorSession)
        .where(
            MonitorSession.id == session_id,
            MonitorSession.task_id == task_id,
            MonitorSession.agent_type == "monitor",
            MonitorSession.source == "ccm",
            MonitorSession.status == "running",
        )
        .values(
            status="completed",
            completed_at=datetime.utcnow(),
            last_summary=body.reason,
            checks_done=MonitorSession.checks_done + 1,
        )
    )
    if not completed.rowcount:
        await db.rollback()
        await _monitor_session_or_error(db, task_id, session_id)
    checks_done = await db.scalar(
        select(MonitorSession.checks_done)
        .where(
            MonitorSession.id == session_id,
            MonitorSession.task_id == task_id,
        )
        .with_for_update()
    )
    check = MonitorCheck(
        monitor_session_id=session_id,
        check_number=checks_done,
        status="completed",
        summary=body.reason,
    )
    db.add(check)
    await db.commit()

    from backend.main import dispatcher
    import json as _json

    chat_injected = False

    await dispatcher.broadcaster.broadcast(
        f"task:{task_id}",
        {
            "event": "monitor_check",
            "monitor_session_id": session_id,
            "check_number": checks_done,
            "status": "completed",
            "summary": body.reason,
            "is_important": False,
            "chat_injected": False,
            "source": "monitor",
        },
    )
    await dispatcher.broadcaster.broadcast(
        f"task:{task_id}",
        {"event": "monitor_session_status", "monitor_session_id": session_id, "status": "completed"},
    )

    # Check if the last report_status already notified the main agent
    # (is_important=True). Only skip if the MOST RECENT check was important,
    # not any historical one.
    from backend.models.log_entry import LogEntry
    last_report_log = await db.scalar(
        select(LogEntry.raw_json)
        .where(
            LogEntry.task_id == task_id,
            LogEntry.event_type == "system_event",
            LogEntry.raw_json.like(f'%"monitor_session_id": {session_id}%'),
            LogEntry.raw_json.like('%"check_number"%'),
        )
        .order_by(LogEntry.id.desc())
    )
    already_notified = False
    if last_report_log:
        try:
            already_notified = _json.loads(last_report_log).get("is_important", False)
        except (ValueError, TypeError):
            pass
    if not already_notified:
        from backend.services.dispatcher import PRIORITY_MONITOR_COMPLETE
        complete_log = LogEntry(
            instance_id=1,
            task_id=task_id,
            event_type="system_event",
            role="system",
            content=f"[Monitor #{session_id}] 监控完成: {body.reason}",
            raw_json=_json.dumps({"source": "monitor", "monitor_session_id": session_id,
                                  "check_number": checks_done, "is_important": True}),
            is_error=False,
        )
        db.add(complete_log)
        await db.commit()

        complete_prompt = (
            f"[Monitor #{session_id} 完成] {body.reason}\n\n"
            "请向用户简要转达监控结果。"
        )
        await dispatcher.enqueue_message(
            task_id=task_id,
            prompt=complete_prompt,
            priority=PRIORITY_MONITOR_COMPLETE,
            source="monitor:complete",
            user_message_text=f"[Monitor #{session_id}] 监控完成: {body.reason}",
            monitor_session_id=session_id,
        )

    return {"ok": True, "message": "Session completed. Your task is done — stop all activity now."}
