"""One-click update & restart pipeline for CCM."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select

from backend.models.task import Task
from backend.services.git_info import git_head_commit
from backend.services.ws_broadcaster import WebSocketBroadcaster

logger = logging.getLogger(__name__)

WS_CHANNEL = "system_update"
MAX_BACKUPS = 5
ACTIVE_TASK_STATUSES = ("in_progress", "executing")
DRY_RUN_CACHE_SECONDS = 30.0
DRY_RUN_ERROR_CACHE_SECONDS = 5.0


@dataclass
class StepInfo:
    name: str
    status: str = "pending"  # pending | running | completed | failed | skipped
    duration_ms: int | None = None
    started_at: str | None = None
    result: dict[str, Any] | None = None
    message: str | None = None


@dataclass
class UpdateState:
    update_id: str = ""
    status: str = "idle"  # idle | running | completed | failed | rolled_back | restarting
    steps: list[StepInfo] = field(default_factory=list)
    old_commit: str = ""
    new_commit: str = ""
    backup_file: str = ""
    started_at: str = ""
    completed_at: str = ""
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "update_id": self.update_id,
            "status": self.status,
            "current_step": next(
                (i + 1 for i, s in enumerate(self.steps) if s.status == "running"),
                len([s for s in self.steps if s.status in ("completed", "skipped", "failed")]),
            ),
            "total_steps": len(self.steps),
            "steps": [
                {
                    "name": s.name,
                    "status": s.status,
                    "duration_ms": s.duration_ms,
                    "started_at": s.started_at,
                    "result": s.result,
                    "message": s.message,
                }
                for s in self.steps
            ],
            "old_commit": self.old_commit,
            "new_commit": self.new_commit,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "error": self.error,
        }


STEP_NAMES = [
    "git_pull",
    "detect_changes",
    "backup_database",
    "uv_sync",
    "refresh_pty",
    "npm_install",
    "frontend_build",
    "stop_service",
    "alembic_upgrade",
    "start_service",
]

STEP_LABELS = {
    "git_pull": "拉取最新代码",
    "detect_changes": "检测变更",
    "backup_database": "备份数据库",
    "uv_sync": "同步 Python 依赖",
    "refresh_pty": "更新 PTY 依赖",
    "npm_install": "安装前端依赖",
    "frontend_build": "构建前端",
    "stop_service": "停止服务",
    "alembic_upgrade": "数据库迁移",
    "start_service": "启动服务",
}

STEP_TIMEOUTS = {
    "git_pull": 60,
    "uv_sync": 300,
    "refresh_pty": 120,
    "npm_install": 120,
    "frontend_build": 300,
}


def _find_tool(name: str) -> str:
    """Find a CLI tool by searching PATH + common install locations."""
    import shutil
    found = shutil.which(name)
    if found:
        return found
    home = Path.home()
    extra_dirs = [
        home / ".local" / "bin",
        home / ".cargo" / "bin",
        Path("/usr/local/bin"),
        Path("/opt/homebrew/bin"),
    ]
    for d in extra_dirs:
        candidate = d / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return name


def _resolve_db_path(project_dir: str) -> Path:
    """Resolve SQLite database path from settings."""
    from backend.config import settings
    url = settings.database_url
    if "sqlite" in url:
        # "sqlite+aiosqlite:///./claude_manager.db" → "./claude_manager.db"
        raw = url.split("///", 1)[-1] if "///" in url else url
        p = Path(raw)
        if not p.is_absolute():
            p = Path(project_dir) / p
        return p.resolve()
    return Path(project_dir) / "claude_manager.db"


class UpdateService:
    def __init__(
        self,
        broadcaster: WebSocketBroadcaster,
        port: int,
        project_dir: str,
        db_factory: Any | None = None,
        dispatcher: Any | None = None,
        running_commit: str | None = None,
    ):
        self.broadcaster = broadcaster
        self.port = port
        self.project_dir = project_dir
        self.db_factory = db_factory
        self.dispatcher = dispatcher
        # Capture the version loaded by this process exactly once.  Reading
        # HEAD later only tells us what is on disk after a manual git pull.
        self._running_commit = (
            running_commit.strip()
            if running_commit is not None
            else git_head_commit(project_dir)
        )
        self.db_path = _resolve_db_path(project_dir)
        self._lock = asyncio.Lock()
        self._start_lock = asyncio.Lock()
        self._dry_run_lock = asyncio.Lock()
        self._dry_run_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._current: UpdateState | None = None
        self._status_file = Path(f"/tmp/ccm-update-status-{port}.json")
        from backend.config import settings
        self._service_name = settings.service_name
        self._service_scope = settings.service_scope
        self._tools = {
            "git": _find_tool("git"),
            "uv": _find_tool("uv"),
            "npm": _find_tool("npm"),
            "bash": _find_tool("bash"),
            "systemctl": _find_tool("systemctl"),
            "systemd-run": _find_tool("systemd-run"),
            "sudo": _find_tool("sudo"),
        }
        logger.info("Resolved tool paths: %s", self._tools)

    def recover_from_status_file(self):
        """Called on startup to recover state from a previous update cycle."""
        if not self._status_file.exists():
            return
        try:
            data = json.loads(self._status_file.read_text())
            status = data.get("status", "")
            if status in ("completed", "rolled_back", "failed"):
                state = UpdateState(
                    update_id=f"recovered_{int(time.time())}",
                    status=status,
                    old_commit=data.get("old_commit", ""),
                    backup_file=data.get("backup_file", ""),
                    completed_at=data.get("timestamp", ""),
                    error=data.get("message", "") if status in ("failed", "rolled_back") else "",
                )
                state.steps = [StepInfo(name=n) for n in STEP_NAMES]
                step_name = data.get("step", "")
                for s in state.steps:
                    if s.name == step_name:
                        s.status = "completed" if status == "completed" else "failed"
                        break
                    s.status = "completed"
                self._current = state
                logger.info("Recovered update status: %s (from %s)", status, step_name)
            elif status in ("restarting", "starting"):
                # "starting": migration script succeeded and was about to (or did)
                # start us — since we are running, treat it as completed.
                state = UpdateState(
                    update_id=f"recovered_{int(time.time())}",
                    status="completed",
                    old_commit=data.get("old_commit", ""),
                    new_commit=data.get("new_commit", ""),
                    completed_at=data.get("timestamp", ""),
                )
                state.steps = [StepInfo(name=n, status="completed") for n in STEP_NAMES]
                self._current = state
                logger.info("Recovered from restart — marking completed")
            elif status in ("stopping", "migrating", "rolling_back"):
                # The external update script died mid-way (e.g. killed together
                # with the service cgroup). We are running again, but the update
                # never finished — surface it as failed so the user can retry.
                step_name = data.get("step", "")
                state = UpdateState(
                    update_id=f"recovered_{int(time.time())}",
                    status="failed",
                    old_commit=data.get("old_commit", ""),
                    backup_file=data.get("backup_file", ""),
                    completed_at=data.get("timestamp", ""),
                    error=f"更新脚本在「{STEP_LABELS.get(step_name, step_name)}」阶段意外中断，请重新触发更新",
                )
                state.steps = [StepInfo(name=n) for n in STEP_NAMES]
                for s in state.steps:
                    if s.name == step_name:
                        s.status = "failed"
                        s.message = state.error
                        break
                    s.status = "completed"
                self._current = state
                logger.warning("Recovered interrupted update (stuck at %s) — marking failed", step_name)
        except Exception:
            logger.exception("Failed to recover update status")

    async def get_status(self) -> dict[str, Any]:
        if self._current:
            return self._current.to_dict()
        return {"status": "idle"}

    async def _get_active_tasks(self) -> list[dict[str, Any]]:
        """Return tasks that would be interrupted by a service restart."""
        if self.db_factory is None:
            return []
        async with self.db_factory() as db:
            rows = (await db.execute(
                select(Task.id, Task.title, Task.status)
                .where(Task.status.in_(ACTIVE_TASK_STATUSES))
                .order_by(Task.id.asc())
            )).all()
        return [
            {"id": row.id, "title": row.title, "status": row.status}
            for row in rows
        ]

    async def _get_blocking_tasks(
        self,
        pending_task_ids: set[int] | None = None,
    ) -> list[dict[str, Any]]:
        """Combine running DB tasks with queued/in-flight resume work."""
        active_tasks = await self._get_active_tasks()
        active_ids = {task["id"] for task in active_tasks}
        if pending_task_ids is None:
            if self.dispatcher is None or not hasattr(
                self.dispatcher, "pending_task_start_ids"
            ):
                pending_task_ids = set()
            else:
                pending_task_ids = await self.dispatcher.pending_task_start_ids()
        queued_ids = set(pending_task_ids) - active_ids
        if not queued_ids:
            return active_tasks

        if self.db_factory is None:
            queued_tasks = [
                {"id": task_id, "title": f"Task {task_id}", "status": "queued_resume"}
                for task_id in sorted(queued_ids)
            ]
        else:
            async with self.db_factory() as db:
                rows = (await db.execute(
                    select(Task.id, Task.title)
                    .where(Task.id.in_(queued_ids))
                    .order_by(Task.id.asc())
                )).all()
            found = {row.id for row in rows}
            queued_tasks = [
                {"id": row.id, "title": row.title, "status": "queued_resume"}
                for row in rows
            ]
            queued_tasks.extend(
                {"id": task_id, "title": f"Task {task_id}", "status": "queued_resume"}
                for task_id in sorted(queued_ids - found)
            )
        return active_tasks + queued_tasks

    @staticmethod
    def _blocker_payload(active_tasks: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "update_blocked": bool(active_tasks),
            "active_task_count": len(active_tasks),
            "active_tasks": active_tasks,
        }

    async def _pause_dispatching(self) -> None:
        if self.dispatcher is not None:
            await self.dispatcher.pause_dispatching()

    def _resume_dispatching(self) -> None:
        if self.dispatcher is not None:
            self.dispatcher.resume_dispatching()

    @asynccontextmanager
    async def _maintenance_shutdown_guard(self):
        if self.dispatcher is not None and hasattr(
            self.dispatcher, "maintenance_shutdown_guard"
        ):
            async with self.dispatcher.maintenance_shutdown_guard() as pending_ids:
                yield pending_ids
        else:
            yield set()

    async def _commit_shutdown_if_idle(self, action) -> list[dict[str, Any]]:
        """Atomically recheck blockers and synchronously schedule shutdown.

        All user-visible broadcasts and grace sleeps must happen before this
        helper. There is intentionally no await between the successful blocker
        query and ``action()`` while task-start admission is held closed.
        """
        async with self._maintenance_shutdown_guard() as pending_ids:
            blockers = await self._get_blocking_tasks(set(pending_ids))
            if blockers:
                return blockers
            if self.dispatcher is not None and hasattr(
                self.dispatcher, "commit_maintenance_shutdown"
            ):
                self.dispatcher.commit_maintenance_shutdown()
            action()
            return []

    async def _resolve_remote(self, branch: str) -> str:
        """Use the branch's configured tracking remote, falling back to origin."""
        result = await self._run_cmd(
            ["git", "config", "--get", f"branch.{branch}.remote"]
        )
        remote = result["stdout"].strip() if result["returncode"] == 0 else ""
        return remote if remote and remote != "." else "origin"

    async def _disk_commit(self) -> str:
        result = await self._run_cmd(["git", "rev-parse", "HEAD"])
        if result["returncode"] == 0:
            return result["stdout"].strip()
        deploy_commit = Path(self.project_dir) / ".deploy_commit"
        try:
            return deploy_commit.read_text().strip()
        except OSError:
            return ""

    async def _needs_restart(self, disk_commit: str | None = None) -> bool:
        """Check whether disk code differs from the version loaded in memory."""
        try:
            current_disk_commit = disk_commit or await self._disk_commit()
            return bool(
                self._running_commit
                and current_disk_commit
                and self._running_commit != current_disk_commit
            )
        except Exception:
            logger.debug("_needs_restart check failed", exc_info=True)
            return False

    async def _deployment_base_commit(self, disk_commit: str) -> str:
        """Include manually pulled changes in deployment analysis and rollback."""
        if not self._running_commit or self._running_commit == disk_commit:
            return disk_commit
        result = await self._run_cmd(
            ["git", "merge-base", "--is-ancestor", self._running_commit, disk_commit]
        )
        return self._running_commit if result["returncode"] == 0 else disk_commit

    async def _cached_version_check(
        self,
        target_branch: str,
        *,
        force: bool = False,
    ) -> dict[str, Any]:
        """Coalesce concurrent fetches and briefly reuse their version result."""
        now = time.monotonic()
        cached = self._dry_run_cache.get(target_branch)
        if not force and cached and cached[0] > now:
            return dict(cached[1])

        async with self._dry_run_lock:
            now = time.monotonic()
            cached = self._dry_run_cache.get(target_branch)
            if not force and cached and cached[0] > now:
                return dict(cached[1])

            result = await self._check_remote_updates(target_branch)
            ttl = DRY_RUN_ERROR_CACHE_SECONDS if result.get("error") else DRY_RUN_CACHE_SECONDS
            expires_at = time.monotonic() + ttl
            self._dry_run_cache = {
                key: value
                for key, value in self._dry_run_cache.items()
                if value[0] > now
            }
            self._dry_run_cache[target_branch] = (expires_at, dict(result))
            return result

    async def dry_run(
        self,
        branch: str | None = None,
        *,
        force: bool = False,
    ) -> dict[str, Any]:
        """Check for available updates without applying them."""
        target_branch = branch or "main"
        version_result = await self._cached_version_check(target_branch, force=force)
        active_tasks = await self._get_blocking_tasks()
        return {**version_result, **self._blocker_payload(active_tasks)}

    async def _check_remote_updates(self, target_branch: str) -> dict[str, Any]:
        """Fetch and compare versions; caller handles caching and task blockers."""
        remote = await self._resolve_remote(target_branch)
        remote_ref = f"{remote}/{target_branch}"
        refspec = f"+refs/heads/{target_branch}:refs/remotes/{remote}/{target_branch}"
        # Local restart detection must not depend on network availability.  A
        # manual pull changes disk HEAD even when the later fetch fails.
        head = await self._disk_commit()
        needs_restart = await self._needs_restart(head)
        result = await self._run_cmd(["git", "fetch", remote, refspec], timeout=60)
        if result["returncode"] != 0:
            return {
                "has_updates": False,
                "needs_restart": needs_restart,
                "manual_update_detected": needs_restart,
                "remote": remote,
                "current_commit": head[:7],
                "running_commit": self._running_commit[:7],
                "error": result["stderr"],
            }

        remote_head = (await self._run_cmd(["git", "rev-parse", remote_ref]))["stdout"].strip()

        if head == remote_head:
            return {
                "has_updates": False,
                "needs_restart": needs_restart,
                "manual_update_detected": needs_restart,
                "remote": remote,
                "current_commit": head[:7],
                "running_commit": self._running_commit[:7],
                "latest_commit": remote_head[:7],
            }

        diff_output = (await self._run_cmd(
            ["git", "log", "--oneline", f"{head}..{remote_head}"]
        ))["stdout"].strip()
        commits = [line for line in diff_output.split("\n") if line.strip()]

        diff_files = (await self._run_cmd(
            ["git", "diff", "--name-only", f"{head}..{remote_head}"]
        ))["stdout"].strip()
        files = [f for f in diff_files.split("\n") if f.strip()]

        migration_files = [f for f in files if f.startswith("alembic/versions/")]
        frontend_files = [f for f in files if f.startswith("frontend/")]
        has_package_changes = "frontend/package.json" in files

        return {
            "has_updates": bool(commits),
            "needs_restart": needs_restart,
            "manual_update_detected": needs_restart,
            "branch": target_branch,
            "remote": remote,
            "commits_behind": len(commits),
            "has_new_migrations": len(migration_files) > 0,
            "migration_count": len(migration_files),
            "has_frontend_changes": len(frontend_files) > 0,
            "has_package_changes": has_package_changes,
            "current_commit": head[:7],
            "running_commit": self._running_commit[:7],
            "latest_commit": remote_head[:7],
            "commit_messages": [c.split(" ", 1)[-1] if " " in c else c for c in commits[:20]],
        }

    async def start_update(
        self,
        skip_frontend_build: bool = False,
        force: bool = False,
        branch: str | None = None,
    ) -> dict[str, Any]:
        async with self._start_lock:
            if self._lock.locked() or (
                self._current and self._current.status in ("running", "restarting")
            ):
                if self._current:
                    return {"error": "更新正在进行中", "update_id": self._current.update_id}
                return {"error": "更新正在进行中"}

            # Freeze new claims before checking the DB.  Existing tasks are
            # never cancelled; callers retry after they finish.
            await self._pause_dispatching()
            try:
                active_tasks = await self._get_blocking_tasks()
            except Exception as exc:
                self._resume_dispatching()
                logger.exception("Unable to verify active tasks before update")
                return {"error": f"无法确认当前任务状态，已取消更新: {exc}"}
            if active_tasks:
                self._resume_dispatching()
                return {
                    "error": f"当前有 {len(active_tasks)} 个任务正在运行，请等待任务完成后再更新",
                    **self._blocker_payload(active_tasks),
                }

            update_id = f"upd_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
            state = UpdateState(
                update_id=update_id,
                status="running",
                started_at=datetime.now(timezone.utc).isoformat(),
                steps=[StepInfo(name=n) for n in STEP_NAMES],
            )
            self._current = state

            asyncio.create_task(
                self._run_pipeline(state, skip_frontend_build=skip_frontend_build, force=force, branch=branch)
            )
            return {"update_id": update_id, "status": "started"}

    async def rollback(self) -> dict[str, Any]:
        """Manual rollback to previous version."""
        if not self._current or not self._current.old_commit:
            return {"error": "没有可回滚的更新记录"}
        if self._lock.locked():
            return {"error": "有操作正在进行中"}

        await self._pause_dispatching()
        try:
            active_tasks = await self._get_blocking_tasks()
        except Exception as exc:
            self._resume_dispatching()
            logger.exception("Unable to verify active tasks before rollback")
            return {"error": f"无法确认当前任务状态，已取消回滚: {exc}"}
        if active_tasks:
            self._resume_dispatching()
            return {
                "error": f"当前有 {len(active_tasks)} 个任务正在运行，请等待任务完成后再回滚",
                **self._blocker_payload(active_tasks),
            }

        old_commit = self._current.old_commit
        backup_file = self._current.backup_file

        try:
            async with self._lock:
                await self._broadcast("step_update", step="rollback", status="running", message="正在回滚...")

                # Never restore the SQLite file while this process still holds open
                # connections — a later write/checkpoint through the live connection
                # corrupts the restored DB (2026-07-16 test-env rollback incident).
                # The script stops the service (systemctl or kill, per deployment)
                # BEFORE touching the DB, then resets code and starts it again.
                await self._broadcast("restarting", message="服务即将停止进行回滚，请等待自动重连...")
                await asyncio.sleep(1)

                def spawn_rollback() -> None:
                    self._current.status = "restarting"
                    self._write_status_file(
                        "restarting", "正在停服回滚...", old_commit=old_commit
                    )
                    self._spawn_update_script("rollback", old_commit, backup_file or "")

                blockers = await self._commit_shutdown_if_idle(spawn_rollback)
                if blockers:
                    self._resume_dispatching()
                    return {
                        "error": (
                            f"回滚期间出现了 {len(blockers)} 个待处理任务，"
                            "已取消停服；请等待任务完成后重试"
                        ),
                        **self._blocker_payload(blockers),
                    }
                return {"status": "rolling_back", "old_commit": old_commit}
        except Exception:
            self._resume_dispatching()
            raise

    # ---- Pipeline implementation ----

    async def _run_pipeline(
        self,
        state: UpdateState,
        skip_frontend_build: bool = False,
        force: bool = False,
        branch: str | None = None,
    ):
        async with self._lock:
            try:
                await self._pipeline_inner(state, skip_frontend_build, force, branch=branch)
            except Exception as e:
                state.status = "failed"
                state.error = str(e)
                state.completed_at = datetime.now(timezone.utc).isoformat()
                await self._broadcast("update_failed", message=str(e))
                logger.exception("Update pipeline failed")
            finally:
                if state.status != "restarting":
                    self._resume_dispatching()

    async def _pipeline_inner(
        self,
        state: UpdateState,
        skip_frontend_build: bool,
        force: bool,
        branch: str | None = None,
    ):
        target_branch = branch or "main"
        remote = await self._resolve_remote(target_branch)
        has_new_migrations = False
        has_frontend_changes = False
        has_package_changes = False

        # Step 1: check clean → git pull
        step = state.steps[0]
        await self._start_step(step)
        disk_commit = await self._disk_commit()
        state.old_commit = await self._deployment_base_commit(disk_commit)

        # Auto-stash local changes before pulling
        status_result = await self._run_cmd(["git", "status", "--porcelain"])
        dirty_files = [l for l in status_result["stdout"].strip().split("\n") if l.strip() and not l.startswith("??")]
        stashed = False
        if dirty_files:
            stash_result = await self._run_cmd(["git", "stash", "push", "-m", "ccm-auto-stash-before-update"], step=step)
            if stash_result["returncode"] != 0:
                await self._fail_step(step, state, f"git stash 失败: {stash_result['stderr']}")
                return
            stashed = True
            await self._broadcast("log_line", step="git_pull", log=f"已暂存 {len(dirty_files)} 个本地改动", status="running")

        # Checkout target branch before pulling (keeps main clean when
        # updating to a feature branch for testing)
        current_branch = (await self._run_cmd(["git", "rev-parse", "--abbrev-ref", "HEAD"]))["stdout"].strip()
        if current_branch != target_branch:
            refspec = f"+refs/heads/{target_branch}:refs/remotes/{remote}/{target_branch}"
            fetch_result = await self._run_cmd(["git", "fetch", remote, refspec], timeout=60, step=step)
            if fetch_result["returncode"] != 0:
                if stashed:
                    await self._run_cmd(["git", "stash", "pop"])
                await self._fail_step(step, state, f"git fetch 失败: {fetch_result['stderr']}")
                return
            # Create or reset local branch to match remote
            checkout_result = await self._run_cmd(
                ["git", "checkout", "-B", target_branch, f"{remote}/{target_branch}"], step=step
            )
            if checkout_result["returncode"] != 0:
                if stashed:
                    await self._run_cmd(["git", "stash", "pop"])
                await self._fail_step(step, state, f"git checkout 失败: {checkout_result['stderr']}")
                return
            await self._broadcast("log_line", step="git_pull", log=f"已切换到分支 {target_branch}", status="running")

        result = await self._run_cmd(["git", "pull", "--rebase", remote, target_branch], timeout=60, step=step)
        if result["returncode"] != 0:
            if stashed:
                await self._run_cmd(["git", "stash", "pop"])
            await self._fail_step(step, state, f"git pull 失败: {result['stderr']}")
            return

        # Restore stashed changes
        if stashed:
            pop_result = await self._run_cmd(["git", "stash", "pop"])
            if pop_result["returncode"] != 0:
                await self._broadcast("log_line", step="git_pull", log="本地改动与更新冲突，已保留在 git stash 中，请手动处理", status="running")
            else:
                await self._broadcast("log_line", step="git_pull", log="本地改动已恢复", status="running")

        state.new_commit = (await self._run_cmd(["git", "rev-parse", "HEAD"]))["stdout"].strip()

        if state.old_commit == state.new_commit and not force:
            needs_restart = await self._needs_restart()
            if needs_restart:
                step.message = "代码已是最新，但服务需要重启"
                await self._complete_step(step)
                for s in state.steps[1:9]:
                    s.status = "skipped"
                    s.message = "无需更新"
                    await self._broadcast_step(s)
                await self._fast_restart_path(state)
                return
            step.message = "已是最新版本"
            await self._complete_step(step)
            state.status = "completed"
            state.completed_at = datetime.now(timezone.utc).isoformat()
            for s in state.steps[1:]:
                s.status = "skipped"
            await self._broadcast("update_complete", message="已是最新版本，无需更新")
            return

        await self._complete_step(step)

        # Step 2: detect changes
        step = state.steps[1]
        await self._start_step(step)

        diff_result = await self._run_cmd(
            ["git", "diff", "--name-only", f"{state.old_commit}..{state.new_commit}"]
        )
        changed_files = [f for f in diff_result["stdout"].strip().split("\n") if f.strip()]

        migration_files = [f for f in changed_files if f.startswith("alembic/versions/")]
        has_new_migrations = len(migration_files) > 0
        frontend_files = [f for f in changed_files if f.startswith("frontend/")]
        has_frontend_changes = len(frontend_files) > 0
        has_package_changes = "frontend/package.json" in changed_files

        step.result = {
            "has_new_migrations": has_new_migrations,
            "migration_count": len(migration_files),
            "has_frontend_changes": has_frontend_changes,
            "has_package_changes": has_package_changes,
            "total_files_changed": len(changed_files),
        }
        await self._complete_step(step)

        # Step 3: backup database
        step = state.steps[2]
        await self._start_step(step)
        try:
            state.backup_file = await self._backup_database()
            await self._complete_step(step)
        except Exception as e:
            await self._fail_step(step, state, f"备份数据库失败: {e}")
            return

        # Step 4: uv sync
        step = state.steps[3]
        await self._start_step(step)
        result = await self._run_cmd(["uv", "sync"], timeout=300, step=step)
        if result["returncode"] != 0:
            await self._fail_step(step, state, f"uv sync 失败: {result['stderr']}")
            return
        await self._complete_step(step)

        # Step 5: refresh_pty.sh
        step = state.steps[4]
        await self._start_step(step)
        pty_script = Path(self.project_dir) / "scripts" / "refresh_pty.sh"
        if pty_script.exists():
            result = await self._run_cmd(
                ["bash", str(pty_script)], timeout=120, step=step
            )
            if result["returncode"] != 0:
                await self._fail_step(step, state, f"refresh_pty.sh 失败: {result['stderr']}")
                return
            await self._complete_step(step)
        else:
            step.status = "skipped"
            step.message = "脚本不存在"
            await self._broadcast_step(step)

        # Step 6: npm install
        step = state.steps[5]
        if has_package_changes:
            await self._start_step(step)
            result = await self._run_cmd(
                ["npm", "install"],
                timeout=120,
                step=step,
                cwd=str(Path(self.project_dir) / "frontend"),
            )
            if result["returncode"] != 0:
                await self._fail_step(step, state, f"npm install 失败: {result['stderr']}")
                return
            await self._complete_step(step)
        else:
            step.status = "skipped"
            step.message = "package.json 未变更"
            await self._broadcast_step(step)

        # Step 7: frontend build
        step = state.steps[6]
        if skip_frontend_build or not has_frontend_changes:
            step.status = "skipped"
            step.message = "跳过" if skip_frontend_build else "前端无变更"
            await self._broadcast_step(step)
        else:
            await self._start_step(step)
            result = await self._run_cmd(
                ["npm", "run", "build"],
                timeout=300,
                step=step,
                cwd=str(Path(self.project_dir) / "frontend"),
            )
            if result["returncode"] != 0:
                await self._fail_step(step, state, f"前端构建失败: {result['stderr']}")
                return
            await self._complete_step(step)

        # Steps 8-10: migration path vs fast path
        active_tasks = await self._get_blocking_tasks()
        if active_tasks:
            step = state.steps[7]
            await self._start_step(step)
            await self._fail_step(
                step,
                state,
                f"更新期间启动了 {len(active_tasks)} 个任务，已取消重启；请等待任务完成后重试",
            )
            return
        if has_new_migrations:
            await self._migration_path(state)
        else:
            await self._fast_restart_path(state)

    async def _migration_path(self, state: UpdateState) -> bool:
        """Has new migrations: launch external script that survives our own stop (steps 8-10)."""
        step8 = state.steps[7]
        step9 = state.steps[8]
        step10 = state.steps[9]

        step8.status = "running"
        step8.started_at = datetime.now(timezone.utc).isoformat()
        step9.message = "由外部脚本执行"
        step10.message = "由外部脚本执行"

        await self._broadcast("step_update", step="stop_service", status="running",
                              message="即将停服进行数据库迁移...")
        await self._broadcast("restarting", message="服务即将停止进行迁移，请等待自动重连...")
        await asyncio.sleep(1)

        def spawn_migration() -> None:
            state.status = "restarting"
            self._spawn_update_script("migrate", state.old_commit, state.backup_file)

        blockers = await self._commit_shutdown_if_idle(spawn_migration)
        if blockers:
            await self._fail_step(
                step8,
                state,
                f"停服前出现了 {len(blockers)} 个待处理任务，已取消重启；请等待任务完成后重试",
            )
            return False
        return True

    def _spawn_update_script(self, mode: str, old_commit: str, backup_file: str):
        """Launch update_migrate.sh so it survives this service being stopped."""
        script = Path(self.project_dir) / "scripts" / "update_migrate.sh"
        log_file = f"/tmp/ccm-update-migrate-{self.port}.log"

        env = os.environ.copy()
        for tool_path in self._tools.values():
            tool_dir = str(Path(tool_path).parent)
            if tool_dir not in env.get("PATH", ""):
                env["PATH"] = tool_dir + ":" + env.get("PATH", "")

        scope = self._systemd_scope()
        managed = scope is not None
        script_argv = [
            self._tools["bash"], str(script),
            self.project_dir,
            old_commit,
            backup_file,
            str(self.port),
            str(self.db_path),
            # "-" tells the script to stop/start via kill/respawn instead of
            # systemctl (bare-uvicorn deployments)
            self._service_name if managed else "-",
            mode,
            str(os.getpid()),
            sys.executable,
            scope or "auto",
        ]

        if managed:
            # start_new_session only escapes the process group, NOT the service's
            # cgroup — `systemctl stop` kills the whole cgroup including the script,
            # leaving the service stopped with nobody left to start it again.
            # systemd-run puts the script in its own transient unit (own cgroup).
            subprocess.Popen(
                self._systemd_run_cmd(scope) + [
                    "--collect",
                    f"--unit=ccm-update-{self.port}",
                    # transient units do NOT inherit our cwd — without this the
                    # script's git/uv/alembic would run from systemd's default dir
                    f"--working-directory={self.project_dir}",
                    f"--setenv=PATH={env['PATH']}",
                    f"--property=StandardOutput=append:{log_file}",
                    "--property=StandardError=inherit",
                ] + script_argv,
            )
        else:
            subprocess.Popen(
                script_argv,
                stdout=open(log_file, "a"),
                stderr=subprocess.STDOUT,
                start_new_session=True,
                env=env,
            )

    async def _fast_restart_path(self, state: UpdateState) -> bool:
        """No migration: skip steps 8-9, do nohup restart for step 10."""
        state.steps[7].status = "skipped"
        state.steps[7].message = "无新迁移"
        state.steps[8].status = "skipped"
        state.steps[8].message = "无新迁移"
        await self._broadcast_step(state.steps[7])
        await self._broadcast_step(state.steps[8])

        step10 = state.steps[9]
        step10.status = "running"
        step10.started_at = datetime.now(timezone.utc).isoformat()

        await self._broadcast("restarting", message="服务即将重启，请等待自动重连...")
        await asyncio.sleep(1)

        def restart_service() -> None:
            state.status = "restarting"
            self._write_status_file(
                "restarting", "正在重启服务...",
                old_commit=state.old_commit,
                new_commit=state.new_commit,
                backup_file=state.backup_file,
            )
            self._restart_service()

        blockers = await self._commit_shutdown_if_idle(restart_service)
        if blockers:
            await self._fail_step(
                step10,
                state,
                f"重启前出现了 {len(blockers)} 个待处理任务，已取消重启；请等待任务完成后重试",
            )
            return False
        return True

    # ---- Helpers ----

    def _cgroup_text(self) -> str:
        return Path("/proc/self/cgroup").read_text()

    def _normalized_service_name(self) -> str:
        name = self._service_name
        if not name.endswith(".service"):
            name += ".service"
        return name

    def _systemd_scope(self) -> str | None:
        """Return user/system only if THIS process is in its service cgroup.

        `systemctl is-active` is not enough: a manually launched uvicorn can
        coexist with an active (but port-less) systemd unit — it would then
        believe it is systemd-managed and stop/start the *other* instance
        (2026-07-16 test-env orphan incident). /proc/self/cgroup answers for
        this very process; on non-Linux it raises → False → fallback path.
        """
        try:
            text = self._cgroup_text()
        except Exception:
            return None
        if f"/{self._normalized_service_name()}" not in text:
            return None
        configured = (self._service_scope or "auto").strip().lower()
        if configured in {"user", "system"}:
            return configured
        if "/system.slice/" in text:
            return "system"
        if "/user.slice/" in text:
            return "user"
        return "user"

    def _is_managed_by_systemd(self) -> bool:
        return self._systemd_scope() is not None

    def _systemctl_cmd(self, scope: str) -> list[str]:
        if scope == "system":
            return [self._tools["sudo"], "-n", self._tools["systemctl"]]
        return [self._tools["systemctl"], "--user"]

    def _systemd_run_cmd(self, scope: str | None) -> list[str]:
        if scope == "system":
            return [self._tools["sudo"], "-n", self._tools["systemd-run"]]
        return [self._tools["systemd-run"], "--user"]

    def _restart_service(self):
        """Restart via systemd if managed, otherwise re-exec the process."""
        scope = self._systemd_scope()
        if scope:
            cmd = self._systemctl_cmd(scope) + ["restart", self._service_name]
            subprocess.Popen(
                [self._tools["bash"], "-c", "sleep 2 && exec \"$@\"", "ccm-restart", *cmd],
                stdout=open(f"/tmp/ccm-restart-{self.port}.log", "w"),
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        else:
            # Fallback: hardcoded uvicorn args — flags the user originally
            # passed (--reload, --workers, etc.) are not preserved.
            uvicorn_cmd = (
                f"sleep 2 && kill {os.getpid()}; sleep 1; "
                f"cd {self.project_dir} && "
                f"{sys.executable} -m uvicorn backend.main:app "
                f"--host 0.0.0.0 --port {self.port}"
            )
            subprocess.Popen(
                [self._tools["bash"], "-c", uvicorn_cmd],
                stdout=open(f"/tmp/ccm-restart-{self.port}.log", "w"),
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )

    async def _backup_database(self) -> str:
        import sqlite3 as _sqlite3

        db_path = self.db_path
        if not db_path.exists():
            raise FileNotFoundError(f"数据库文件不存在: {db_path}")

        backup_dir = Path(self.project_dir) / "backups"
        backup_dir.mkdir(exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = backup_dir / f"claude_manager.db.bak.{timestamp}"

        def do_backup():
            src = _sqlite3.connect(str(db_path))
            dst = _sqlite3.connect(str(backup_path))
            try:
                src.backup(dst)
            finally:
                dst.close()
                src.close()

        await asyncio.get_event_loop().run_in_executor(None, do_backup)
        self._cleanup_old_backups(backup_dir)
        return str(backup_path)

    def _cleanup_old_backups(self, backup_dir: Path):
        backups = sorted(backup_dir.glob("claude_manager.db.bak.*"), key=lambda p: p.stat().st_mtime)
        while len(backups) > MAX_BACKUPS:
            old = backups.pop(0)
            old.unlink()
            logger.info("Removed old backup: %s", old.name)

    def _resolve_cmd(self, cmd: list[str]) -> list[str]:
        """Replace the first element with its resolved absolute path."""
        if cmd and cmd[0] in self._tools:
            return [self._tools[cmd[0]]] + cmd[1:]
        return cmd

    async def _run_cmd(
        self,
        cmd: list[str],
        timeout: int = 60,
        step: StepInfo | None = None,
        cwd: str | None = None,
    ) -> dict[str, Any]:
        cmd = self._resolve_cmd(cmd)
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd or self.project_dir,
            )

            async def read_stream(stream, is_stderr=False):
                lines = []
                while True:
                    line = await stream.readline()
                    if not line:
                        break
                    text = line.decode("utf-8", errors="replace").rstrip()
                    lines.append(text)
                    if step:
                        await self._broadcast(
                            "log_line",
                            step=step.name,
                            log=text,
                            status="running",
                        )
                return "\n".join(lines)

            try:
                stdout_task = asyncio.create_task(read_stream(proc.stdout))
                stderr_task = asyncio.create_task(read_stream(proc.stderr, True))
                await asyncio.wait_for(proc.wait(), timeout=timeout)
                stdout = await stdout_task
                stderr = await stderr_task
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                return {"returncode": -1, "stdout": "", "stderr": f"命令超时 ({timeout}s)"}

            return {
                "returncode": proc.returncode,
                "stdout": stdout,
                "stderr": stderr,
            }
        except FileNotFoundError:
            return {"returncode": -1, "stdout": "", "stderr": f"命令不存在: {cmd[0]}"}

    async def _start_step(self, step: StepInfo):
        step.status = "running"
        step.started_at = datetime.now(timezone.utc).isoformat()
        label = STEP_LABELS.get(step.name, step.name)
        await self._broadcast("step_update", step=step.name, status="running", message=f"正在{label}...")

    async def _complete_step(self, step: StepInfo):
        step.status = "completed"
        if step.started_at:
            started = datetime.fromisoformat(step.started_at)
            step.duration_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
        await self._broadcast_step(step)

    async def _fail_step(self, step: StepInfo, state: UpdateState, message: str):
        step.status = "failed"
        step.message = message
        if step.started_at:
            started = datetime.fromisoformat(step.started_at)
            step.duration_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
        state.status = "failed"
        state.error = message
        state.completed_at = datetime.now(timezone.utc).isoformat()
        await self._broadcast("update_failed", step=step.name, message=message)

    async def _broadcast_step(self, step: StepInfo):
        await self._broadcast(
            "step_update",
            step=step.name,
            status=step.status,
            message=step.message or "",
            duration_ms=step.duration_ms,
            result=step.result,
        )

    async def _broadcast(self, event: str, **kwargs):
        data = {"event": event}
        if self._current:
            data["update_id"] = self._current.update_id
        data.update(kwargs)
        try:
            await self.broadcaster.broadcast(WS_CHANNEL, data)
        except Exception:
            logger.debug("broadcast failed", exc_info=True)

    def _write_status_file(self, status: str, message: str, **extra):
        data = {
            "status": status,
            "message": message,
            "port": self.port,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        data.update(extra)
        try:
            self._status_file.write_text(json.dumps(data, ensure_ascii=False))
        except Exception:
            logger.exception("Failed to write status file")
