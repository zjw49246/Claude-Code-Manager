import asyncio
import logging
from datetime import datetime

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models.instance import Instance
from backend.models.task import Task
from backend.config import settings
from backend.services.instance_manager import InstanceManager
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
        task = self._loops.pop(instance_id, None)
        if task and not task.done():
            task.cancel()
        logger.info(f"Ralph loop stopped for instance {instance_id}")

    def is_running(self, instance_id: int) -> bool:
        task = self._loops.get(instance_id)
        return task is not None and not task.done()

    async def _loop(self, instance_id: int):
        logger.info(f"Ralph loop running for instance {instance_id}")
        while True:
            try:
                # Dequeue next task
                async with self.db_factory() as db:
                    queue = TaskQueue(db)
                    task = await queue.dequeue()

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

                thinking_budget = task.thinking_budget
                async with self.db_factory() as db:
                    await db.execute(
                        update(Task)
                        .where(Task.id == task.id)
                        .values(instance_id=instance_id)
                    )
                    await db.commit()

                # Plan mode handling
                if task.mode == "plan" and not task.plan_approved:
                    logger.info(f"Task {task.id} is in plan mode, running plan phase")
                    plan_prompt = f"Please analyze the following task and create a detailed plan. Do NOT execute any changes, only describe what you would do:\n\n{task.description}"
                    await self.instance_manager.launch(
                        instance_id=instance_id,
                        prompt=plan_prompt,
                        task_id=task.id,
                        cwd=cwd,
                        model=None,
                        thinking_budget=thinking_budget,
                        provider=task.provider,
                    )
                    process = self.instance_manager.processes.get(instance_id)
                    if process:
                        try:
                            await asyncio.wait_for(process.wait(), timeout=settings.task_timeout_seconds)
                        except asyncio.TimeoutError:
                            logger.warning(f"Plan phase for task {task.id} timed out, killing process")
                            process.kill()
                            await process.wait()

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
                await self.instance_manager.launch(
                    instance_id=instance_id,
                    prompt=task.description,
                    task_id=task.id,
                    cwd=cwd,
                    model=None,
                    thinking_budget=thinking_budget,
                    provider=task.provider,
                )

                # Wait for process to finish (with timeout)
                process = self.instance_manager.processes.get(instance_id)
                if process:
                    try:
                        await asyncio.wait_for(process.wait(), timeout=settings.task_timeout_seconds)
                    except asyncio.TimeoutError:
                        logger.warning(f"Task {task.id} timed out after {settings.task_timeout_seconds}s, killing process")
                        process.kill()
                        await process.wait()

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
                break
            except Exception as e:
                logger.error(f"Ralph loop error for instance {instance_id}: {e}")
                await asyncio.sleep(5)
