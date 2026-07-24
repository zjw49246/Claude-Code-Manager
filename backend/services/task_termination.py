"""Generation-safe local Task termination primitives.

Task rows and reusable Instance slots form one ownership relationship.  A
terminal status alone does not stop an already-running agent, while looking up
the Instance after committing that status can race with slot reuse.  This
module centralizes the exact-generation fences shared by task APIs and
background callers such as PR Monitor.
"""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import TYPE_CHECKING, Awaitable, TypeVar

from sqlalchemy import select, update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.instance import Instance
from backend.models.task import Task
from backend.services.task_queue import (
    PR_REVIEW_SUPERSEDED_METADATA_KEY,
    task_retry_not_superseded_predicate,
)

if TYPE_CHECKING:
    from backend.services.worker_relay import WorkerTaskGeneration


class TaskTerminationConflict(RuntimeError):
    """A local Task generation could not be proven safely terminated."""


class TaskQueueTerminationConflict(TaskTerminationConflict):
    """The queued/in-flight message consumer did not settle."""


class TaskGenerationTerminationConflict(TaskTerminationConflict):
    """The Task changed generation during termination."""


class TaskLaunchTerminationConflict(TaskTerminationConflict):
    """A pre-owner process launch could not be proven aborted."""


class TaskProcessTerminationConflict(TaskTerminationConflict):
    """One or more exact Instance generations could not be proven reaped."""

    def __init__(self, instance_ids: list[int]):
        self.instance_ids = instance_ids
        super().__init__(
            "Process cleanup could not be confirmed for instance(s): "
            + ", ".join(map(str, instance_ids))
        )


class TaskAuxiliaryTerminationConflict(TaskTerminationConflict):
    """One or more CCM-owned auxiliary sessions could not be reaped."""

    def __init__(self, session_ids: list[int]):
        self.session_ids = session_ids
        super().__init__(
            "Auxiliary cleanup could not be confirmed for session(s): "
            + ", ".join(map(str, session_ids))
        )


class WorkerTaskTerminationConflict(TaskTerminationConflict):
    """A Worker-owned Task could not be authoritatively stopped and mirrored."""


@dataclass(frozen=True)
class TaskTerminationResult:
    task_id: int
    previous_status: str
    terminal_status: str
    transitioned: bool
    stopped: bool
    cleared_messages: int
    retry_count: int
    instance_id: int | None
    started_at: datetime | None
    completed_at: datetime | None


@dataclass(frozen=True)
class WorkerTaskTerminationResult:
    task_id: int
    observed: WorkerTaskGeneration
    resulting: WorkerTaskGeneration


@dataclass(frozen=True)
class LocalTaskGeneration:
    """Exact scalar generation expected by a local termination request."""

    status: str
    retry_count: int
    instance_id: int | None
    started_at: datetime | None
    completed_at: datetime | None


_T = TypeVar("_T")


async def _finish_despite_cancellation(awaitable: Awaitable[_T]) -> _T:
    """Finish safety-critical cleanup before propagating caller cancellation."""

    operation = asyncio.create_task(awaitable)
    cancellation: asyncio.CancelledError | None = None
    while not operation.done():
        try:
            await asyncio.shield(operation)
        except asyncio.CancelledError as exc:
            cancellation = exc
    result = operation.result()
    if cancellation is not None:
        raise cancellation
    return result


def _utc_naive(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def local_task_generation(task: Task) -> LocalTaskGeneration:
    return LocalTaskGeneration(
        status=task.status,
        retry_count=task.retry_count,
        instance_id=task.instance_id,
        started_at=_utc_naive(task.started_at),
        completed_at=_utc_naive(task.completed_at),
    )


def normalize_local_task_generation(
    generation: LocalTaskGeneration,
) -> LocalTaskGeneration:
    return LocalTaskGeneration(
        status=generation.status,
        retry_count=generation.retry_count,
        instance_id=generation.instance_id,
        started_at=_utc_naive(generation.started_at),
        completed_at=_utc_naive(generation.completed_at),
    )


def local_task_generation_predicates(
    task_id: int,
    generation: LocalTaskGeneration,
) -> list:
    generation = normalize_local_task_generation(generation)
    return [
        Task.id == task_id,
        Task.worker_id.is_(None),
        Task.shared_from_id.is_(None),
        Task.status == generation.status,
        Task.retry_count == generation.retry_count,
        (
            Task.instance_id.is_(None)
            if generation.instance_id is None
            else Task.instance_id == generation.instance_id
        ),
        (
            Task.started_at.is_(None)
            if generation.started_at is None
            else Task.started_at == generation.started_at
        ),
        (
            Task.completed_at.is_(None)
            if generation.completed_at is None
            else Task.completed_at == generation.completed_at
        ),
    ]


def task_generation_fence(task_id: int, task: Task) -> list:
    """Build an exact Task-generation CAS predicate from an observed row."""

    return local_task_generation_predicates(
        task_id,
        local_task_generation(task),
    )


async def stop_task_process(
    task_id: int,
    db: AsyncSession,
    *,
    expected_generations: list[
        tuple[int, int | None, datetime | None]
    ],
    task_status: str = "completed",
) -> bool:
    """Stop only exact Instance generations invalidated by the caller.

    ``Task.instance_id`` is historical after a turn completes and the reusable
    slot may already belong to another task. Callers must snapshot the reverse
    Instance owner rows in the same transaction that terminally CASes the Task,
    then pass them here. Discovering owners after that commit can target a rapid
    retry of the same task id (ABA), even when PID/start fences are later used.
    """

    del db  # The manager verifies ownership with independent current reads.
    from backend.main import instance_manager

    stopped = False
    for instance_id, expected_pid, expected_started_at in expected_generations:
        stopped = (
            await instance_manager.stop(
                instance_id,
                expected_task_id=task_id,
                expected_pid=expected_pid,
                expected_started_at=expected_started_at,
                task_status=task_status,
                terminal_consumer_timeout=30.0,
                consumer_cancel_timeout=10.0,
            )
            or stopped
        )
    return stopped


async def settle_task_launch_barrier(
    task_id: int,
    instance_id: int | None,
) -> None:
    """Prove a pre-owner launch aborted after the Task became terminal."""

    if instance_id is None:
        return
    from backend.main import instance_manager

    settled = await instance_manager.wait_for_task_launch_barrier(
        instance_id,
        task_id,
    )
    if not settled:
        raise TaskLaunchTerminationConflict(
            "Task was made terminal, but a pre-owner process launch could not "
            "be proven stopped"
        )


async def remaining_task_process_generations(
    task_id: int,
    db: AsyncSession,
    *,
    expected_generations: list[
        tuple[int, int | None, datetime | None]
    ],
) -> list[int]:
    """Return exact owner generations that stop could not clear.

    ``InstanceManager.stop(False)`` can mean either "the old generation was
    already gone" or "runtime cleanup could not be proven". A locking/current
    read distinguishes those cases even under MySQL REPEATABLE READ.
    """

    remaining: list[int] = []
    for instance_id, expected_pid, expected_started_at in expected_generations:
        predicates = [
            Instance.id == instance_id,
            Instance.current_task_id == task_id,
            (
                Instance.pid.is_(None)
                if expected_pid is None
                else Instance.pid == expected_pid
            ),
            (
                Instance.started_at.is_(None)
                if expected_started_at is None
                else Instance.started_at == expected_started_at
            ),
        ]
        owner = await db.scalar(
            select(Instance.id)
            .where(*predicates)
            .with_for_update()
        )
        if owner is not None:
            remaining.append(instance_id)
    # Release any row locks before further lifecycle waits/broadcasts.
    await db.rollback()
    return remaining


async def lock_task_generation(
    task_id: int,
    db: AsyncSession,
    *,
    expected_status: str,
    expected_retry_count: int,
    expected_instance_id: int | None,
    expected_started_at: datetime | None,
    expected_completed_at: datetime | None,
) -> Task | None:
    """Lock one exact Task generation until its terminal event is published."""

    predicates = [
        Task.id == task_id,
        Task.worker_id.is_(None),
        Task.shared_from_id.is_(None),
        Task.status == expected_status,
        Task.retry_count == expected_retry_count,
        (
            Task.instance_id.is_(None)
            if expected_instance_id is None
            else Task.instance_id == expected_instance_id
        ),
        (
            Task.started_at.is_(None)
            if expected_started_at is None
            else Task.started_at == expected_started_at
        ),
        (
            Task.completed_at.is_(None)
            if expected_completed_at is None
            else Task.completed_at == expected_completed_at
        ),
    ]
    locked = await db.execute(
        sa_update(Task)
        .where(*predicates)
        .values(status=expected_status)
    )
    if not locked.rowcount:
        await db.rollback()
        return None
    db.expire_all()
    return await db.get(Task, task_id)


async def read_persisted_task_completed_at(
    task_id: int,
    db: AsyncSession,
) -> datetime | None:
    """Read the database-normalized terminal timestamp written here."""

    return await db.scalar(
        select(Task.completed_at)
        .where(
            Task.id == task_id,
            Task.worker_id.is_(None),
            Task.shared_from_id.is_(None),
        )
        .with_for_update()
    )


async def _finish_termination(
    task_id: int,
    db: AsyncSession,
    *,
    expected_generations: list[
        tuple[int, int | None, datetime | None]
    ],
    expected_status: str,
    expected_retry_count: int,
    expected_instance_id: int | None,
    expected_started_at: datetime | None,
    expected_completed_at: datetime | None,
    transitioned: bool,
    auxiliary_sessions: list[tuple[int, str, str]],
) -> bool:
    await settle_task_launch_barrier(task_id, expected_instance_id)
    try:
        stopped = await stop_task_process(
            task_id,
            db,
            expected_generations=expected_generations,
            # InstanceManager only uses this fallback when the Task is still
            # active. Our generation CAS has already made it terminal, so use
            # a supported value while reconciling an existing failed Task.
            task_status=(
                expected_status
                if expected_status in {"completed", "cancelled"}
                else "completed"
            ),
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        raise TaskProcessTerminationConflict(
            [instance_id for instance_id, _pid, _started_at in expected_generations]
        ) from exc

    from backend.main import dispatcher

    for session_id, agent_type, source in auxiliary_sessions:
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
            raise TaskAuxiliaryTerminationConflict([session_id]) from exc

    remaining = await remaining_task_process_generations(
        task_id,
        db,
        expected_generations=expected_generations,
    )
    locked_task = await lock_task_generation(
        task_id,
        db,
        expected_status=expected_status,
        expected_retry_count=expected_retry_count,
        expected_instance_id=expected_instance_id,
        expected_started_at=expected_started_at,
        expected_completed_at=expected_completed_at,
    )
    if locked_task is None:
        raise TaskGenerationTerminationConflict(
            "Task started a newer generation while its old session was stopping"
        )
    if remaining:
        await db.rollback()
        raise TaskProcessTerminationConflict(remaining)

    if transitioned:
        from backend.services.task_events import broadcast_status_change

        try:
            await broadcast_status_change(task_id, expected_status)
        except BaseException:
            await db.rollback()
            raise
    await db.commit()
    return stopped


async def _read_pr_review_supersede_generation(
    task_id: int,
    db: AsyncSession,
    *,
    expected_generation: LocalTaskGeneration | None,
    active_statuses: tuple[str, ...],
    terminal_statuses: tuple[str, ...],
) -> LocalTaskGeneration:
    """Take the exact pre-abort generation without leaving a durable gate.

    Queue abort can time out before any process ownership is known. Persisting
    a retry block before that wait would strand an active Task when cleanup
    cannot even begin. The returned scalar generation is instead required by
    the post-abort locking read, where terminal status, marker and owner
    snapshot are committed atomically.
    """

    await db.rollback()
    db.expire_all()
    predicates = [
        Task.id == task_id,
        Task.worker_id.is_(None),
        Task.shared_from_id.is_(None),
    ]
    if expected_generation is not None:
        predicates = local_task_generation_predicates(
            task_id,
            expected_generation,
        )
    task = (
        await db.execute(
            select(Task).where(*predicates)
        )
    ).scalar_one_or_none()
    if task is None:
        await db.rollback()
        raise TaskGenerationTerminationConflict(
            f"Task {task_id} no longer matches the expected local generation"
        )
    if task.status not in active_statuses + terminal_statuses:
        await db.rollback()
        raise TaskGenerationTerminationConflict(
            f"Task {task_id} cannot be superseded from status {task.status}"
        )

    generation = local_task_generation(task)
    await db.rollback()
    return generation


async def _terminate_local_task_generation_impl(
    task_id: int,
    db: AsyncSession,
    *,
    reason: str,
    expected_generation: LocalTaskGeneration | None = None,
    active_statuses: tuple[str, ...] = (
        "pending",
        "in_progress",
        "executing",
        "merging",
    ),
    terminal_statuses: tuple[str, ...] = (
        "completed",
        "failed",
        "cancelled",
        "conflict",
    ),
) -> TaskTerminationResult:
    """Safely terminalize one local Task and reap its exact Instance owners.

    Queue admission is aborted first. The Task generation CAS and reverse
    Instance owner snapshot share one transaction; after commit, cleanup uses
    those exact ``(instance_id, pid, started_at)`` fences. A newer Task
    generation or reused Instance slot can therefore never be mistaken for the
    one being superseded.
    """

    from backend.main import dispatcher

    pre_abort_generation = await _read_pr_review_supersede_generation(
        task_id,
        db,
        expected_generation=expected_generation,
        active_statuses=active_statuses,
        terminal_statuses=terminal_statuses,
    )

    try:
        cleared = await dispatcher.abort_task_queue(task_id)
    except Exception as exc:
        from backend.services.dispatcher import TaskQueueAbortTimeoutError

        if isinstance(exc, TaskQueueAbortTimeoutError):
            raise TaskQueueTerminationConflict(
                f"Task {task_id} queue worker could not be proven stopped"
            ) from exc
        raise

    # Queue cancellation is a suspension point. Start a fresh current/locking
    # read afterwards so MySQL RR cannot replay the pre-abort local generation.
    await db.rollback()
    db.expire_all()
    task = (
        await db.execute(
            select(Task)
            .where(*local_task_generation_predicates(
                task_id,
                pre_abort_generation,
            ))
            .with_for_update()
        )
    ).scalar_one_or_none()
    if task is None:
        await db.rollback()
        raise TaskGenerationTerminationConflict(
            f"Task {task_id} disappeared or changed execution authority"
        )

    previous_status = task.status
    transitioned = previous_status in active_statuses
    if not transitioned and previous_status not in terminal_statuses:
        raise TaskGenerationTerminationConflict(
            f"Task {task_id} cannot be terminated from status {previous_status}"
        )

    terminal_status = "completed" if transitioned else previous_status
    metadata = dict(task.metadata_ or {})
    marker_already_persisted = (
        metadata.get(PR_REVIEW_SUPERSEDED_METADATA_KEY) is True
    )
    metadata[PR_REVIEW_SUPERSEDED_METADATA_KEY] = True
    values = {
        "status": terminal_status,
        "metadata_": metadata,
    }
    if transitioned:
        values.update(
            completed_at=datetime.utcnow(),
            error_message=reason,
        )
    generation_predicates = task_generation_fence(task_id, task)
    if not marker_already_persisted:
        generation_predicates.append(task_retry_not_superseded_predicate())
    guarded = await db.execute(
        sa_update(Task)
        .where(*generation_predicates)
        .values(**values)
    )
    if not guarded.rowcount:
        await db.rollback()
        raise TaskGenerationTerminationConflict(
            f"Task {task_id} generation changed while termination was starting"
        )

    # Global lock order is Task -> Instance. The Task UPDATE above holds the
    # generation while this reverse-owner snapshot is taken.
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

    from backend.models.monitor_session import MonitorSession

    auxiliary_rows = await db.execute(
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
    auxiliary_sessions = list(auxiliary_rows.all())
    await db.execute(
        sa_update(MonitorSession)
        .where(
            MonitorSession.task_id == task_id,
            MonitorSession.status == "running",
        )
        .values(status="cancelled", completed_at=datetime.utcnow())
    )
    expected_retry_count = task.retry_count
    expected_instance_id = task.instance_id
    expected_started_at = task.started_at
    expected_completed_at = (
        await read_persisted_task_completed_at(task_id, db)
        if transitioned
        else task.completed_at
    )
    await db.commit()

    stopped = await _finish_termination(
        task_id,
        db,
        expected_generations=expected_generations,
        expected_status=terminal_status,
        expected_retry_count=expected_retry_count,
        expected_instance_id=expected_instance_id,
        expected_started_at=expected_started_at,
        expected_completed_at=expected_completed_at,
        transitioned=transitioned,
        auxiliary_sessions=auxiliary_sessions,
    )
    return TaskTerminationResult(
        task_id=task_id,
        previous_status=previous_status,
        terminal_status=terminal_status,
        transitioned=transitioned,
        stopped=stopped,
        cleared_messages=cleared,
        retry_count=expected_retry_count,
        instance_id=expected_instance_id,
        started_at=expected_started_at,
        completed_at=expected_completed_at,
    )


async def terminate_local_task_generation(
    task_id: int,
    db: AsyncSession,
    *,
    reason: str,
    expected_generation: LocalTaskGeneration | None = None,
    active_statuses: tuple[str, ...] = (
        "pending",
        "in_progress",
        "executing",
        "merging",
    ),
    terminal_statuses: tuple[str, ...] = (
        "completed",
        "failed",
        "cancelled",
        "conflict",
    ),
) -> TaskTerminationResult:
    """Run the complete termination transaction despite caller cancellation.

    Cancellation may arrive while a database commit has an indeterminate
    outcome. Shielding only process cleanup would let the request disappear
    after publishing a terminal Task but before reaping its owner. The whole
    queue-abort → generation-CAS → owner-snapshot → reap → publication flow is
    therefore one delayed-cancellation operation.
    """

    return await _finish_despite_cancellation(
        _terminate_local_task_generation_impl(
            task_id,
            db,
            reason=reason,
            expected_generation=expected_generation,
            active_statuses=active_statuses,
            terminal_statuses=terminal_statuses,
        )
    )


async def lock_worker_task_generation(
    db: AsyncSession,
    generation,
) -> Task | None:
    """Lock an exact authoritative Worker mirror generation."""

    from backend.services.worker_relay import worker_task_generation_predicates

    guarded = await db.execute(
        sa_update(Task)
        .where(
            *worker_task_generation_predicates(generation),
            Task.shared_from_id.is_(None),
        )
        .values(status=generation.status)
    )
    if guarded.rowcount != 1:
        await db.rollback()
        return None
    db.expire_all()
    return await db.get(Task, generation.task_id)


async def _terminate_worker_task_generation_impl(
    task_id: int,
    db: AsyncSession,
    *,
    operation_locks_held: bool,
) -> WorkerTaskTerminationResult:
    """Stop one Worker Task under migration/proxy locks and mirror its result."""

    from backend.main import task_migrator, worker_proxy
    from backend.services.worker_relay import (
        apply_authoritative_worker_task,
        authoritative_worker_task_values,
        read_worker_task_generation,
        worker_task_generation,
    )

    if worker_proxy is None:
        raise WorkerTaskTerminationConflict("Worker proxy is not available")

    # End the webhook's earlier REPEATABLE READ snapshot before choosing the
    # authoritative Worker assignment.
    await db.rollback()
    db.expire_all()
    initial_task = (
        await db.execute(
            select(Task).where(
                Task.id == task_id,
                Task.worker_id.is_not(None),
                Task.shared_from_id.is_(None),
            )
        )
    ).scalar_one_or_none()
    if initial_task is None or type(initial_task.worker_id) is not int:
        await db.rollback()
        raise WorkerTaskTerminationConflict(
            f"Task {task_id} is absent or no longer Worker-authoritative"
        )
    worker_id = initial_task.worker_id
    await db.rollback()

    migration_lock = (
        task_migrator._locks.setdefault(task_id, asyncio.Lock())
        if task_migrator is not None and not operation_locks_held
        else None
    )
    operation_lock = (
        worker_proxy.task_operation_lock(task_id)
        if not operation_locks_held
        else None
    )
    async with AsyncExitStack() as stack:
        if not operation_locks_held:
            if migration_lock is not None:
                await stack.enter_async_context(migration_lock)
            await stack.enter_async_context(operation_lock)

        # Revalidate after both locks. Do not retain a database row lock across
        # network I/O; exact response application below is the durable CAS.
        await db.rollback()
        db.expire_all()
        current_task = (
            await db.execute(
                select(Task).where(
                    Task.id == task_id,
                    Task.worker_id == worker_id,
                    Task.shared_from_id.is_(None),
                )
            )
        ).scalar_one_or_none()
        observed = (
            worker_task_generation(
                current_task,
                expected_worker_id=worker_id,
            )
            if current_task is not None
            else None
        )
        if observed is None:
            await db.rollback()
            raise WorkerTaskTerminationConflict(
                f"Task {task_id} Worker assignment changed before stop"
            )
        await db.rollback()

        routing_task = SimpleNamespace(id=task_id, worker_id=worker_id)
        try:
            remote_before = await worker_proxy.proxy_to_worker(
                routing_task,
                "GET",
                f"/api/tasks/{task_id}",
                require_json=True,
                operation_lock_held=True,
            )
        except Exception as exc:
            raise WorkerTaskTerminationConflict(
                f"Could not read Worker task {task_id} before stop"
            ) from exc

        remote_values = authoritative_worker_task_values(
            remote_before,
            task_id=task_id,
        )
        if (
            remote_values is None
            or remote_before.get("retry_count") != observed.retry_count
        ):
            raise WorkerTaskTerminationConflict(
                f"Worker task {task_id} generation does not match its Manager mirror"
            )

        remote_status = remote_before["status"]
        terminal_statuses = {"completed", "failed", "cancelled", "conflict"}
        active_statuses = {"pending", "in_progress", "executing", "merging"}
        if remote_status not in active_statuses | terminal_statuses:
            raise WorkerTaskTerminationConflict(
                f"Worker task {task_id} cannot be stopped from {remote_status}"
            )
        try:
            remote_result = await worker_proxy.proxy_to_worker(
                routing_task,
                "POST",
                f"/api/tasks/{task_id}/terminate-generation",
                body={
                    "expected_status": remote_before["status"],
                    "expected_retry_count": remote_before["retry_count"],
                    "expected_instance_id": remote_before.get("instance_id"),
                    "expected_started_at": remote_before.get("started_at"),
                    "expected_completed_at": remote_before.get("completed_at"),
                },
                require_json=True,
                operation_lock_held=True,
            )
        except WorkerTaskTerminationConflict:
            raise
        except Exception as exc:
            # A timeout can mean the Worker committed the stop but its response
            # was lost. Never create a replacement on an indeterminate result;
            # a later synchronize retries through the terminal cleanup branch.
            raise WorkerTaskTerminationConflict(
                f"Worker task {task_id} stop was not authoritatively confirmed"
            ) from exc

        result_values = authoritative_worker_task_values(
            remote_result,
            task_id=task_id,
        )
        if (
            result_values is None
            or remote_result.get("retry_count") != observed.retry_count
            or remote_result.get("status") not in terminal_statuses
            or (
                remote_result.get("metadata_") or {}
            ).get(PR_REVIEW_SUPERSEDED_METADATA_KEY) is not True
        ):
            raise WorkerTaskTerminationConflict(
                f"Worker task {task_id} returned a non-terminal generation"
            )

        resulting = await apply_authoritative_worker_task(
            db,
            observed,
            remote_result,
            metadata_updates={
                PR_REVIEW_SUPERSEDED_METADATA_KEY: True,
            },
        )
        if resulting is None:
            # Relay may win the same authoritative status update while the
            # HTTP response is in flight. Re-read and either accept that exact
            # Worker retry generation or CAS the current mirror once more.
            await db.rollback()
            current = await read_worker_task_generation(db, task_id, worker_id)
            if (
                current is None
                or current.retry_count != remote_result["retry_count"]
            ):
                raise WorkerTaskTerminationConflict(
                    f"Task {task_id} mirror changed before Worker stop applied"
                )
            resulting = await apply_authoritative_worker_task(
                db,
                current,
                remote_result,
                metadata_updates={
                    PR_REVIEW_SUPERSEDED_METADATA_KEY: True,
                },
            )
            if resulting is None:
                raise WorkerTaskTerminationConflict(
                    f"Task {task_id} mirror rejected Worker stop result"
                )

        locked = await lock_worker_task_generation(db, resulting)
        if locked is None:
            raise WorkerTaskTerminationConflict(
                f"Task {task_id} changed generation before terminal publication"
            )
        if resulting.status != observed.status:
            from backend.services.task_events import broadcast_status_change

            await broadcast_status_change(task_id, resulting.status)
        await db.commit()
        return WorkerTaskTerminationResult(
            task_id=task_id,
            observed=observed,
            resulting=resulting,
        )


async def terminate_worker_task_generation(
    task_id: int,
    db: AsyncSession,
    *,
    operation_locks_held: bool = False,
) -> WorkerTaskTerminationResult:
    """Cancellation-safe authoritative stop for a Worker-owned Task."""

    return await _finish_despite_cancellation(
        _terminate_worker_task_generation_impl(
            task_id,
            db,
            operation_locks_held=operation_locks_held,
        )
    )


async def terminate_authoritative_task_generation(
    task_id: int,
    db: AsyncSession,
    *,
    reason: str,
    operation_locks_held: bool = False,
) -> TaskTerminationResult | WorkerTaskTerminationResult:
    """Route termination to the currently authoritative local/Worker owner."""

    await db.rollback()
    authority = (
        await db.execute(
            select(
                Task.worker_id,
                Task.shared_from_id,
            ).where(Task.id == task_id)
        )
    ).one_or_none()
    await db.rollback()
    if authority is None or authority.shared_from_id is not None:
        raise TaskTerminationConflict(
            f"Task {task_id} is absent or is not authoritative on this Manager"
        )
    if authority.worker_id is None:
        return await terminate_local_task_generation(
            task_id,
            db,
            reason=reason,
        )
    if type(authority.worker_id) is not int:
        raise TaskTerminationConflict(
            f"Task {task_id} has an invalid Worker assignment"
        )
    return await terminate_worker_task_generation(
        task_id,
        db,
        operation_locks_held=operation_locks_held,
    )


@asynccontextmanager
async def task_termination_operation_locks(task_ids):
    """Hold migration/proxy mutation locks through supersede replacement."""

    from backend.main import task_migrator, worker_proxy

    async with AsyncExitStack() as stack:
        for task_id in sorted(set(task_ids)):
            if task_migrator is not None:
                migration_lock = task_migrator._locks.setdefault(
                    task_id,
                    asyncio.Lock(),
                )
                await stack.enter_async_context(migration_lock)
            if worker_proxy is not None:
                await stack.enter_async_context(
                    worker_proxy.task_operation_lock(task_id)
                )
        yield
