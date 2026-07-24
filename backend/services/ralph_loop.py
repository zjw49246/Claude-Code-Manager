import asyncio
import logging
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.instance import Instance
from backend.models.task import Task
from backend.config import settings
from backend.services.instance_manager import InstanceManager
from backend.services.dispatcher import TaskStartPausedError
from backend.services.task_queue import (
    TaskGenerationFence,
    TaskQueue,
    append_task_generation_predicates,
    task_generation_fence,
)
from backend.services.ws_broadcaster import WebSocketBroadcaster

logger = logging.getLogger(__name__)
DEFAULT_RALPH_STOP_TIMEOUT = 15.0


class RalphLoop:
    """Auto-continuation loop: pick task -> run -> repeat.

    Claude Code handles worktree creation, git operations, and cleanup
    autonomously based on the project's CLAUDE.md instructions.
    """

    def __init__(
        self,
        db_factory,
        instance_manager: InstanceManager,
        broadcaster: WebSocketBroadcaster,
    ):
        self.db_factory = db_factory
        self.instance_manager = instance_manager
        self.broadcaster = broadcaster
        self._loops: dict[int, asyncio.Task] = {}
        self._shutting_down = False

    async def start(self, instance_id: int):
        if self._shutting_down:
            raise RuntimeError("Ralph loop is shutting down")
        if instance_id in self._loops and not self._loops[instance_id].done():
            return
        self._loops[instance_id] = asyncio.create_task(self._loop(instance_id))
        logger.info(f"Ralph loop started for instance {instance_id}")

    async def stop(
        self,
        instance_id: int,
        *,
        timeout: float = DEFAULT_RALPH_STOP_TIMEOUT,
    ) -> bool:
        """Cancel one loop with a bounded, evidence-preserving wait."""

        task = self._loops.get(instance_id)
        if task and not task.done():
            task.cancel()
            done, pending = await asyncio.wait({task}, timeout=timeout)
            if pending:
                logger.error(
                    "Ralph loop for instance %s ignored cancellation for %.1fs",
                    instance_id,
                    timeout,
                )
                # Keep the exact task registered so a later admin stop or
                # shutdown can retry.  Popping here would make a still-live
                # dequeue owner invisible and allow unsafe instance deletion.
                return False
            await asyncio.gather(*done, return_exceptions=True)
        if self._loops.get(instance_id) is task:
            self._loops.pop(instance_id, None)
        logger.info(f"Ralph loop stopped for instance {instance_id}")
        return True

    async def shutdown(
        self,
        *,
        timeout: float = DEFAULT_RALPH_STOP_TIMEOUT,
    ) -> None:
        """Close admission and settle every legacy dequeue producer."""

        self._shutting_down = True
        observed = dict(self._loops)
        pending_tasks = {
            task for task in observed.values() if not task.done()
        }
        for task in pending_tasks:
            task.cancel()
        if pending_tasks:
            done, pending = await asyncio.wait(
                pending_tasks,
                timeout=timeout,
            )
            if done:
                await asyncio.gather(*done, return_exceptions=True)
            if pending:
                # Retain exact registrations so shutdown retry/diagnostics can
                # still find every producer that ignored cancellation.
                raise RuntimeError(
                    f"{len(pending)} Ralph loop(s) ignored shutdown "
                    f"cancellation for {timeout:.1f}s"
                )
        for instance_id, task in observed.items():
            if task.done() and self._loops.get(instance_id) is task:
                self._loops.pop(instance_id, None)
        logger.info("Ralph loop shutdown complete")

    def is_running(self, instance_id: int) -> bool:
        task = self._loops.get(instance_id)
        return task is not None and not task.done()

    async def _launch_task_on_bound_account(
        self,
        instance_id: int,
        task: Task,
        prompt: str,
        cwd: str,
    ) -> int:
        """Launch through the same provider-account resolver as Dispatcher.

        Ralph is a legacy dequeue path, but it still runs normal Task rows.  A
        Codex task must therefore keep its native thread and CODEX_HOME binding
        instead of silently inheriting the service's default account.
        """

        from backend.main import dispatcher

        config_dir = await dispatcher._resolve_resume_config_dir(
            task.session_id,
            task.provider,
            task_id=task.id,
        )
        resume_session_id = (
            task.session_id
            if (task.provider or "claude").lower() == "codex"
            else None
        )
        return await self.instance_manager.launch(
            instance_id=instance_id,
            prompt=prompt,
            task_id=task.id,
            cwd=cwd,
            model=None,
            resume_session_id=resume_session_id,
            thinking_budget=task.thinking_budget,
            provider=task.provider,
            config_dir=config_dir,
        )

    async def _wait_for_turn(
        self,
        instance_id: int,
        task: Task,
        process,
        *,
        label: str,
    ) -> None:
        """Wait for both the CLI turn and its output/account bookkeeping."""

        if process:
            try:
                await asyncio.wait_for(
                    process.wait(), timeout=settings.task_timeout_seconds
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "%s for task %s timed out after %ss, killing process",
                    label,
                    task.id,
                    settings.task_timeout_seconds,
                )
                killed = await self.instance_manager.kill_process_generation(
                    instance_id,
                    process,
                )
                if not killed:
                    raise RuntimeError(
                        f"Timed-out process generation changed for instance {instance_id}"
                    )

        try:
            await self.instance_manager.wait_for_output_consumer(
                instance_id,
                provider=task.provider,
                timeout=30,
                expected_process=process,
            )
        except asyncio.TimeoutError as exc:
            # The CLI parent may be terminal while its output consumer still
            # owns process-group reaping, DB finalization, or account/session
            # migration.  Treating that timeout as success would let Ralph
            # mark the task complete and reuse the Instance while the exact
            # generation is still live.  Bubble into the fail-closed lifecycle
            # handler, which retains and reaps the observed process identity.
            raise RuntimeError(
                f"Output consumer did not finish after {label} for task "
                f"{task.id}"
            ) from exc

    async def _broadcast_generation_event(
        self,
        task_id: int,
        original_generation: TaskGenerationFence,
        expected_status: str,
        event: dict,
        *,
        retry_count_delta: int = 0,
        released: bool = False,
        terminal: bool = False,
    ) -> bool:
        """Publish while holding a no-op lock on the exact resulting Task.

        The state transition itself commits before publication. This second
        exact UPDATE prevents a retry/reclaim from slipping between the final
        generation check and the awaited WebSocket broadcast.
        """

        (
            original_retry_count,
            original_instance_id,
            original_started_at,
            original_completed_at,
        ) = original_generation
        expected_retry_count = original_retry_count + retry_count_delta
        expected_instance_id = None if released else original_instance_id
        expected_started_at = None if released else original_started_at

        async with self.db_factory() as db:
            current = await db.get(Task, task_id)
            if (
                current is None
                or current.status != expected_status
                or current.retry_count != expected_retry_count
                or current.instance_id != expected_instance_id
                or current.started_at != expected_started_at
                or (
                    terminal
                    and current.completed_at is None
                )
                or (
                    not terminal
                    and current.completed_at
                    != (None if released else original_completed_at)
                )
            ):
                return False

            resulting_generation = task_generation_fence(current)
            predicates = [
                Task.id == task_id,
                Task.status == expected_status,
            ]
            append_task_generation_predicates(
                predicates,
                resulting_generation,
            )
            locked = await db.execute(
                update(Task)
                .where(*predicates)
                .values(status=expected_status)
            )
            if not locked.rowcount:
                await db.rollback()
                return False
            try:
                await self.broadcaster.broadcast("tasks", event)
            except Exception:
                logger.exception(
                    "Failed to broadcast Ralph generation event for task %s",
                    task_id,
                )
            await db.commit()
            return True

    async def _handle_account_routing_failure(
        self,
        instance_id: int,
        task: Task,
        reason: str,
        *,
        retry_after: float | None,
    ) -> float:
        """Release a Ralph-owned task when account routing cannot launch it."""

        task_id = task.id
        generation = task_generation_fence(task)
        if retry_after is None:
            async with self.db_factory() as db:
                queue = TaskQueue(db)
                failed = await queue.mark_failed(
                    task_id,
                    reason[:500],
                    expected_statuses=("in_progress", "executing"),
                    instance_id=instance_id,
                    generation_fence=generation,
                )
            if not failed:
                return 0.0
            status = "failed"
            delay = 0.0
        else:
            async with self.db_factory() as db:
                queue = TaskQueue(db)
                deferred = await queue.defer(
                    task_id,
                    reason[:500],
                    instance_id=instance_id,
                    generation_fence=generation,
                )
            if not deferred:
                return 0.0
            status = "pending"
            delay = max(1.0, min(float(retry_after), 300.0))

        await self._broadcast_generation_event(
            task_id,
            generation,
            status,
            {
            "event": "status_change",
            "task_id": task_id,
            "new_status": status,
            "instance_id": instance_id,
            "reason": "codex_account_wait" if status == "pending" else "codex_account_routing",
            },
            released=status == "pending",
            terminal=status == "failed",
        )
        return delay

    async def _store_plan_if_owned(
        self,
        instance_id: int,
        task: Task,
        plan_content: str,
    ) -> bool:
        """Publish a plan only while this Ralph generation owns the task."""

        predicates = [
            Task.id == task.id,
            Task.status.in_(("in_progress", "executing")),
            Task.instance_id == instance_id,
        ]
        append_task_generation_predicates(
            predicates,
            task_generation_fence(task),
        )
        async with self.db_factory() as db:
            result = await db.execute(
                update(Task)
                .where(*predicates)
                .values(plan_content=plan_content, status="plan_review")
            )
            await db.commit()
        return bool(result.rowcount)

    async def _record_cancel_cleanup_failure(
        self,
        instance_id: int,
        task_id: int,
        reason: str,
        *,
        instance_snapshot: tuple[
            str,
            int | None,
            int | None,
            datetime | None,
        ],
        generation_fence: TaskGenerationFence | None = None,
        task_statuses: tuple[str, ...] = ("in_progress", "executing"),
        broadcast_event: bool = True,
    ) -> bool:
        """Fail only the exact instance generation whose cleanup failed."""

        message = reason[:500]
        (
            expected_status,
            expected_pid,
            expected_task_id,
            expected_started_at,
        ) = instance_snapshot
        if expected_task_id != task_id:
            return False

        async with self.db_factory() as db:
            # Lock/update the Task before the Instance, matching every other
            # dual-row lifecycle transaction.  If the Instance CAS below
            # loses to a replacement generation, rolling this transaction
            # back also restores the Task atomically.
            task_predicates = [
                Task.id == task_id,
                Task.status.in_(task_statuses),
                Task.instance_id == instance_id,
            ]
            append_task_generation_predicates(
                task_predicates,
                generation_fence,
            )
            task_result = await db.execute(
                update(Task)
                .where(*task_predicates)
                .values(
                    status="failed",
                    error_message=message,
                    completed_at=datetime.utcnow(),
                )
            )
            if not task_result.rowcount:
                await db.rollback()
                return False

            instance_predicates = [
                Instance.id == instance_id,
                Instance.status == expected_status,
                Instance.current_task_id == expected_task_id,
            ]
            instance_predicates.append(
                Instance.pid.is_(None)
                if expected_pid is None
                else Instance.pid == expected_pid
            )
            instance_predicates.append(
                Instance.started_at.is_(None)
                if expected_started_at is None
                else Instance.started_at == expected_started_at
            )
            instance_result = await db.execute(
                update(Instance)
                .where(*instance_predicates)
                .values(status="error")
            )
            if not instance_result.rowcount:
                await db.rollback()
                return False
            await db.commit()

        if (
            task_result.rowcount
            and generation_fence is not None
            and broadcast_event
        ):
            await self._broadcast_generation_event(
                task_id,
                generation_fence,
                "failed",
                {
                    "event": "status_change",
                    "task_id": task_id,
                    "old_status": "in_progress",
                    "new_status": "failed",
                    "instance_id": instance_id,
                    "reason": "ralph_stop_cleanup_failed",
                },
                terminal=True,
            )
        return bool(task_result.rowcount)

    async def _release_cancelled_claim(
        self,
        instance_id: int,
        task: Task | None,
    ) -> None:
        """Stop a Ralph-owned turn and release only a proven-clean claim.

        Cancelling only the loop used to strand the task in ``in_progress``;
        cancelling while a subprocess was active also left that process running
        without a lifecycle owner. Cleanup failures retain the instance/process
        evidence and fail the task instead of creating a second runnable copy.
        """

        if task is None:
            return

        task_id = task.id
        instance_snapshot: tuple[
            str,
            int | None,
            int | None,
            datetime | None,
        ] | None = None
        try:
            async with self.db_factory() as db:
                current = await db.get(Task, task_id)
                instance = await db.get(Instance, instance_id)
                if (
                    current is None
                    or current.instance_id != instance_id
                    or current.status not in ("in_progress", "executing")
                    or task_generation_fence(current)
                    != task_generation_fence(task)
                ):
                    return
                if instance is not None:
                    instance_snapshot = (
                        instance.status,
                        instance.pid,
                        instance.current_task_id,
                        instance.started_at,
                    )
                process_owned = bool(
                    instance and instance.current_task_id == task_id
                )

            manager_running = self.instance_manager.is_running(instance_id)
            if process_owned:
                if not manager_running:
                    raise RuntimeError(
                        "Ralph could not prove that the persisted process "
                        "generation was reaped"
                    )
                stopped = await self.instance_manager.stop(
                    instance_id,
                    expected_task_id=task_id,
                    expected_pid=instance_snapshot[1],
                    expected_started_at=instance_snapshot[3],
                    terminal_consumer_timeout=30.0,
                    consumer_cancel_timeout=10.0,
                )
                if not stopped:
                    raise RuntimeError(
                        "Ralph process cleanup did not settle the owned generation"
                    )

                # InstanceManager.stop owns the atomic process/consumer cleanup
                # and claim release. Never inspect or mutate the task after its
                # successful commit: it may already have been claimed again on
                # this same reusable instance by a newer generation.
                return

            if manager_running:
                raise RuntimeError(
                    "Instance has a managed generation that is not owned by "
                    f"Ralph task {task_id}"
                )
        except Exception as exc:
            logger.exception(
                "Failed to stop Ralph-owned process for task %s on instance %s",
                task_id,
                instance_id,
            )
            try:
                if instance_snapshot is not None:
                    await self._record_cancel_cleanup_failure(
                        instance_id,
                        task_id,
                        "Ralph loop stopped but process cleanup could not be "
                        f"confirmed: {exc}",
                        instance_snapshot=instance_snapshot,
                        generation_fence=task_generation_fence(task),
                    )
            except Exception:
                logger.exception(
                    "Failed to preserve cancelled Ralph claim for task %s",
                    task_id,
                )
            return

        try:
            async with self.db_factory() as db:
                queue = TaskQueue(db)
                released = await queue.defer(
                    task_id,
                    "Ralph loop stopped; task returned to the queue",
                    instance_id=instance_id,
                    generation_fence=task_generation_fence(task),
                )
        except Exception:
            logger.exception(
                "Failed to release cancelled Ralph claim for task %s",
                task_id,
            )
            return

        if released:
            await self._broadcast_generation_event(
                task_id,
                task_generation_fence(task),
                "pending",
                {
                    "event": "status_change",
                    "task_id": task_id,
                    "old_status": "in_progress",
                    "new_status": "pending",
                    "instance_id": instance_id,
                    "reason": "ralph_stopped",
                },
                released=True,
            )

    async def _fail_unexpected_claim(
        self,
        instance_id: int,
        task: Task | None,
        exc: Exception,
    ) -> None:
        """Fail and reap the exact Ralph generation after an internal error.

        An exception may happen before launch, while the process is running, or
        in output bookkeeping.  Leaving the dequeue claim active makes Ralph
        sleep and retry its outer loop forever while the Task remains stuck.
        Mark the still-owned Task terminal first so no scheduler can duplicate
        uncertain work, then reap only the process object observed for that
        generation.  Process identity plus the persisted Instance snapshot
        prevent a rapid retry on the same reusable slot from being killed or
        overwritten by this stale error handler.
        """

        if task is None:
            return

        task_id = task.id
        reason = f"Ralph loop failed: {exc}"[:500]
        expected_retry_count = task.retry_count
        expected_started_at = task.started_at
        expected_completed_at = task.completed_at
        instance_snapshot: tuple[
            str,
            int | None,
            int | None,
            datetime | None,
        ] | None = None
        process = None

        async with self.db_factory() as db:
            current = await db.get(Task, task_id)
            instance = await db.get(Instance, instance_id)
            if (
                current is None
                or current.status not in ("in_progress", "executing")
                or current.instance_id != instance_id
                or current.retry_count != expected_retry_count
                or current.started_at != expected_started_at
                or current.completed_at != expected_completed_at
            ):
                return
            if instance is not None:
                instance_snapshot = (
                    instance.status,
                    instance.pid,
                    instance.current_task_id,
                    instance.started_at,
                )
                if instance.current_task_id == task_id:
                    process = self.instance_manager.processes.get(instance_id)

            failed_at = datetime.utcnow()
            task_predicates = [
                Task.id == task_id,
                Task.status.in_(("in_progress", "executing")),
                Task.instance_id == instance_id,
                Task.retry_count == expected_retry_count,
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
            result = await db.execute(
                update(Task)
                .where(*task_predicates)
                .values(
                    status="failed",
                    error_message=reason,
                    completed_at=failed_at,
                )
            )
            persisted_failed_at = None
            if result.rowcount:
                # MySQL DATETIME may normalize away microseconds.  Read the
                # exact stored value while this Task lock is still held before
                # using it as the cleanup generation fence.
                persisted_failed_at = await db.scalar(
                    select(Task.completed_at)
                    .where(Task.id == task_id)
                    .with_for_update()
                )
            await db.commit()

        if not result.rowcount:
            return

        cleanup_error: Exception | None = None
        if instance_snapshot is not None and instance_snapshot[2] == task_id:
            if process is None:
                cleanup_error = RuntimeError(
                    "persisted Ralph process generation is not managed in memory"
                )
            else:
                try:
                    killed = await self.instance_manager.kill_process_generation(
                        instance_id,
                        process,
                    )
                    if killed:
                        await self.instance_manager.wait_for_output_consumer(
                            instance_id,
                            provider=task.provider,
                            timeout=30,
                            expected_process=process,
                            preserve_error=True,
                        )
                    # If the map already points at a replacement process, exact
                    # identity did its job. Never fall back to task-id stop.
                except Exception as cleanup_exc:
                    cleanup_error = cleanup_exc

        if cleanup_error is not None:
            logger.error(
                "Failed to reap Ralph generation for task %s on instance %s",
                task_id,
                instance_id,
                exc_info=(
                    type(cleanup_error),
                    cleanup_error,
                    cleanup_error.__traceback__,
                ),
            )
            await self._record_cancel_cleanup_failure(
                instance_id,
                task_id,
                f"{reason}; process cleanup could not be confirmed: "
                f"{cleanup_error}",
                instance_snapshot=instance_snapshot,
                generation_fence=(
                    expected_retry_count,
                    instance_id,
                    expected_started_at,
                    persisted_failed_at,
                ),
                task_statuses=("failed",),
                broadcast_event=False,
            )

        await self._broadcast_generation_event(
            task_id,
            (
                expected_retry_count,
                instance_id,
                expected_started_at,
                expected_completed_at,
            ),
            "failed",
            {
                "event": "status_change",
                "task_id": task_id,
                "old_status": "in_progress",
                "new_status": "failed",
                "instance_id": instance_id,
                "reason": "ralph_internal_error",
            },
            terminal=True,
        )

    async def _loop(self, instance_id: int):
        logger.info(f"Ralph loop running for instance {instance_id}")
        while True:
            task = None
            try:
                # Dequeue next task
                from backend.main import dispatcher
                try:
                    async with dispatcher.task_start_guard():
                        async with self.db_factory() as db:
                            queue = TaskQueue(db)
                            task = await queue.dequeue(instance_id=instance_id)
                except TaskStartPausedError:
                    await dispatcher.wait_until_resumed()
                    continue

                if not task:
                    await asyncio.sleep(5)
                    continue

                logger.info(f"Instance {instance_id} picked task {task.id}: {task.title}")

                # Publish the claim while holding the exact resulting Task
                # generation. A cancellation/retry that wins after dequeue
                # must not be followed by a stale ``in_progress`` event, nor
                # by this Ralph loop launching that superseded claim.
                claim_is_current = await self._broadcast_generation_event(
                    task.id,
                    task_generation_fence(task),
                    "in_progress",
                    {
                        "event": "status_change",
                        "task_id": task.id,
                        "old_status": "pending",
                        "new_status": "in_progress",
                        "instance_id": instance_id,
                    },
                )
                if not claim_is_current:
                    continue

                cwd = task.target_repo or "."

                # Plan mode handling
                if task.mode == "plan" and not task.plan_approved:
                    logger.info(f"Task {task.id} is in plan mode, running plan phase")
                    plan_prompt = f"Please analyze the following task and create a detailed plan. Do NOT execute any changes, only describe what you would do:\n\n{task.description}"
                    await self._launch_task_on_bound_account(
                        instance_id,
                        task,
                        plan_prompt,
                        cwd,
                    )
                    process = self.instance_manager.processes.get(instance_id)
                    await self._wait_for_turn(
                        instance_id,
                        task,
                        process,
                        label="Plan phase",
                    )

                    # Collect plan content from logs
                    async with self.db_factory() as db:
                        from sqlalchemy import select
                        from backend.models.log_entry import LogEntry
                        result = await db.execute(
                            select(LogEntry.content)
                            .where(LogEntry.task_id == task.id, LogEntry.event_type == "message", LogEntry.role == "assistant")
                            .order_by(LogEntry.id)
                        )
                        plan_texts = [r[0] for r in result.all() if r[0]]
                        plan_content = "\n".join(plan_texts)

                    stored = await self._store_plan_if_owned(
                        instance_id,
                        task,
                        plan_content,
                    )
                    if stored:
                        await self._broadcast_generation_event(
                            task.id,
                            task_generation_fence(task),
                            "plan_review",
                            {
                                "event": "plan_ready",
                                "task_id": task.id,
                                "instance_id": instance_id,
                            },
                        )
                    continue  # Move to next task; this one waits for approval

                # Normal execution — Claude Code is fully autonomous
                await self._launch_task_on_bound_account(
                    instance_id,
                    task,
                    task.description,
                    cwd,
                )

                # Wait for process to finish (with timeout)
                process = self.instance_manager.processes.get(instance_id)
                await self._wait_for_turn(
                    instance_id,
                    task,
                    process,
                    label="Task run",
                )

                exit_code = process.returncode if process else -1

                # Handle result
                async with self.db_factory() as db:
                    queue = TaskQueue(db)
                    status = None
                    if exit_code == 0:
                        if await queue.mark_completed(
                            task.id,
                            instance_id=instance_id,
                            generation_fence=task_generation_fence(task),
                        ):
                            status = "completed"
                    else:
                        if (
                            task.retry_count < task.max_retries
                        ):
                            retried = await queue.retry(
                                task.id,
                                expected_statuses=("in_progress", "executing"),
                                instance_id=instance_id,
                                generation_fence=task_generation_fence(task),
                            )
                            if retried:
                                status = "retrying"
                        else:
                            failed = await queue.mark_failed(
                                task.id,
                                f"Exit code: {exit_code}",
                                instance_id=instance_id,
                                generation_fence=task_generation_fence(task),
                            )
                            if failed:
                                status = "failed"

                if status is not None:
                    await self._broadcast_generation_event(
                        task.id,
                        task_generation_fence(task),
                        "pending" if status == "retrying" else status,
                        {
                            "event": "status_change",
                            "task_id": task.id,
                            "new_status": status,
                            "instance_id": instance_id,
                        },
                        retry_count_delta=1 if status == "retrying" else 0,
                        released=status == "retrying",
                        terminal=status in ("completed", "failed"),
                    )

            except asyncio.CancelledError:
                logger.info(f"Ralph loop cancelled for instance {instance_id}")
                # Once dequeue succeeds, cancellation must either reconcile the
                # active turn or atomically return the claim.  Await cleanup
                # before allowing stop() to report success. Repeated caller
                # cancellation must not interrupt this ownership handoff.
                cleanup = asyncio.create_task(
                    self._release_cancelled_claim(instance_id, task)
                )
                while not cleanup.done():
                    try:
                        await asyncio.shield(cleanup)
                    except asyncio.CancelledError:
                        continue
                cleanup.result()
                raise
            except Exception as e:
                from backend.services.codex_app_server import (
                    CodexAppServerBusyError,
                    CodexThreadHomeMismatchError,
                )
                from backend.services.dispatcher import CodexAccountRoutingError

                if task is not None and isinstance(
                    e,
                    (
                        CodexAccountRoutingError,
                        CodexAppServerBusyError,
                        CodexThreadHomeMismatchError,
                    ),
                ):
                    retry_after = (
                        e.retry_after
                        if isinstance(e, CodexAccountRoutingError)
                        else 5.0
                    )
                    delay = await self._handle_account_routing_failure(
                        instance_id,
                        task,
                        str(e),
                        retry_after=retry_after,
                    )
                    if delay:
                        await asyncio.sleep(delay)
                    continue
                logger.error(f"Ralph loop error for instance {instance_id}: {e}")
                cleanup = asyncio.create_task(
                    self._fail_unexpected_claim(instance_id, task, e)
                )
                cancellation: asyncio.CancelledError | None = None
                while not cleanup.done():
                    try:
                        await asyncio.shield(cleanup)
                    except asyncio.CancelledError as cancel_exc:
                        cancellation = cancel_exc
                cleanup.result()
                if cancellation is not None:
                    raise cancellation
                await asyncio.sleep(5)
