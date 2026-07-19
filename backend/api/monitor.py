from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, func
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


@router.post("", response_model=MonitorSessionResponse)
async def create_monitor_session(
    task_id: int,
    body: MonitorSessionCreate,
    db: AsyncSession = Depends(get_db),
):
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
        return await worker_proxy.proxy_to_worker(
            task, "POST", f"/api/tasks/{task_id}/monitor-sessions",
            body=body.model_dump(),
        )
    # Monitor 子 agent 硬编码跑 claude CLI（_launch_monitor_agent），对 codex
    # 任务放行会静默起一个 Claude 子进程——显式拒绝，别让它悄悄跑错 provider
    if (task.provider or "claude").lower() != "claude":
        raise HTTPException(
            400, "Monitor sub-agents are claude-only; this task runs on "
                 f"provider '{task.provider}'"
        )
    skills = task.enabled_skills or {}
    if not skills.get("monitor"):
        raise HTTPException(403, "Monitor skill not enabled for this task")
    if task.status not in ("in_progress", "executing"):
        raise HTTPException(400, "Cannot create monitor for inactive task")

    active_count = await db.scalar(
        select(func.count(MonitorSession.id))
        .where(MonitorSession.task_id == task_id, MonitorSession.status == "running")
    )
    if active_count >= MAX_CONCURRENT_MONITORS:
        raise HTTPException(
            429,
            f"Too many active monitors ({active_count}/{MAX_CONCURRENT_MONITORS}). "
            "Stop an existing monitor first.",
        )

    ms = MonitorSession(
        task_id=task_id,
        description=body.description,
        monitor_context=body.monitor_context,
        interval=body.interval,
        max_checks=body.max_checks,
        model=body.model,
    )
    db.add(ms)
    await db.commit()
    await db.refresh(ms)

    from backend.main import dispatcher
    dispatcher.start_monitor_session(ms)

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
        if ms.status == "running":
            ms.status = "cancelled"
            ms.completed_at = datetime.utcnow()
            await db.commit()
        return result

    if ms.status == "running":
        ms.status = "cancelled"
        ms.completed_at = datetime.utcnow()
        await db.commit()

    from backend.main import dispatcher
    atask = dispatcher._monitor_tasks.get(session_id)
    if atask and not atask.done():
        atask.cancel()
    proc = dispatcher._monitor_processes.get(session_id)
    if proc and proc.returncode is None:
        proc.kill()
        await proc.wait()

    from backend.services.mcp_config import cleanup_monitor_agent_mcp_config
    cleanup_monitor_agent_mcp_config(session_id)

    await dispatcher.broadcaster.broadcast(
        f"task:{task_id}",
        {"event": "monitor_session_status", "monitor_session_id": session_id, "status": "cancelled"},
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
    ms = await db.get(MonitorSession, session_id)
    if not ms or ms.task_id != task_id:
        raise HTTPException(404, "Monitor session not found")
    if ms.status != "running":
        raise HTTPException(400, "Monitor session is not running")

    ms.checks_done += 1
    ms.last_summary = body.summary
    new_checks_done = ms.checks_done

    auto_complete = new_checks_done >= ms.max_checks

    check = MonitorCheck(
        monitor_session_id=session_id,
        check_number=new_checks_done,
        status=body.status,
        summary=body.summary,
    )
    db.add(check)

    if auto_complete:
        ms.status = "completed"
        ms.completed_at = datetime.utcnow()

    await db.commit()
    await db.refresh(check)

    from backend.main import dispatcher
    import json as _json

    chat_injected = False

    # Only persist LogEntry and enqueue for important checks (dedup: avoid
    # triple-rendering of the same info as card + bubble + agent reply).
    if body.is_important and not auto_complete:
        from backend.models.log_entry import LogEntry
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
        await db.commit()

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

    # Always broadcast monitor_check for panel updates; chat_injected tells
    # frontend whether a user_message will also arrive (so it can skip
    # inserting a duplicate card into the chat flow).
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
            {"event": "monitor_session_status", "monitor_session_id": session_id, "status": "completed"},
        )
        # Notify main agent that monitoring is complete
        from backend.services.dispatcher import PRIORITY_MONITOR_COMPLETE
        from backend.models.log_entry import LogEntry
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

        complete_prompt = (
            f"[Monitor #{session_id} 完成] 已达最大检查次数（{ms.max_checks}次）。最后状态: {body.summary}\n\n"
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
        sub_proc = dispatcher._monitor_processes.get(session_id)
        if sub_proc and sub_proc.returncode is None:
            sub_proc.kill()
            await sub_proc.wait()

    return check


@router.post("/{session_id}/complete")
async def complete_monitor_session(
    task_id: int,
    session_id: int,
    body: MonitorCompleteRequest,
    db: AsyncSession = Depends(get_db),
):
    """Sub-agent marks itself as complete."""
    ms = await db.get(MonitorSession, session_id)
    if not ms or ms.task_id != task_id:
        raise HTTPException(404, "Monitor session not found")
    if ms.status != "running":
        raise HTTPException(400, "Monitor session is not running")

    ms.status = "completed"
    ms.completed_at = datetime.utcnow()
    ms.last_summary = body.reason
    ms.checks_done += 1

    check = MonitorCheck(
        monitor_session_id=session_id,
        check_number=ms.checks_done,
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
            "check_number": ms.checks_done,
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
                                  "check_number": ms.checks_done, "is_important": True}),
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
