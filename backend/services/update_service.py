"""One-click update & restart pipeline for CCM."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.services.ws_broadcaster import WebSocketBroadcaster

logger = logging.getLogger(__name__)

WS_CHANNEL = "system_update"
MAX_BACKUPS = 5


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
    def __init__(self, broadcaster: WebSocketBroadcaster, port: int, project_dir: str):
        self.broadcaster = broadcaster
        self.port = port
        self.project_dir = project_dir
        self.db_path = _resolve_db_path(project_dir)
        self._lock = asyncio.Lock()
        self._current: UpdateState | None = None
        self._status_file = Path(f"/tmp/ccm-update-status-{port}.json")
        self._tools = {
            "git": _find_tool("git"),
            "uv": _find_tool("uv"),
            "npm": _find_tool("npm"),
            "bash": _find_tool("bash"),
            "systemctl": _find_tool("systemctl"),
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
            elif status == "restarting":
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
        except Exception:
            logger.exception("Failed to recover update status")

    async def get_status(self) -> dict[str, Any]:
        if self._current:
            return self._current.to_dict()
        return {"status": "idle"}

    async def dry_run(self) -> dict[str, Any]:
        """Check for available updates without applying them."""
        result = await self._run_cmd(["git", "fetch", "origin", "main"], timeout=60)
        if result["returncode"] != 0:
            return {"has_updates": False, "error": result["stderr"]}

        head = (await self._run_cmd(["git", "rev-parse", "HEAD"]))["stdout"].strip()
        origin = (await self._run_cmd(["git", "rev-parse", "origin/main"]))["stdout"].strip()

        if head == origin:
            return {
                "has_updates": False,
                "current_commit": head[:7],
                "latest_commit": origin[:7],
            }

        diff_output = (await self._run_cmd(
            ["git", "log", "--oneline", f"{head}..{origin}"]
        ))["stdout"].strip()
        commits = [line for line in diff_output.split("\n") if line.strip()]

        diff_files = (await self._run_cmd(
            ["git", "diff", "--name-only", f"{head}..{origin}"]
        ))["stdout"].strip()
        files = [f for f in diff_files.split("\n") if f.strip()]

        migration_files = [f for f in files if f.startswith("alembic/versions/")]
        frontend_files = [f for f in files if f.startswith("frontend/")]
        has_package_changes = "frontend/package.json" in files

        return {
            "has_updates": True,
            "commits_behind": len(commits),
            "has_new_migrations": len(migration_files) > 0,
            "migration_count": len(migration_files),
            "has_frontend_changes": len(frontend_files) > 0,
            "has_package_changes": has_package_changes,
            "current_commit": head[:7],
            "latest_commit": origin[:7],
            "commit_messages": [c.split(" ", 1)[-1] if " " in c else c for c in commits[:20]],
        }

    async def start_update(
        self,
        skip_frontend_build: bool = False,
        force: bool = False,
    ) -> dict[str, Any]:
        if self._lock.locked():
            if self._current:
                return {"error": "更新正在进行中", "update_id": self._current.update_id}
            return {"error": "更新正在进行中"}

        update_id = f"upd_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
        state = UpdateState(
            update_id=update_id,
            status="running",
            started_at=datetime.now(timezone.utc).isoformat(),
            steps=[StepInfo(name=n) for n in STEP_NAMES],
        )
        self._current = state

        asyncio.create_task(
            self._run_pipeline(state, skip_frontend_build=skip_frontend_build, force=force)
        )
        return {"update_id": update_id, "status": "started"}

    async def rollback(self) -> dict[str, Any]:
        """Manual rollback to previous version."""
        if not self._current or not self._current.old_commit:
            return {"error": "没有可回滚的更新记录"}
        if self._lock.locked():
            return {"error": "有操作正在进行中"}

        old_commit = self._current.old_commit
        backup_file = self._current.backup_file

        async with self._lock:
            await self._broadcast("step_update", step="rollback", status="running", message="正在回滚...")

            if backup_file and Path(backup_file).exists():
                db_path = self.db_path
                for ext in ("-wal", "-shm"):
                    p = db_path.with_suffix(db_path.suffix + ext)
                    if p.exists():
                        p.unlink()
                shutil.copy2(backup_file, db_path)

            await self._run_cmd(["git", "reset", "--hard", old_commit])
            await self._run_cmd(["uv", "sync"], timeout=300)

            self._write_status_file("restarting", "回滚完成，正在重启...", old_commit=old_commit)
            await self._broadcast("restarting", message="服务即将重启...")
            await asyncio.sleep(1)

            systemctl = self._tools["systemctl"]
            subprocess.Popen(
                [self._tools["bash"], "-c", f"sleep 2 && {systemctl} --user restart ccm.service"],
                stdout=open(f"/tmp/ccm-restart-{self.port}.log", "w"),
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            return {"status": "rolling_back", "old_commit": old_commit}

    # ---- Pipeline implementation ----

    async def _run_pipeline(
        self,
        state: UpdateState,
        skip_frontend_build: bool = False,
        force: bool = False,
    ):
        async with self._lock:
            try:
                await self._pipeline_inner(state, skip_frontend_build, force)
            except Exception as e:
                state.status = "failed"
                state.error = str(e)
                state.completed_at = datetime.now(timezone.utc).isoformat()
                await self._broadcast("update_failed", message=str(e))
                logger.exception("Update pipeline failed")

    async def _pipeline_inner(
        self,
        state: UpdateState,
        skip_frontend_build: bool,
        force: bool,
    ):
        has_new_migrations = False
        has_frontend_changes = False
        has_package_changes = False

        # Step 1: check clean → git pull
        step = state.steps[0]
        await self._start_step(step)
        state.old_commit = (await self._run_cmd(["git", "rev-parse", "HEAD"]))["stdout"].strip()

        # Reject if working directory has uncommitted changes
        status_result = await self._run_cmd(["git", "status", "--porcelain"])
        dirty_files = [l for l in status_result["stdout"].strip().split("\n") if l.strip()]
        if dirty_files:
            await self._fail_step(
                step, state,
                f"工作目录有未提交的改动（{len(dirty_files)} 个文件），请先提交或清理后再更新"
            )
            return

        result = await self._run_cmd(["git", "pull", "--rebase", "origin", "main"], timeout=60, step=step)
        if result["returncode"] != 0:
            await self._fail_step(step, state, f"git pull 失败: {result['stderr']}")
            return

        state.new_commit = (await self._run_cmd(["git", "rev-parse", "HEAD"]))["stdout"].strip()

        if state.old_commit == state.new_commit and not force:
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
        if has_new_migrations:
            await self._migration_path(state)
        else:
            await self._fast_restart_path(state)

    async def _migration_path(self, state: UpdateState):
        """Has new migrations: launch external script via nohup (steps 8-10)."""
        step8 = state.steps[7]
        step9 = state.steps[8]
        step10 = state.steps[9]

        step8.status = "running"
        step8.started_at = datetime.now(timezone.utc).isoformat()
        step9.message = "由外部脚本执行"
        step10.message = "由外部脚本执行"

        await self._broadcast("step_update", step="stop_service", status="running",
                              message="即将停服进行数据库迁移...")
        await asyncio.sleep(1)

        script = Path(self.project_dir) / "scripts" / "update_migrate.sh"
        log_file = f"/tmp/ccm-update-migrate-{self.port}.log"

        env = os.environ.copy()
        for tool_path in self._tools.values():
            tool_dir = str(Path(tool_path).parent)
            if tool_dir not in env.get("PATH", ""):
                env["PATH"] = tool_dir + ":" + env.get("PATH", "")

        subprocess.Popen(
            [
                self._tools["bash"], str(script),
                self.project_dir,
                state.old_commit,
                state.backup_file,
                str(self.port),
                str(self.db_path),
            ],
            stdout=open(log_file, "a"),
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env=env,
        )

        state.status = "restarting"
        await self._broadcast("restarting", message="服务即将停止进行迁移，请等待自动重连...")

    async def _fast_restart_path(self, state: UpdateState):
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

        self._write_status_file(
            "restarting", "正在重启服务...",
            old_commit=state.old_commit,
            new_commit=state.new_commit,
            backup_file=state.backup_file,
        )

        await self._broadcast("restarting", message="服务即将重启，请等待自动重连...")
        await asyncio.sleep(1)

        systemctl = self._tools["systemctl"]
        subprocess.Popen(
            [self._tools["bash"], "-c", f"sleep 2 && {systemctl} --user restart ccm.service"],
            stdout=open(f"/tmp/ccm-restart-{self.port}.log", "w"),
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    # ---- Helpers ----

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
