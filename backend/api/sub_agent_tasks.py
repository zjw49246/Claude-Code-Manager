"""Sub-Agent session CRUD + progress/result endpoints.

Parallel to backend/api/monitor.py but for one-shot sub-agent tasks
(agent_type="sub_agent").
"""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database import get_db
from backend.models.task import Task
from backend.models.sub_agent import SubAgentSession, SubAgentReport
from backend.models.project import Project

router = APIRouter(prefix="/api/tasks/{task_id}/sub-agent-sessions", tags=["sub-agent-tasks"])

MAX_SUB_AGENTS_PER_TASK = 3


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
    status: str = "completed"


# ---- Endpoints ----

@router.post("", response_model=SubAgentSessionResponse, status_code=201)
async def create_sub_agent_session(
    task_id: int,
    body: SubAgentSessionCreate,
    db: AsyncSession = Depends(get_db),
):
    """Create a sub-agent session and start its subprocess via dispatcher."""
    task = await db.get(Task, task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    skills = task.enabled_skills or {}
    if not skills.get("sub-agent"):
        raise HTTPException(403, "Sub-Agent skill not enabled for this task")
    if task.status not in ("in_progress", "executing"):
        raise HTTPException(400, "Cannot create sub-agent for inactive task")

    running_count = await db.scalar(
        select(func.count(SubAgentSession.id)).where(
            SubAgentSession.task_id == task_id,
            SubAgentSession.agent_type == "sub_agent",
            SubAgentSession.status == "running",
        )
    )
    if running_count >= MAX_SUB_AGENTS_PER_TASK:
        raise HTTPException(
            429,
            f"Too many running sub-agents ({running_count}/{MAX_SUB_AGENTS_PER_TASK}). "
            "Stop an existing one first.",
        )

    session = SubAgentSession(
        task_id=task_id,
        agent_type="sub_agent",
        source="ccm",
        description=body.name,
        monitor_context=body.context or None,
        # Sub-agents don't use interval/max_checks but we store the prompt
        # in monitor_context for retrieval via get_context endpoint.
        interval=0,
        max_checks=0,
        model=body.model,
    )
    # Store original prompt in last_summary temporarily (will be overwritten by progress)
    session.last_summary = body.prompt
    db.add(session)
    await db.commit()
    await db.refresh(session)

    from backend.main import dispatcher
    dispatcher.start_sub_agent_session(session)

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

    if sa.status == "running":
        sa.status = "stopped"
        sa.completed_at = datetime.utcnow()
        await db.commit()

    from backend.main import dispatcher
    atask = dispatcher._sub_agent_tasks.get(session_id)
    if atask and not atask.done():
        atask.cancel()
    proc = dispatcher._sub_agent_processes.get(session_id)
    if proc and proc.returncode is None:
        proc.kill()
        await proc.wait()

    from backend.services.mcp_config import cleanup_sub_agent_mcp_config
    cleanup_sub_agent_mcp_config(session_id)

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
    sa = await db.get(SubAgentSession, session_id)
    if not sa or sa.task_id != task_id:
        raise HTTPException(404, "Sub-agent session not found")
    if sa.status != "running":
        raise HTTPException(400, "Sub-agent session is not running")

    sa.checks_done += 1
    sa.last_summary = body.summary
    progress_count = sa.checks_done

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
        content=f"[Sub-Agent #{session_id}: {sa.description}] {body.summary}",
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
            "description": sa.description,
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
    sa = await db.get(SubAgentSession, session_id)
    if not sa or sa.task_id != task_id:
        raise HTTPException(404, "Sub-agent session not found")
    if sa.status != "running":
        raise HTTPException(400, "Sub-agent session is not running")

    sa.status = body.status
    sa.completed_at = datetime.utcnow()
    sa.last_summary = body.result[:500] if body.result else None

    # Store result as a final report
    sa.checks_done += 1
    report = SubAgentReport(
        session_id=session_id,
        check_number=sa.checks_done,
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
    result_text = f"[Sub-Agent: {sa.description}] 任务{'完成' if body.status == 'completed' else '失败'}\n\n{body.result}"
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
    proc = dispatcher._sub_agent_processes.get(session_id)
    if proc and proc.returncode is None:
        proc.kill()
        await proc.wait()

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
