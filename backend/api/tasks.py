import os
import shutil
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database import get_db
from backend.models.task import Task
from backend.models.instance import Instance
from backend.schemas.task import TaskCreate, TaskUpdate, TaskResponse
from backend.services.task_queue import TaskQueue
from backend.api.deps import get_current_user_id, get_current_user_role, require_task_access, require_admin

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


def _find_session_jsonl(session_id: str) -> Path | None:
    """Locate a session JSONL on disk, pool-aware.

    Pool deployments split sessions across multiple ~/.claude-account-N dirs,
    so a lookup that only checks ~/.claude / CLAUDE_CONFIG_DIR (and only the
    exact last_cwd-encoded project subdir) misses sessions created under a pool
    account and silently degrades recovery to a lossy summary (prod task #725).
    We reuse the pool's own locator (searches every account dir) and glob across
    all project subdirs so cwd-encoding differences don't hide the file either.
    """
    config_dir: str | None = None
    try:
        from backend.main import dispatcher
        if dispatcher and dispatcher.pool:
            config_dir = dispatcher.pool.locate_session_config_dir(session_id)
    except Exception:
        config_dir = None
    # Try pool locator result first, then env CLAUDE_CONFIG_DIR, then default
    dirs_to_check = []
    if config_dir:
        dirs_to_check.append(config_dir)
    env_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if env_dir and env_dir not in dirs_to_check:
        dirs_to_check.append(env_dir)
    default_dir = os.path.expanduser("~/.claude")
    if default_dir not in dirs_to_check:
        dirs_to_check.append(default_dir)
    for d in dirs_to_check:
        try:
            match = next(Path(d).glob(f"projects/*/{session_id}.jsonl"), None)
            if match:
                return match
        except OSError:
            pass
    # Fallback: scan all ~/.claude* dirs on disk. Covers accounts that were
    # removed from the pool but whose config dirs still exist on disk.
    home = Path.home()
    try:
        for d in sorted(home.iterdir()):
            if not d.name.startswith(".claude") or not d.is_dir():
                continue
            try:
                match = next(d.glob(f"projects/*/{session_id}.jsonl"), None)
                if match:
                    return match
            except OSError:
                continue
    except OSError:
        pass
    return None


async def _clone_session(source_task_id: int, db: AsyncSession) -> dict | None:
    """Clone a Claude Code session file from a source task, returning new session_id and last_cwd."""
    source = await db.get(Task, source_task_id)
    if not source or not source.session_id or not source.last_cwd:
        return None

    source_jsonl = _find_session_jsonl(source.session_id)
    if source_jsonl is None:
        return None

    new_session_id = str(uuid.uuid4())
    dest_jsonl = source_jsonl.parent / f"{new_session_id}.jsonl"
    shutil.copy2(source_jsonl, dest_jsonl)

    return {"session_id": new_session_id, "last_cwd": source.last_cwd}


def _get_queue(db: AsyncSession = Depends(get_db)) -> TaskQueue:
    return TaskQueue(db)


@router.get("/count")
async def count_tasks(
    request: Request,
    status: str | None = None,
    include_archived: bool = False,
    archived_only: bool = False,
    project_id: int | None = None,
    starred: bool | None = None,
    has_unread: bool | None = None,
    queue: TaskQueue = Depends(_get_queue),
):
    user_id = get_current_user_id(request)
    user_role = get_current_user_role(request)
    total = await queue.count_tasks(
        status=status, include_archived=include_archived,
        archived_only=archived_only,
        project_id=project_id, starred=starred,
        has_unread=has_unread,
        user_id=user_id if user_role not in ("admin", "super_admin") else None,
    )
    return {"total": total}


@router.get("", response_model=list[TaskResponse])
async def list_tasks(
    request: Request,
    status: str | None = None,
    include_archived: bool = False,
    archived_only: bool = False,
    project_id: int | None = None,
    starred: bool | None = None,
    has_unread: bool | None = None,
    limit: int = 50,
    offset: int = 0,
    queue: TaskQueue = Depends(_get_queue),
):
    user_id = get_current_user_id(request)
    user_role = get_current_user_role(request)
    return await queue.list_tasks(
        status=status, include_archived=include_archived,
        archived_only=archived_only,
        project_id=project_id, starred=starred,
        has_unread=has_unread,
        limit=limit, offset=offset,
        user_id=user_id if user_role not in ("admin", "super_admin") else None,
    )


@router.post("", response_model=TaskResponse, status_code=201)
async def create_task(request: Request, body: TaskCreate, queue: TaskQueue = Depends(_get_queue), db: AsyncSession = Depends(get_db)):
    user_id = get_current_user_id(request)
    user_role = get_current_user_role(request)
    if user_role not in ("admin", "super_admin") and user_id:
        from backend.models.worker import Worker
        from backend.models.team_share import TeamProjectShare
        has_worker = (await db.execute(
            select(Worker.id).where(Worker.owner_user_id == user_id).limit(1)
        )).scalar_one_or_none()
        project_id = body.project_id if hasattr(body, 'project_id') else None
        has_project = False
        if project_id:
            has_project = (await db.execute(
                select(TeamProjectShare.id).where(
                    TeamProjectShare.project_id == project_id,
                    TeamProjectShare.target_type == "user",
                    TeamProjectShare.target_id == user_id,
                ).limit(1)
            )).scalar_one_or_none() is not None
        if not has_worker and not has_project:
            raise HTTPException(403, "You need a Worker or Project access to create Tasks")
    data = body.model_dump()
    data["created_by"] = user_id
    if data.get("id") is None:
        data.pop("id", None)  # 未指定 → 正常自增；指定 → 用 Manager 分配的全局 ID
    image_paths = data.pop("image_paths", None)
    file_paths = data.pop("file_paths", None)
    attachments = data.pop("attachments", None)
    secret_ids = data.pop("secret_ids", None)
    clone_from_task_id = data.pop("clone_from_task_id", None)
    meta = data.get("metadata_") or {}
    all_paths = file_paths or image_paths
    if all_paths:
        meta["image_paths"] = all_paths
    if attachments:
        meta["attachments"] = attachments
    if secret_ids:
        meta["secret_ids"] = secret_ids
    if meta:
        data["metadata_"] = meta

    if clone_from_task_id:
        cloned = await _clone_session(clone_from_task_id, db)
        if cloned:
            data["session_id"] = cloned["session_id"]
            data["last_cwd"] = cloned["last_cwd"]

    # 设置归 Task：创建时填入全局默认值，后续不再依赖 instance fallback
    from backend.config import settings as app_settings
    if not data.get("model"):
        data["model"] = (
            app_settings.default_codex_model
            if data.get("provider") == "codex"
            else app_settings.default_model
        )
    if not data.get("effort_level"):
        data["effort_level"] = app_settings.default_effort

    task = await queue.create(**data)

    # Auto-share if project has active project-level shares
    if task.project_id:
        try:
            from backend.services.task_sharing import auto_share_new_task
            await auto_share_new_task(db, task.id, task.project_id)
        except Exception:
            pass  # best-effort

    return task


@router.get("/{task_id}", response_model=TaskResponse)
async def get_task(task_id: int, queue: TaskQueue = Depends(_get_queue)):
    task = await queue.get(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    return task


@router.put("/{task_id}", response_model=TaskResponse)
async def update_task(
    task_id: int, body: TaskUpdate, request: Request, queue: TaskQueue = Depends(_get_queue)
):
    # Permission: only creator or admin can modify task config
    user_id = get_current_user_id(request)
    user_role = get_current_user_role(request)
    if user_role not in ("admin", "super_admin"):
        task = await queue.get(task_id)
        if task and task.created_by != user_id:
            raise HTTPException(403, "Only the task creator or admin can modify task config")
    updates = body.model_dump(exclude_unset=True)

    # 执行位置切换走 TaskMigrator（同 mode/model 一样在 task 详情改，
    # 但语义是迁移而非改字段）。-1 = 切回本机
    if "worker_id" in updates:
        target = updates.pop("worker_id")
        if target == -1:
            target = None
        task = await queue.get(task_id)
        if not task:
            raise HTTPException(404, "Task not found")
        if task.worker_id != target:
            from backend.main import task_migrator
            if task_migrator is None:
                raise HTTPException(503, "Worker 功能未启用")
            from backend.services.task_migrator import MigrationError
            try:
                # 同步执行：迁移结束后才返回，前端拿到的就是最终状态。
                # 大工作目录会久——前端按钮置灰 + migrating 状态广播兜底
                await task_migrator.migrate(task_id, target)
            except MigrationError as e:
                raise HTTPException(409, str(e))
            # migrate 在独立 session 写库；当前 DI session 的 identity map
            # 还缓存着旧 worker_id，必须 expire 否则响应返回迁移前的值
            queue.db.expire_all()

    if not updates:
        task = await queue.get(task_id)
        if not task:
            raise HTTPException(404, "Task not found")
        return task
    task = await queue.update_task(task_id, **updates)
    if not task:
        raise HTTPException(404, "Task not found")
    return task


async def _stop_task_process(task_id: int, db: AsyncSession) -> bool:
    """Stop the running Claude Code process for a task, if any. Returns True if stopped."""
    from backend.main import instance_manager
    for inst_id, proc in list(instance_manager.processes.items()):
        if proc.returncode is not None:
            continue
        inst = await db.get(Instance, inst_id)
        if inst and inst.current_task_id == task_id:
            await instance_manager.stop(inst_id)
            return True
    task = await db.get(Task, task_id)
    if task and task.instance_id:
        return await instance_manager.stop(task.instance_id)
    return False


@router.delete("/{task_id}")
async def delete_task(task_id: int, request: Request, queue: TaskQueue = Depends(_get_queue), db: AsyncSession = Depends(get_db)):
    task = await db.get(Task, task_id)
    if task:
        await require_task_access(request, task, db)
    ok = await queue.delete(task_id)
    if not ok:
        raise HTTPException(400, "Cannot delete task (not found or not in deletable state)")
    return {"ok": True}



async def _worker_task_or_none(db: AsyncSession, task_id: int) -> Task | None:
    """task 在 Worker 上则返回之（代理路径），本机返回 None。"""
    task = await db.get(Task, task_id)
    return task if (task and task.worker_id is not None) else None


async def _proxy(task: Task, method: str, path: str, body=None):
    from backend.main import worker_proxy
    if worker_proxy is None:
        raise HTTPException(503, "Worker 功能未启用")
    return await worker_proxy.proxy_to_worker(task, method, path, body)


async def _sync_task_from_worker_response(db: AsyncSession, task: Task, result):
    """代理响应是 worker 的 task JSON 时，同步关键字段（status 等 relay 也会同步，
    这里立即写一份让 API 响应不滞后）。"""
    if isinstance(result, dict) and result.get("id") == task.id:
        for f in ("status", "plan_approved", "error_message", "loop_progress"):
            if f in result and result[f] is not None:
                setattr(task, f, result[f])
        await db.commit()
        await db.refresh(task)
    return task


@router.post("/{task_id}/stop-session")
async def stop_task_session(task_id: int, db: AsyncSession = Depends(get_db)):
    """Stop the running Claude Code session for a task.

    Clears pending queued chat messages first so the queue consumer does not
    immediately relaunch after the current process is stopped.
    """
    wt = await _worker_task_or_none(db, task_id)
    if wt is not None:
        return await _proxy(wt, "POST", f"/api/tasks/{task_id}/stop-session")

    from backend.main import dispatcher
    cleared = dispatcher.clear_task_queue(task_id)
    stopped = await _stop_task_process(task_id, db)
    if not stopped:
        task = await db.get(Task, task_id)
        if task and task.status in ("executing", "in_progress"):
            task.status = "completed"
            await db.commit()
            return {
                "ok": True,
                "stopped": False,
                "cleared_messages": cleared,
                "note": "No running process found, task marked as completed",
            }
        if cleared:
            return {"ok": True, "stopped": False, "cleared_messages": cleared}
        raise HTTPException(400, "No running session found for this task")
    return {"ok": True, "stopped": True, "cleared_messages": cleared}


@router.post("/{task_id}/cancel", response_model=TaskResponse)
async def cancel_task(task_id: int, request: Request, queue: TaskQueue = Depends(_get_queue), db: AsyncSession = Depends(get_db)):
    task = await db.get(Task, task_id)
    if task:
        await require_task_access(request, task, db)
    wt = await _worker_task_or_none(db, task_id)
    if wt is not None:
        result = await _proxy(wt, "POST", f"/api/tasks/{task_id}/cancel")
        return await _sync_task_from_worker_response(db, wt, result)

    task = await queue.cancel(task_id)
    if not task:
        raise HTTPException(400, "Cannot cancel task")
    await _stop_task_process(task_id, db)

    from backend.main import dispatcher
    from backend.models.monitor_session import MonitorSession
    from sqlalchemy import select as sa_select
    result = await db.execute(
        sa_select(MonitorSession.id)
        .where(MonitorSession.task_id == task_id, MonitorSession.status.in_(["running"]))
    )
    for (ms_id,) in result.all():
        proc = dispatcher._monitor_processes.get(ms_id)
        if proc and proc.returncode is None:
            proc.kill()
            await proc.wait()
        atask = dispatcher._monitor_tasks.get(ms_id)
        if atask and not atask.done():
            atask.cancel()

    return task


@router.post("/{task_id}/retry", response_model=TaskResponse)
async def retry_task(task_id: int, request: Request, queue: TaskQueue = Depends(_get_queue), db: AsyncSession = Depends(get_db)):
    task = await db.get(Task, task_id)
    if task:
        await require_task_access(request, task, db)
    wt = await _worker_task_or_none(db, task_id)
    if wt is not None:
        result = await _proxy(wt, "POST", f"/api/tasks/{task_id}/retry")
        return await _sync_task_from_worker_response(db, wt, result)

    task = await queue.retry(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    return task


@router.post("/{task_id}/star", response_model=TaskResponse)
async def star_task(task_id: int, queue: TaskQueue = Depends(_get_queue)):
    task = await queue.star(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    return task


@router.post("/{task_id}/read", response_model=TaskResponse)
async def mark_task_read(task_id: int, queue: TaskQueue = Depends(_get_queue)):
    task = await queue.update_task(task_id, has_unread=False)
    if not task:
        raise HTTPException(404, "Task not found")
    return task


@router.post("/{task_id}/unread", response_model=TaskResponse)
async def mark_task_unread(task_id: int, queue: TaskQueue = Depends(_get_queue)):
    task = await queue.update_task(task_id, has_unread=True)
    if not task:
        raise HTTPException(404, "Task not found")
    return task


@router.post("/{task_id}/archive", response_model=TaskResponse)
async def archive_task(task_id: int, queue: TaskQueue = Depends(_get_queue)):
    task = await queue.archive(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    return task


@router.get("/queue/next", response_model=list[TaskResponse])
async def get_queue(queue: TaskQueue = Depends(_get_queue)):
    return await queue.list_tasks(status="pending")


@router.post("/{task_id}/plan/approve", response_model=TaskResponse)
async def approve_plan(task_id: int, request: Request, queue: TaskQueue = Depends(_get_queue), db: AsyncSession = Depends(get_db)):
    """Approve a plan-mode task's plan and queue it for execution."""
    task = await queue.get(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    await require_task_access(request, task, db)
    if task.worker_id is not None:
        result = await _proxy(task, "POST", f"/api/tasks/{task_id}/plan/approve")
        # worker 上回到 pending 由 worker 自己的 Dispatcher 接力执行
        return await _sync_task_from_worker_response(queue.db, task, result)
    if task.mode != "plan" or task.status != "plan_review":
        raise HTTPException(400, "Task is not in plan review state")
    task = await queue.update_task(task_id, plan_approved=True, status="pending")
    return task


@router.post("/{task_id}/plan/reject", response_model=TaskResponse)
async def reject_plan(task_id: int, request: Request, queue: TaskQueue = Depends(_get_queue), db: AsyncSession = Depends(get_db)):
    """Reject a plan-mode task's plan."""
    task = await queue.get(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    await require_task_access(request, task, db)
    if task.worker_id is not None:
        result = await _proxy(task, "POST", f"/api/tasks/{task_id}/plan/reject")
        return await _sync_task_from_worker_response(queue.db, task, result)
    if task.mode != "plan" or task.status != "plan_review":
        raise HTTPException(400, "Task is not in plan review state")
    task = await queue.update_task(task_id, plan_approved=False, status="cancelled")
    return task
