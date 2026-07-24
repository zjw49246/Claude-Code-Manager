from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.deps import require_admin
from backend.config import settings
from backend.database import get_db
from backend.models.instance import Instance
from backend.models.log_entry import LogEntry
from backend.schemas.instance import (
    InstanceCreate,
    InstanceResponse,
    InstanceStopRequest,
)
from backend.schemas.log_entry import LogEntryResponse
from backend.services.instance_capacity import (
    instance_capacity_lock,
    occupied_slot_predicate,
)
from backend.services.task_queue import persisted_pid_is_definitively_dead

router = APIRouter(
    prefix="/api/instances",
    tags=["instances"],
    dependencies=[Depends(require_admin)],
)
dispatcher_router = APIRouter(
    prefix="/api/dispatcher",
    tags=["dispatcher"],
    dependencies=[Depends(require_admin)],
)
def _instance_generation_predicates(instance: Instance) -> list:
    """Build a complete persisted-generation fence for one Instance row."""

    return [
        Instance.id == instance.id,
        Instance.status == instance.status,
        (
            Instance.current_task_id.is_(None)
            if instance.current_task_id is None
            else Instance.current_task_id == instance.current_task_id
        ),
        (
            Instance.pid.is_(None)
            if instance.pid is None
            else Instance.pid == instance.pid
        ),
        (
            Instance.started_at.is_(None)
            if instance.started_at is None
            else Instance.started_at == instance.started_at
        ),
    ]


async def _lock_instance_current(
    db: AsyncSession,
    instance_id: int,
) -> Instance | None:
    """Use a locking/current read instead of a MySQL RR snapshot reload."""

    return (
        await db.execute(
            select(Instance)
            .where(Instance.id == instance_id)
            .with_for_update()
        )
    ).scalar_one_or_none()


async def _delete_exact_instance_generation(
    db: AsyncSession,
    instance: Instance,
) -> bool:
    """Delete only the exact generation approved under the lifecycle lock."""

    deleted = await db.execute(
        sa_delete(Instance).where(*_instance_generation_predicates(instance))
    )
    return deleted.rowcount == 1


async def _reconcile_dead_terminal_pid(
    db: AsyncSession,
    instance: Instance,
) -> bool:
    """Detach a terminal persisted generation only after a definitive ESRCH.

    The caller must hold InstanceManager's lifecycle lock. The exact status,
    PID and task owner predicates keep a stale cleanup request from clearing a
    newer generation that changed while the OS probe was in progress.
    """

    pid = instance.pid
    if pid is None or not persisted_pid_is_definitively_dead(pid):
        return False
    predicates = [
        Instance.id == instance.id,
        Instance.status == instance.status,
        Instance.pid == pid,
    ]
    if instance.current_task_id is None:
        predicates.append(Instance.current_task_id.is_(None))
    else:
        predicates.append(Instance.current_task_id == instance.current_task_id)
    predicates.append(
        Instance.started_at.is_(None)
        if instance.started_at is None
        else Instance.started_at == instance.started_at
    )
    reconciled = await db.execute(
        update(Instance)
        .where(*predicates)
        .values(pid=None, current_task_id=None)
    )
    await db.commit()
    return bool(reconciled.rowcount)


@router.get("", response_model=list[InstanceResponse])
async def list_instances(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Instance).order_by(Instance.id))
    return list(result.scalars().all())


@router.post("", response_model=InstanceResponse, status_code=201)
async def create_instance(
    request: Request,
    body: InstanceCreate,
    db: AsyncSession = Depends(get_db),
):
    require_admin(request)
    # The Dispatcher uses the same lock for its count-and-create transaction.
    # Without it two simultaneous API/dispatcher admissions can both observe a
    # free slot and exceed the configured hard cap.
    async with instance_capacity_lock:
        cap = settings.max_concurrent_instances
        if cap > 0:
            live_count = await db.scalar(
                select(func.count(Instance.id)).where(
                    occupied_slot_predicate()
                )
            )
            if (live_count or 0) >= cap:
                raise HTTPException(
                    status_code=409,
                    detail=f"Instance capacity limit reached ({cap})",
                )

        instance = Instance(
            name=body.name,
            config=body.config,
        )
        db.add(instance)
        await db.commit()
        await db.refresh(instance)
    return instance


@router.delete("/cleanup")
async def cleanup_instances(request: Request, db: AsyncSession = Depends(get_db)):
    require_admin(request)
    from backend.main import dispatcher, instance_manager, ralph_loop
    result = await db.execute(
        select(Instance).where(Instance.status.in_(["error", "stopped"]))
    )
    target_ids = [instance.id for instance in result.scalars().all()]
    # Do not retain a MySQL REPEATABLE READ snapshot while waiting for an
    # in-process lifecycle lock. The lock holder may need to commit the newer
    # Instance generation before it can release that lock.
    await db.rollback()
    deleted = 0
    skipped_running: list[int] = []
    for instance_id in target_ids:
        # New Ralph loops cannot be started anymore, but an upgraded process
        # may still have one. Reap it before deleting its slot.
        if await ralph_loop.stop(instance_id) is False:
            skipped_running.append(instance_id)
            continue
        lifecycle_lock = instance_manager._instance_lifecycle_lock(instance_id)
        async with dispatcher._instance_claim_lock, lifecycle_lock:
            if instance_id in dispatcher._instance_claim_owners:
                skipped_running.append(instance_id)
                continue
            db.expire_all()
            inst = await _lock_instance_current(db, instance_id)
            if inst is None:
                await db.rollback()
                continue
            if (
                instance_manager.is_running(instance_id)
                or inst.status not in ("error", "stopped")
            ):
                skipped_running.append(instance_id)
                await db.rollback()
                continue
            if inst.pid is not None:
                if not await _reconcile_dead_terminal_pid(db, inst):
                    skipped_running.append(instance_id)
                    await db.rollback()
                    continue
                db.expire_all()
                inst = await _lock_instance_current(db, instance_id)
                if inst is None:
                    await db.rollback()
                    continue
            if inst.current_task_id is not None:
                skipped_running.append(instance_id)
                await db.rollback()
                continue
            if not await _delete_exact_instance_generation(db, inst):
                skipped_running.append(instance_id)
                await db.rollback()
                continue
            # Commit while the exact Instance lifecycle lock is still held so
            # a concurrent cleanup/delete/launch cannot observe the old row.
            await db.commit()
            deleted += 1
    if dispatcher.status().get("running"):
        await dispatcher._ensure_instances()
    return {
        "ok": True,
        "deleted": deleted,
        "skipped_running": skipped_running,
    }


@router.get("/{instance_id}", response_model=InstanceResponse)
async def get_instance(instance_id: int, db: AsyncSession = Depends(get_db)):
    instance = await db.get(Instance, instance_id)
    if not instance:
        raise HTTPException(404, "Instance not found")
    return instance


@router.delete("/{instance_id}")
async def delete_instance(
    instance_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    require_admin(request)
    from backend.main import dispatcher, instance_manager, ralph_loop
    instance = await db.get(Instance, instance_id)
    if not instance:
        raise HTTPException(404, "Instance not found")
    # The initial lookup is only an early 404. Release its RR snapshot before
    # waiting for a lifecycle owner that may itself be committing this row.
    await db.rollback()

    # Prevent a legacy Ralph loop from claiming this slot while deletion waits
    # for InstanceManager's launch/stop admission lock.
    if await ralph_loop.stop(instance_id) is False:
        raise HTTPException(
            status_code=409,
            detail="Ralph loop did not stop; instance was not deleted",
        )
    lifecycle_lock = instance_manager._instance_lifecycle_lock(instance_id)
    async with dispatcher._instance_claim_lock, lifecycle_lock:
        if (
            instance_id in dispatcher._instance_claim_owners
            or instance_id in dispatcher._active_local_instance_ids()
        ):
            raise HTTPException(
                status_code=409,
                detail="Instance is reserved for a task lifecycle; retry after refresh",
            )
        db.expire_all()
        instance = await _lock_instance_current(db, instance_id)
        if instance is None:
            await db.rollback()
            raise HTTPException(404, "Instance not found")
        if (
            instance_manager.is_running(instance_id)
            or instance.status == "running"
        ):
            await db.rollback()
            raise HTTPException(
                status_code=409,
                detail="Instance is active; stop it before deleting",
            )
        if instance.pid is not None:
            observed_pid = instance.pid
            if not await _reconcile_dead_terminal_pid(db, instance):
                await db.rollback()
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"Instance PID {observed_pid} may still be alive; "
                        "stop or reconcile it before deleting"
                    ),
                )
            db.expire_all()
            instance = await _lock_instance_current(db, instance_id)
            if instance is None:
                await db.rollback()
                raise HTTPException(404, "Instance not found")
        if instance.current_task_id is not None:
            await db.rollback()
            raise HTTPException(
                status_code=409,
                detail="Instance still owns a task; reconcile it before deleting",
            )
        if not await _delete_exact_instance_generation(db, instance):
            await db.rollback()
            raise HTTPException(
                status_code=409,
                detail="Instance generation changed while deleting; refresh and retry",
            )
        await db.commit()
    return {"ok": True}


@router.post("/{instance_id}/stop")
async def stop_instance(
    instance_id: int,
    body: InstanceStopRequest,
    db: AsyncSession = Depends(get_db),
):
    from backend.main import instance_manager, ralph_loop
    instance = await db.get(Instance, instance_id)
    if instance is None:
        raise HTTPException(404, "Instance not found")
    if (
        instance.current_task_id != body.expected_task_id
        or instance.pid != body.expected_pid
        or instance.started_at != body.expected_started_at
    ):
        raise HTTPException(
            409,
            "Instance process generation changed; refresh before stopping",
        )

    # Stop the producer first so it cannot claim another task immediately after
    # InstanceManager has reaped the current process.
    ralph_was_running = ralph_loop.is_running(instance_id)
    if await ralph_loop.stop(instance_id) is False:
        raise HTTPException(
            status_code=409,
            detail="Ralph loop did not stop; instance process was not changed",
        )
    ok = await instance_manager.stop(
        instance_id,
        expected_task_id=body.expected_task_id,
        expected_pid=body.expected_pid,
        expected_started_at=body.expected_started_at,
        terminal_consumer_timeout=30.0,
        consumer_cancel_timeout=10.0,
    )
    if not ok:
        db.expire_all()
        remaining_exact_owner = await db.scalar(
            select(Instance.id)
            .where(
                Instance.id == instance_id,
                Instance.current_task_id == body.expected_task_id,
                (
                    Instance.pid.is_(None)
                    if body.expected_pid is None
                    else Instance.pid == body.expected_pid
                ),
                (
                    Instance.started_at.is_(None)
                    if body.expected_started_at is None
                    else Instance.started_at == body.expected_started_at
                ),
            )
            .with_for_update()
        )
        await db.rollback()
        if remaining_exact_owner is None and ralph_was_running:
            return {"ok": True}
        raise HTTPException(
            409,
            "Instance process cleanup could not be confirmed or its owner changed",
        )
    return {"ok": True}


@router.post("/{instance_id}/run")
async def run_task_on_instance(instance_id: int):
    """Retired: direct launch bypassed TaskQueue ownership and status CAS."""
    raise HTTPException(
        status_code=410,
        detail="Direct Instance execution was removed; create or retry a Task instead",
    )


@router.get("/{instance_id}/logs", response_model=list[LogEntryResponse])
async def get_logs(
    instance_id: int,
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    after_id: int | None = Query(default=None, ge=0),
    event_type: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    if await db.get(Instance, instance_id) is None:
        raise HTTPException(404, "Instance not found")
    if after_id is not None and offset:
        raise HTTPException(
            status_code=422,
            detail="after_id and offset cannot be used together",
        )

    stmt = select(LogEntry).where(LogEntry.instance_id == instance_id)
    if event_type:
        stmt = stmt.where(LogEntry.event_type == event_type)
    if after_id is not None:
        # Cursor pages are oldest-first so callers can advance monotonically
        # and recover every persisted event missed during a WebSocket outage.
        stmt = (
            stmt.where(LogEntry.id > after_id)
            .order_by(LogEntry.id.asc())
            .limit(limit)
        )
    else:
        # Preserve the historical endpoint contract for initial/latest-page
        # loads and existing callers.
        stmt = stmt.order_by(LogEntry.id.desc()).limit(limit).offset(offset)
    result = await db.execute(stmt)
    return list(result.scalars().all())


@router.post("/{instance_id}/ralph/start")
async def start_ralph_loop(instance_id: int):
    """Retired: GlobalDispatcher is the only supported task dequeue owner."""
    raise HTTPException(
        status_code=410,
        detail="Ralph Loop was retired; use the global Dispatcher instead",
    )


@router.post("/{instance_id}/ralph/stop")
async def stop_ralph_loop(instance_id: int):
    """Stop the Ralph Loop for an instance."""
    from backend.main import ralph_loop
    if await ralph_loop.stop(instance_id) is False:
        raise HTTPException(
            status_code=409,
            detail="Ralph loop did not stop; exact loop evidence was retained",
        )
    return {"ok": True}


@router.get("/{instance_id}/ralph/status")
async def ralph_loop_status(instance_id: int):
    from backend.main import ralph_loop
    return {"running": ralph_loop.is_running(instance_id)}


# ── Dispatcher endpoints ──

@dispatcher_router.get("/status")
async def dispatcher_status():
    from backend.main import dispatcher
    return dispatcher.status()


@dispatcher_router.post("/start")
async def start_dispatcher(request: Request):
    require_admin(request)
    from backend.main import dispatcher
    await dispatcher.start()
    return {"ok": True, "message": "Dispatcher started"}


@dispatcher_router.post("/stop")
async def stop_dispatcher(request: Request):
    require_admin(request)
    from backend.main import dispatcher
    try:
        await dispatcher.stop()
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True, "message": "Dispatcher stopped"}
