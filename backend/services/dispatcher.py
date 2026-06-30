import asyncio
import glob
import logging
import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from sqlalchemy import select, update, func

from sqlalchemy import select as sa_select

from backend.config import settings
from backend.models.instance import Instance
from backend.models.log_entry import LogEntry
from backend.models.task import Task
from backend.models.project import Project
from backend.models.global_settings import GlobalSettings
from backend.models.secret import Secret
from backend.services.git_config import merge_git_config, settings_to_dict
from backend.services.instance_manager import InstanceManager
from backend.services.task_queue import TaskQueue
from backend.services.ws_broadcaster import WebSocketBroadcaster

logger = logging.getLogger(__name__)


def _cleanup_skill_prompt_files(task_id: int):
    """Clean up temporary skill prompt files created during task launch."""
    tmpdir = tempfile.gettempdir()
    for pattern in [f"ccm-skills-{task_id}-*", f"ccm-user-skills-{task_id}-*"]:
        for f in glob.glob(os.path.join(tmpdir, pattern)):
            try:
                os.unlink(f)
            except OSError:
                pass


def _default_provider() -> str:
    provider = getattr(settings, "default_provider", "claude")
    return provider if isinstance(provider, str) and provider else "claude"


# Priority levels for the per-task message queue
PRIORITY_USER = 0
PRIORITY_MONITOR_COMPLETE = 1
PRIORITY_MONITOR_IMPORTANT = 2

# Per-task queue consumer tuning (module-level so tests can patch them).
# QUEUE_CONSUMER_IDLE_TIMEOUT: stop a consumer after this many idle seconds.
# QUEUE_HEARTBEAT_INTERVAL: how often the consumer marks itself alive.
# QUEUE_STUCK_THRESHOLD: _ensure_queue_worker treats a consumer whose heartbeat
#   is older than this as wedged and respawns it. It MUST be comfortably larger
#   than the heartbeat interval — a heartbeat now runs for the consumer's whole
#   lifetime (incl. a multi-minute turn or an idle wait), so a fresh heartbeat
#   means "alive" and only a truly wedged event loop trips the watchdog. See
#   prod task #728: a 14-min turn used to look "stuck", got respawned, and
#   produced concurrent `claude --resume` on one session.
QUEUE_CONSUMER_IDLE_TIMEOUT = 300
QUEUE_HEARTBEAT_INTERVAL = 30
QUEUE_STUCK_THRESHOLD = 120


@dataclass(order=True)
class QueuedMessage:
    priority: int
    timestamp: float = field(compare=True)
    prompt: str = field(compare=False)
    source: str = field(compare=False, default="user")
    user_message_text: str | None = field(compare=False, default=None)
    command_skills: dict | None = field(compare=False, default=None)
    # One-shot model override for this message only (not persisted to task)
    model_override: str | None = field(compare=False, default=None)


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
        # Instances mid-launch by the queued-message path. Their DB status is
        # still "idle" until launch() flips it to "running", so without an
        # in-memory claim the dispatch loop (and other queued-message launches)
        # could grab the same instance and clobber the half-started PTY session
        # (prod task #676).
        self._launching_instances: set[int] = set()
        self._running = False
        self._monitor_tasks: dict[int, asyncio.Task] = {}           # monitor_session_id -> asyncio task
        self._monitor_processes: dict[int, asyncio.subprocess.Process] = {}  # monitor_session_id -> subprocess
        self._monitor_log_fhs: dict[int, object] = {}  # monitor_session_id -> log file handle

        # Per-task message queue for serialized chat/monitor messages
        self._task_queues: dict[int, asyncio.PriorityQueue] = {}
        self._task_queue_workers: dict[int, asyncio.Task] = {}
        self._task_queue_activity: dict[int, float] = {}

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
            logger.info("Claude pool enabled with %d accounts", len(self.pool._accounts))

        await self._cleanup_stale_state()

        # Ensure we have worker instances up to max_concurrent_instances
        await self._ensure_instances()

        self._dispatch_task = asyncio.create_task(self._dispatch_loop())
        self._curator_task = asyncio.create_task(self._curator_loop())
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
        curator = getattr(self, "_curator_task", None)
        if curator and not curator.done():
            curator.cancel()
            try:
                await curator
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

    async def _ensure_min_idle_instances(self):
        """Auto top-up: keep at least min_idle_instances idle workers available.

        Named worker-<N> continuing from the highest existing numeric suffix,
        so deletions never produce duplicate names.
        """
        if settings.min_idle_instances <= 0:
            return
        async with self.db_factory() as db:
            result = await db.execute(select(Instance))
            existing = list(result.scalars().all())
            idle_count = sum(1 for i in existing if i.status == "idle")
            needed = settings.min_idle_instances - idle_count
            if needed <= 0:
                return
            base = 0
            for inst in existing:
                m = re.match(r"worker-(\d+)$", inst.name or "")
                if m:
                    base = max(base, int(m.group(1)))
            for i in range(needed):
                db.add(Instance(name=f"worker-{base + i + 1}"))
            await db.commit()
        logger.info(
            f"Auto-added {needed} worker instances "
            f"(idle was {idle_count}, min_idle_instances={settings.min_idle_instances})"
        )

    def _resolve_timeout(self, task) -> float | None:
        """任务有效超时（秒）。None = 不限时。

        task.timeout_hours: NULL = 全局默认（settings.task_timeout_seconds），
        0 = 不限时，>0 = 指定小时数。
        """
        th = getattr(task, "timeout_hours", None)
        if th is not None:
            return th * 3600 if th > 0 else None
        return settings.task_timeout_seconds

    async def _wait_process(self, process, task, label: str) -> None:
        """Wait for a launched process honoring the task-level timeout."""
        timeout = self._resolve_timeout(task)
        if timeout:
            try:
                await asyncio.wait_for(process.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                logger.warning(
                    f"{label} (task {task.id}) timed out after {timeout:.0f}s, killing process"
                )
                process.kill()
                await process.wait()
        else:
            await process.wait()

    async def _curator_loop(self):
        """Background curator: periodic skill lifecycle management.

        Checks every hour; only runs when:
          - >= 7 days since last run
          - No executing tasks (system is idle)
          - Project has enough history
        Reference: Hermes Curator scheduling + MiMo project age check.
        """
        _last_curator_run: datetime | None = None
        while self._running:
            try:
                await asyncio.sleep(3600)  # Check every hour
                if not self._running:
                    break

                now = datetime.utcnow()

                # First run: seed timestamp, defer by one full interval (Hermes pattern)
                if _last_curator_run is None:
                    _last_curator_run = now
                    continue

                # Check interval
                hours_since = (now - _last_curator_run).total_seconds() / 3600
                if hours_since < 168:  # 7 days
                    continue

                # Check if system is idle (no executing tasks)
                async with self.db_factory() as db:
                    from backend.models.task import Task
                    executing = (await db.execute(
                        select(func.count()).select_from(Task)
                        .where(Task.status.in_(["executing", "in_progress"]))
                    )).scalar() or 0
                if executing > 0:
                    continue

                # Run curator (every 7 days)
                logger.info("curator: starting periodic run")
                async with self.db_factory() as db:
                    from backend.services.skill_curator import run_curator
                    summary = await run_curator(db)
                    logger.info("curator: checked %d skills, %d stale",
                                summary["checked"], len(summary["stale"]))

                # Run distill (every 30 days — check if 30 days since last distill)
                if hours_since >= 720:  # 30 days
                    try:
                        logger.info("distill: starting periodic analysis")
                        async with self.db_factory() as db:
                            from backend.services.skill_distill import analyze_patterns
                            result = await analyze_patterns(db)
                            logger.info("distill: %s", result.get("summary", ""))
                    except Exception:
                        logger.exception("distill: periodic analysis failed")

                _last_curator_run = now

            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("curator: error in curator loop")

    async def _dispatch_loop(self):
        """Poll for idle instances + pending tasks and dispatch."""
        while self._running:
            try:
                # Top up idle workers before looking for capacity
                await self._ensure_min_idle_instances()

                # 路径 1：分布式 Worker task —— 不消耗本地 instance，直接转发
                await self._dispatch_worker_tasks()

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
                    # Skip if the queued-message path is mid-launching on it
                    # (DB status not yet flipped to "running" — prod task #676)
                    if instance.id in self._launching_instances:
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

    async def _dispatch_worker_tasks(self):
        """转发 pending 的 worker task（elastic-worker 设计 §5.3）。

        取出后立即标 in_progress，防止 2 秒后重复转发；转发失败回 failed。
        """
        from backend.main import worker_proxy
        if worker_proxy is None:
            return
        from backend.models.worker import Worker as WorkerModel

        async with self.db_factory() as db:
            result = await db.execute(
                select(Task).where(
                    Task.status == "pending", Task.worker_id.isnot(None)
                )
            )
            worker_tasks = list(result.scalars().all())

        for task in worker_tasks:
            async with self.db_factory() as db:
                worker = await db.get(WorkerModel, task.worker_id)
            if not worker or worker.status != "ready":
                continue  # worker 没就绪，留在 pending 等下轮
            # 与本地路径一致：把 project.local_path 写进 target_repo——
            # 否则迁回本机后 chat 解析不出 cwd（实测 task 58 教训）
            if task.project_id and not task.target_repo:
                async with self.db_factory() as db:
                    project = await db.get(Project, task.project_id)
                    if project and project.local_path:
                        await db.execute(
                            update(Task).where(Task.id == task.id)
                            .values(target_repo=project.local_path)
                        )
                        await db.commit()
                        task.target_repo = project.local_path
            async with self.db_factory() as db:
                await db.execute(
                    update(Task).where(Task.id == task.id)
                    .values(status="in_progress", started_at=datetime.utcnow())
                )
                await db.commit()
            # 与本地 task 一致地广播，前端立即看到状态（不等 relay 回传）
            await self.broadcaster.broadcast("tasks", {
                "event": "status_change",
                "task_id": task.id,
                "old_status": "pending",
                "new_status": "in_progress",
            })
            t = asyncio.create_task(self._safe_forward_to_worker(task))
            key = f"worker-{task.id}"
            self._running_tasks[key] = t  # 强引用防 GC
            t.add_done_callback(lambda _t, k=key: self._running_tasks.pop(k, None))

    async def _safe_forward_to_worker(self, task: Task):
        from backend.main import worker_proxy
        max_retries = 3
        for attempt in range(max_retries):
            try:
                await worker_proxy.forward_task_to_worker(task)
                return
            except Exception as e:
                if attempt < max_retries - 1:
                    delay = 2 ** attempt
                    logger.warning("forward task %s to worker failed (attempt %d/%d), retry in %ds: %s",
                                   task.id, attempt + 1, max_retries, delay, e)
                    await asyncio.sleep(delay)
                else:
                    logger.error("forward task %s to worker failed after %d attempts: %s",
                                 task.id, max_retries, e)
                    async with self.db_factory() as db:
                        await db.execute(
                            update(Task).where(Task.id == task.id)
                            .values(status="failed", error_message=f"转发到 Worker 失败 ({max_retries} 次重试): {e}")
                        )
                        await db.commit()
                    await self.broadcaster.broadcast("tasks", {
                        "event": "status_change",
                        "task_id": task.id,
                        "old_status": "in_progress",
                        "new_status": "failed",
                    })

    async def _pool_select(self, exclude: set[str] | None = None) -> str | None:
        """Select a pool account config_dir, or None if pool is off / exhausted."""
        if not self.pool:
            return None
        # validate=True probes accounts with a blocking subprocess (up to 30s
        # each) — must run off the event loop
        return await self.pool.select_async(exclude=exclude, validate=True)

    async def _resolve_resume_config_dir(self, session_id: str | None) -> str | None:
        """Resolve the CLAUDE_CONFIG_DIR for a (possibly resuming) launch.

        Prefers a fresh, validated pool account and migrates the session JSONL
        into it. The critical case is when the pool can hand out **no** healthy
        account (every account rate-limited): we must NOT let the launch fall
        through to an arbitrary inherited ``CLAUDE_CONFIG_DIR`` — that account
        won't hold the session JSONL and ``claude --resume`` dies with
        "No conversation found with session ID", which hard-fails the task and
        loses the session (prod tasks #734/#740). Instead anchor the resume to
        whichever account dir actually holds the session; if that account is
        rate-limited it surfaces as a recoverable rate-limit/transient event the
        existing retry paths handle, rather than a fatal lookup miss.

        Returns a config_dir, or None when there is no pool (default account)
        or no session to anchor a fallback to.
        """
        if not (self.pool and self.pool.enabled):
            return None

        # --- Resume happy path: anchor to the session's resident account ---
        # The expensive part used to be ``select_async(validate=True)``, which
        # spawned a ``claude -p`` probe (a full API round-trip, up to 30s) on
        # EVERY message before resume even started. That probe is redundant on
        # the resume path: rate-limited / auth-failed accounts are already
        # excluded by the in-memory cooldown map, and a limit that slips through
        # surfaces as a recoverable event the reactive rotation path handles.
        # Worse, validate-driven round-robin drifted the config_dir off the
        # session's resident account every turn, forcing the PTY pool to drop a
        # hot session and pay an 8s cold restart. So: if the session lives on a
        # healthy (not cooled-down) account, reuse it directly — no probe, no
        # migration, no config_dir drift, PTY hot-session preserved.
        if session_id:
            resident = self.pool.locate_session_config_dir(session_id)
            if (
                resident
                and not self.pool.is_in_cooldown(resident)
                and not self.pool.is_disabled(resident)
                and self.pool.is_known_account(resident)
            ):
                return resident
            # Resident account is missing, rate-limited, or disabled → pick a
            # healthy enabled account cheaply (cooldown/enabled-aware, no
            # subprocess) and migrate the session in. The disabled case makes
            # ``enabled=false`` a hard guarantee: an in-flight session sitting on
            # a retired account is moved off it on its next resume instead of
            # being reused.
            config_dir = self.pool.select(validate=False)
            if config_dir:
                if resident and resident != config_dir:
                    from backend.services.claude_pool import migrate_session
                    migrate_session(
                        old_config_dir=resident,
                        new_config_dir=config_dir,
                        session_id=session_id,
                    )
                return config_dir
            # Pool exhausted: anchor to where the session actually lives so
            # --resume finds the conversation instead of hard-failing on a wrong
            # (inherited) account dir.
            if resident:
                logger.warning(
                    "Pool exhausted; resuming session %s on its resident account "
                    "dir %s (account may be rate-limited, but --resume can still "
                    "find the conversation)",
                    session_id, resident,
                )
            return resident

        # Fresh launch (no session to anchor): just pick a healthy account.
        return self.pool.select(validate=False)

    async def _collect_failure_output(self, instance_id: int, task_id: int) -> str:
        """Gather stderr + recent log text once for failure classification.

        ``get_last_stderr()`` is destructive (it pops), so a caller that needs
        to test for BOTH transient overload and account rotation must collect
        once and pass the combined text into both detectors.
        """
        from backend.services.claude_pool import collect_process_output_for_detection
        stderr = self.instance_manager.get_last_stderr(instance_id)
        log_contents = await self.instance_manager.get_recent_log_contents(task_id, limit=10)
        return collect_process_output_for_detection(stderr, log_contents)

    async def _check_rate_limit_and_rotate(
        self,
        instance_id: int,
        task_id: int,
        exit_code: int,
        combined: str | None = None,
    ) -> dict | None:
        """After a failed process, check if it was a rate limit and attempt rotation.

        Returns a dict with {config_dir, session_id, excluded} if rotation is
        possible, or None if this is not a pool-rotatable failure. ``combined``
        may be pre-collected by the caller (see _collect_failure_output) to
        avoid double-popping stderr.
        """
        if not self.pool or exit_code == 0 or exit_code in (-2, 130):
            return None

        from backend.services.claude_pool import is_pool_rotatable, is_auth_failure, is_rate_limited
        from backend.services.claude_pool import migrate_session, collect_process_output_for_detection

        if combined is None:
            stderr = self.instance_manager.get_last_stderr(instance_id)
            log_contents = await self.instance_manager.get_recent_log_contents(task_id, limit=10)
            combined = collect_process_output_for_detection(stderr, log_contents)

        if not is_pool_rotatable(combined):
            return None

        old_config_dir = self.instance_manager.get_config_dir(instance_id)
        if not old_config_dir:
            # Launched on the default account (no explicit config_dir) —
            # rotation must still work; the default dir is a pool member.
            import os as _os
            old_config_dir = _os.path.expanduser("~/.claude")

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

        new_config_dir = await self._pool_select(exclude=excluded)
        if not new_config_dir:
            logger.warning("Pool exhausted — no alternative account for task %d", task_id)
            return None

        # Get session_id for --resume
        async with self.db_factory() as db:
            t = await db.get(Task, task_id)
            session_id = t.session_id if t else None

        if session_id:
            source_dir = self.pool.locate_session_config_dir(session_id) or old_config_dir
            migrate_session(
                old_config_dir=source_dir,
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

    async def _build_task_prompt(self, task: Task) -> str:
        """Reconstruct a task's initial prompt (CLAUDE.md preamble + secrets +
        images + enabled-skill templates + description). Shared by the first
        launch and every fresh re-launch (rotation / transient retry)."""
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
        if task.enabled_skills:
            from backend.services.command_registry import COMMAND_REGISTRY
            for skill_name, enabled in task.enabled_skills.items():
                if enabled and skill_name in COMMAND_REGISTRY:
                    cmd = COMMAND_REGISTRY[skill_name]
                    if not cmd.always_available:
                        parts.append(cmd.prompt_template)
        parts.append(f"任务:\n{task.description}")
        return "\n\n".join(parts)

    async def _relaunch_and_wait(
        self,
        instance_id: int,
        task: Task,
        cwd: str,
        git_env: dict | None,
        config_dir: str | None,
        session_id: str | None,
        *,
        thinking_budget: int | None,
        effort_level: str | None,
        label: str,
    ) -> int:
        """Resume (or fresh-launch) a task on a specific account, wait for the
        process + output consumer, and return its exit code."""
        if session_id:
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
            full_prompt = await self._build_task_prompt(task)
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

        process = self.instance_manager.processes.get(instance_id)
        if process:
            await self._wait_process(process, task, label)
        consumer = self.instance_manager._tasks.get(instance_id)
        if consumer:
            try:
                await asyncio.wait_for(consumer, timeout=30)
            except asyncio.TimeoutError:
                pass
        return process.returncode if process else -1

    async def _run_transient_retry(
        self,
        instance_id: int,
        task: Task,
        cwd: str,
        git_env: dict | None,
        *,
        thinking_budget: int | None = None,
        effort_level: str | None = None,
        attempt: int = 1,
    ):
        """Wait out a transient server-side 429/overload and retry the SAME account.

        Anthropic infra throttling/overload ("Server is temporarily limiting
        requests (not your usage limit)" / overloaded) is NOT an account usage
        limit — rotating accounts wouldn't help — so we back off and --resume
        the same session, up to settings.transient_retry_max times. Once the
        failure is no longer transient (or the budget is exhausted) we hand off
        to account rotation, then to the normal retry/fail path.
        """
        from backend.services.claude_pool import is_transient_overload, transient_retry_delay

        delay = transient_retry_delay(
            attempt,
            settings.transient_retry_base_delay,
            settings.transient_retry_max_delay,
        )
        logger.info(
            "Task %d transient 429/overload — waiting %.0fs before retry #%d/%d",
            task.id, delay, attempt, settings.transient_retry_max,
        )
        await self.broadcaster.broadcast(f"task:{task.id}", {
            "event_type": "transient_retry",
            "task_id": task.id,
            "attempt": attempt,
            "max_attempts": settings.transient_retry_max,
            "delay": round(delay, 1),
        })
        await asyncio.sleep(delay)

        config_dir = self.instance_manager.get_config_dir(instance_id)
        async with self.db_factory() as db:
            t = await db.get(Task, task.id)
            session_id = t.session_id if t else task.session_id

        exit_code = await self._relaunch_and_wait(
            instance_id, task, cwd, git_env, config_dir, session_id,
            thinking_budget=thinking_budget, effort_level=effort_level,
            label=f"Transient retry #{attempt}",
        )

        # PTY mode: another transient overload also aborts with exit_code 0, so
        # the flag — not the exit code — tells us whether it recovered.
        still_transient = (
            settings.transient_retry_enabled
            and self.instance_manager.transient_error_seen(instance_id)
        )

        if exit_code in (0, -2, 130) and not still_transient:
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
            logger.info("Task %d recovered after %d transient retry(ies)", task.id, attempt)
            return

        # Still failing — keep backing off while it's transient and budget
        # remains (flag covers PTY's exit_code-0 repeat; text covers stderr).
        combined = await self._collect_failure_output(instance_id, task.id)
        if (
            settings.transient_retry_enabled
            and attempt < settings.transient_retry_max
            and (still_transient or is_transient_overload(combined))
        ):
            await self._run_transient_retry(
                instance_id, task, cwd, git_env,
                thinking_budget=thinking_budget,
                effort_level=effort_level,
                attempt=attempt + 1,
            )
            return

        # No longer transient, or budget exhausted → account rotation, then
        # normal retry/fail. (Rotation never re-enters the transient path, so
        # there is no ping-pong between the two.)
        rotation = await self._check_rate_limit_and_rotate(
            instance_id, task.id, exit_code, combined=combined
        )
        if rotation:
            await self._run_pool_retry(
                instance_id, task, cwd, git_env,
                rotation["config_dir"], rotation["session_id"], rotation["excluded"],
                thinking_budget=thinking_budget, effort_level=effort_level,
            )
            return

        async with self.db_factory() as db:
            queue = TaskQueue(db)
            t = await queue.get(task.id)
            if t and t.retry_count < t.max_retries:
                await queue.retry(task.id)
                status = "pending"
            else:
                if still_transient:
                    reason = f"Transient server overload persisted after {attempt} retries"
                else:
                    reason = f"Exit code: {exit_code} after {attempt} transient retry(ies)"
                await queue.mark_failed(task.id, reason)
                status = "failed"
        await self.broadcaster.broadcast("tasks", {
            "event": "status_change",
            "task_id": task.id,
            "new_status": status,
            "instance_id": instance_id,
        })

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
            # 必须是绝对路径：PTY 模式按 cwd 推导 JSONL 轮询路径，"." 会落空
            cwd = task.last_cwd or task.target_repo or os.getcwd()
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
            full_prompt = await self._build_task_prompt(task)

            # Pool: select an account for this launch. For a resume (retry of a
            # task that already has a session) this also anchors to the session's
            # resident dir when the pool is exhausted, so --resume doesn't miss
            # the JSONL and hard-fail with "No conversation found" (prod #734/#740).
            pool_config_dir = await self._resolve_resume_config_dir(task.session_id)

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
                system_prompt_mode=task.system_prompt_mode,
            )

            # Wait for process to finish (with timeout)
            process = self.instance_manager.processes.get(instance_id)
            if process:
                await self._wait_process(process, task, "Task run")

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

            # PTY mode aborts a transient-429/overload turn but keeps the
            # persistent session alive, so it reports exit_code 0. The per-turn
            # flag (set in _process_event) is the reliable cross-mode signal →
            # wait + retry the same account before judging success/failure.
            if settings.transient_retry_enabled and self.instance_manager.transient_error_seen(instance_id):
                await self._run_transient_retry(
                    instance_id, task, cwd, git_env,
                    thinking_budget=thinking_budget,
                    effort_level=effort_level,
                )
                return

            if exit_code != 0:
                from backend.services.claude_pool import is_transient_overload
                combined = await self._collect_failure_output(instance_id, task.id)

                # Transient overload that only surfaced on stderr (subprocess
                # mode) — the flag above may miss it, so re-check the text.
                if settings.transient_retry_enabled and is_transient_overload(combined):
                    await self._run_transient_retry(
                        instance_id, task, cwd, git_env,
                        thinking_budget=thinking_budget,
                        effort_level=effort_level,
                    )
                    return

                # Account usage-limit / auth-failure → rotate account and resume
                rotation = await self._check_rate_limit_and_rotate(
                    instance_id, task.id, exit_code, combined=combined
                )
                if rotation:
                    await self._run_pool_retry(
                        instance_id, task, cwd, git_env,
                        rotation["config_dir"], rotation["session_id"],
                        rotation["excluded"],
                        thinking_budget=thinking_budget,
                        effort_level=effort_level,
                    )
                    return

                # "Prompt is too long" — compact and retry instead of failing
                log_contents = await self.instance_manager.get_recent_log_contents(task.id, limit=5)
                log_text = " ".join(log_contents) if isinstance(log_contents, list) else str(log_contents)
                if "prompt is too long" in log_text.lower():
                    try:
                        async with self.db_factory() as db:
                            t = await db.get(Task, task.id)
                            if t and t.session_id:
                                logger.warning("Task %d hit 'Prompt is too long', compacting session", task.id)
                                summary = await self._compact_session(task.id, t.session_id, db)
                                if summary:
                                    t.session_id = None
                                    t.context_window_usage = None
                                    t.status = "pending"
                                    t.description = f"[Context compacted]\n{summary}\n\n---\n\n{t.description or ''}"
                                    await db.commit()
                                    await self.broadcaster.broadcast("tasks", {
                                        "event": "status_change",
                                        "task_id": task.id,
                                        "new_status": "pending",
                                        "instance_id": instance_id,
                                    })
                                    return
                    except Exception:
                        logger.exception("Prompt-too-long compact failed for task %d", task.id)

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

            await self._handle_pr_review_completion(task)

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
            await self._handle_pr_review_failure(task, str(e))
        finally:
            from backend.services.mcp_config import cleanup_mcp_config
            cleanup_mcp_config(task.id)
            _cleanup_skill_prompt_files(task.id)
            self._running_tasks.pop(instance_id, None)
            await self._reset_instance_if_stale(instance_id, task.id)

    async def _handle_pr_review_completion(self, task: Task):
        meta = task.metadata_ or {}
        pr_review_id = meta.get("pr_review_id")
        if not pr_review_id:
            return
        try:
            from backend.models.pr_monitor import MonitoredRepo, PRReview
            from backend.services.pr_review_service import check_and_update_review
            async with self.db_factory() as db:
                review = await db.get(PRReview, pr_review_id)
                if not review:
                    return
                repo = await db.get(MonitoredRepo, review.repo_id)
                if not repo:
                    return
                await check_and_update_review(db, pr_review_id, repo.repo_full_name)
        except Exception as e:
            logger.error(f"PR review completion handler error: {e}", exc_info=True)

    async def _handle_pr_review_failure(self, task: Task, error: str):
        meta = task.metadata_ or {}
        pr_review_id = meta.get("pr_review_id")
        if not pr_review_id:
            return
        try:
            from backend.models.pr_monitor import PRReview
            from datetime import datetime
            async with self.db_factory() as db:
                review = await db.get(PRReview, pr_review_id)
                if review and review.status in ("pending", "reviewing"):
                    review.status = "error"
                    review.action_taken = "error"
                    review.review_summary = f"Task failed: {error[:500]}"
                    review.completed_at = datetime.utcnow()
                    await db.commit()
                    await self.broadcaster.broadcast("pr-monitor", {
                        "type": "review_updated",
                        "review_id": review.id,
                        "status": "error",
                    })
        except Exception as e:
            logger.error(f"PR review failure handler error: {e}", exc_info=True)

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

        exit_code = await self._relaunch_and_wait(
            instance_id, task, cwd, git_env, config_dir, session_id,
            thinking_budget=thinking_budget, effort_level=effort_level,
            label="Pool retry run",
        )

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
        """Loop entry: run iterations, then always release the PTY session.

        In PTY mode the whole loop shares one hot session (one iteration ==
        one turn); releasing it afterwards keeps the pool free of one-shot
        leftovers. No-op in -p mode.
        """
        try:
            await self._run_loop_iterations(
                instance_id, task, cwd, git_env, effort_level=effort_level
            )
        finally:
            try:
                async with self.db_factory() as db:
                    t = await db.get(Task, task.id)
                    sid = t.session_id if t else None
                if sid:
                    release = getattr(self.instance_manager, "release_pty_session", None)
                    if release is not None:
                        result = release(sid)
                        import inspect as _inspect
                        if _inspect.isawaitable(result):
                            await result
            except Exception:
                logger.exception("Failed to release loop PTY session for task %d", task.id)

    async def _run_loop_iterations(self, instance_id: int, task: Task, cwd: str, git_env: dict | None = None, effort_level: str | None = None):
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
                # 每轮刷新可变设置（用户可能在 Config 面板中修改了
                # model/effort/thinking/timeout，下一轮立即生效）
                task = t

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

            # PTY mode: iterations after the first reuse the same hot session
            # (one iteration == one turn) — no cold start, continuous context.
            # -p mode keeps its stateless-per-iteration semantics (no resume).
            resume_sid = None
            if iteration > 0 and getattr(self.instance_manager, "pty_mode_enabled", False):
                async with self.db_factory() as db:
                    t = await db.get(Task, task.id)
                    resume_sid = t.session_id if t else None

            # Pool: pick the account for this iteration (mirrors the non-loop
            # Step 4 path). Without this, loop launches passed config_dir=None
            # and silently inherited the hardcoded systemd CLAUDE_CONFIG_DIR —
            # the pool was never consulted, cooled-down accounts were never
            # avoided, and a PTY resume on iteration>0 could hit the wrong
            # account and die with "No conversation found". For a resume it
            # anchors to the session's resident account (no config_dir drift →
            # PTY hot session preserved); fresh iterations get a healthy pick.
            config_dir = await self._resolve_resume_config_dir(resume_sid)

            await self.instance_manager.launch(
                instance_id=instance_id,
                prompt=prompt,
                task_id=task.id,
                cwd=cwd,
                model=task.model,
                resume_session_id=resume_sid,
                loop_iteration=iteration,
                git_env=git_env or {},
                thinking_budget=task.thinking_budget,
                effort_level=task.effort_level or effort_level,
                provider=task.provider,
                config_dir=config_dir,
                enable_workflows=task.enable_workflows,
                enabled_skills=task.enabled_skills,
            )

            process = self.instance_manager.processes.get(instance_id)
            if process:
                await self._wait_process(process, task, "Loop iteration")

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
                # 每轮刷新可变设置（model/effort/thinking/timeout 下一轮生效）
                task = t

            if turn == 0:
                prompt = self._build_goal_initial_prompt(task)
                # Pool: pick a healthy account for the fresh session (mirrors the
                # non-goal Step 4 path). Without this, goal launches passed
                # config_dir=None and silently inherited the hardcoded systemd
                # CLAUDE_CONFIG_DIR — the pool was never consulted. See loop fix
                # (#770); goal had the identical gap.
                config_dir = await self._resolve_resume_config_dir(None)
                await self.instance_manager.launch(
                    instance_id=instance_id,
                    prompt=prompt,
                    task_id=task.id,
                    cwd=cwd,
                    model=task.model,
                    loop_iteration=turn,
                    git_env=git_env or {},
                    thinking_budget=task.thinking_budget,
                    effort_level=task.effort_level or effort_level,
                    provider=task.provider,
                    config_dir=config_dir,
                    enable_workflows=task.enable_workflows,
                    enabled_skills=task.enabled_skills,
                )
            else:
                follow_up = self._build_goal_followup_prompt(last_reason, turn, max_turns)
                # Resume on the session's resident account (no config_dir drift →
                # PTY hot session preserved); migrate / fall back if cooled down.
                config_dir = await self._resolve_resume_config_dir(session_id)
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
                    effort_level=task.effort_level or effort_level,
                    provider=task.provider,
                    config_dir=config_dir,
                    enable_workflows=task.enable_workflows,
                    enabled_skills=task.enabled_skills,
                )

            # Wait for process to finish
            process = self.instance_manager.processes.get(instance_id)
            if process:
                await self._wait_process(process, task, "Goal turn")

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

        # Same pool anchoring as the main loop launch: resume on the session's
        # resident account, not the inherited systemd default (else --resume
        # misses the JSONL on the wrong account).
        config_dir = await self._resolve_resume_config_dir(resume_sid)

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
            config_dir=config_dir,
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
            await self._wait_process(process, task, "Plan phase")

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
        """Run a persistent monitor sub-agent process.

        New flow: read DB → build prompt → generate MCP config → launch
        persistent Claude subprocess → wait for process exit (up to 4h) →
        check session status → cleanup.

        The sub-agent communicates via its own MCP tools (report_status,
        mark_complete, get_context) which call back into the CCM API.
        """
        from backend.models.monitor_session import MonitorSession
        from backend.services.mcp_config import (
            generate_monitor_agent_mcp_config,
            cleanup_monitor_agent_mcp_config,
        )

        task_id: int | None = None

        try:
            async with self.db_factory() as db:
                ms = await db.get(MonitorSession, monitor_session_id)
                if not ms:
                    return
                task = await db.get(Task, ms.task_id)
                if not task:
                    return
                task_id = ms.task_id
                ms_description = ms.description
                ms_context = ms.monitor_context
                ms_interval = ms.interval
                ms_max_checks = ms.max_checks
                model = ms.model
                task_cwd = task.last_cwd or task.target_repo or os.getcwd()

            # Dynamic timeout: expected duration + 50% buffer, minimum 30 min
            expected_seconds = ms_interval * ms_max_checks
            timeout_seconds = max(expected_seconds * 1.5, 1800)

            prompt = self._build_monitor_agent_prompt(
                description=ms_description,
                context=ms_context,
            )

            mcp_config_path = generate_monitor_agent_mcp_config(
                monitor_session_id=monitor_session_id,
                task_id=task_id,
            )

            proc = await self._launch_monitor_agent(
                prompt=prompt,
                cwd=task_cwd,
                model=model,
                monitor_session_id=monitor_session_id,
                mcp_config_path=mcp_config_path,
            )

            try:
                await asyncio.wait_for(
                    proc.wait(),
                    timeout=timeout_seconds,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    f"Monitor session {monitor_session_id} timed out after {timeout_seconds:.0f}s, killing"
                )
                proc.kill()
                await proc.wait()

            # If session is still running after process exit, the sub-agent
            # exited abnormally without calling mark_complete → mark failed
            async with self.db_factory() as db:
                ms = await db.get(MonitorSession, monitor_session_id)
                if ms and ms.status == "running":
                    ms.status = "failed"
                    ms.completed_at = datetime.utcnow()
                    await db.commit()
                    await self.broadcaster.broadcast(
                        f"task:{task_id}",
                        {
                            "event": "monitor_session_status",
                            "monitor_session_id": monitor_session_id,
                            "status": "failed",
                        },
                    )
                    logger.warning(
                        f"Monitor session {monitor_session_id} process exited "
                        f"(rc={proc.returncode}) without calling mark_complete, marked failed"
                    )

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
            cleanup_monitor_agent_mcp_config(monitor_session_id)
            log_fh = self._monitor_log_fhs.pop(monitor_session_id, None)
            if log_fh:
                try:
                    log_fh.close()
                except Exception:
                    pass
            self._monitor_tasks.pop(monitor_session_id, None)
            self._monitor_processes.pop(monitor_session_id, None)

    async def _launch_monitor_agent(
        self,
        prompt: str,
        cwd: str,
        model: str | None,
        monitor_session_id: int,
        mcp_config_path: Path,
    ) -> asyncio.subprocess.Process:
        """Launch a persistent Claude subprocess for a monitor sub-agent.

        Stdout is written to a log file (not PIPE) to prevent buffer blocking.
        The process runs in its own session (start_new_session=True) so it can
        be killed independently without affecting the parent process group.
        """
        cmd = [
            settings.claude_binary,
            "-p", prompt,
            "--output-format", "stream-json",
            "--verbose",
            "--dangerously-skip-permissions",
            "--disallowedTools", "Edit,Write,NotebookEdit,Workflow,Agent,Monitor",
            "--mcp-config", str(mcp_config_path),
        ]
        if model:
            cmd.extend(["--model", model])
        elif settings.default_model:
            cmd.extend(["--model", settings.default_model])

        env = {k: v for k, v in os.environ.items()
               if k.upper() not in ("CLAUDECODE", "CLAUDE_CODE")}

        # Monitor sub-agent needs a logged-in account. Pick one from the pool
        # (or fall back to default ~/.claude).
        if self.pool:
            config_dir = await self._pool_select()
            if config_dir:
                env["CLAUDE_CONFIG_DIR"] = config_dir

        log_path = Path(f"/tmp/ccm_monitor_{monitor_session_id}.log")
        log_fh = open(log_path, "wb")
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=log_fh,
                stderr=log_fh,
                cwd=cwd,
                env=env,
                start_new_session=True,
            )
        except Exception:
            log_fh.close()
            raise
        # Keep log_fh open — closing it terminates the subprocess's stdout pipe
        self._monitor_log_fhs[monitor_session_id] = log_fh

        self._monitor_processes[monitor_session_id] = process
        logger.info(
            f"Monitor agent launched: session={monitor_session_id} pid={process.pid} "
            f"log={log_path}"
        )
        return process

    def _build_monitor_agent_prompt(self, description: str, context: str | None) -> str:
        """Build the system prompt for a monitor sub-agent."""
        parts = [
            "你是一个自主监控 Agent，持续监控目标并在有变化时主动汇报。",
            "",
            "## 监控目标",
            description,
        ]
        if context:
            parts.append("")
            parts.append("## 上下文")
            parts.append(context)
        parts.append("")
        parts.append("""\
## 你的 MCP 工具
- report_status(summary, is_important): 报告状态。重要变化设 is_important=True
- mark_complete(reason): 监控目标完成时调用，然后立即停止所有活动
- get_context(): 获取最新监控配置

## 行为准则
1. 用 Bash 执行 ps、tail、cat 等命令检查状态
2. 每次检查后调用 report_status 汇报
3. 等待下一轮检查时，用 python 延时命令等待，例如：
   `Bash(command="python3 -c \"import time; time.sleep(30)\"", timeout=120000)`
   【重要】不要使用 bash 的 sleep 命令（会被系统拦截），必须用 python3 的 time.sleep。
4. 【关键】你必须严格按以下循环执行，绝不中断：
   检查 → report_status → python sleep → 检查 → report_status → python sleep → ...
   每一步都是一个独立的工具调用。你的进程必须持续运行直到目标完成。
5. 任务完成/失败/异常 → mark_complete 并说明原因，然后停止
6. 你是只读观察者，不要修改任何文件
7. 【禁止】不要使用内置的 Agent 工具
8. 【禁止】不要使用 Monitor 工具、ScheduleWakeup 工具或 run_in_background —— 这些会导致你退出进程
9. 【禁止】不要在调用 mark_complete 之前结束你的回合（end_turn）

先做一次初始状态检查并 report_status，然后用 python sleep 等待，然后继续下一轮。""")
        return "\n".join(parts)

    # -----------------------------------------------------------------------
    # Per-task message queue (chat + monitor reports)
    # -----------------------------------------------------------------------

    def _get_task_queue(self, task_id: int) -> asyncio.PriorityQueue:
        if task_id not in self._task_queues:
            self._task_queues[task_id] = asyncio.PriorityQueue()
        return self._task_queues[task_id]

    def _ensure_queue_worker(self, task_id: int):
        existing = self._task_queue_workers.get(task_id)
        if existing and not existing.done():
            last_activity = self._task_queue_activity.get(task_id, 0)
            if last_activity and time.monotonic() - last_activity > QUEUE_STUCK_THRESHOLD:
                logger.warning(
                    f"Task {task_id} queue consumer stuck for >{QUEUE_STUCK_THRESHOLD}s, "
                    f"cancelling and respawning"
                )
                existing.cancel()
            else:
                return
        self._task_queue_activity[task_id] = time.monotonic()
        worker = asyncio.create_task(self._task_queue_consumer(task_id))
        self._task_queue_workers[task_id] = worker

    async def enqueue_message(
        self,
        task_id: int,
        prompt: str,
        priority: int = PRIORITY_USER,
        source: str = "user",
        user_message_text: str | None = None,
        command_skills: dict | None = None,
        model_override: str | None = None,
    ):
        """Enqueue a message for the main agent of a task.

        Messages are processed serially by a per-task consumer.
        """
        q = self._get_task_queue(task_id)
        msg = QueuedMessage(
            priority=priority,
            timestamp=time.monotonic(),
            prompt=prompt,
            source=source,
            user_message_text=user_message_text,
            command_skills=command_skills,
            model_override=model_override,
        )
        await q.put(msg)
        self._ensure_queue_worker(task_id)
        logger.info(
            f"Enqueued message for task {task_id}: source={source} priority={priority} "
            f"queue_depth={q.qsize()}"
        )

    def clear_task_queue(self, task_id: int) -> int:
        """Drop all pending queued messages for a task (used on interrupt).

        Returns the number of messages discarded. The message currently being
        processed (if any) is not affected — callers stop the process separately.
        """
        q = self._task_queues.get(task_id)
        if not q:
            return 0
        cleared = 0
        while True:
            try:
                q.get_nowait()
            except asyncio.QueueEmpty:
                break
            q.task_done()
            cleared += 1
        if cleared:
            logger.info(f"Cleared {cleared} pending queued message(s) for task {task_id} on interrupt")
        return cleared

    async def _queue_heartbeat(self, task_id: int):
        """Continuously mark a task's queue consumer as alive.

        Runs for the consumer's entire lifetime — including a long turn blocked
        in `_wait_process` and an idle wait on `q.get()` — so neither looks
        "stuck" to `_ensure_queue_worker`. Previously the activity timestamp was
        only bumped around each `_process_queued_message` call, so any turn
        longer than QUEUE_STUCK_THRESHOLD froze the heartbeat, the watchdog
        respawned the consumer, and the orphaned `claude` subprocess kept
        running — yielding concurrent `--resume` on one session (prod task #728).
        """
        try:
            while True:
                self._task_queue_activity[task_id] = time.monotonic()
                await asyncio.sleep(QUEUE_HEARTBEAT_INTERVAL)
        except asyncio.CancelledError:
            pass

    async def _task_queue_consumer(self, task_id: int):
        """Serial consumer: process queued messages one at a time for a task."""
        q = self._get_task_queue(task_id)
        # Lifetime heartbeat: keeps the watchdog from respawning us during a
        # long-running turn or an idle wait (see _queue_heartbeat / task #728).
        hb_task = asyncio.create_task(self._queue_heartbeat(task_id))

        try:
            while True:
                try:
                    msg: QueuedMessage = await asyncio.wait_for(
                        q.get(), timeout=QUEUE_CONSUMER_IDLE_TIMEOUT
                    )
                except asyncio.TimeoutError:
                    logger.info(
                        f"Task {task_id} queue consumer idle for "
                        f"{QUEUE_CONSUMER_IDLE_TIMEOUT}s, stopping"
                    )
                    break

                try:
                    await self._process_queued_message(task_id, msg)
                except Exception:
                    logger.exception(f"Error processing queued message for task {task_id}")
                finally:
                    q.task_done()
        finally:
            hb_task.cancel()
            # Only deregister if THIS task is still the registered worker. The
            # watchdog (_ensure_queue_worker) may have already cancelled us and
            # registered a fresh consumer; popping unconditionally would erase
            # the live worker's registration, so the next enqueue would spawn a
            # *second* live consumer → two concurrent `--resume` (task #728).
            if self._task_queue_workers.get(task_id) is asyncio.current_task():
                self._task_queue_workers.pop(task_id, None)
            if q.empty():
                self._task_queues.pop(task_id, None)

    async def _process_queued_message(self, task_id: int, msg: QueuedMessage):
        """Resume main agent session with a queued message."""
        # Phase 1: read task state, find idle instance, launch process
        inst_id: int | None = None
        has_temp_skills = False
        original_skills: dict = {}
        async with self.db_factory() as db:
            task = await db.get(Task, task_id)
            if not task or not task.session_id:
                logger.warning(f"Task {task_id} not found or no session, skipping queued message")
                return

            # Recover before resuming when the session can't be resumed:
            #   - task=failed after an abnormal exit (session may still be on disk), OR
            #   - the session JSONL is gone (resume would die with "No conversation
            #     found", which non-0 exits and hard-fails the task).
            # Without the on-disk check the FIRST message after a session vanishes
            # is always sacrificed to flip the task to "failed"; only the SECOND
            # message reaches this branch and recovers (prod task #725).
            from backend.api.tasks import _clone_session, _find_session_jsonl
            session_gone = bool(task.session_id) and _find_session_jsonl(task.session_id) is None
            if task.session_id and (task.status == "failed" or session_gone):
                if task.status == "failed":
                    logger.info("Task %d session crashed, recovering session...", task_id)
                else:
                    logger.warning(
                        "Task %d session %s not on disk, recovering before resume "
                        "(would otherwise hard-fail with 'No conversation found')",
                        task_id, task.session_id,
                    )
                cloned = await _clone_session(task_id, db)
                if cloned:
                    task.session_id = cloned["session_id"]
                    logger.info("Task %d cloned session -> %s", task_id, task.session_id)
                else:
                    # JSONL file missing, fall back to compact summary
                    logger.warning("Task %d JSONL not found, falling back to compact summary", task_id)
                    summary = await self._compact_session(task_id, task.session_id, db)
                    task.session_id = None
                    task.context_window_usage = None
                    if summary:
                        msg.prompt = (
                            f"[上下文摘要 — 之前的对话记录（会话异常中断后恢复）]\n{summary}\n\n"
                            f"---\n\n[新消息]\n{msg.prompt}"
                        )
                # 关键：不能设成 "pending"——否则主调度循环 (dequeue) 会把它当作
                # 新任务抢走一个空闲 instance 从头执行 task 描述，导致同一 task 出现
                # 两个 Claude session（一个回应聊天、一个重跑任务）。设成 "in_progress"
                # 表示"已被 queue consumer 认领、待 resume"，dispatch loop 不会重复分配。
                # 详见 PROGRESS.md task #707 双 session 竞争条件。
                task.status = "in_progress"
                task.error_message = None
                await db.commit()

            # Wait for main agent to be idle (not executing)
            for attempt in range(60):
                is_busy = False
                for iid, proc in list(self.instance_manager.processes.items()):
                    if proc.returncode is None:
                        inst = await db.get(Instance, iid)
                        if inst and inst.current_task_id == task_id:
                            is_busy = True
                            break
                if not is_busy:
                    break
                await asyncio.sleep(2)
            else:
                logger.warning(f"Task {task_id} still busy after 120s, re-queueing message: {msg.source}")
                q = self._get_task_queue(task_id)
                await q.put(msg)
                await asyncio.sleep(5)
                return

            # Find idle instance — exclude instances the dispatch loop has
            # claimed for an in-flight lifecycle, or that another queued-message
            # launch is mid-launching. Their DB status is still "idle" until
            # launch() flips it, so selecting one here would let two paths grab
            # the same instance and the second launch would kill the first's
            # half-started PTY session (prod task #676).
            busy_iids = {
                iid for iid, t in self._running_tasks.items() if not t.done()
            } | self._launching_instances
            stmt = select(Instance).where(Instance.status == "idle")
            if busy_iids:
                stmt = stmt.where(Instance.id.notin_(busy_iids))
            result = await db.execute(stmt.order_by(Instance.id).limit(1))
            inst = result.scalar_one_or_none()
            if not inst:
                logger.warning(f"No idle instance for task {task_id}, re-queueing message")
                q = self._get_task_queue(task_id)
                await q.put(msg)
                await asyncio.sleep(5)
                return

            # Build git env
            merged: dict = {}
            if task.project_id:
                project = await db.get(Project, task.project_id)
                global_cfg = await db.get(GlobalSettings, 1)
                if project:
                    merged = merge_git_config(settings_to_dict(project), settings_to_dict(global_cfg))
            git_env = _build_git_env(merged)

            effort_level = task.effort_level or settings.default_effort

            # Pool: pick the account for this resume. Resolves a fresh validated
            # account (migrating the session into it) when one is available, and
            # — crucially — when the pool is exhausted still anchors --resume to
            # the session's resident dir instead of letting it fall through to an
            # inherited CLAUDE_CONFIG_DIR that lacks the JSONL (prod #734/#740).
            config_dir = await self._resolve_resume_config_dir(task.session_id)

            logger.info(
                f"Processing queued message for task {task_id}: source={msg.source} "
                f"on instance {inst.id}"
            )

            # Merge command_skills with task's enabled_skills for this launch
            original_skills = dict(task.enabled_skills or {})
            effective_skills = dict(original_skills)
            if msg.command_skills:
                has_temp_skills = True
                effective_skills.update(msg.command_skills)
                # Write to DB before launch so API-level checks pass
                # (MCP server may call API immediately after process starts)
                task.enabled_skills = effective_skills
                await db.commit()

            # 上下文超 90% 时自动摘要 + 新 session（无限续聊）
            if task.session_id and task.context_window_usage:
                usage = task.context_window_usage
                total_input = (usage.get("input_tokens") or 0) + (usage.get("cache_read_input_tokens") or 0) + (usage.get("cache_creation_input_tokens") or 0)
                # context_window 可能被 CC 低报（1M 模型报 200K），用模型名兜底
                window = usage.get("context_window") or 200_000
                model_lower = (msg.model_override or task.model or "").lower()
                if "[1m]" in model_lower or "fable" in model_lower:
                    window = max(window, 1_000_000)
                utilization = total_input / window if window else 0
                if utilization >= 0.90:
                    logger.info(
                        "Task %d context at %.0f%% (%d/%d), compacting session...",
                        task_id, utilization * 100, total_input, window,
                    )
                    # 收集最近对话摘要
                    summary = await self._compact_session(task_id, task.session_id, db)
                    if summary:
                        # 清空 session_id → 下次 launch 开新 session，prompt 带摘要
                        task.session_id = None
                        task.context_window_usage = None
                        await db.commit()
                        msg.prompt = (
                            f"[上下文摘要 — 之前的对话记录]\n{summary}\n\n"
                            f"---\n\n[新消息]\n{msg.prompt}"
                        )
                        logger.info("Task %d compacted, new session will start with summary", task_id)

            # Capture launch params before closing DB session
            launch_kwargs = dict(
                instance_id=inst.id,
                prompt=msg.prompt,
                task_id=task_id,
                cwd=task.last_cwd or task.target_repo or os.getcwd(),
                model=msg.model_override or task.model,
                resume_session_id=task.session_id,
                git_env=git_env,
                thinking_budget=task.thinking_budget,
                effort_level=effort_level,
                chat_initiated=True,
                config_dir=config_dir,
                provider=task.provider,
                enable_workflows=task.enable_workflows,
                enabled_skills=effective_skills,
                system_prompt_mode=task.system_prompt_mode,
            )
            inst_id = inst.id

            # Write a log entry for monitor-sourced messages so frontend can track source
            if msg.source and msg.source.startswith("monitor:"):
                import json as _json
                monitor_log = LogEntry(
                    instance_id=inst.id,
                    task_id=task_id,
                    event_type="user_message",
                    role="user",
                    content=msg.user_message_text or msg.prompt,
                    raw_json=_json.dumps({"source": "monitor"}),
                    is_error=False,
                )
                db.add(monitor_log)
                await db.flush()

                await self.broadcaster.broadcast(f"task:{task_id}", {
                    "event_type": "user_message",
                    "role": "user",
                    "content": msg.user_message_text or msg.prompt,
                    "source": "monitor",
                })

            task.status = "executing"
            task.completed_at = None
            await db.commit()

            # Claim the instance across the launch window: launch() only flips
            # its DB status to "running" once the PTY session is fully spawned,
            # so until then both the dispatch loop and other queued-message
            # launches must treat it as taken (prod task #676). Released in
            # finally so a failed launch can't leak the claim and wedge the
            # instance out of the dispatch pool forever.
            self._launching_instances.add(inst_id)
            try:
                await self.instance_manager.launch(**launch_kwargs)
            finally:
                self._launching_instances.discard(inst_id)

            await self.broadcaster.broadcast("tasks", {
                "event": "status_change",
                "task_id": task_id,
                "new_status": "executing",
                "instance_id": inst_id,
            })
        # DB session closed — process runs independently

        # Phase 2: wait for process to finish (no DB held)
        try:
            process = self.instance_manager.processes.get(inst_id)
            if process:
                # Chat 路径同样遵守任务级超时（此前 chat 无超时，可无限占住 instance）
                await self._wait_process(process, task, "Chat run")
            # Status management is handled by _consume_output (chat_initiated=True)
            #
            # PTY mode: a transient 429/overload aborts the turn but the
            # persistent session stays alive, so on_exit reports exit_code 0 and
            # never enters the failure path. The per-turn flag set in
            # _process_event is the only reliable signal here. We drive the
            # wait+retry loop from this consumer (heartbeat-covered, so the
            # watchdog won't respawn us): each retry relaunches the same account
            # and we re-check the flag after it finishes. Subprocess mode is
            # handled via exit_code in _consume_output, so restrict to PTY to
            # avoid double-firing.
            if (
                settings.transient_retry_enabled
                and inst_id is not None
                and self.instance_manager.pty_mode_enabled
            ):
                while self.instance_manager.transient_error_seen(inst_id):
                    launched = await self.instance_manager._try_chat_transient_retry(
                        inst_id, task_id, 1, ""
                    )
                    if not launched:
                        break  # budget exhausted / no longer transient
                    process = self.instance_manager.processes.get(inst_id)
                    if process:
                        await self._wait_process(process, task, "Chat transient retry")
                self.instance_manager._transient_attempts.pop(inst_id, None)
        finally:
            # Phase 3: remove temporarily added skills (must run even on crash)
            if has_temp_skills:
                try:
                    async with self.db_factory() as db:
                        task = await db.get(Task, task_id)
                        if task:
                            current = dict(task.enabled_skills or {})
                            for key in msg.command_skills:
                                if key not in original_skills or not original_skills[key]:
                                    current.pop(key, None)
                            task.enabled_skills = current
                            await db.commit()
                except Exception:
                    logger.exception(f"Failed to restore enabled_skills for task {task_id}")

    async def _compact_session(self, task_id: int, session_id: str, db) -> str | None:
        """收集当前 session 的对话摘要，用于上下文压缩后带入新 session。

        取最近的 user_message + assistant message 对，拼成摘要文本。
        保留最后 10 轮对话的要点，更早的只保留 task description。
        """
        try:
            from backend.models.log_entry import LogEntry
            from backend.models.task import Task

            task = await db.get(Task, task_id)
            parts = []

            # 任务原始描述
            if task and task.description:
                parts.append(f"## 任务描述\n{task.description[:2000]}")

            # 最近的对话轮次
            result = await db.execute(
                select(LogEntry)
                .where(
                    LogEntry.task_id == task_id,
                    LogEntry.event_type.in_(["user_message", "message", "result"]),
                )
                .order_by(LogEntry.id.desc())
                .limit(30)
            )
            entries = list(reversed(result.scalars().all()))

            # 提取最近 10 轮 user→assistant 对
            rounds = []
            current_user = None
            for e in entries:
                if e.event_type == "user_message":
                    current_user = (e.content or "")[:500]
                elif e.event_type in ("message", "result") and e.role == "assistant":
                    content = (e.content or "")[:1000]
                    if current_user:
                        rounds.append(f"用户: {current_user}\n助手: {content}")
                        current_user = None

            if rounds:
                # 保留最近 10 轮
                recent = rounds[-10:]
                parts.append("## 最近对话（摘要）\n" + "\n\n".join(recent))

            summary = "\n\n".join(parts)
            if len(summary) > 8000:
                summary = summary[-8000:]
            return summary if summary.strip() else None
        except Exception as e:
            logger.exception("compact session failed for task %d: %s", task_id, e)
            return None
