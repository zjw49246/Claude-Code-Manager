import asyncio
import logging
import os
import shutil
from datetime import datetime
from pathlib import Path

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


def _default_provider() -> str:
    provider = getattr(settings, "default_provider", "claude")
    return provider if isinstance(provider, str) and provider else "claude"


def _binary_available(binary: str) -> bool:
    if not isinstance(binary, str) or not binary:
        return False
    path = Path(binary).expanduser()
    if path.is_absolute() or any(sep in binary for sep in (os.sep, os.altsep) if sep):
        return path.exists()
    return shutil.which(binary) is not None


def _codex_binary_available() -> bool:
    configured = settings.codex_binary
    if configured and configured.lower() != "codex":
        return _binary_available(configured)
    if _binary_available(configured or "codex"):
        return True

    local_appdata = os.environ.get("LOCALAPPDATA")
    if not local_appdata:
        return False
    bin_root = Path(local_appdata) / "OpenAI" / "Codex" / "bin"
    return any(bin_root.glob("*/codex.exe"))


def _provider_available(provider: str) -> bool:
    provider = (provider or "claude").lower()
    if provider == "claude":
        return _binary_available(settings.claude_binary)
    if provider == "codex":
        return _codex_binary_available()
    return False


def _default_worker_provider() -> str:
    provider = _default_provider()
    if _provider_available(provider):
        return provider
    if provider == "claude" and _provider_available("codex"):
        return "codex"
    return provider


def _default_worker_model(provider: str) -> str:
    return settings.default_codex_model if provider == "codex" else settings.default_model


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
    - Pool rotation on rate limit (when pool is enabled)
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
        self._monitor_tasks: dict[int, asyncio.Task] = {}           # monitor_session_id -> asyncio task
        self._monitor_processes: dict[int, asyncio.subprocess.Process] = {}  # monitor_session_id -> subprocess

        # Pool: initialized lazily on start() if pool_enabled
        self.pool: "ClaudePool | None" = None

    @property
    def is_running(self) -> bool:
        return self._running

    async def start(self):
        if self._running:
            return
        self._running = True

        # Initialize pool if enabled
        if settings.pool_enabled:
            from backend.services.claude_pool import ClaudePool
            self.pool = ClaudePool(
                config_path=settings.pool_config_path,
                cooldown_seconds=settings.pool_cooldown_seconds,
            )
            if self.pool.enabled:
                logger.info("Claude pool enabled with %d accounts", len(self.pool._accounts))
            else:
                logger.warning("Pool enabled in config but only %d account(s) — rotation disabled", len(self.pool._accounts))
                self.pool = None

        await self._cleanup_stale_state()

        # Ensure we have worker instances up to max_concurrent_instances
        await self._ensure_instances()

        self._dispatch_task = asyncio.create_task(self._dispatch_loop())
        logger.info("GlobalDispatcher started")

    async def _cleanup_stale_state(self):
        """Reset instances and tasks stuck in active states after a crash/restart."""
        import os
        async with self.db_factory() as db:
            result = await db.execute(
                select(Instance).where(Instance.status == "running")
            )
            for inst in result.scalars().all():
                alive = False
                if inst.pid:
                    try:
                        os.kill(inst.pid, 0)
                        alive = True
                    except OSError:
                        pass
                if not alive:
                    logger.warning(f"Cleaning up stale instance {inst.id} ({inst.name}), dead PID {inst.pid}")
                    await db.execute(
                        update(Instance)
                        .where(Instance.id == inst.id)
                        .values(status="idle", current_task_id=None, pid=None)
                    )
            result = await db.execute(
                select(Task).where(Task.status.in_(["executing", "in_progress"]))
            )
            for t in result.scalars().all():
                logger.warning(f"Resetting stuck task {t.id} from '{t.status}' to 'completed'")
                t.status = "completed"
                t.error_message = None

            from backend.models.monitor_session import MonitorSession
            result = await db.execute(
                select(MonitorSession).where(MonitorSession.status == "running")
            )
            for ms in result.scalars().all():
                logger.warning(f"Cleaning up stale monitor session {ms.id}")
                ms.status = "failed"
                ms.completed_at = datetime.utcnow()

            await db.commit()

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
                    instance = Instance(name=name)
                    db.add(instance)
                await db.commit()
            logger.info(f"Created {needed} worker instances")

    async def _dispatch_loop(self):
        """Poll for idle instances + pending tasks and dispatch."""
        while self._running:
            try:
                # Find idle instances
                async with self.db_factory() as db:
                    result = await db.execute(
                        select(Instance).where(Instance.status == "idle")
                    )
                    idle_instances = list(result.scalars().all())

                for instance in idle_instances:
                    # Skip if already running a lifecycle
                    if instance.id in self._running_tasks and not self._running_tasks[instance.id].done():
                        continue

                    async with self.db_factory() as db:
                        queue = TaskQueue(db)
                        task = await queue.dequeue()

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

                await asyncio.sleep(2)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Dispatch loop error: {e}", exc_info=True)
                await asyncio.sleep(5)

    def _pool_select(self, exclude: set[str] | None = None) -> str | None:
        """Select a pool account config_dir, or None if pool is off / exhausted."""
        if not self.pool:
            return None
        return self.pool.select(exclude=exclude, validate=True)

    async def _check_rate_limit_and_rotate(
        self,
        instance_id: int,
        task_id: int,
        exit_code: int,
    ) -> dict | None:
        """After a failed process, check if it was a rate limit and attempt rotation.

        Returns a dict with {config_dir, session_id, excluded} if rotation is
        possible, or None if this is not a pool-rotatable failure.
        """
        if not self.pool or exit_code == 0 or exit_code in (-2, 130):
            return None

        from backend.services.claude_pool import is_pool_rotatable, is_auth_failure, is_rate_limited
        from backend.services.claude_pool import migrate_session, collect_process_output_for_detection

        stderr = self.instance_manager.get_last_stderr(instance_id)
        log_contents = await self.instance_manager.get_recent_log_contents(task_id, limit=10)
        combined = collect_process_output_for_detection(stderr, log_contents)

        if not is_pool_rotatable(combined):
            return None

        old_config_dir = self.instance_manager.get_config_dir(instance_id)
        if not old_config_dir:
            return None

        # Mark the old account
        if is_auth_failure(combined):
            self.pool.mark_auth_failure(old_config_dir)
            logger.warning("Pool account %s auth failure, marked indefinite cooldown", old_config_dir)
        elif is_rate_limited(combined):
            self.pool.mark_rate_limited(old_config_dir)
            logger.info("Pool account %s rate-limited, marked cooldown", old_config_dir)

        # Build exclusion set
        old_account_id = self.pool.account_id_from_config_dir(old_config_dir)
        excluded = {old_account_id} if old_account_id else set()

        new_config_dir = self._pool_select(exclude=excluded)
        if not new_config_dir:
            logger.warning("Pool exhausted — no alternative account for task %d", task_id)
            return None

        # Get session_id for --resume
        async with self.db_factory() as db:
            t = await db.get(Task, task_id)
            session_id = t.session_id if t else None

        if session_id:
            migrate_session(
                old_config_dir=old_config_dir,
                new_config_dir=new_config_dir,
                session_id=session_id,
            )

        # Broadcast pool rotation event
        await self.broadcaster.broadcast(f"task:{task_id}", {
            "event_type": "pool_rotation",
            "old_account": old_account_id,
            "new_account": self.pool.account_id_from_config_dir(new_config_dir),
            "reason": "rate_limit" if is_rate_limited(combined) else "auth_failure",
        })
        await self.broadcaster.broadcast("system", {
            "event": "pool_rotation",
            "task_id": task_id,
            "instance_id": instance_id,
            "old_account": old_account_id,
            "new_account": self.pool.account_id_from_config_dir(new_config_dir),
        })

        return {
            "config_dir": new_config_dir,
            "session_id": session_id,
            "excluded": excluded,
        }

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
            cwd = task.last_cwd or task.target_repo or "."
            thinking_budget = task.thinking_budget
            effort_level = task.effort_level or settings.default_effort
            async with self.db_factory() as db:
                await db.execute(
                    update(Task)
                    .where(Task.id == task.id)
                    .values(status="executing", instance_id=instance_id)
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
                await self._run_plan_phase(instance_id, task, cwd, git_env, effort_level=effort_level)
                return

            # === Step 3b: Loop mode ===
            if task.mode == "loop":
                await self._run_loop_lifecycle(instance_id, task, cwd, git_env, effort_level=effort_level)
                return

            # === Step 3c: Goal mode ===
            if task.mode == "goal":
                await self._run_goal_lifecycle(instance_id, task, cwd, git_env, effort_level=effort_level)
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
            if task.enabled_skills and task.enabled_skills.get("monitor"):
                parts.append(
                    "你拥有后台监控能力（通过 ccm-skills MCP 工具）。"
                    "当用户要求监控后台进程或长时间运行的任务时，"
                    "调用 create_monitor 启动后台只读监控。"
                    "可用工具: create_monitor / check_monitors / stop_monitor。"
                )
            parts.append(f"任务:\n{task.description}")
            full_prompt = "\n\n".join(parts)

            # Pool: select an account for the initial launch
            pool_config_dir = self._pool_select()

            await self.instance_manager.launch(
                instance_id=instance_id,
                prompt=full_prompt,
                task_id=task.id,
                cwd=cwd,
                model=task.model,
                resume_session_id=task.session_id,
                git_env=git_env or {},
                thinking_budget=thinking_budget,
                effort_level=effort_level,
                provider=task.provider,
                config_dir=pool_config_dir,
                enable_workflows=task.enable_workflows,
                enabled_skills=task.enabled_skills,
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

            # Wait for output consumer to finish processing all remaining
            # buffered output before judging the result. Without this the
            # task can be marked completed while the last chunk of Claude's
            # reply is still being parsed/broadcast.
            consumer = self.instance_manager._tasks.get(instance_id)
            if consumer:
                try:
                    await asyncio.wait_for(consumer, timeout=30)
                except asyncio.TimeoutError:
                    logger.warning(f"Output consumer for instance {instance_id} did not finish in 30s, proceeding")

            exit_code = process.returncode if process else -1

            # === Step 5: Judge result ===
            # SIGINT (exit code -2 or 130) means user interrupted — not a failure.
            # Keep session alive so user can resume via chat.
            interrupted = exit_code in (-2, 130)
            if interrupted:
                logger.info(f"Task {task.id} was interrupted by user (exit_code={exit_code})")
                async with self.db_factory() as db:
                    await db.execute(
                        update(Task).where(Task.id == task.id).values(status="completed", error_message=None)
                    )
                    await db.commit()
                await self.broadcaster.broadcast("tasks", {
                    "event": "status_change",
                    "task_id": task.id,
                    "new_status": "completed",
                    "instance_id": instance_id,
                })
                return

            if exit_code != 0:
                # Pool rotation: if rate-limited, switch account and resume
                rotation = await self._check_rate_limit_and_rotate(instance_id, task.id, exit_code)
                if rotation:
                    await self._run_pool_retry(
                        instance_id, task, cwd, git_env,
                        rotation["config_dir"], rotation["session_id"],
                        rotation["excluded"],
                        thinking_budget=thinking_budget,
                        effort_level=effort_level,
                    )
                    return

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
            from backend.services.mcp_config import cleanup_mcp_config
            cleanup_mcp_config(task.id)
            self._running_tasks.pop(instance_id, None)
            await self._reset_instance_if_stale(instance_id, task.id)

    async def _reset_instance_if_stale(self, instance_id: int, task_id: int):
        """Safety net: reset instance to idle if _consume_output didn't clean up."""
        try:
            async with self.db_factory() as db:
                inst = await db.get(Instance, instance_id)
                if inst and inst.status == "running":
                    inst.status = "idle"
                    inst.current_task_id = None
                    inst.pid = None
                    await db.commit()
                    logger.warning(f"Safety reset: instance {instance_id} was still 'running' after lifecycle ended")
                t = await db.get(Task, task_id)
                if t and t.status in ("executing", "in_progress"):
                    t.status = "completed"
                    t.error_message = None
                    await db.commit()
                    logger.warning(f"Safety reset: task {task_id} was still '{t.status}' after lifecycle ended")
        except Exception:
            logger.exception(f"Failed to safety-reset instance {instance_id} / task {task_id}")

    async def _run_pool_retry(
        self,
        instance_id: int,
        task: Task,
        cwd: str,
        git_env: dict | None,
        config_dir: str,
        session_id: str | None,
        excluded: set[str],
        *,
        thinking_budget: int | None = None,
        effort_level: str | None = None,
        max_rotations: int = 5,
        _rotation_count: int = 1,
    ):
        """Resume a task on a different pool account after rate limit.

        If the new account also hits a rate limit, recurse with the accumulated
        exclusion set until max_rotations is reached or accounts are exhausted.
        """
        logger.info(
            "Pool retry #%d for task %d: switching to %s (session=%s)",
            _rotation_count, task.id, config_dir, session_id,
        )

        if session_id:
            # Resume the same session on the new account
            await self.instance_manager.launch(
                instance_id=instance_id,
                prompt="请继续之前的工作。",
                task_id=task.id,
                cwd=cwd,
                model=task.model,
                resume_session_id=session_id,
                git_env=git_env or {},
                thinking_budget=thinking_budget,
                effort_level=effort_level,
                provider=task.provider,
                config_dir=config_dir,
                enable_workflows=task.enable_workflows,
                enabled_skills=task.enabled_skills,
            )
        else:
            # No session to resume — re-launch from scratch
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
            if task.enabled_skills and task.enabled_skills.get("monitor"):
                parts.append(
                    "你拥有后台监控能力（通过 ccm-skills MCP 工具）。"
                    "当用户要求监控后台进程或长时间运行的任务时，"
                    "调用 create_monitor 启动后台只读监控。"
                    "可用工具: create_monitor / check_monitors / stop_monitor。"
                )
            parts.append(f"任务:\n{task.description}")
            full_prompt = "\n\n".join(parts)

            await self.instance_manager.launch(
                instance_id=instance_id,
                prompt=full_prompt,
                task_id=task.id,
                cwd=cwd,
                model=task.model,
                git_env=git_env or {},
                thinking_budget=thinking_budget,
                effort_level=effort_level,
                provider=task.provider,
                config_dir=config_dir,
                enable_workflows=task.enable_workflows,
                enabled_skills=task.enabled_skills,
            )

        # Wait for process
        process = self.instance_manager.processes.get(instance_id)
        if process:
            try:
                await asyncio.wait_for(process.wait(), timeout=settings.task_timeout_seconds)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()

        consumer = self.instance_manager._tasks.get(instance_id)
        if consumer:
            try:
                await asyncio.wait_for(consumer, timeout=30)
            except asyncio.TimeoutError:
                pass

        exit_code = process.returncode if process else -1

        if exit_code in (0, -2, 130):
            # Success or user interrupt
            if exit_code == 0:
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
                logger.info("Task %d completed after %d pool rotation(s)", task.id, _rotation_count)
            else:
                async with self.db_factory() as db:
                    await db.execute(
                        update(Task).where(Task.id == task.id).values(status="completed", error_message=None)
                    )
                    await db.commit()
                await self.broadcaster.broadcast("tasks", {
                    "event": "status_change",
                    "task_id": task.id,
                    "new_status": "completed",
                    "instance_id": instance_id,
                })
            return

        # Failed again — try another rotation if budget remains
        if _rotation_count < max_rotations:
            rotation = await self._check_rate_limit_and_rotate(instance_id, task.id, exit_code)
            if rotation:
                merged_excluded = excluded | rotation["excluded"]
                await self._run_pool_retry(
                    instance_id, task, cwd, git_env,
                    rotation["config_dir"], rotation["session_id"],
                    merged_excluded,
                    thinking_budget=thinking_budget,
                    effort_level=effort_level,
                    max_rotations=max_rotations,
                    _rotation_count=_rotation_count + 1,
                )
                return

        # Non-rotatable failure or exhausted rotations — normal retry/fail
        async with self.db_factory() as db:
            queue = TaskQueue(db)
            t = await queue.get(task.id)
            if t and t.retry_count < t.max_retries:
                await queue.retry(task.id)
                status = "pending"
            else:
                await queue.mark_failed(task.id, f"Exit code: {exit_code} after {_rotation_count} pool rotation(s)")
                status = "failed"
        await self.broadcaster.broadcast("tasks", {
            "event": "status_change",
            "task_id": task.id,
            "new_status": status,
            "instance_id": instance_id,
        })

    async def _run_loop_lifecycle(self, instance_id: int, task: Task, cwd: str, git_env: dict | None = None, effort_level: str | None = None):
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
        history: list[dict] = []
        anchored_total: int | None = None
        plan: str | None = None

        max_iterations = task.max_iterations or 50

        while True:
            # Check if task was cancelled or deleted externally between iterations
            async with self.db_factory() as db:
                t = await db.get(Task, task.id)
                if not t:
                    logger.info(f"Loop task {task.id} deleted, stopping")
                    return
                if t.status == "cancelled":
                    logger.info(f"Loop task {task.id} cancelled, stopping")
                    return

            # Enforce max iterations limit
            if iteration >= max_iterations:
                if task.must_complete:
                    last_progress = history[-1]["progress"] if history else "unknown"
                    fail_msg = f"未能在 {max_iterations} 轮内完成所有任务项（当前进度: {last_progress}）"
                else:
                    fail_msg = f"超出最大迭代次数限制 ({max_iterations})"
                async with self.db_factory() as db:
                    queue = TaskQueue(db)
                    await queue.mark_failed(task.id, fail_msg)
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

            prompt = self._build_loop_prompt(
                task, iteration, str(signal_path), history, anchored_total, plan,
            )

            await self.instance_manager.launch(
                instance_id=instance_id,
                prompt=prompt,
                task_id=task.id,
                cwd=cwd,
                model=task.model,
                loop_iteration=iteration,
                git_env=git_env or {},
                thinking_budget=task.thinking_budget,
                effort_level=effort_level,
                provider=task.provider,
                enable_workflows=task.enable_workflows,
                enabled_skills=task.enabled_skills,
            )

            process = self.instance_manager.processes.get(instance_id)
            if process:
                try:
                    await asyncio.wait_for(process.wait(), timeout=settings.task_timeout_seconds)
                except asyncio.TimeoutError:
                    logger.warning(f"Loop task {task.id} iteration {iteration} timed out, killing")
                    process.kill()
                    await process.wait()

            # P1: Check if task was cancelled/deleted while the iteration was running
            async with self.db_factory() as db:
                t = await db.get(Task, task.id)
                if not t:
                    logger.info(f"Loop task {task.id} deleted during iteration {iteration}, stopping")
                    return
                if t.status == "cancelled":
                    logger.info(f"Loop task {task.id} cancelled during iteration {iteration}, stopping")
                    return

            signal = self._read_loop_signal(signal_path)

            # P0: If signal is missing, attempt one resume to ask Claude to write it
            if signal.get("reason") == "Signal file missing or invalid JSON":
                signal = await self._resume_fix_signal(
                    instance_id, task, cwd, signal_path, iteration, git_env or {},
                    effort_level=effort_level,
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

            # Anchor total from the first progress report so subsequent iterations stay consistent
            progress_str = signal.get("progress", "")
            if progress_str and anchored_total is None:
                try:
                    anchored_total = int(progress_str.split("/")[1])
                except (IndexError, ValueError):
                    pass

            # Capture plan from signal (latest plan overwrites previous)
            if signal.get("plan"):
                plan = signal["plan"]

            # Collect iteration history for subsequent prompts
            history.append({
                "iteration": iteration + 1,
                "progress": progress_str,
                "summary": signal.get("summary", ""),
            })

            # Broadcast iteration result so frontend can update the panel header
            await self.broadcaster.broadcast(f"task:{task.id}", {
                "event": "loop_iteration_end",
                "iteration": iteration,
                "action": signal.get("action", "abort"),
                "reason": signal.get("reason", ""),
                "progress": signal.get("progress"),
            })

            action = signal.get("action")

            # must_complete: reject "done" if progress shows incomplete
            if action == "done" and task.must_complete and anchored_total is not None:
                try:
                    numerator = int(progress_str.split("/")[0])
                except (IndexError, ValueError):
                    numerator = None
                if numerator is not None and numerator < anchored_total:
                    logger.info(
                        f"Loop task {task.id} rejected premature done "
                        f"(progress {progress_str}, need {anchored_total}), forcing continue"
                    )
                    iteration += 1
                    continue

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

    # ------------------------------------------------------------------ #
    #                       Goal mode lifecycle                           #
    # ------------------------------------------------------------------ #

    async def _run_goal_lifecycle(
        self,
        instance_id: int,
        task: Task,
        cwd: str,
        git_env: dict | None = None,
        effort_level: str | None = None,
    ):
        """Goal mode: repeatedly invoke Claude Code until an evaluator confirms
        the goal condition is met.

        Uses --resume to keep the same session across turns, preserving full
        context. After each turn, a lightweight evaluator model judges the
        conversation transcript against the goal condition.
        """
        from backend.services.goal_evaluator import GoalEvaluator

        evaluator = GoalEvaluator()
        turn = 0
        max_turns = task.goal_max_turns or 30
        session_id: str | None = None

        while turn < max_turns:
            # Check if task was cancelled or deleted externally between turns
            async with self.db_factory() as db:
                t = await db.get(Task, task.id)
                if not t:
                    logger.info(f"Goal task {task.id} deleted, stopping")
                    return
                if t.status == "cancelled":
                    logger.info(f"Goal task {task.id} cancelled, stopping")
                    return

            if turn == 0:
                prompt = self._build_goal_initial_prompt(task)
                await self.instance_manager.launch(
                    instance_id=instance_id,
                    prompt=prompt,
                    task_id=task.id,
                    cwd=cwd,
                    model=task.model,
                    loop_iteration=turn,
                    git_env=git_env or {},
                    thinking_budget=task.thinking_budget,
                    effort_level=effort_level,
                    provider=task.provider,
                    enable_workflows=task.enable_workflows,
                    enabled_skills=task.enabled_skills,
                )
            else:
                follow_up = self._build_goal_followup_prompt(last_reason, turn, max_turns)
                await self.instance_manager.launch(
                    instance_id=instance_id,
                    prompt=follow_up,
                    task_id=task.id,
                    cwd=cwd,
                    model=task.model,
                    resume_session_id=session_id,
                    loop_iteration=turn,
                    git_env=git_env or {},
                    thinking_budget=task.thinking_budget,
                    effort_level=effort_level,
                    provider=task.provider,
                    enable_workflows=task.enable_workflows,
                    enabled_skills=task.enabled_skills,
                )

            # Wait for process to finish
            process = self.instance_manager.processes.get(instance_id)
            if process:
                try:
                    await asyncio.wait_for(process.wait(), timeout=settings.task_timeout_seconds)
                except asyncio.TimeoutError:
                    logger.warning(f"Goal task {task.id} turn {turn} timed out, killing")
                    process.kill()
                    await process.wait()

            # Check if cancelled/deleted during execution
            async with self.db_factory() as db:
                t = await db.get(Task, task.id)
                if not t:
                    logger.info(f"Goal task {task.id} deleted during turn {turn}, stopping")
                    return
                if t.status == "cancelled":
                    logger.info(f"Goal task {task.id} cancelled during turn {turn}, stopping")
                    return

            # Get session_id from DB (set by _consume_output)
            async with self.db_factory() as db:
                t = await db.get(Task, task.id)
                if t and t.session_id:
                    session_id = t.session_id

            # Collect conversation summary for evaluator
            conversation_summary = await self._collect_goal_conversation(task.id, turn)

            # Evaluate goal condition
            eval_result = await evaluator.evaluate(
                condition=task.goal_condition,
                conversation_summary=conversation_summary,
                model=task.goal_evaluator_model,
                provider=task.provider or "claude",
            )

            turn += 1
            last_reason = eval_result.reason

            # Update progress in DB
            async with self.db_factory() as db:
                await db.execute(
                    update(Task)
                    .where(Task.id == task.id)
                    .values(
                        goal_turns_used=turn,
                        goal_last_reason=eval_result.reason,
                    )
                )
                await db.commit()

            # Broadcast evaluation result
            await self.broadcaster.broadcast(f"task:{task.id}", {
                "event_type": "goal_evaluation",
                "turn": turn,
                "max_turns": max_turns,
                "achieved": eval_result.achieved,
                "reason": eval_result.reason,
            })
            await self.broadcaster.broadcast("tasks", {
                "event": "goal_evaluation",
                "task_id": task.id,
                "turn": turn,
                "achieved": eval_result.achieved,
            })

            if eval_result.achieved:
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
                logger.info(f"Goal task {task.id} achieved after {turn} turn(s)")
                return

        # Exceeded max turns
        fail_msg = f"未在 {max_turns} 轮内达成目标条件"
        async with self.db_factory() as db:
            queue = TaskQueue(db)
            await queue.mark_failed(task.id, fail_msg)
        await self.broadcaster.broadcast("tasks", {
            "event": "status_change",
            "task_id": task.id,
            "new_status": "failed",
            "instance_id": instance_id,
        })
        logger.warning(f"Goal task {task.id} exceeded max turns ({max_turns})")

    def _build_goal_initial_prompt(self, task: Task) -> str:
        """Build the first-turn prompt for a goal task."""
        parts = ["请阅读项目根目录的 CLAUDE.md 了解项目规范和任务完成后的 git 流程。"]

        metadata = task.metadata_ or {}
        image_paths = metadata.get("image_paths") or []
        if image_paths:
            image_list = "\n".join(f"- {p}" for p in image_paths)
            parts.append(f"用户提供了以下参考图片，请先用 Read 工具查看：\n{image_list}")

        parts.append(f"任务:\n{task.description}")
        parts.append(
            f"\n目标完成条件:\n{task.goal_condition}\n\n"
            f"请持续工作直到满足以上目标条件。每轮结束后，"
            f"一个独立的评估器会检查你的工作是否已达成目标。"
            f"你有最多 {task.goal_max_turns or 30} 轮来完成。"
            f"请在每轮结束时简要说明本轮完成了什么、当前状态如何。"
        )
        return "\n\n".join(parts)

    def _build_goal_followup_prompt(self, last_reason: str, turn: int, max_turns: int) -> str:
        """Build follow-up prompt for subsequent goal turns."""
        remaining = max_turns - turn
        return (
            f"评估器判断目标尚未达成。\n\n"
            f"评估器反馈: {last_reason}\n\n"
            f"请继续工作以满足目标条件。你还有 {remaining} 轮机会。\n"
            f"本轮结束时请简要说明完成了什么、当前状态如何。"
        )

    async def _collect_goal_conversation(self, task_id: int, current_turn: int) -> str:
        """Collect recent conversation log entries for the evaluator.

        Reads the last N assistant messages from log_entries to build a
        summary that the evaluator can judge against the goal condition.
        Only sends recent turns to keep the evaluator prompt concise.
        """
        from backend.models.log_entry import LogEntry

        async with self.db_factory() as db:
            result = await db.execute(
                select(LogEntry.content, LogEntry.loop_iteration)
                .where(
                    LogEntry.task_id == task_id,
                    LogEntry.event_type == "message",
                    LogEntry.role == "assistant",
                )
                .order_by(LogEntry.id.desc())
                .limit(30)
            )
            rows = list(result.all())

        if not rows:
            return "(No conversation output recorded)"

        rows.reverse()
        parts = []
        for content, iteration in rows:
            if content:
                turn_label = f"[Turn {(iteration or 0) + 1}] " if iteration is not None else ""
                parts.append(f"{turn_label}{content}")

        summary = "\n\n".join(parts)
        if len(summary) > 15000:
            summary = summary[-15000:]
        return summary

    def _build_loop_prompt(self, task: Task, iteration: int, signal_path: str,
                           history: list[dict] | None = None, anchored_total: int | None = None,
                           plan: str | None = None) -> str:
        """Build the per-iteration prompt for a loop task.

        Only describes todo-related responsibilities. Git/commit/worktree lifecycle
        is already covered by CLAUDE.md — no need to repeat it here.
        """
        parts = []
        max_iterations = task.max_iterations or 50
        remaining = max_iterations - iteration

        if task.description:
            parts.append(f"背景说明：{task.description}\n")

        # Include previous iterations' summaries so Claude has context
        if history:
            parts.append("=== 前几轮完成情况 ===")
            for h in history:
                line = f"第 {h['iteration']} 轮"
                if h.get("progress"):
                    line += f" | 进度: {h['progress']}"
                if h.get("summary"):
                    line += f" | {h['summary']}"
                parts.append(line)
            parts.append("=== 前几轮完成情况结束 ===\n")

        # Include plan from previous iterations
        if plan:
            parts.append("=== 整体计划 ===")
            parts.append(plan)
            parts.append("=== 整体计划结束 ===\n")

        # Progress format: anchor total from first iteration so denominator stays consistent
        if anchored_total is not None:
            progress_hint = f"已完成数/{anchored_total}"
        else:
            progress_hint = "已完成数/总数"

        # Signal template: plan field for must_complete, without for normal
        plan_field = ', "plan": "后续每轮计划（简洁，如需调整则更新，无变化则留空）"' if task.must_complete else ""

        if task.must_complete and iteration == 0:
            # First iteration of must_complete: require planning
            parts.append(f"""\
请遵循 CLAUDE.md 中的所有要求和项目约定。

这是一个必须全部完成的循环任务，你总共有 {max_iterations} 轮来完成所有任务项。

你的职责：
1. 打开 {task.todo_file_path}，理解其结构，统计所有待完成的任务项
2. 制定整体执行计划：规划每一轮大致完成哪些项，确保在 {max_iterations} 轮内全部完成
3. 执行本轮计划的任务项，在 todo 文件中标记为已完成

完成后，将以下 JSON 写入 {signal_path}：

还有待完成项，请继续下一轮：
{{"action": "continue", "reason": "...", "progress": "{progress_hint}", "summary": "本轮做了什么（一句话）", "plan": "后续每轮计划（简洁）"}}

全部完成：
{{"action": "done", "reason": "所有 todo 项已完成", "progress": "{progress_hint}", "summary": "本轮做了什么（一句话）"}}

注意：所有任务项必须全部完成，任务才算成功。请合理分配每轮工作量，确保在 {max_iterations} 轮内完成。
""")
        elif task.must_complete:
            # Subsequent iterations of must_complete
            # Calculate remaining items
            remaining_items = ""
            if anchored_total is not None and history:
                last_progress = ""
                for h in reversed(history):
                    if h.get("progress"):
                        last_progress = h["progress"]
                        break
                if last_progress:
                    try:
                        done_count = int(last_progress.split("/")[0])
                        remaining_items = f"，还剩 {anchored_total - done_count} 项未完成"
                    except (IndexError, ValueError):
                        pass

            parts.append(f"""\
请遵循 CLAUDE.md 中的所有要求和项目约定。

这是一个必须全部完成的循环任务的第 {iteration + 1} 轮，还剩 {remaining} 轮。

你的职责：
1. 打开 {task.todo_file_path}，按照计划执行本轮应完成的任务项
2. 在 todo 文件中将完成的项标记为已完成

完成后，将以下 JSON 写入 {signal_path}：

还有待完成项，请继续下一轮：
{{"action": "continue", "reason": "...", "progress": "{progress_hint}", "summary": "本轮做了什么（一句话）"{plan_field}}}

全部完成：
{{"action": "done", "reason": "所有 todo 项已完成", "progress": "{progress_hint}", "summary": "本轮做了什么（一句话）"}}

无法继续（遇到阻塞或明确问题）：
{{"action": "abort", "reason": "具体原因", "progress": "{progress_hint}", "summary": "本轮做了什么（一句话）"}}

注意：{"任务总数已确定为 " + str(anchored_total) + remaining_items + "，" if anchored_total is not None else ""}你还有 {remaining} 轮机会。
所有任务项必须全部完成。如需调整计划，在 plan 字段中更新。
""")
        else:
            # Normal (non-must_complete) loop
            total_note = ""
            if anchored_total is not None:
                total_note = f"\n注意：任务总数已确定为 {anchored_total}，progress 分母必须始终为 {anchored_total}，不要重新计数。"

            parts.append(f"""\
请遵循 CLAUDE.md 中的所有要求和项目约定。

这是一个持续循环任务的第 {iteration + 1} 轮。

你的职责：
1. 打开 {task.todo_file_path}，理解其结构，找到下一个待完成的任务项
2. 根据 CLAUDE.md 的要求执行该任务项
3. 在 todo 文件中将该项标记为已完成

完成后，将以下 JSON 写入 {signal_path}：

还有待完成项，请继续下一轮：
{{"action": "continue", "reason": "...", "progress": "{progress_hint}", "summary": "本轮做了什么（一句话）"}}

全部完成：
{{"action": "done", "reason": "所有 todo 项已完成", "progress": "{progress_hint}", "summary": "本轮做了什么（一句话）"}}

无法继续（遇到阻塞或明确问题）：
{{"action": "abort", "reason": "具体原因", "progress": "{progress_hint}", "summary": "本轮做了什么（一句话）"}}
{total_note}
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
        effort_level: str | None = None,
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
            model=task.model,
            resume_session_id=resume_sid,
            loop_iteration=iteration,
            git_env=git_env,
            thinking_budget=task.thinking_budget,
            effort_level=effort_level,
            provider=task.provider,
            enable_workflows=task.enable_workflows,
            enabled_skills=task.enabled_skills,
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

    async def _run_plan_phase(self, instance_id: int, task: Task, cwd: str, git_env: dict | None = None, effort_level: str | None = None):
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
            model=task.model,
            git_env=git_env or {},
            thinking_budget=task.thinking_budget,
            effort_level=effort_level,
            provider=task.provider,
            enable_workflows=task.enable_workflows,
            enabled_skills=task.enabled_skills,
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

    # -----------------------------------------------------------------------
    # Monitor Session lifecycle
    # -----------------------------------------------------------------------

    def start_monitor_session(self, monitor_session):
        task = asyncio.create_task(
            self._monitor_session_lifecycle(monitor_session.id)
        )
        self._monitor_tasks[monitor_session.id] = task

    async def _monitor_session_lifecycle(self, monitor_session_id: int):
        import json as _json
        from backend.models.monitor_session import MonitorSession, MonitorCheck

        try:
            async with self.db_factory() as db:
                ms = await db.get(MonitorSession, monitor_session_id)
                task = await db.get(Task, ms.task_id)
                if not ms or not task:
                    return
                task_id = ms.task_id
                interval = ms.interval
                max_checks = ms.max_checks
                model = ms.model

            while True:
                async with self.db_factory() as db:
                    ms = await db.get(MonitorSession, monitor_session_id)
                    task = await db.get(Task, task_id)
                    if not ms or ms.status != "running":
                        break
                    checks_done = ms.checks_done
                    ms_description = ms.description
                    ms_context = ms.monitor_context
                    task_status = task.status
                    task_cwd = task.last_cwd or task.target_repo or os.getcwd()

                if task_status in ("completed", "failed", "cancelled"):
                    final_status = "completed" if task_status == "completed" else "cancelled"
                    async with self.db_factory() as db:
                        ms = await db.get(MonitorSession, monitor_session_id)
                        ms.status = final_status
                        ms.completed_at = datetime.utcnow()
                        await db.commit()
                    await self.broadcaster.broadcast(
                        f"task:{task_id}",
                        {"event": "monitor_session_status", "monitor_session_id": monitor_session_id, "status": final_status},
                    )
                    break

                if checks_done >= max_checks:
                    async with self.db_factory() as db:
                        ms = await db.get(MonitorSession, monitor_session_id)
                        ms.status = "completed"
                        ms.completed_at = datetime.utcnow()
                        await db.commit()
                    await self.broadcaster.broadcast(
                        f"task:{task_id}",
                        {"event": "monitor_session_status", "monitor_session_id": monitor_session_id, "status": "completed"},
                    )
                    break

                prompt = self._build_monitor_prompt(
                    checks_done=checks_done,
                    description=ms_description,
                    context=ms_context,
                )

                check_status = "success"
                summary = ""
                full_output = ""
                is_done = False

                try:
                    full_output = await self._run_monitor_subprocess(
                        prompt=prompt,
                        cwd=task_cwd,
                        model=model,
                        monitor_session_id=monitor_session_id,
                    )
                    for line in full_output.splitlines():
                        line_stripped = line.strip()
                        if line_stripped.startswith("STATUS:"):
                            status_val = line_stripped[7:].strip().lower()
                            if status_val == "done":
                                is_done = True
                            elif status_val == "error":
                                check_status = "failed"
                        elif line_stripped.startswith("SUMMARY:"):
                            summary = line_stripped[8:].strip()
                except asyncio.TimeoutError:
                    check_status = "failed"
                    summary = "Monitor check timed out"
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    check_status = "failed"
                    summary = f"Monitor check error: {e}"

                async with self.db_factory() as db:
                    ms = await db.get(MonitorSession, monitor_session_id)
                    ms.checks_done += 1
                    ms.last_summary = summary
                    new_checks_done = ms.checks_done
                    check = MonitorCheck(
                        monitor_session_id=monitor_session_id,
                        check_number=new_checks_done,
                        status=check_status,
                        summary=summary,
                        full_output=full_output[:10000] if full_output else None,
                    )
                    db.add(check)

                    if is_done:
                        ms.status = "completed"
                        ms.completed_at = datetime.utcnow()

                    await db.commit()

                await self.broadcaster.broadcast(
                    f"task:{task_id}",
                    {
                        "event": "monitor_check",
                        "monitor_session_id": monitor_session_id,
                        "check_number": new_checks_done,
                        "status": check_status,
                        "summary": summary,
                        "is_monitor": True,
                    },
                )

                if is_done:
                    await self.broadcaster.broadcast(
                        f"task:{task_id}",
                        {"event": "monitor_session_status", "monitor_session_id": monitor_session_id, "status": "completed"},
                    )
                    break

                await asyncio.sleep(interval)

        except asyncio.CancelledError:
            proc = self._monitor_processes.get(monitor_session_id)
            if proc and proc.returncode is None:
                proc.kill()
                await proc.wait()
        except Exception:
            logger.exception(f"Monitor session {monitor_session_id} failed unexpectedly")
            try:
                async with self.db_factory() as db:
                    ms = await db.get(MonitorSession, monitor_session_id)
                    if ms and ms.status == "running":
                        ms.status = "failed"
                        ms.completed_at = datetime.utcnow()
                        await db.commit()
                        await self.broadcaster.broadcast(
                            f"task:{ms.task_id}",
                            {"event": "monitor_session_status", "monitor_session_id": ms.id, "status": "failed"},
                        )
            except Exception:
                pass
        finally:
            self._monitor_tasks.pop(monitor_session_id, None)
            self._monitor_processes.pop(monitor_session_id, None)

    async def _run_monitor_subprocess(self, prompt: str, cwd: str, model: str | None, monitor_session_id: int) -> str:
        import json as _json
        from backend.services.stream_parser import StreamParser

        cmd = [
            settings.claude_binary,
            "-p", prompt,
            "--output-format", "stream-json",
            "--verbose",
            "--dangerously-skip-permissions",
            "--disallowedTools", "Edit,Write,NotebookEdit",
        ]
        if model:
            cmd.extend(["--model", model])
        elif settings.default_model:
            cmd.extend(["--model", settings.default_model])

        env = {k: v for k, v in os.environ.items()
               if k.upper() not in ("CLAUDECODE", "CLAUDE_CODE")}

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=env,
            limit=10 * 1024 * 1024,
        )
        self._monitor_processes[monitor_session_id] = process

        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=300,
            )
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            raise
        finally:
            self._monitor_processes.pop(monitor_session_id, None)

        parser = StreamParser()
        text_parts = []
        for line in stdout.decode(errors="replace").splitlines():
            if not line.strip():
                continue
            events = parser.parse_line(line)
            for event in events:
                if event["event_type"] == "message" and event.get("content"):
                    text_parts.append(event["content"])

        return "\n".join(text_parts)

    def _build_monitor_prompt(self, checks_done: int, description: str, context: str | None) -> str:
        parts = [
            f"你是一个后台监控进程，这是第 {checks_done + 1} 次检查。",
            f"监控目标: {description}",
        ]
        if context:
            parts.append(f"上下文: {context}")
        parts.append(
            "\n检查并报告当前状态。使用 Bash 工具执行 ps aux、tail 日志等命令。"
            "\n\n最后两行必须严格遵循以下格式:"
            "\nSUMMARY: <一句话概括当前状态>"
            "\nSTATUS: running|done|error"
        )
        return "\n".join(parts)
