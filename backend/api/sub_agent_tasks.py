"""Sub-Agent session CRUD + progress/result endpoints.

Parallel to backend/api/monitor.py but for one-shot sub-agent tasks
(agent_type="sub_agent").
"""
import asyncio
from weakref import WeakValueDictionary
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, func, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database import get_db
from backend.models.task import Task
from backend.models.sub_agent import SubAgentSession, SubAgentReport
from backend.models.project import Project

router = APIRouter(prefix="/api/tasks/{task_id}/sub-agent-sessions", tags=["sub-agent-tasks"])

MAX_SUB_AGENTS_PER_TASK = 3
_sub_agent_admission_locks: WeakValueDictionary[int, asyncio.Lock] = (
    WeakValueDictionary()
)


def _sub_agent_admission_lock(task_id: int) -> asyncio.Lock:
    lock = _sub_agent_admission_locks.get(task_id)
    if lock is None:
        lock = asyncio.Lock()
        _sub_agent_admission_locks[task_id] = lock
    return lock


async def _settle_shielded(operation: asyncio.Task) -> asyncio.CancelledError | None:
    delayed_cancellation: asyncio.CancelledError | None = None
    while not operation.done():
        try:
            await asyncio.shield(operation)
        except asyncio.CancelledError as exc:
            delayed_cancellation = exc
        except BaseException:
            break
    return delayed_cancellation


async def _mark_sub_agent_admission_failed(
    db: AsyncSession,
    task_id: int,
    session_id: int,
) -> None:
    await db.execute(
        update(SubAgentSession)
        .where(
            SubAgentSession.id == session_id,
            SubAgentSession.task_id == task_id,
            SubAgentSession.agent_type == "sub_agent",
            SubAgentSession.source == "ccm",
            SubAgentSession.status == "running",
        )
        .values(status="failed", completed_at=datetime.utcnow())
    )
    await db.commit()


async def _commit_and_admit_sub_agent(
    db: AsyncSession,
    task_id: int,
    session: SubAgentSession,
    dispatcher,
) -> None:
    committed = False

    async def commit_and_start() -> None:
        nonlocal committed
        await db.commit()
        committed = True
        dispatcher.start_sub_agent_session(session)

    operation = asyncio.create_task(commit_and_start())
    delayed_cancellation = await _settle_shielded(operation)
    try:
        operation.result()
    except Exception as exc:
        if committed:
            cleanup = asyncio.create_task(
                _mark_sub_agent_admission_failed(db, task_id, session.id)
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


async def _sub_agent_session_or_error(
    db: AsyncSession,
    task_id: int,
    session_id: int,
) -> SubAgentSession:
    db.expire_all()
    session = await db.get(SubAgentSession, session_id)
    if session is None or session.task_id != task_id:
        raise HTTPException(404, "Sub-agent session not found")
    raise HTTPException(400, "Sub-agent session is not running")


# ---- Pydantic schemas ----

class SubAgentSessionCreate(BaseModel):
    name: str
    prompt: str
    context: str = ""
    model: str | None = None


class SubAgentSessionResponse(BaseModel):
    id: int
    task_id: int
    agent_type: str
    source: str
    description: str
    monitor_context: str | None
    status: str
    checks_done: int
    last_summary: str | None
    created_at: datetime
    completed_at: datetime | None

    model_config = {"from_attributes": True}


class SubAgentProgressRequest(BaseModel):
    summary: str


class SubAgentResultRequest(BaseModel):
    result: str
    status: Literal["completed", "failed"] = "completed"


# ---- Endpoints ----

@router.post("", response_model=SubAgentSessionResponse, status_code=201)
async def create_sub_agent_session(
    task_id: int,
    body: SubAgentSessionCreate,
    db: AsyncSession = Depends(get_db),
):
    """Create a sub-agent session and start its subprocess via dispatcher."""
    # Route remote ownership before taking the local Task write barrier. The
    # proxy is a network await and must not retain a Manager DB transaction.
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
            f"/api/tasks/{task_id}/sub-agent-sessions",
            body=body.model_dump(),
        )
    await db.rollback()

    async with _sub_agent_admission_lock(task_id):
        try:
            # The keyed lock makes SQLite cap admission deterministic in one
            # CCM process; this Task write barrier additionally serializes
            # cancellation and other backend processes.
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
                        f"/api/tasks/{task_id}/sub-agent-sessions",
                        body=body.model_dump(),
                    )
                raise HTTPException(
                    400,
                    "Cannot create sub-agent for inactive task",
                )
            db.expire_all()
            task = await db.get(Task, task_id)
            if task is None:
                raise HTTPException(404, "Task not found")
            # Sub-agents are currently hard-wired to Claude CLI.
            if (task.provider or "claude").lower() != "claude":
                raise HTTPException(
                    400,
                    "Sub-agents are claude-only; this task runs on "
                    f"provider '{task.provider}'",
                )
            skills = task.enabled_skills or {}
            if not skills.get("sub-agent"):
                raise HTTPException(
                    403,
                    "Sub-Agent skill not enabled for this task",
                )

            running_count = await db.scalar(
                select(func.count(SubAgentSession.id)).where(
                    SubAgentSession.task_id == task_id,
                    SubAgentSession.agent_type == "sub_agent",
                    SubAgentSession.source == "ccm",
                    SubAgentSession.status == "running",
                )
            )
            if running_count >= MAX_SUB_AGENTS_PER_TASK:
                raise HTTPException(
                    429,
                    "Too many running sub-agents "
                    f"({running_count}/{MAX_SUB_AGENTS_PER_TASK}). "
                    "Stop an existing one first.",
                )

            from backend.main import dispatcher
            if getattr(dispatcher, "_shutting_down", False) is True:
                raise HTTPException(503, "Dispatcher is shutting down")

            session = SubAgentSession(
                task_id=task_id,
                agent_type="sub_agent",
                source="ccm",
                description=body.name,
                monitor_context=body.context or None,
                interval=0,
                max_checks=0,
                model=body.model,
                last_summary=body.prompt,
            )
            db.add(session)
            await db.flush()
            await _commit_and_admit_sub_agent(
                db,
                task_id,
                session,
                dispatcher,
            )
        except BaseException:
            if db.in_transaction():
                await db.rollback()
            raise

    await dispatcher.broadcaster.broadcast(
        f"task:{task_id}",
        {
            "event": "sub_agent_session_created",
            "sub_agent_session_id": session.id,
            "description": session.description,
            "agent_type": "sub_agent",
        },
    )

    return session


@router.get("", response_model=list[SubAgentSessionResponse])
async def list_sub_agent_sessions(
    task_id: int,
    agent_type: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    """List sub-agent sessions, optionally filtered by agent_type."""
    stmt = (
        select(SubAgentSession)
        .where(SubAgentSession.task_id == task_id)
    )
    if agent_type:
        stmt = stmt.where(SubAgentSession.agent_type == agent_type)
    else:
        stmt = stmt.where(SubAgentSession.agent_type == "sub_agent")
    stmt = stmt.order_by(SubAgentSession.created_at.desc())
    result = await db.execute(stmt)
    return list(result.scalars().all())


@router.get("/{session_id}", response_model=SubAgentSessionResponse)
async def get_sub_agent_session(
    task_id: int,
    session_id: int,
    db: AsyncSession = Depends(get_db),
):
    sa = await db.get(SubAgentSession, session_id)
    if not sa or sa.task_id != task_id:
        raise HTTPException(404, "Sub-agent session not found")
    return sa


@router.delete("/{session_id}")
async def delete_sub_agent_session(
    task_id: int,
    session_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Stop/cancel a running sub-agent."""
    sa = await db.get(SubAgentSession, session_id)
    if not sa or sa.task_id != task_id:
        raise HTTPException(404, "Sub-agent session not found")

    transitioned = await db.execute(
        update(SubAgentSession)
        .where(
            SubAgentSession.id == session_id,
            SubAgentSession.task_id == task_id,
            SubAgentSession.agent_type == "sub_agent",
            SubAgentSession.source == "ccm",
            SubAgentSession.status == "running",
        )
        .values(status="stopped", completed_at=datetime.utcnow())
    )
    await db.commit()

    from backend.main import dispatcher
    await dispatcher.stop_sub_agent_session_process(session_id)

    from backend.services.mcp_config import cleanup_sub_agent_mcp_config
    cleanup_sub_agent_mcp_config(session_id)

    if transitioned.rowcount:
        await dispatcher.broadcaster.broadcast(
            f"task:{task_id}",
            {
                "event": "sub_agent_session_status",
                "sub_agent_session_id": session_id,
                "status": "stopped",
            },
        )

    return {"ok": True}


@router.post("/{session_id}/progress")
async def sub_agent_report_progress(
    task_id: int,
    session_id: int,
    body: SubAgentProgressRequest,
    db: AsyncSession = Depends(get_db),
):
    """Sub-agent reports progress via MCP tool."""
    advanced = await db.execute(
        update(SubAgentSession)
        .where(
            SubAgentSession.id == session_id,
            SubAgentSession.task_id == task_id,
            SubAgentSession.agent_type == "sub_agent",
            SubAgentSession.source == "ccm",
            SubAgentSession.status == "running",
        )
        .values(
            checks_done=SubAgentSession.checks_done + 1,
            last_summary=body.summary,
        )
    )
    if not advanced.rowcount:
        await db.rollback()
        await _sub_agent_session_or_error(db, task_id, session_id)
    state = (
        await db.execute(
            select(
                SubAgentSession.description,
                SubAgentSession.checks_done,
            )
            .where(
                SubAgentSession.id == session_id,
                SubAgentSession.task_id == task_id,
            )
            .with_for_update()
        )
    ).one()
    description, progress_count = state

    report = SubAgentReport(
        session_id=session_id,
        check_number=progress_count,
        status="progress",
        summary=body.summary,
    )
    db.add(report)

    # Write system_event log for progress
    from backend.models.log_entry import LogEntry
    import json as _json
    log_entry = LogEntry(
        instance_id=1,
        task_id=task_id,
        event_type="system_event",
        role="system",
        content=f"[Sub-Agent #{session_id}: {description}] {body.summary}",
        raw_json=_json.dumps({"source": "sub-agent", "sub_agent_session_id": session_id,
                              "progress_count": progress_count}),
        is_error=False,
    )
    db.add(log_entry)
    await db.commit()

    from backend.main import dispatcher
    await dispatcher.broadcaster.broadcast(
        f"task:{task_id}",
        {
            "event": "sub_agent_progress",
            "sub_agent_session_id": session_id,
            "progress_count": progress_count,
            "summary": body.summary,
            "description": description,
            "source": "sub-agent",
        },
    )

    return {"ok": True, "progress_count": progress_count}


@router.post("/{session_id}/result")
async def sub_agent_submit_result(
    task_id: int,
    session_id: int,
    body: SubAgentResultRequest,
    db: AsyncSession = Depends(get_db),
):
    """Sub-agent submits final result and marks completed."""
    completed = await db.execute(
        update(SubAgentSession)
        .where(
            SubAgentSession.id == session_id,
            SubAgentSession.task_id == task_id,
            SubAgentSession.agent_type == "sub_agent",
            SubAgentSession.source == "ccm",
            SubAgentSession.status == "running",
        )
        .values(
            status=body.status,
            completed_at=datetime.utcnow(),
            last_summary=body.result[:500] if body.result else None,
            checks_done=SubAgentSession.checks_done + 1,
        )
    )
    if not completed.rowcount:
        await db.rollback()
        await _sub_agent_session_or_error(db, task_id, session_id)
    state = (
        await db.execute(
            select(
                SubAgentSession.description,
                SubAgentSession.checks_done,
            )
            .where(
                SubAgentSession.id == session_id,
                SubAgentSession.task_id == task_id,
            )
            .with_for_update()
        )
    ).one()
    description, checks_done = state

    report = SubAgentReport(
        session_id=session_id,
        check_number=checks_done,
        status=body.status,
        summary=body.result,
    )
    db.add(report)
    await db.commit()

    from backend.main import dispatcher

    # Broadcast completion event (panel update only, no chat insert)
    await dispatcher.broadcaster.broadcast(
        f"task:{task_id}",
        {
            "event": "sub_agent_session_status",
            "sub_agent_session_id": session_id,
            "status": body.status,
        },
    )

    # Enqueue result into main session as user_message
    result_text = (
        f"[Sub-Agent: {description}] "
        f"任务{'完成' if body.status == 'completed' else '失败'}"
        f"\n\n{body.result}"
    )
    from backend.services.dispatcher import PRIORITY_MONITOR_COMPLETE
    await dispatcher.enqueue_message(
        task_id=task_id,
        prompt=result_text,
        priority=PRIORITY_MONITOR_COMPLETE,
        source="sub-agent:result",
        user_message_text=result_text,
        monitor_session_id=session_id,
    )

    # Kill the subprocess since it's done
    await dispatcher.stop_sub_agent_session_process(session_id)

    return {"ok": True, "status": body.status}


@router.get("/{session_id}/context")
async def get_sub_agent_context(
    task_id: int,
    session_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Get task context for a sub-agent."""
    sa = await db.get(SubAgentSession, session_id)
    if not sa or sa.task_id != task_id:
        raise HTTPException(404, "Sub-agent session not found")

    task = await db.get(Task, task_id)
    context: dict = {
        "task_description": task.description if task else "",
        "task_prompt": task.prompt if task else "",
        "sub_agent_prompt": sa.last_summary or "",
        "sub_agent_context": sa.monitor_context or "",
    }
    if task and task.project_id:
        project = await db.get(Project, task.project_id)
        if project:
            context["project_name"] = project.name
            context["project_path"] = project.local_path or ""

    return context
