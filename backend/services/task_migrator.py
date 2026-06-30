"""TaskMigrator — 统一迁移机制（elastic-worker 设计 §10）。

三个场景同一本质："把 task 的执行态从机器 A 搬到机器 B"：
1. 实时切换执行位置（PUT /api/tasks/{id} 改 worker_id）
2. Worker 销毁 = 对其全部 task migrate 回本机
3. 跨机克隆（只搬 session 的子集操作）

搬运原则：先复制后切指针——源机文件不删，任一步失败状态复原可重试。
前提：所有机器 WORKSPACE_DIR 一致（bootstrap 保证），cwd 编码出的 session
路径两边天然对得上，迁过去 --resume 直接续聊。
"""

from __future__ import annotations

import asyncio
import glob
import logging
import os
import shutil
import tempfile

import httpx

from backend.config import settings
from backend.models.project import Project
from backend.models.task import Task
from backend.models.worker import Worker
from backend.services.ssh_executor import SSHExecutor

logger = logging.getLogger(__name__)


class MigrationError(Exception):
    pass


class TaskMigrator:
    def __init__(self, db_factory, relay, broadcaster=None):
        self.db_factory = db_factory
        self.relay = relay
        self.broadcaster = broadcaster
        self._locks: dict[int, asyncio.Lock] = {}

    # ------------------------------------------------------------------
    # 入口
    # ------------------------------------------------------------------

    async def migrate(self, task_id: int, target_worker_id: int | None):
        """把 task 迁到 target（worker_id 或 None=本机）。"""
        lock = self._locks.setdefault(task_id, asyncio.Lock())
        if lock.locked():
            raise MigrationError("该 task 正在迁移中")
        async with lock:
            await self._migrate_locked(task_id, target_worker_id)

    async def _migrate_locked(self, task_id: int, target: int | None):
        async with self.db_factory() as db:
            task = await db.get(Task, task_id)
            if not task:
                raise MigrationError("task 不存在")
            if task.worker_id == target:
                return  # 已在目标位置
            if task.status in ("executing", "migrating"):
                raise MigrationError(f"task 状态 {task.status}，先停止再切换")
            prev_status = task.status
            src_worker_id = task.worker_id

        src = await self._get_worker(src_worker_id) if src_worker_id else None
        dst = await self._get_worker(target) if target else None
        if target and (not dst or dst.status != "ready"):
            raise MigrationError(f"目标 Worker {dst.name if dst else target} 不可用")
        if src_worker_id and (not src or src.status not in ("ready", "destroying")):
            raise MigrationError(
                f"源 Worker {src.name if src else src_worker_id} 不可用（{src.status if src else '不存在'}）——"
                "无法取回执行态。可先启动该 Worker 再切换"
            )

        await self._set_status(task_id, "migrating")
        try:
            # 1. 源是 worker：先把 relay 收不到的字段同步回来（session_id/last_cwd）
            if src is not None:
                await self._sync_task_fields_from_worker(src, task_id)

            async with self.db_factory() as db:
                task = await db.get(Task, task_id)
                session_id = task.session_id
                project_id = task.project_id

            # 2. 工作目录搬运（含 .git + 未提交改动，无过滤全量 rsync）
            local_path = None
            if project_id:
                async with self.db_factory() as db:
                    project = await db.get(Project, project_id)
                local_path = project.local_path if project else None
            if local_path:
                await self._sync_workspace(src, dst, local_path)

            # 3. session 文件搬运（落目标机第一账号 ~/.claude）
            if session_id:
                await self._move_session(src, dst, session_id)

            # 4. 目标是 worker：确保项目记录 + 用同 ID 重建 task
            if dst is not None:
                from backend.main import worker_proxy
                async with self.db_factory() as db:
                    task = await db.get(Task, task_id)
                    worker_project_id = await worker_proxy.ensure_worker_project(dst, task)
                    await self._ensure_worker_task(dst, task, worker_project_id)

            # 5. relay 订阅切换
            if src is not None:
                self.relay.unsubscribe_task(src.id, task_id)
            if dst is not None:
                await self.relay.subscribe_task(dst, task_id)

            # 6. 切指针 + 状态复原
            async with self.db_factory() as db:
                task = await db.get(Task, task_id)
                task.worker_id = target
                task.status = prev_status
                # last_cwd 防护：失败启动会把 os.getcwd() 写进 last_cwd（污染），
                # 且它优先于 target_repo——切回本机时不存在/不在项目内的一律清掉，
                # 让 cwd 解析回落到 target_repo
                if target is None and task.last_cwd:
                    valid = os.path.isdir(task.last_cwd) and (
                        not task.target_repo
                        or task.last_cwd.startswith(task.target_repo)
                    )
                    if not valid:
                        task.last_cwd = None
                await db.commit()
            await self._broadcast_status(task_id, "migrating", prev_status)
            logger.info("task %s migrated: %s -> %s", task_id, src_worker_id, target)
        except Exception:
            # 复制式搬运：源机文件未动，失败无害，状态复原可重试
            await self._set_status(task_id, prev_status, old="migrating")
            raise

    # ------------------------------------------------------------------
    # 子操作
    # ------------------------------------------------------------------

    async def _get_worker(self, worker_id: int) -> Worker | None:
        async with self.db_factory() as db:
            return await db.get(Worker, worker_id)

    def _ssh(self, worker: Worker) -> SSHExecutor:
        return SSHExecutor(
            host=worker.private_ip,
            user=worker.ssh_user,
            key_path=worker.ssh_key_path or settings.worker_ssh_key_path,
        )

    async def _set_status(self, task_id: int, status: str, old: str | None = None):
        async with self.db_factory() as db:
            task = await db.get(Task, task_id)
            if task:
                prev = task.status
                task.status = status
                await db.commit()
        await self._broadcast_status(task_id, old or prev, status)

    async def _broadcast_status(self, task_id: int, old: str, new: str):
        if self.broadcaster:
            await self.broadcaster.broadcast("tasks", {
                "event": "status_change", "task_id": task_id,
                "old_status": old, "new_status": new,
            })

    async def _sync_task_fields_from_worker(self, worker: Worker, task_id: int):
        """worker 广播会 pop session_id、last_cwd 只写 worker DB——迁移前必须拉全。"""
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.get(
                f"http://{worker.private_ip}:{worker.ccm_port}/api/tasks/{task_id}",
                headers={"Authorization": f"Bearer {worker.auth_token}"},
            )
            if r.status_code != 200:
                raise MigrationError(f"从 worker 拉取 task 详情失败: HTTP {r.status_code}")
            wt = r.json()
        async with self.db_factory() as db:
            task = await db.get(Task, task_id)
            for f in ("session_id", "last_cwd", "target_repo", "error_message"):
                if wt.get(f):
                    setattr(task, f, wt[f])
            await db.commit()

    async def _sync_workspace(self, src: Worker | None, dst: Worker | None, local_path: str):
        """项目目录在机器间搬运。worker→worker 经 Manager 两跳。"""
        path = os.path.expanduser(local_path).rstrip("/")
        if src is None and dst is not None:
            if not os.path.isdir(path):
                return  # 本机没有工作目录可推
            await self._ssh(dst).rsync_to(path + "/", path + "/", excludes=[], timeout=1200)
        elif src is not None and dst is None:
            await self._ssh(src).rsync_from(path + "/", path + "/", timeout=1200)
        elif src is not None and dst is not None:
            tmp = tempfile.mkdtemp(prefix="ccm-migrate-")
            try:
                hop = os.path.join(tmp, "ws")
                await self._ssh(src).rsync_from(path + "/", hop + "/", timeout=1200)
                await self._ssh(dst).rsync_to(hop + "/", path + "/", excludes=[], timeout=1200)
            finally:
                shutil.rmtree(tmp, ignore_errors=True)

    # -- session 搬运 ---------------------------------------------------

    @staticmethod
    def _local_session_glob(session_id: str) -> list[str]:
        home = os.path.expanduser("~")
        pats = [
            f"{home}/.claude/projects/*/{session_id}.jsonl",
            f"{home}/.claude-*/projects/*/{session_id}.jsonl",
        ]
        out: list[str] = []
        for p in pats:
            out.extend(glob.glob(p))
        return out

    async def _move_session(self, src: Worker | None, dst: Worker | None, session_id: str):
        """session JSONL：源机定位（任意账号 config_dir）→ 目标机 ~/.claude 同编码路径。"""
        if src is None:
            matches = self._local_session_glob(session_id)
            if not matches:
                logger.warning("session %s 本机未找到，跳过 session 搬运", session_id)
                return
            src_file = matches[0]
            encoded = os.path.basename(os.path.dirname(src_file))
        else:
            ssh = self._ssh(src)
            code, out = await ssh.run(
                f"ls ~/.claude/projects/*/{session_id}.jsonl "
                f"~/.claude-*/projects/*/{session_id}.jsonl 2>/dev/null | head -1"
            )
            remote_file = out.strip().splitlines()[0].strip() if out.strip() else ""
            if not remote_file:
                logger.warning("session %s 在 worker %s 未找到，跳过", session_id, src.id)
                return
            encoded = os.path.basename(os.path.dirname(remote_file))
            tmp = tempfile.mkdtemp(prefix="ccm-sess-")
            src_file = os.path.join(tmp, f"{session_id}.jsonl")
            await ssh.rsync_from(remote_file, src_file, delete=False)

        if dst is None:
            config_dir = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
            target = os.path.join(config_dir, f"projects/{encoded}/{session_id}.jsonl")
            os.makedirs(os.path.dirname(target), exist_ok=True)
            if os.path.abspath(src_file) != os.path.abspath(target):
                shutil.copy2(src_file, target)
        else:
            target = f"/home/{dst.ssh_user}/.claude/projects/{encoded}/{session_id}.jsonl"
            await self._ssh(dst).copy_file(src_file, target)

    # -- 目标 worker 上重建 task ----------------------------------------

    async def _ensure_worker_task(self, dst: Worker, task: Task, worker_project_id: int):
        """worker 上同 ID 建 task（带 session_id/last_cwd 以便 --resume）；
        已存在（之前转发过）则 PUT 更新关键字段。"""
        headers = {"Authorization": f"Bearer {dst.auth_token}"}
        base = f"http://{dst.private_ip}:{dst.ccm_port}/api/tasks"
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.get(f"{base}/{task.id}", headers=headers)
            if r.status_code == 200:
                r = await c.put(f"{base}/{task.id}", headers=headers, json={
                    "project_id": worker_project_id,
                })
                r.raise_for_status()
                return
            payload = {
                "id": task.id,
                "worker_id": None,
                "title": task.title,
                "description": task.description or task.title or "migrated task",
                "project_id": worker_project_id,
                "target_branch": task.target_branch or "main",
                "mode": task.mode,
                "todo_file_path": task.todo_file_path,
                "goal_condition": task.goal_condition,
                "provider": task.provider,
                "model": task.model,
                "effort_level": task.effort_level,
                "enable_workflows": task.enable_workflows,
                "enabled_skills": task.enabled_skills,
                "session_id": task.session_id,
                "last_cwd": task.last_cwd,
            }
            r = await c.post(base, headers=headers, json=payload)
            r.raise_for_status()
            # 关键：新建即 pending，worker Dispatcher 2 秒内就会把任务描述
            # 当新任务执行一遍——必须立刻 cancel 压住。后续 chat 只依赖
            # session_id，cancelled 状态不影响续聊
            r = await c.post(f"{base}/{task.id}/cancel", headers=headers)
            if r.status_code >= 400:
                raise MigrationError(f"压制 worker 侧重建 task 失败: HTTP {r.status_code}")
