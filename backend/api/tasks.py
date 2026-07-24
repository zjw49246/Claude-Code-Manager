import asyncio
import os
import shutil
import uuid
from contextlib import AsyncExitStack
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select, update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database import get_db
from backend.models.task import Task
from backend.models.instance import Instance
from backend.schemas.task import (
    TaskCreate,
    TaskMigrationImport,
    TaskResponse,
    TaskTerminationRequest,
    TaskUpdate,
)
from backend.services.task_queue import TaskQueue, task_delete_fence
from backend.services.task_termination import (
    TaskLaunchTerminationConflict,
    _finish_despite_cancellation as _finish_task_operation,
    lock_task_generation as _lock_task_generation,
    read_persisted_task_completed_at as _read_persisted_task_completed_at,
    remaining_task_process_generations as _remaining_task_process_generations,
    stop_task_process as _stop_task_process,
    task_generation_fence as _task_generation_fence,
)
from backend.services.worker_relay import (
    WorkerTaskGeneration,
    apply_authoritative_worker_task,
    worker_task_generation,
    worker_task_generation_predicates,
)
from backend.services.worker_proxy import get_task_operation_lock
from backend.api.deps import get_current_user_id, get_current_user_role, require_task_access, require_admin

router = APIRouter(prefix="/api/tasks", tags=["tasks"])
_MANUAL_RETRYABLE_STATUSES = frozenset(
    {"failed", "cancelled", "conflict", "completed"}
)


def _find_session_jsonl(session_id: str, provider: str = "claude") -> Path | None:
    """Locate a provider session JSONL on disk.

    Codex stores rollouts under ``$CODEX_HOME/sessions/YYYY/MM/DD``.  This
    branch must run before the Claude pool lookup: treating a valid Codex
    rollout as a missing Claude session makes every follow-up abandon native
    history/cache and start a new thread.

    Pool deployments split sessions across multiple ~/.claude-account-N dirs,
    so a lookup that only checks ~/.claude / CLAUDE_CONFIG_DIR (and only the
    exact last_cwd-encoded project subdir) misses sessions created under a pool
    account and silently degrades recovery to a lossy summary (prod task #725).
    We reuse the pool's own locator (searches every account dir) and glob across
    all project subdirs so cwd-encoding differences don't hide the file either.
    """
    if (provider or "claude").lower() == "codex":
        homes_to_check: list[Path] = []

        # Pool account homes are the primary source of truth in multi-account
        # deployments.  Include disabled/cooling accounts too: their rollout
        # history remains valid even when the credentials cannot run a turn.
        try:
            from backend.main import codex_pool
            if codex_pool:
                for account in codex_pool.list_accounts():
                    codex_home = account.get("codex_home")
                    if codex_home:
                        homes_to_check.append(Path(codex_home).expanduser())
        except Exception:
            pass

        env_home = os.environ.get("CODEX_HOME")
        if env_home:
            homes_to_check.append(Path(env_home).expanduser())
        homes_to_check.append(Path.home() / ".codex")

        # Disk fallback covers removed pool entries and legacy account naming
        # such as ~/.codex-account-2.  A missing sessions/ child is harmless.
        try:
            homes_to_check.extend(
                path for path in sorted(Path.home().glob(".codex*")) if path.is_dir()
            )
        except OSError:
            pass

        seen: set[str] = set()
        for codex_home in homes_to_check:
            key = os.path.abspath(str(codex_home))
            if key in seen:
                continue
            seen.add(key)
            try:
                match = next(
                    (
                        path
                        for path in codex_home.glob(
                            f"sessions/*/*/*/rollout-*-{session_id}.jsonl"
                        )
                        if path.is_file()
                    ),
                    None,
                )
                if match:
                    return match
            except OSError:
                continue
        return None

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

    # A Codex rollout embeds its thread id in both the filename and session
    # metadata.  Copying it under a random filename does not create a valid new
    # thread, so keep this legacy clone operation Claude-only.
    if (source.provider or "claude").lower() != "claude":
        return None

    source_jsonl = _find_session_jsonl(source.session_id, provider="claude")
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
        from backend.models.user_group import UserGroupMember
        has_worker = (await db.execute(
            select(Worker.id).where(Worker.owner_user_id == user_id).limit(1)
        )).scalar_one_or_none()
        project_id = body.project_id if hasattr(body, 'project_id') else None
        has_project = False
        if project_id:
            user_group_ids = select(UserGroupMember.group_id).where(UserGroupMember.user_id == user_id)
            has_project = (await db.execute(
                select(TeamProjectShare.id).where(
                    TeamProjectShare.project_id == project_id,
                    ((TeamProjectShare.target_type == "user") & (TeamProjectShare.target_id == user_id))
                    | ((TeamProjectShare.target_type == "group") & TeamProjectShare.target_id.in_(user_group_ids))
                ).limit(1)
            )).scalar_one_or_none() is not None
        if not has_worker and not has_project:
            raise HTTPException(403, "You need a Worker or Project access to create Tasks")
    data = body.model_dump()
    data["created_by"] = user_id
    # Task inherits worker_id from its Project
    if data.get("project_id") and not data.get("worker_id"):
        from backend.models.project import Project as _Proj
        _proj = await db.get(_Proj, data["project_id"])
        if _proj and _proj.worker_id:
            data["worker_id"] = _proj.worker_id
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
    # Eliminate the dispatcher's historical 0-2s polling delay.  Importing
    # here avoids a module cycle during application construction.
    try:
        from backend.main import dispatcher
        if dispatcher:
            dispatcher.wake()
    except Exception:
        pass

    # Auto-share if project has active project-level shares
    if task.project_id:
        try:
            from backend.services.task_sharing import auto_share_new_task
            await auto_share_new_task(db, task.id, task.project_id)
        except Exception:
            pass  # best-effort

    return task


@router.post("/migration-import", response_model=TaskResponse, status_code=201)
async def import_migrated_task(
    request: Request,
    body: TaskMigrationImport,
    queue: TaskQueue = Depends(_get_queue),
    db: AsyncSession = Depends(get_db),
):
    """Create or refresh an inert task copied from a Manager.

    A normal task create commits ``pending`` and immediately wakes the local
    dispatcher.  Task migration used to call that endpoint and cancel in a
    second request, leaving a real window where the destination Worker could
    claim and execute the imported task.  This admin-only endpoint persists
    the task as ``cancelled`` in the same transaction and never wakes the
    dispatcher.

    Existing inactive copies are refreshed with a status CAS.  If a legacy
    copy has already become active, fail closed instead of cancelling work
    which may really be running.
    """
    require_admin(request)

    data = body.model_dump()
    for transient_field in (
        "image_paths",
        "file_paths",
        "attachments",
        "secret_ids",
        "clone_from_task_id",
    ):
        data.pop(transient_field, None)
    data.update(
        worker_id=None,
        status="cancelled",
        created_by=get_current_user_id(request),
    )

    from backend.config import settings as app_settings
    if not data.get("model"):
        data["model"] = (
            app_settings.default_codex_model
            if data.get("provider") == "codex"
            else app_settings.default_model
        )
    if not data.get("effort_level"):
        data["effort_level"] = app_settings.default_effort

    existing = await db.get(Task, body.id)
    if existing is None:
        # The first visible state is already inert.  In particular there is no
        # pending commit and no dispatcher.wake() between create and cancel.
        return await queue.create(**data)

    old_status = existing.status
    if old_status in ("in_progress", "executing", "migrating"):
        raise HTTPException(
            409,
            f"Destination task {body.id} is active ({old_status})",
        )

    values = {key: value for key, value in data.items() if key != "id"}
    result = await db.execute(
        sa_update(Task)
        .where(*_task_generation_fence(body.id, existing))
        .values(**values)
    )
    if result.rowcount != 1:
        await db.rollback()
        raise HTTPException(409, "Destination task changed during migration import")
    await db.commit()
    db.expire_all()
    task = await db.get(Task, body.id)
    if task is None:  # defensive: a concurrent delete must not look successful
        raise HTTPException(409, "Destination task disappeared during migration import")
    if old_status != "cancelled":
        from backend.services.task_events import broadcast_status_change
        await broadcast_status_change(task.id, "cancelled")
    return task


@router.get("/{task_id}", response_model=TaskResponse)
async def get_task(task_id: int, request: Request, queue: TaskQueue = Depends(_get_queue), db: AsyncSession = Depends(get_db)):
    task = await queue.get(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    await require_task_access(request, task, db)
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

    # "off" sentinel → None (exclude_unset can't distinguish None from unset)
    if updates.get("system_prompt_mode") == "off":
        updates["system_prompt_mode"] = None

    if not updates:
        task = await queue.get(task_id)
        if not task:
            raise HTTPException(404, "Task not found")
        return task
    task = await queue.update_task(task_id, **updates)
    if not task:
        raise HTTPException(404, "Task not found")
    return task


async def _settle_task_launch_barrier(
    task_id: int,
    instance_id: int | None,
) -> None:
    """Prove a pre-owner launch aborted after the Task became terminal."""

    from backend.services.task_termination import settle_task_launch_barrier

    try:
        await settle_task_launch_barrier(task_id, instance_id)
    except TaskLaunchTerminationConflict as exc:
        raise HTTPException(
            409,
            str(exc),
        ) from exc


async def _retry_local_task_safely(
    task_id: int,
    queue: TaskQueue,
    db: AsyncSession,
) -> Task | None:
    """Retry without discarding evidence of a possibly-live orphan process.

    Startup recovery intentionally retains ``Task.instance_id`` plus the
    Instance PID/current owner when it cannot prove that an unmanaged process
    died.  The retry endpoint is the only normal path that releases that
    terminal claim, so it must reconcile under InstanceManager's exact
    lifecycle lock before ``TaskQueue.retry`` clears the task-side owner.
    """

    from backend.main import instance_manager

    db.expire_all()
    task = await db.get(Task, task_id)
    if task is None:
        return None

    observed_status = task.status
    if observed_status not in _MANUAL_RETRYABLE_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=f"Task status {observed_status} is not retryable",
        )
    observed_generation = (
        task.retry_count,
        task.instance_id,
        task.started_at,
        task.completed_at,
    )
    reverse_owner_ids = set(
        (
            await db.execute(
                select(Instance.id).where(
                    Instance.current_task_id == task_id
                )
            )
        )
        .scalars()
        .all()
    )
    candidate_ids = set(reverse_owner_ids)
    if task.instance_id is not None:
        candidate_ids.add(task.instance_id)

    # Release the discovery snapshot before waiting for lifecycle locks. A
    # launch holder may need to commit Task/Instance ownership before releasing
    # that lock, and MySQL RR would otherwise keep all lock-internal reads on
    # the stale generation.
    await db.rollback()

    # Take every relevant lifecycle lock in stable order. This covers the
    # one-sided recovery state where Task.instance_id is NULL but an Instance
    # still names the task, and avoids deadlocks between two malformed rows.
    async with AsyncExitStack() as stack:
        for instance_id in sorted(candidate_ids):
            await stack.enter_async_context(
                instance_manager._instance_lifecycle_lock(instance_id)
            )

        db.expire_all()
        current_task = await db.get(Task, task_id)
        if current_task is None:
            return None
        current_generation = (
            current_task.retry_count,
            current_task.instance_id,
            current_task.started_at,
            current_task.completed_at,
        )
        if (
            current_task.status != observed_status
            or current_generation != observed_generation
        ):
            raise HTTPException(
                status_code=409,
                detail=(
                    "Task ownership changed or generation changed while retrying; "
                    "refresh and try again"
                ),
            )

        # Take the Task row/current-generation lock before any Instance row.
        # cancel/delete use the same Task -> Instance order; without this
        # guard retry could hold Instance while cancellation waits for it and
        # then block on cancellation's Task lock.
        guarded_task = await db.execute(
            sa_update(Task)
            .where(*_task_generation_fence(task_id, current_task))
            .values(status=current_task.status)
        )
        if not guarded_task.rowcount:
            await db.rollback()
            raise HTTPException(
                status_code=409,
                detail=(
                    "Task ownership changed or generation changed while retrying; "
                    "refresh and try again"
                ),
            )

        owner_result = await db.execute(
            select(Instance)
            .where(Instance.current_task_id == task_id)
            .with_for_update()
        )
        reverse_owners = list(owner_result.scalars().all())
        current_candidate_ids = {instance.id for instance in reverse_owners}
        if current_task.instance_id is not None:
            current_candidate_ids.add(current_task.instance_id)
        if not current_candidate_ids.issubset(candidate_ids):
            raise HTTPException(
                status_code=409,
                detail=(
                    "Task ownership changed while retrying; refresh and try again"
                ),
            )

        # A task-side link without a reverse owner can still point at a
        # pre-commit managed generation. Treat it as uncertain unless the slot
        # now explicitly belongs to another task.
        if current_task.instance_id is not None:
            task_side_instance = (
                await db.execute(
                    select(Instance)
                    .where(Instance.id == current_task.instance_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if (
                task_side_instance is not None
                and task_side_instance.current_task_id in (None, task_id)
                and instance_manager.is_running(current_task.instance_id)
            ):
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"Instance {current_task.instance_id} still has a live "
                        "managed generation; stop it before retrying"
                    ),
                )

        for instance in reverse_owners:
            if instance_manager.is_running(instance.id):
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"Instance {instance.id} still has a live managed "
                        "generation; stop it before retrying"
                    ),
                )

            pid = instance.pid
            if pid is not None:
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    pass
                except OSError:
                    # Permission errors and platform-specific failures do not
                    # prove death. Keep all ownership evidence fail-closed.
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            f"Unmanaged process PID {pid} may still be alive; "
                            "stop or reconcile it before retrying"
                        ),
                    )
                else:
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            f"Unmanaged process PID {pid} is still alive; "
                            "stop it before retrying"
                        ),
                    )
            elif instance.status == "running":
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"Instance {instance.id} still has an uncertain running "
                        "owner; stop or reconcile it before retrying"
                    ),
                )

            instance_predicates = [
                Instance.id == instance.id,
                Instance.current_task_id == task_id,
                Instance.status == instance.status,
                (
                    Instance.pid.is_(None)
                    if pid is None
                    else Instance.pid == pid
                ),
                (
                    Instance.started_at.is_(None)
                    if instance.started_at is None
                    else Instance.started_at == instance.started_at
                ),
            ]
            cleared = await db.execute(
                sa_update(Instance)
                .where(*instance_predicates)
                .values(status="error", current_task_id=None, pid=None)
            )
            if not cleared.rowcount:
                await db.rollback()
                raise HTTPException(
                    status_code=409,
                    detail="Instance ownership changed while retrying; try again",
                )

        retried = await queue.retry(
            task_id,
            expected_statuses=(observed_status,),
            generation_fence=observed_generation,
            rollback_on_miss=True,
        )
        if retried is None:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Task ownership changed or generation changed while retrying; "
                    "refresh and try again"
                ),
            )
        return retried


@router.delete("/{task_id}")
async def delete_task(task_id: int, request: Request, queue: TaskQueue = Depends(_get_queue), db: AsyncSession = Depends(get_db)):
    task = await db.get(Task, task_id)
    if task:
        await require_task_access(request, task, db)
    from backend.main import instance_manager, task_migrator, worker_proxy

    if task is None:
        raise HTTPException(404, "Task not found")

    if task is not None and task.worker_id is not None:
        # A Worker task has two durable copies, but only the remote copy owns
        # its process lifecycle.  Serialize against migration so an A→B→A ABA
        # cannot rebuild the task after the remote delete and still satisfy the
        # Manager mirror fence.
        await db.rollback()
        migration_lock = None
        if task_migrator is not None:
            migration_lock = task_migrator._locks.setdefault(
                task_id,
                asyncio.Lock(),
            )
            if migration_lock.locked():
                raise HTTPException(
                    409,
                    "Task is being migrated; retry deletion after migration",
                )
        if worker_proxy is None:
            raise HTTPException(503, "Worker 功能未启用")
        worker_operation_lock = worker_proxy.task_operation_lock(task_id)

        async with AsyncExitStack() as stack:
            if migration_lock is not None:
                await stack.enter_async_context(migration_lock)
            await stack.enter_async_context(worker_operation_lock)

            db.expire_all()
            worker_task = await db.get(Task, task_id)
            if worker_task is None:
                raise HTTPException(404, "Task not found")
            await require_task_access(request, worker_task, db)
            if worker_task.worker_id is None:
                raise HTTPException(
                    409,
                    "Task moved back to this Manager; refresh before deleting",
                )
            if worker_task.status not in (
                "pending",
                "failed",
                "cancelled",
                "conflict",
                "completed",
            ):
                raise HTTPException(
                    400,
                    "Cannot delete task (not in deletable state)",
                )

            worker_id = worker_task.worker_id
            delete_fence = task_delete_fence(worker_task)
            remote_result = await _proxy(
                worker_task,
                "DELETE",
                f"/api/tasks/{task_id}",
                require_json=True,
                allow_task_absent=True,
                operation_lock_held=True,
            )
            if (
                not isinstance(remote_result, dict)
                or remote_result.get("ok") is not True
            ):
                await db.rollback()
                raise HTTPException(
                    502,
                    "Worker did not explicitly confirm task deletion; "
                    "Manager mirror was preserved",
                )

            # Drop the pre-proxy read snapshot. TaskQueue.delete starts with an
            # exact current-write CAS over the original Worker generation, so
            # a concurrent relay/retry cannot make us erase a newer mirror.
            await db.rollback()
            ok = await queue.delete(
                task_id,
                expected_fence=delete_fence,
                remote_worker_deleted=True,
            )
            if not ok:
                # Relay/status updates can legitimately land after the Worker
                # has committed deletion. Remote mutation/forwarding and task
                # migration are still fenced by the two locks above, so a
                # current mirror on the same Worker is only stale state, not a
                # rebuilt remote generation. Lock and delete that exact current
                # row to make the cross-CCM delete converge.
                await db.rollback()
                current_worker_task = (
                    await db.execute(
                        select(Task)
                        .where(Task.id == task_id)
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                if current_worker_task is None:
                    ok = True
                elif current_worker_task.worker_id != worker_id:
                    await db.rollback()
                    worker_proxy.relay.unsubscribe_task(worker_id, task_id)
                    raise HTTPException(
                        409,
                        "Worker deleted the old task, but the Manager mirror "
                        "moved to another execution location and was preserved",
                    )
                else:
                    current_fence = task_delete_fence(current_worker_task)
                    ok = await queue.delete(
                        task_id,
                        expected_fence=current_fence,
                        remote_worker_deleted=True,
                    )
                if not ok:
                    raise HTTPException(
                        409,
                        "Worker deleted the task, but local runtime ownership "
                        "could not be safely reconciled; the mirror was preserved",
                    )
            worker_proxy.relay.unsubscribe_task(worker_id, task_id)
        return {"ok": True}

    lifecycle_ids = set(
        (
            await db.execute(
                select(Instance.id).where(
                    Instance.current_task_id == task_id
                )
            )
        )
        .scalars()
        .all()
    )
    if task is not None and task.instance_id is not None:
        task_side_instance = await db.get(Instance, task.instance_id)
        if (
            task_side_instance is not None
            and task_side_instance.current_task_id in (None, task_id)
        ):
            lifecycle_ids.add(task.instance_id)
    # Do not wait on a lifecycle lock while retaining a read transaction:
    # launch holds that lock while committing Task/Instance metadata.
    await db.rollback()

    # Serialize deletion with the complete launch/spawn/persist window. A
    # terminal Task can otherwise disappear just before a child is registered;
    # the launch would eventually abort, but shutdown in that gap would have no
    # durable Task evidence.
    async with AsyncExitStack() as stack:
        for instance_id in sorted(lifecycle_ids):
            await stack.enter_async_context(
                instance_manager._instance_lifecycle_lock(instance_id)
            )
        ok = await queue.delete(task_id)
    if not ok:
        raise HTTPException(400, "Cannot delete task (not found or not in deletable state)")
    return {"ok": True}



async def _worker_task_or_none(db: AsyncSession, task_id: int) -> Task | None:
    """task 在 Worker 上则返回之（代理路径），本机返回 None。"""
    task = await db.get(Task, task_id)
    return task if (task and task.worker_id is not None) else None


async def _proxy(
    task: Task,
    method: str,
    path: str,
    body=None,
    *,
    require_json: bool = False,
    allow_task_absent: bool = False,
    operation_lock_held: bool = False,
):
    from backend.main import worker_proxy
    if worker_proxy is None:
        raise HTTPException(503, "Worker 功能未启用")
    if require_json or allow_task_absent or operation_lock_held:
        return await worker_proxy.proxy_to_worker(
            task,
            method,
            path,
            body,
            require_json=require_json,
            allow_task_absent=allow_task_absent,
            operation_lock_held=operation_lock_held,
        )
    return await worker_proxy.proxy_to_worker(task, method, path, body)


async def _sync_task_from_worker_response(
    db: AsyncSession,
    task: Task,
    result,
    *,
    observed: WorkerTaskGeneration,
):
    """代理响应是 worker 的 task JSON 时，同步关键字段（status 等 relay 也会同步，
    这里立即写一份让 API 响应不滞后）。

    ``observed`` 必须在代理网络请求前捕获。响应回来后只允许 CAS 那个
    Worker assignment/generation，不能重新读取当前 Task 后把旧响应套到新代次。
    """

    task_id = observed.task_id
    resulting = await apply_authoritative_worker_task(db, observed, result)
    if resulting is None:
        await db.rollback()
        raise HTTPException(
            409,
            "Task Worker assignment or generation changed while the request "
            "was in flight",
        )
    status_changed = resulting.status != observed.status
    if status_changed:
        # relay 断连窗口内 Worker 侧广播镜像不过来，这里本地补一次。
        # Hold an exact-result no-op UPDATE across publication so a rapid retry
        # cannot let this old status event cross the replacement generation.
        guarded = await db.execute(
            sa_update(Task)
            .where(*worker_task_generation_predicates(resulting))
            .values(status=resulting.status)
        )
        if guarded.rowcount == 1:
            from backend.services.task_events import broadcast_status_change

            await broadcast_status_change(task_id, resulting.status)
            await db.commit()
        else:
            await db.rollback()
            raise HTTPException(
                409,
                "Task Worker assignment or generation changed before status "
                "publication",
            )

    db.expire_all()
    current = await db.get(Task, task_id)
    if current is None:
        raise HTTPException(
            409,
            "Task disappeared while the Worker request was in flight",
        )
    return current


@router.post(
    "/{task_id}/terminate-generation",
    response_model=TaskResponse,
    include_in_schema=False,
)
async def terminate_task_generation(
    task_id: int,
    body: TaskTerminationRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Internal Manager→Worker exact-generation termination endpoint.

    The complete local lifecycle is cancellation-shielded. The resulting Task
    row remains locked through response serialization so a remote retry cannot
    overtake the authoritative terminal snapshot returned to the Manager.
    """

    task = await db.get(Task, task_id)
    if task:
        await require_task_access(request, task, db)
    if task is None:
        raise HTTPException(404, "Task not found")
    metadata_marker = type((task.metadata_ or {}).get("pr_review_id")) is int
    tags = task.tags or []
    tag_marker = (
        isinstance(tags, (list, tuple, set, dict))
        and "pr-review" in tags
    )
    if not (metadata_marker or tag_marker):
        raise HTTPException(
            400,
            "Exact-generation termination is restricted to PR review tasks",
        )

    from backend.services.task_termination import (
        LocalTaskGeneration,
        TaskTerminationConflict,
        lock_task_generation,
        terminate_local_task_generation,
    )

    try:
        terminated = await terminate_local_task_generation(
            task_id,
            db,
            reason="Superseded by new PR push",
            expected_generation=LocalTaskGeneration(
                status=body.expected_status,
                retry_count=body.expected_retry_count,
                instance_id=body.expected_instance_id,
                started_at=body.expected_started_at,
                completed_at=body.expected_completed_at,
            ),
        )
    except TaskTerminationConflict as exc:
        await db.rollback()
        raise HTTPException(
            409,
            "Task generation cleanup could not be confirmed",
        ) from exc

    locked_task = await lock_task_generation(
        task_id,
        db,
        expected_status=terminated.terminal_status,
        expected_retry_count=terminated.retry_count,
        expected_instance_id=terminated.instance_id,
        expected_started_at=terminated.started_at,
        expected_completed_at=terminated.completed_at,
    )
    if locked_task is None:
        raise HTTPException(
            409,
            "Task started a newer generation after termination",
        )
    return locked_task


async def _stop_task_session_local_impl(
    task_id: int,
    db: AsyncSession,
) -> dict:
    """Cancellation-safe local core for ``POST /stop-session``."""

    from backend.main import dispatcher

    # Authentication/routing above opened a read transaction. Do not retain a
    # MySQL REPEATABLE READ snapshot while waiting for a queue consumer that may
    # itself commit the current Task generation.
    await db.rollback()
    try:
        cleared = await dispatcher.abort_task_queue(task_id)
    except Exception as exc:
        from backend.services.dispatcher import TaskQueueAbortTimeoutError

        if isinstance(exc, TaskQueueAbortTimeoutError):
            raise HTTPException(
                409,
                "Task queue worker could not be proven stopped; no terminal "
                "state was published",
            ) from exc
        raise

    # Queue cancellation is a suspension point. Start a fresh current/locking
    # read before the Task -> Instance transaction so a local→Worker migration
    # or newer retry cannot be terminalized through a stale RR snapshot.
    await db.rollback()
    db.expire_all()
    active_task = (
        await db.execute(
            select(Task)
            .where(
                Task.id == task_id,
                Task.worker_id.is_(None),
                Task.shared_from_id.is_(None),
            )
            .with_for_update()
        )
    ).scalar_one_or_none()

    expected_generations: list[
        tuple[int, int | None, datetime | None]
    ] = []
    completed_count = 0
    committed_retry_count: int | None = None
    committed_instance_id: int | None = None
    committed_started_at: datetime | None = None
    committed_completed_at: datetime | None = None
    guarded_expected_status: str | None = None
    guarded_terminal_generation = False
    if active_task is not None and active_task.status in (
        "executing",
        "in_progress",
    ):
        completed = await db.execute(
            sa_update(Task)
            .where(*_task_generation_fence(task_id, active_task))
            .values(status="completed", completed_at=datetime.utcnow())
        )
        completed_count = completed.rowcount or 0
        if completed_count:
            guarded_expected_status = "completed"
            committed_retry_count = active_task.retry_count
            committed_instance_id = active_task.instance_id
            committed_started_at = active_task.started_at
            committed_completed_at = await _read_persisted_task_completed_at(
                task_id,
                db,
            )
    elif active_task is not None and active_task.status in (
        "failed",
        "completed",
        "cancelled",
        "conflict",
    ):
        # A terminal Task may deliberately retain a possibly-live owner after a
        # fail-closed cleanup. The no-op exact UPDATE keeps retry from changing
        # its generation while reverse owners are snapshotted.
        guarded = await db.execute(
            sa_update(Task)
            .where(*_task_generation_fence(task_id, active_task))
            .values(status=active_task.status)
        )
        guarded_terminal_generation = bool(guarded.rowcount)
        if guarded_terminal_generation:
            guarded_expected_status = active_task.status
            committed_retry_count = active_task.retry_count
            committed_instance_id = active_task.instance_id
            committed_started_at = active_task.started_at
            committed_completed_at = active_task.completed_at

    if completed_count or guarded_terminal_generation:
        owner_rows = await db.execute(
            select(
                Instance.id,
                Instance.pid,
                Instance.started_at,
            )
            .where(Instance.current_task_id == task_id)
            .with_for_update()
        )
        expected_generations = list(owner_rows.all())
    await db.commit()

    await _settle_task_launch_barrier(task_id, committed_instance_id)
    stopped = await _stop_task_process(
        task_id,
        db,
        expected_generations=expected_generations,
    )
    remaining_generations = await _remaining_task_process_generations(
        task_id,
        db,
        expected_generations=expected_generations,
    )
    generation_still_guarded = False
    if completed_count or guarded_terminal_generation:
        locked_task = await _lock_task_generation(
            task_id,
            db,
            expected_status=guarded_expected_status,
            expected_retry_count=committed_retry_count,
            expected_instance_id=committed_instance_id,
            expected_started_at=committed_started_at,
            expected_completed_at=committed_completed_at,
        )
        generation_still_guarded = locked_task is not None
        if not generation_still_guarded:
            raise HTTPException(
                409,
                "Task started a newer generation while its old session was stopping",
            )
    if remaining_generations:
        await db.rollback()
        raise HTTPException(
            409,
            "Task was marked completed, but process cleanup could not be "
            "confirmed for instance(s): "
            + ", ".join(map(str, remaining_generations)),
        )

    if completed_count and generation_still_guarded:
        from backend.services.task_events import broadcast_status_change

        try:
            await broadcast_status_change(task_id, "completed")
        except BaseException:
            await db.rollback()
            raise
    if completed_count or guarded_terminal_generation:
        await db.commit()
    if not stopped:
        if completed_count and generation_still_guarded:
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


@router.post("/{task_id}/stop-session")
async def stop_task_session(
    task_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Stop the running Claude Code session for a task.

    Clear queued messages and atomically make the active Task terminal before
    looking for its Instance process.  The status CAS is the launch
    invalidation barrier: a fresh/queued launch that has not committed its
    owner yet must observe the terminal Task and abort, rather than appearing
    immediately after a no-owner SELECT.
    """

    task = await db.get(Task, task_id)
    if task:
        await require_task_access(request, task, db)
    wt = await _worker_task_or_none(db, task_id)
    if wt is not None:
        return await _proxy(wt, "POST", f"/api/tasks/{task_id}/stop-session")

    return await _finish_task_operation(
        _stop_task_session_local_impl(task_id, db)
    )


async def _cancel_local_task_impl(
    task_id: int,
    db: AsyncSession,
) -> Task:
    """Cancellation-safe local core for ``POST /cancel``."""

    from backend.main import dispatcher

    # End the authentication/routing snapshot before waiting. A queue consumer
    # may need to commit its final launch state before it can terminate.
    await db.rollback()
    try:
        await dispatcher.abort_task_queue(task_id)
    except Exception as exc:
        from backend.services.dispatcher import TaskQueueAbortTimeoutError

        if isinstance(exc, TaskQueueAbortTimeoutError):
            raise HTTPException(
                409,
                "Task queue worker could not be proven stopped; cancellation "
                "was not published",
            ) from exc
        raise

    # Re-enter with a current locking read after the suspension point. This is
    # both the MySQL RR reset and the first row in the global Task -> Instance
    # -> auxiliary lock order.
    await db.rollback()
    db.expire_all()
    active_task = (
        await db.execute(
            select(Task)
            .where(
                Task.id == task_id,
                Task.worker_id.is_(None),
                Task.shared_from_id.is_(None),
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    active_statuses = (
        "pending",
        "in_progress",
        "executing",
        "merging",
    )
    if active_task is None or active_task.status not in (
        *active_statuses,
        "cancelled",
    ):
        await db.rollback()
        raise HTTPException(400, "Cannot cancel task")

    transitioned = active_task.status in active_statuses
    cancelled_values = (
        {"status": "cancelled", "completed_at": datetime.utcnow()}
        if transitioned
        else {"status": "cancelled"}
    )
    cancelled = await db.execute(
        sa_update(Task)
        .where(*_task_generation_fence(task_id, active_task))
        .values(**cancelled_values)
    )
    if not cancelled.rowcount:
        await db.rollback()
        raise HTTPException(
            409,
            "Task generation changed while cancellation was starting",
        )

    from backend.models.monitor_session import MonitorSession

    owner_rows = await db.execute(
        select(
            Instance.id,
            Instance.pid,
            Instance.started_at,
        )
        .where(Instance.current_task_id == task_id)
        .with_for_update()
    )
    expected_generations = list(owner_rows.all())
    monitor_rows = await db.execute(
        select(
            MonitorSession.id,
            MonitorSession.agent_type,
            MonitorSession.source,
        )
        .where(
            MonitorSession.task_id == task_id,
            MonitorSession.status.in_(("running", "cancelled")),
        )
        .with_for_update()
    )
    auxiliary_sessions = list(monitor_rows.all())
    await db.execute(
        sa_update(MonitorSession)
        .where(
            MonitorSession.task_id == task_id,
            MonitorSession.status == "running",
        )
        .values(status="cancelled", completed_at=datetime.utcnow())
    )
    committed_retry_count = active_task.retry_count
    committed_instance_id = active_task.instance_id
    committed_started_at = active_task.started_at
    committed_completed_at = (
        await _read_persisted_task_completed_at(task_id, db)
        if transitioned
        else active_task.completed_at
    )
    await db.commit()

    await _settle_task_launch_barrier(task_id, committed_instance_id)
    await _stop_task_process(
        task_id,
        db,
        expected_generations=expected_generations,
    )

    for session_id, agent_type, source in auxiliary_sessions:
        # Native agents are part of the main process tree. CCM-owned auxiliary
        # processes use their own exact registries and must be reaped explicitly.
        if source != "ccm":
            continue
        try:
            if agent_type == "sub_agent":
                await dispatcher.stop_sub_agent_session_process(session_id)
            elif agent_type == "monitor":
                await dispatcher.stop_monitor_session_process(session_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise HTTPException(
                409,
                "Task was cancelled, but auxiliary process cleanup could not "
                f"be confirmed for session {session_id}",
            ) from exc

    remaining_generations = await _remaining_task_process_generations(
        task_id,
        db,
        expected_generations=expected_generations,
    )
    current_task = await _lock_task_generation(
        task_id,
        db,
        expected_status="cancelled",
        expected_retry_count=committed_retry_count,
        expected_instance_id=committed_instance_id,
        expected_started_at=committed_started_at,
        expected_completed_at=committed_completed_at,
    )
    if current_task is None:
        raise HTTPException(
            409,
            "Task started a newer generation while cancellation was finishing",
        )
    if remaining_generations:
        await db.rollback()
        raise HTTPException(
            409,
            "Task was cancelled, but process cleanup could not be confirmed "
            "for instance(s): "
            + ", ".join(map(str, remaining_generations)),
        )

    if transitioned:
        from backend.services.task_events import broadcast_status_change

        try:
            await broadcast_status_change(task_id, "cancelled")
        except BaseException:
            await db.rollback()
            raise
    await db.commit()
    return current_task


@router.post("/{task_id}/cancel", response_model=TaskResponse)
async def cancel_task(
    task_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    task = await db.get(Task, task_id)
    if task:
        await require_task_access(request, task, db)
    wt = await _worker_task_or_none(db, task_id)
    if wt is not None:
        observed = worker_task_generation(wt)
        if observed is None:
            raise HTTPException(409, "Task Worker assignment changed")
        result = await _proxy(wt, "POST", f"/api/tasks/{task_id}/cancel")
        return await _sync_task_from_worker_response(
            db,
            wt,
            result,
            observed=observed,
        )

    return await _finish_task_operation(
        _cancel_local_task_impl(task_id, db)
    )


@router.post("/{task_id}/retry", response_model=TaskResponse)
async def retry_task(task_id: int, request: Request, queue: TaskQueue = Depends(_get_queue), db: AsyncSession = Depends(get_db)):
    task = await db.get(Task, task_id)
    if task:
        await require_task_access(request, task, db)
    # The operation lock is shared with TaskMigrator.  Keep it through the
    # remote response CAS/local retry commit and status publication, otherwise
    # migration can copy an old generation while retry is still in flight.
    await db.rollback()
    async with get_task_operation_lock(task_id):
        db.expire_all()
        current = await db.get(Task, task_id)
        if current is None:
            raise HTTPException(404, "Task not found")
        await require_task_access(request, current, db)
        if current.status not in _MANUAL_RETRYABLE_STATUSES:
            raise HTTPException(
                409,
                f"Task status {current.status} is not retryable",
            )

        if current.worker_id is not None:
            observed = worker_task_generation(current)
            if observed is None:
                raise HTTPException(409, "Task Worker assignment changed")
            result = await _proxy(
                current,
                "POST",
                f"/api/tasks/{task_id}/retry",
                operation_lock_held=True,
            )
            return await _sync_task_from_worker_response(
                db,
                current,
                result,
                observed=observed,
            )

        retried = await _retry_local_task_safely(task_id, queue, db)
        if not retried:
            raise HTTPException(404, "Task not found")
        locked_task = await _lock_task_generation(
            task_id,
            db,
            expected_status=retried.status,
            expected_retry_count=retried.retry_count,
            expected_instance_id=retried.instance_id,
            expected_started_at=retried.started_at,
            expected_completed_at=retried.completed_at,
        )
        if locked_task is None:
            raise HTTPException(
                409,
                "Task was claimed by a newer generation before retry publication",
            )
        from backend.services.task_events import broadcast_status_change
        await broadcast_status_change(task_id, retried.status)
        await db.commit()
        return locked_task


@router.post("/{task_id}/star", response_model=TaskResponse)
async def star_task(task_id: int, request: Request, queue: TaskQueue = Depends(_get_queue), db: AsyncSession = Depends(get_db)):
    task = await db.get(Task, task_id)
    if task:
        await require_task_access(request, task, db)
    task = await queue.star(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    return task


@router.post("/{task_id}/read", response_model=TaskResponse)
async def mark_task_read(task_id: int, request: Request, queue: TaskQueue = Depends(_get_queue), db: AsyncSession = Depends(get_db)):
    task = await db.get(Task, task_id)
    if task:
        await require_task_access(request, task, db)
    task = await queue.update_task(task_id, has_unread=False)
    if not task:
        raise HTTPException(404, "Task not found")
    return task


@router.post("/{task_id}/unread", response_model=TaskResponse)
async def mark_task_unread(task_id: int, request: Request, queue: TaskQueue = Depends(_get_queue), db: AsyncSession = Depends(get_db)):
    task = await db.get(Task, task_id)
    if task:
        await require_task_access(request, task, db)
    task = await queue.update_task(task_id, has_unread=True)
    if not task:
        raise HTTPException(404, "Task not found")
    return task


@router.post("/{task_id}/archive", response_model=TaskResponse)
async def archive_task(task_id: int, request: Request, queue: TaskQueue = Depends(_get_queue), db: AsyncSession = Depends(get_db)):
    task = await db.get(Task, task_id)
    if task:
        await require_task_access(request, task, db)
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
    await db.rollback()
    async with get_task_operation_lock(task_id):
        db.expire_all()
        current = await db.get(Task, task_id)
        if current is None:
            raise HTTPException(404, "Task not found")
        await require_task_access(request, current, db)
        if current.mode != "plan" or current.status != "plan_review":
            raise HTTPException(400, "Task is not in plan review state")

        if current.worker_id is not None:
            observed = worker_task_generation(current)
            if observed is None:
                raise HTTPException(409, "Task Worker assignment changed")
            result = await _proxy(
                current,
                "POST",
                f"/api/tasks/{task_id}/plan/approve",
                operation_lock_held=True,
            )
            # worker 上回到 pending 由 worker 自己的 Dispatcher 接力执行
            return await _sync_task_from_worker_response(
                queue.db,
                current,
                result,
                observed=observed,
            )

        changed = await db.execute(
            sa_update(Task)
            .where(*_task_generation_fence(task_id, current))
            .values(plan_approved=True, status="pending")
        )
        if changed.rowcount != 1:
            await db.rollback()
            raise HTTPException(
                409,
                "Task generation changed while approving the plan",
            )
        await db.commit()
        db.expire_all()
        approved = await db.get(Task, task_id)
        if approved is None:
            raise HTTPException(409, "Task disappeared while approving the plan")
        try:
            from backend.main import dispatcher
            if dispatcher:
                dispatcher.wake()
        except Exception:
            pass
        from backend.services.task_events import broadcast_status_change
        await broadcast_status_change(task_id, "pending")
        return approved


@router.post("/{task_id}/plan/reject", response_model=TaskResponse)
async def reject_plan(task_id: int, request: Request, queue: TaskQueue = Depends(_get_queue), db: AsyncSession = Depends(get_db)):
    """Reject a plan-mode task's plan."""
    task = await queue.get(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    await require_task_access(request, task, db)
    await db.rollback()
    async with get_task_operation_lock(task_id):
        db.expire_all()
        current = await db.get(Task, task_id)
        if current is None:
            raise HTTPException(404, "Task not found")
        await require_task_access(request, current, db)
        if current.mode != "plan" or current.status != "plan_review":
            raise HTTPException(400, "Task is not in plan review state")

        if current.worker_id is not None:
            observed = worker_task_generation(current)
            if observed is None:
                raise HTTPException(409, "Task Worker assignment changed")
            result = await _proxy(
                current,
                "POST",
                f"/api/tasks/{task_id}/plan/reject",
                operation_lock_held=True,
            )
            return await _sync_task_from_worker_response(
                queue.db,
                current,
                result,
                observed=observed,
            )

        changed = await db.execute(
            sa_update(Task)
            .where(*_task_generation_fence(task_id, current))
            .values(plan_approved=False, status="cancelled")
        )
        if changed.rowcount != 1:
            await db.rollback()
            raise HTTPException(
                409,
                "Task generation changed while rejecting the plan",
            )
        await db.commit()
        db.expire_all()
        rejected = await db.get(Task, task_id)
        if rejected is None:
            raise HTTPException(409, "Task disappeared while rejecting the plan")
        from backend.services.task_events import broadcast_status_change
        await broadcast_status_change(task_id, "cancelled")
        return rejected
