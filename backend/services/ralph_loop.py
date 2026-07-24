import asyncio
import logging
from datetime import datetime

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.instance import Instance
from backend.models.task import Task
from backend.config import settings
from backend.services.instance_manager import InstanceManager
from backend.services.dispatcher import TaskStartPausedError
from backend.services.task_queue import TaskQueue
from backend.services.ws_broadcaster import WebSocketBroadcaster

logger = logging.getLogger(__name__)


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

    async def start(self, instance_id: int):
        if instance_id in self._loops and not self._loops[instance_id].done():
            return
        self._loops[instance_id] = asyncio.create_task(self._loop(instance_id))
        logger.info(f"Ralph loop started for instance {instance_id}")

    async def stop(self, instance_id: int):
        task = self._loops.get(instance_id)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        if self._loops.get(instance_id) is task:
            self._loops.pop(instance_id, None)
        logger.info(f"Ralph loop stopped for instance {instance_id}")

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
                process.kill()
                await process.wait()

        try:
            await self.instance_manager.wait_for_output_consumer(
                instance_id,
                provider=task.provider,
                timeout=30,
                expected_process=process,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "Output consumer did not finish after %s for task %s",
                label,
                task.id,
            )

    async def _handle_account_routing_failure(
        self,
        instance_id: int,
        task_id: int,
        reason: str,
        *,
        retry_after: float | None,
    ) -> float:
        """Release a Ralph-owned task when account routing cannot launch it."""

        if retry_after is None:
            async with self.db_factory() as db:
                # A user cancellation/deletion may win while account routing is
                # failing.  Only fail the task while Ralph still owns an active
                # claim; never overwrite a concurrent terminal state.
                result = await db.execute(
                    update(Task)
                    .where(
                        Task.id == task_id,
                        Task.status.in_(("in_progress", "executing")),
                    )
                    .values(
                        status="failed",
                        error_message=reason[:500],
                        completed_at=datetime.utcnow(),
                    )
                )
                await db.commit()
            if not result.rowcount:
                return 0.0
            status = "failed"
            delay = 0.0
        else:
            async with self.db_factory() as db:
                queue = TaskQueue(db)
                deferred = await queue.defer(task_id, reason[:500])
            if not deferred:
                return 0.0
            status = "pending"
            delay = max(1.0, min(float(retry_after), 300.0))

        await self.broadcaster.broadcast("tasks", {
            "event": "status_change",
            "task_id": task_id,
            "new_status": status,
            "instance_id": instance_id,
            "reason": "codex_account_wait" if status == "pending" else "codex_account_routing",
        })
        return delay

    async def _release_cancelled_claim(
        self,
        instance_id: int,
        task: Task | None,
    ) -> None:
        """Stop a Ralph-owned turn and return its claim to the pending queue.

        Cancelling only the loop used to strand the task in ``in_progress``;
        cancelling while a subprocess was active also left that process running
        without a lifecycle owner.  Ownership is checked both before stopping
        the process and in the final compare-and-swap defer, so a concurrent
        user cancellation or terminal transition always wins.
        """

        if task is None:
            return

        task_id = task.id
        provider = (task.provider or "claude").lower()
        process_owned = False
        try:
            async with self.db_factory() as db:
                current = await db.get(Task, task_id)
                instance = await db.get(Instance, instance_id)
                if (
                    current is None
                    or current.instance_id != instance_id
                    or current.status not in ("in_progress", "executing")
                ):
                    return
                provider = (current.provider or "claude").lower()
                process_owned = bool(
                    instance and instance.current_task_id == task_id
                )

            owned_process = self.instance_manager.processes.get(instance_id)
            if process_owned and self.instance_manager.is_running(instance_id):
                await self.instance_manager.stop(instance_id)
            if process_owned:
                await self.instance_manager.wait_for_output_consumer(
                    instance_id,
                    provider=provider,
                    timeout=30,
                    expected_process=owned_process,
                )
        except Exception:
            # The status CAS below is still required even when process cleanup
            # reports an error; leaving an unowned in_progress row wedges it
            # forever. InstanceManager retains its own process/consumer guards.
            logger.exception(
                "Failed to stop Ralph-owned process for task %s on instance %s",
                task_id,
                instance_id,
            )

        try:
            async with self.db_factory() as db:
                queue = TaskQueue(db)
                released = await queue.defer(
                    task_id,
                    "Ralph loop stopped; task returned to the queue",
                    instance_id=instance_id,
                )
        except Exception:
            logger.exception(
                "Failed to release cancelled Ralph claim for task %s",
                task_id,
            )
            return

        if released:
            await self.broadcaster.broadcast("tasks", {
                "event": "status_change",
                "task_id": task_id,
                "old_status": "in_progress",
                "new_status": "pending",
                "instance_id": instance_id,
                "reason": "ralph_stopped",
            })

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

                # Broadcast task assignment
                await self.broadcaster.broadcast("tasks", {
                    "event": "status_change",
                    "task_id": task.id,
                    "old_status": "pending",
                    "new_status": "in_progress",
                    "instance_id": instance_id,
                })

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

                        await db.execute(
                            update(Task)
                            .where(Task.id == task.id)
                            .values(plan_content=plan_content, status="plan_review")
                        )
                        await db.commit()

                    await self.broadcaster.broadcast("tasks", {
                        "event": "plan_ready",
                        "task_id": task.id,
                        "instance_id": instance_id,
                    })
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
                    if exit_code == 0:
                        await queue.mark_completed(task.id)
                        status = "completed"
                    else:
                        t = await queue.get(task.id)
                        if t and t.retry_count < t.max_retries:
                            await queue.retry(task.id)
                            status = "retrying"
                        else:
                            await queue.mark_failed(task.id, f"Exit code: {exit_code}")
                            status = "failed"

                await self.broadcaster.broadcast("tasks", {
                    "event": "status_change",
                    "task_id": task.id,
                    "new_status": status,
                    "instance_id": instance_id,
                })

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
                        task.id,
                        str(e),
                        retry_after=retry_after,
                    )
                    if delay:
                        await asyncio.sleep(delay)
                    continue
                logger.error(f"Ralph loop error for instance {instance_id}: {e}")
                await asyncio.sleep(5)
