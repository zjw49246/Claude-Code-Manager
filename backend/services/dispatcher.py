import asyncio
import logging
from datetime import datetime

from sqlalchemy import select, update

from sqlalchemy import select as sa_select

from backend.config import settings
from backend.models.instance import Instance
from backend.models.task import Task
from backend.models.project import Project
from backend.models.global_settings import GlobalSettings
from backend.models.secret import Secret
from backend.services.git_config import merge_git_config, settings_to_dict
from backend.services.instance_manager import InstanceManager
from backend.services.task_queue import TaskQueue
from backend.services.ws_broadcaster import WebSocketBroadcaster

logger = logging.getLogger(__name__)


def _build_git_env(merged_config: dict) -> dict:
    """Build git-related environment variables from a merged git config dict.

    GIT_AUTHOR_* / GIT_COMMITTER_* override user.name/email for every git commit
    executed inside the Claude Code subprocess, regardless of any ~/.gitconfig.
    GIT_SSH_COMMAND overrides the SSH key used for push/pull over SSH.
    GIT_ASKPASS overrides credentials for push/pull over HTTPS.

    Both SSH and HTTPS credentials are injected simultaneously when available,
    because the remote URL protocol determines which one git actually uses.
    This way, users don't need to worry about matching credential type to URL.

    Priority: project-level > global settings > instance-level (settings.git_ssh_key_path).
    """
    env: dict = {}
    if merged_config.get("git_author_name"):
        env["GIT_AUTHOR_NAME"] = merged_config["git_author_name"]
        env["GIT_COMMITTER_NAME"] = merged_config["git_author_name"]
    if merged_config.get("git_author_email"):
        env["GIT_AUTHOR_EMAIL"] = merged_config["git_author_email"]
        env["GIT_COMMITTER_EMAIL"] = merged_config["git_author_email"]

    # Inject SSH credentials if available
    if merged_config.get("git_ssh_key_path"):
        env["GIT_SSH_COMMAND"] = f"ssh -i {merged_config['git_ssh_key_path']} -o IdentitiesOnly=yes -o StrictHostKeyChecking=no"

    # Inject HTTPS credentials if available
    if merged_config.get("git_https_token"):
        askpass_script = _get_or_create_askpass_script(
            merged_config.get("git_https_username") or "",
            merged_config["git_https_token"],
        )
        env["GIT_ASKPASS"] = askpass_script
        env["GIT_TERMINAL_PROMPT"] = "0"
        # Bypass global/system git config entirely so that macOS osxkeychain
        # (or any other system credential helper) never intercepts our credentials.
        # GIT_CONFIG_COUNT approach doesn't work: empty credential.helper via env
        # is treated as an additive entry, not a chain reset.
        env["GIT_CONFIG_GLOBAL"] = "/dev/null"
        env["GIT_CONFIG_NOSYSTEM"] = "1"

    # Fallback to instance-level SSH key (set via GIT_SSH_KEY_PATH env var)
    if "GIT_SSH_COMMAND" not in env and settings.git_ssh_key_path:
        env["GIT_SSH_COMMAND"] = f"ssh -i {settings.git_ssh_key_path} -o IdentitiesOnly=yes -o StrictHostKeyChecking=no"
    return env


def _get_or_create_askpass_script(username: str, token: str) -> str:
    """Create a temporary GIT_ASKPASS script that provides HTTPS credentials.

    The script echoes the username when git asks for "Username" and the token
    when git asks for "Password". This avoids storing credentials in the URL
    or relying on any system credential helper.
    """
    import hashlib
    import stat
    import tempfile
    from pathlib import Path

    # Use a stable path based on credential hash so we don't create unlimited files
    cred_hash = hashlib.sha256(f"{username}:{token}".encode()).hexdigest()[:12]
    askpass_dir = Path(tempfile.gettempdir()) / "claude-manager-askpass"
    askpass_dir.mkdir(exist_ok=True)
    askpass_path = askpass_dir / f"askpass_{cred_hash}.sh"

    if not askpass_path.exists():
        # The script receives a prompt like "Username for ..." or "Password for ..."
        script_content = f"""#!/bin/sh
case "$1" in
    Username*) echo "{username}" ;;
    *) echo "{token}" ;;
esac
"""
        askpass_path.write_text(script_content)
        askpass_path.chmod(stat.S_IRWXU)  # 0o700

    return str(askpass_path)


async def _build_secrets_block(db_factory, secret_ids: list[int]) -> str:
    """Load secrets by IDs and format them as a prompt block."""
    if not secret_ids:
        return ""
    async with db_factory() as db:
        result = await db.execute(
            sa_select(Secret).where(Secret.id.in_(secret_ids))
        )
        secrets = list(result.scalars().all())
    if not secrets:
        return ""
    lines = ["以下是用户提供的私密信息，请在需要时使用（不要在输出中泄露）："]
    for s in secrets:
        lines.append(f"- {s.name}: {s.content}")
    return "\n".join(lines)


class GlobalDispatcher:
    """Single global dispatcher that manages all instances and task lifecycle.

    Claude Code is fully autonomous — it handles worktree creation, commit,
    fetch, merge, push, conflict resolution, and cleanup itself via CLAUDE.md.
    The dispatcher only manages:
    - Task assignment (dequeue)
    - Starting/waiting on Claude Code processes
    - Marking tasks completed/failed
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
        self._dispatch_task: asyncio.Task | None = None
        self._running_tasks: dict[int, asyncio.Task] = {}  # instance_id -> lifecycle task
        self._running = False

    @property
    def is_running(self) -> bool:
        return self._running

    async def start(self):
        if self._running:
            return
        self._running = True

        # Ensure we have worker instances up to max_concurrent_instances
        await self._ensure_instances()

        self._dispatch_task = asyncio.create_task(self._dispatch_loop())
        logger.info("GlobalDispatcher started")

    async def stop(self):
        self._running = False
        if self._dispatch_task and not self._dispatch_task.done():
            self._dispatch_task.cancel()
            try:
                await self._dispatch_task
            except asyncio.CancelledError:
                pass

        # Cancel all running lifecycle tasks
        for instance_id, task in list(self._running_tasks.items()):
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._running_tasks.clear()
        logger.info("GlobalDispatcher stopped")

    def status(self) -> dict:
        return {
            "running": self._running,
            "active_tasks": {
                iid: not t.done() for iid, t in self._running_tasks.items()
            },
        }

    async def _ensure_instances(self):
        """Create worker instances in DB if fewer than max_concurrent_instances exist."""
        async with self.db_factory() as db:
            result = await db.execute(select(Instance))
            existing = list(result.scalars().all())

        needed = settings.max_concurrent_instances - len(existing)
        if needed > 0:
            async with self.db_factory() as db:
                for i in range(needed):
                    name = f"worker-{len(existing) + i + 1}"
                    instance = Instance(name=name, model=settings.default_model)
                    db.add(instance)
                await db.commit()
            logger.info(f"Created {needed} worker instances")

    async def _ensure_instances_for_pending_tasks(self):
        """Auto-create instances for pending tasks whose required model has no instance."""
        async with self.db_factory() as db:
            # Get distinct models required by pending tasks (excluding None/empty)
            result = await db.execute(
                select(Task.model).where(
                    Task.status == "pending",
                    Task.model.isnot(None),
                    Task.model != "",
                ).distinct()
            )
            required_models = [row[0] for row in result.all()]

            if not required_models:
                return

            # Get existing instance models
            result = await db.execute(select(Instance.model))
            existing_models = {row[0] for row in result.all()}

            # Normalize: treat "default" and the actual default_model as equivalent
            default_model = settings.default_model
            has_default = "default" in existing_models or default_model in existing_models

            # Create instances for models that have no instance at all
            for model in required_models:
                # "default" and the default_model (e.g. "opus") are equivalent
                if model in ("default", default_model) and has_default:
                    continue
                if model not in existing_models:
                    name = f"worker-{model}-1"
                    instance = Instance(name=name, model=model)
                    db.add(instance)
                    existing_models.add(model)
                    logger.info(f"Auto-created instance '{name}' for model '{model}'")

            await db.commit()

    async def _dispatch_loop(self):
        """Poll for idle instances + pending tasks and dispatch."""
        while self._running:
            try:
                # Find idle instances
                async with self.db_factory() as db:
                    result = await db.execute(
                        select(Instance).where(Instance.status.in_(["idle", "stopped"]))
                    )
                    idle_instances = list(result.scalars().all())

                for instance in idle_instances:
                    # Skip if already running a lifecycle
                    if instance.id in self._running_tasks and not self._running_tasks[instance.id].done():
                        continue

                    # Dequeue a task matching this instance's model
                    async with self.db_factory() as db:
                        queue = TaskQueue(db)
                        task = await queue.dequeue(instance_model=instance.model)

                    if not task:
                        continue  # No matching task for this instance, try next

                    # Resolve project -> target_repo + git config
                    merged: dict = {}
                    if task.project_id:
                        async with self.db_factory() as db:
                            project = await db.get(Project, task.project_id)
                            global_cfg = await db.get(GlobalSettings, 1)
                            if project:
                                if project.local_path and not task.target_repo:
                                    await db.execute(
                                        update(Task)
                                        .where(Task.id == task.id)
                                        .values(target_repo=project.local_path)
                                    )
                                    await db.commit()
                                    task.target_repo = project.local_path
                                merged = merge_git_config(settings_to_dict(project), settings_to_dict(global_cfg))
                    git_env = _build_git_env(merged)

                    logger.info(f"Dispatching task {task.id} ({task.title}) to instance {instance.id} ({instance.name})")
                    self._running_tasks[instance.id] = asyncio.create_task(
                        self._run_task_lifecycle(instance.id, task, git_env)
                    )

                # Auto-create instances for pending tasks whose model has no instance
                await self._ensure_instances_for_pending_tasks()

                await asyncio.sleep(2)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Dispatch loop error: {e}", exc_info=True)
                await asyncio.sleep(5)

    async def _run_task_lifecycle(self, instance_id: int, task: Task, git_env: dict | None = None):
        """Execute the task lifecycle: assign → Claude Code → judge result.

        Claude Code handles worktree creation, git operations, and cleanup
        autonomously based on the project's CLAUDE.md instructions.
        """
        try:
            # === Step 1: Mark in_progress ===
            await self.broadcaster.broadcast("tasks", {
                "event": "status_change",
                "task_id": task.id,
                "old_status": "pending",
                "new_status": "in_progress",
                "instance_id": instance_id,
            })

            # === Step 2: Determine cwd and update task ===
            cwd = task.target_repo or "."
            async with self.db_factory() as db:
                # Resolve actual model: task's own model, or fall back to instance's model
                if not task.model:
                    instance = await db.get(Instance, instance_id)
                    if instance:
                        resolved_model = instance.model if instance.model != "default" else None
                        if resolved_model:
                            task.model = resolved_model
                update_values: dict = {"status": "executing", "instance_id": instance_id}
                if task.model:
                    update_values["model"] = task.model
                await db.execute(
                    update(Task)
                    .where(Task.id == task.id)
                    .values(**update_values)
                )
                await db.commit()
            await self.broadcaster.broadcast("tasks", {
                "event": "status_change",
                "task_id": task.id,
                "new_status": "executing",
                "instance_id": instance_id,
            })

            # === Step 3: Plan mode check ===
            if task.mode == "plan" and not task.plan_approved:
                await self._run_plan_phase(instance_id, task, cwd, git_env)
                return

            # === Step 3b: Loop mode ===
            if task.mode == "loop":
                await self._run_loop_lifecycle(instance_id, task, cwd, git_env)
                return

            # === Step 4: Launch Claude Code ===
            metadata = task.metadata_ or {}
            image_paths = metadata.get("image_paths") or []
            secret_ids = metadata.get("secret_ids") or []
            secrets_block = await _build_secrets_block(self.db_factory, secret_ids)

            parts = ["请阅读项目根目录的 CLAUDE.md 了解项目规范和任务完成后的 git 流程。"]
            if secrets_block:
                parts.append(secrets_block)
            if image_paths:
                image_list = "\n".join(f"- {p}" for p in image_paths)
                parts.append(f"用户提供了以下参考图片，请先用 Read 工具查看：\n{image_list}")
            parts.append(f"任务:\n{task.description}")
            full_prompt = "\n\n".join(parts)

            await self.instance_manager.launch(
                instance_id=instance_id,
                prompt=full_prompt,
                task_id=task.id,
                cwd=cwd,
                model=task.model,
                git_env=git_env or {},
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

            # === Step 5: Judge result ===
            # SIGINT (exit code -2 or 130) means user interrupted — not a failure.
            # Keep session alive so user can resume via chat.
            interrupted = exit_code in (-2, 130)
            if interrupted:
                logger.info(f"Task {task.id} was interrupted by user (exit_code={exit_code})")
                async with self.db_factory() as db:
                    # Reset task to pending so it's not marked as failed,
                    # but keep session_id so chat can resume
                    await db.execute(
                        update(Task).where(Task.id == task.id).values(status="pending")
                    )
                    await db.commit()
                await self.broadcaster.broadcast("tasks", {
                    "event": "status_change",
                    "task_id": task.id,
                    "new_status": "pending",
                    "instance_id": instance_id,
                })
                return

            if exit_code != 0:
                async with self.db_factory() as db:
                    queue = TaskQueue(db)
                    t = await queue.get(task.id)
                    if t and t.retry_count < t.max_retries:
                        await queue.retry(task.id)
                        status = "pending"
                    else:
                        await queue.mark_failed(task.id, f"Exit code: {exit_code}")
                        status = "failed"

                await self.broadcaster.broadcast("tasks", {
                    "event": "status_change",
                    "task_id": task.id,
                    "new_status": status,
                    "instance_id": instance_id,
                })
                return

            # === Claude Code completed successfully ===
            async with self.db_factory() as db:
                queue = TaskQueue(db)
                await queue.mark_completed(task.id)

            await self.broadcaster.broadcast("tasks", {
                "event": "status_change",
                "task_id": task.id,
                "new_status": "completed",
                "instance_id": instance_id,
            })

            async with self.db_factory() as db:
                await db.execute(
                    update(Instance)
                    .where(Instance.id == instance_id)
                    .values(total_tasks_completed=Instance.total_tasks_completed + 1)
                )
                await db.commit()

            logger.info(f"Task {task.id} ({task.title}) completed successfully on instance {instance_id}")

        except asyncio.CancelledError:
            logger.info(f"Lifecycle cancelled for task {task.id} on instance {instance_id}")
            raise
        except Exception as e:
            logger.error(f"Lifecycle error for task {task.id}: {e}", exc_info=True)
            async with self.db_factory() as db:
                queue = TaskQueue(db)
                await queue.mark_failed(task.id, str(e)[:500])
            await self.broadcaster.broadcast("tasks", {
                "event": "status_change",
                "task_id": task.id,
                "new_status": "failed",
                "instance_id": instance_id,
            })
        finally:
            self._running_tasks.pop(instance_id, None)

    async def _run_loop_lifecycle(self, instance_id: int, task: Task, cwd: str, git_env: dict | None = None):
        """Loop: repeatedly invoke Claude Code until it signals done or abort.

        Each iteration starts a fresh Claude Code subprocess. Claude reads the todo
        file itself, executes the next item, marks it done, then writes a signal file
        telling us whether to continue, stop (done), or give up (abort).
        The backend never parses the todo file — Claude owns that logic entirely.
        """
        import json
        from pathlib import Path

        signal_path = Path(cwd) / ".claude-manager" / f"loop_signal_{task.id}.json"
        signal_path.parent.mkdir(parents=True, exist_ok=True)

        iteration = 0

        max_iterations = task.max_iterations or 50

        while True:
            # Check if task was cancelled externally between iterations
            async with self.db_factory() as db:
                t = await db.get(Task, task.id)
                if t and t.status == "cancelled":
                    logger.info(f"Loop task {task.id} cancelled, stopping")
                    return

            # Enforce max iterations limit
            if iteration >= max_iterations:
                async with self.db_factory() as db:
                    queue = TaskQueue(db)
                    await queue.mark_failed(task.id, f"超出最大迭代次数限制 ({max_iterations})")
                await self.broadcaster.broadcast("tasks", {
                    "event": "status_change",
                    "task_id": task.id,
                    "new_status": "failed",
                    "instance_id": instance_id,
                })
                logger.warning(f"Loop task {task.id} exceeded max iterations ({max_iterations}), aborting")
                return

            # Clear signal file so we can detect if Claude fails to write one
            signal_path.unlink(missing_ok=True)

            prompt = self._build_loop_prompt(task, iteration, str(signal_path))

            await self.instance_manager.launch(
                instance_id=instance_id,
                prompt=prompt,
                task_id=task.id,
                cwd=cwd,
                model=None,
                loop_iteration=iteration,
                git_env=git_env or {},
            )

            process = self.instance_manager.processes.get(instance_id)
            if process:
                try:
                    await asyncio.wait_for(process.wait(), timeout=settings.task_timeout_seconds)
                except asyncio.TimeoutError:
                    logger.warning(f"Loop task {task.id} iteration {iteration} timed out, killing")
                    process.kill()
                    await process.wait()

            # P1: Check if task was cancelled while the iteration was running
            # (e.g. user called cancel + stop-session mid-iteration)
            async with self.db_factory() as db:
                t = await db.get(Task, task.id)
                if t and t.status == "cancelled":
                    logger.info(f"Loop task {task.id} cancelled during iteration {iteration}, stopping")
                    return

            signal = self._read_loop_signal(signal_path)

            # P0: If signal is missing, attempt one resume to ask Claude to write it
            if signal.get("reason") == "Signal file missing or invalid JSON":
                signal = await self._resume_fix_signal(
                    instance_id, task, cwd, signal_path, iteration, git_env or {}
                )

            # Update loop_progress from signal (Claude's self-reported progress string)
            if signal.get("progress"):
                async with self.db_factory() as db:
                    await db.execute(
                        update(Task)
                        .where(Task.id == task.id)
                        .values(loop_progress=signal["progress"])
                    )
                    await db.commit()

            # Broadcast iteration result so frontend can update the panel header
            await self.broadcaster.broadcast(f"task:{task.id}", {
                "event": "loop_iteration_end",
                "iteration": iteration,
                "action": signal.get("action", "abort"),
                "reason": signal.get("reason", ""),
                "progress": signal.get("progress"),
            })

            action = signal.get("action")

            if action == "continue":
                iteration += 1
                continue

            elif action == "done":
                async with self.db_factory() as db:
                    queue = TaskQueue(db)
                    await queue.mark_completed(task.id)
                    await db.execute(
                        update(Instance)
                        .where(Instance.id == instance_id)
                        .values(total_tasks_completed=Instance.total_tasks_completed + 1)
                    )
                    await db.commit()
                await self.broadcaster.broadcast("tasks", {
                    "event": "status_change",
                    "task_id": task.id,
                    "new_status": "completed",
                    "instance_id": instance_id,
                })
                logger.info(f"Loop task {task.id} completed after {iteration + 1} iteration(s)")
                break

            else:
                # "abort" or missing/malformed signal — P1: retry if attempts remain
                reason = signal.get("reason") or "Claude did not write a valid loop signal"
                async with self.db_factory() as db:
                    queue = TaskQueue(db)
                    t = await queue.get(task.id)
                    if t and t.retry_count < t.max_retries:
                        await queue.retry(task.id)
                        status = "pending"
                    else:
                        await queue.mark_failed(task.id, reason)
                        status = "failed"
                await self.broadcaster.broadcast("tasks", {
                    "event": "status_change",
                    "task_id": task.id,
                    "new_status": status,
                    "instance_id": instance_id,
                })
                logger.warning(f"Loop task {task.id} aborted at iteration {iteration} → {status}: {reason}")
                break

        signal_path.unlink(missing_ok=True)

    def _build_loop_prompt(self, task: Task, iteration: int, signal_path: str) -> str:
        """Build the per-iteration prompt for a loop task.

        Only describes todo-related responsibilities. Git/commit/worktree lifecycle
        is already covered by CLAUDE.md — no need to repeat it here.
        """
        parts = []

        if task.description:
            parts.append(f"背景说明：{task.description}\n")

        parts.append(f"""\
请遵循 CLAUDE.md 中的所有要求和项目约定。

这是一个持续循环任务的第 {iteration + 1} 轮。

你的职责：
1. 打开 {task.todo_file_path}，理解其结构，找到下一个待完成的任务项
2. 根据 CLAUDE.md 的要求执行该任务项
3. 在 todo 文件中将该项标记为已完成

完成后，将以下 JSON 写入 {signal_path}：

还有待完成项，请继续下一轮：
{{"action": "continue", "reason": "...", "progress": "已完成数/总数"}}

全部完成：
{{"action": "done", "reason": "所有 todo 项已完成"}}

无法继续（遇到阻塞或明确问题）：
{{"action": "abort", "reason": "具体原因"}}
""")
        return "\n".join(parts)

    def _read_loop_signal(self, signal_path) -> dict:
        """Read and parse the signal file Claude writes at the end of each iteration.

        Returns abort with a reason if the file is missing or malformed, so the
        while loop always terminates cleanly instead of spinning indefinitely.
        """
        import json
        from pathlib import Path
        try:
            return json.loads(Path(signal_path).read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"Failed to read loop signal from {signal_path}: {e}")
            return {"action": "abort", "reason": "Signal file missing or invalid JSON"}

    async def _resume_fix_signal(
        self,
        instance_id: int,
        task: Task,
        cwd: str,
        signal_path,
        iteration: int,
        git_env: dict,
    ) -> dict:
        """Resume the last session to ask Claude to write the missing signal file.

        Called at most once per iteration when Claude completes work but forgets
        to write the signal JSON.  Returns the signal dict (may still be abort if
        Claude fails to write it on the second attempt).
        """
        async with self.db_factory() as db:
            t = await db.get(Task, task.id)
            resume_sid = t.session_id if t else None

        if not resume_sid:
            logger.warning(f"Loop task {task.id} iter {iteration}: signal missing and no session_id to resume")
            return {"action": "abort", "reason": "Signal file missing and no session to resume"}

        fix_prompt = (
            f"你刚才完成了工作但忘记写信号文件。请检查 {task.todo_file_path} 的当前状态，"
            f"然后立即将以下其中一个 JSON 写入 {signal_path}：\n\n"
            f'还有待完成项：{{"action": "continue", "reason": "...", "progress": "已完成数/总数"}}\n'
            f'全部完成：{{"action": "done", "reason": "所有 todo 项已完成"}}\n'
            f'无法继续：{{"action": "abort", "reason": "具体原因"}}'
        )

        logger.info(f"Loop task {task.id} iter {iteration}: resuming session {resume_sid} to fix missing signal")
        await self.instance_manager.launch(
            instance_id=instance_id,
            prompt=fix_prompt,
            task_id=task.id,
            cwd=cwd,
            resume_session_id=resume_sid,
            loop_iteration=iteration,
            git_env=git_env,
        )

        fix_proc = self.instance_manager.processes.get(instance_id)
        if fix_proc:
            try:
                await asyncio.wait_for(fix_proc.wait(), timeout=60)
            except asyncio.TimeoutError:
                logger.warning(f"Loop task {task.id} iter {iteration}: resume fix timed out, killing")
                fix_proc.kill()
                await fix_proc.wait()

        return self._read_loop_signal(signal_path)

    async def _run_plan_phase(self, instance_id: int, task: Task, cwd: str, git_env: dict | None = None):
        """Run plan phase for plan-mode tasks."""
        plan_prompt = (
            f"Please analyze the following task and create a detailed plan. "
            f"Do NOT execute any changes, only describe what you would do:\n\n{task.description}"
        )
        await self.instance_manager.launch(
            instance_id=instance_id,
            prompt=plan_prompt,
            task_id=task.id,
            cwd=cwd,
            model=None,
            git_env=git_env or {},
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
            from sqlalchemy import select as sa_select
            from backend.models.log_entry import LogEntry
            result = await db.execute(
                sa_select(LogEntry.content)
                .where(
                    LogEntry.task_id == task.id,
                    LogEntry.event_type == "message",
                    LogEntry.role == "assistant",
                )
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
