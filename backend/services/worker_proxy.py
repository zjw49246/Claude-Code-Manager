"""Manager→Worker 任务转发与操作代理（elastic-worker 设计 §5.3/§6.3/§6.4/§8）。

- forward_task_to_worker：确保 worker 有项目 → 先订阅 relay → 用 Manager 分配的
  同一 task ID 在 worker 上创建 task（ID 全局统一，见设计 §2）
- proxy_to_worker：通用操作代理（stop/cancel/retry/plan/monitor），转发前确保
  relay 已订阅（幂等；retry 场景 Manager 重启后 relay 未订阅，不补订阅则全丢）
"""

from __future__ import annotations

import asyncio
import logging

import httpx
from fastapi import HTTPException

from backend.models.project import Project
from backend.models.task import Task
from backend.models.worker import Worker

logger = logging.getLogger(__name__)

# (worker_id, manager_project_id) -> Lock，防并发 task 重复建项目
_project_locks: dict[tuple[int, int], asyncio.Lock] = {}


class WorkerProxy:
    def __init__(self, db_factory, relay):
        self.db_factory = db_factory
        self.relay = relay

    @staticmethod
    def _api(worker: Worker, path: str) -> str:
        return f"http://{worker.private_ip}:{worker.ccm_port}{path}"

    @staticmethod
    def _headers(worker: Worker) -> dict:
        return {"Authorization": f"Bearer {worker.auth_token}"}

    async def get_worker(self, worker_id: int) -> Worker | None:
        async with self.db_factory() as db:
            return await db.get(Worker, worker_id)

    async def require_ready_worker(self, worker_id: int) -> Worker:
        worker = await self.get_worker(worker_id)
        if not worker:
            raise HTTPException(404, f"Worker {worker_id} 不存在")
        if worker.status != "ready":
            raise HTTPException(
                503,
                f"Worker {worker.name} 当前状态 {worker.status}，无法执行操作。"
                "请等待 Worker 恢复或将 task 切回本机执行。",
            )
        return worker

    # ------------------------------------------------------------------
    # 项目映射（设计 §8）
    # ------------------------------------------------------------------

    async def ensure_worker_project(self, worker: Worker, task: Task) -> int:
        """确保 worker 上有 task 对应的项目，返回 worker 侧 project_id。

        Phase 2 仅支持有 git remote 的项目（worker 自己 clone）；
        纯本地项目走 Phase 3 的播种方案，这里直接报错。
        """
        if not task.project_id:
            raise RuntimeError("worker task 必须关联项目（需要 git 信息）")

        key = (worker.id, task.project_id)
        lock = _project_locks.setdefault(key, asyncio.Lock())
        async with lock:
            async with self.db_factory() as db:
                w = await db.get(Worker, worker.id)
                mapping = dict(w.project_mapping or {})
            if str(task.project_id) in mapping:
                return mapping[str(task.project_id)]

            async with self.db_factory() as db:
                project = await db.get(Project, task.project_id)
            if not project:
                raise RuntimeError(f"项目 {task.project_id} 不存在")
            if not project.git_url:
                # 纯本地项目：先把整个项目目录（含 .git 和未提交改动）rsync 到
                # worker 同路径，worker 的 _init_local_repo 见 .git 存在即跳过 init
                import os as _os
                from backend.config import settings as _settings
                from backend.services.ssh_executor import SSHExecutor as _SSH
                path = _os.path.expanduser(project.local_path).rstrip("/")
                if not _os.path.isdir(path):
                    raise RuntimeError(f"项目目录不存在: {path}")
                ssh = _SSH(
                    host=worker.private_ip, user=worker.ssh_user,
                    key_path=worker.ssh_key_path or _settings.worker_ssh_key_path,
                )
                await ssh.rsync_to(path + "/", path + "/", excludes=[], timeout=1200)

            async with httpx.AsyncClient(timeout=30) as c:
                # 同名项目可能已存在（之前转发过/手工建过）
                r = await c.get(self._api(worker, "/api/projects"), headers=self._headers(worker))
                r.raise_for_status()
                items = r.json()
                if isinstance(items, dict):
                    items = items.get("projects", [])
                remote = next((p for p in items if p.get("name") == project.name), None)
                if remote is None:
                    r = await c.post(
                        self._api(worker, "/api/projects"),
                        headers=self._headers(worker),
                        json={
                            "name": project.name,
                            "git_url": project.git_url,
                            "default_branch": project.default_branch or "main",
                            "git_author_name": project.git_author_name,
                            "git_author_email": project.git_author_email,
                            "git_credential_type": project.git_credential_type,
                            "git_https_username": project.git_https_username,
                            "git_https_token": project.git_https_token,
                        },
                    )
                    r.raise_for_status()
                    remote = r.json()
                remote_id = remote["id"]

                # clone 是后台任务，等 status=ready（worker dispatch 需要 local_path 就绪）
                deadline = asyncio.get_event_loop().time() + 300
                while remote.get("status") != "ready":
                    if asyncio.get_event_loop().time() > deadline:
                        raise RuntimeError(f"worker 项目 {project.name} clone 超时")
                    await asyncio.sleep(3)
                    r = await c.get(
                        self._api(worker, f"/api/projects/{remote_id}"),
                        headers=self._headers(worker),
                    )
                    r.raise_for_status()
                    remote = r.json()

            async with self.db_factory() as db:
                w = await db.get(Worker, worker.id)
                mapping = dict(w.project_mapping or {})
                mapping[str(task.project_id)] = remote_id
                w.project_mapping = mapping
                await db.commit()
            return remote_id

    # ------------------------------------------------------------------
    # 任务转发（设计 §5.3）
    # ------------------------------------------------------------------

    async def forward_task_to_worker(self, task: Task):
        worker = await self.get_worker(task.worker_id)
        if not worker or worker.status != "ready":
            raise RuntimeError(
                f"Worker {worker.name if worker else task.worker_id} 不可用"
                f"（{worker.status if worker else 'not found'}）"
            )

        worker_project_id = await self.ensure_worker_project(worker, task)

        # 先订阅 relay 再创建：worker Dispatcher 可能创建后立即执行，后订阅丢初始事件
        await self.relay.subscribe_task(worker, task.id)

        payload = {
            "id": task.id,  # 关键：Manager 分配的全局 ID
            "title": task.title,
            "description": task.description or "",
            "project_id": worker_project_id,
            "target_branch": task.target_branch or "main",
            "priority": task.priority,
            "max_retries": task.max_retries,
            "mode": task.mode,
            "todo_file_path": task.todo_file_path,
            "max_iterations": task.max_iterations,
            "must_complete": task.must_complete,
            "goal_condition": task.goal_condition,
            "goal_max_turns": task.goal_max_turns,
            "goal_evaluator_model": task.goal_evaluator_model,
            "provider": task.provider,
            "model": task.model,
            "effort_level": task.effort_level,
            "thinking_budget": task.thinking_budget,
            "timeout_hours": task.timeout_hours,
            "enable_workflows": task.enable_workflows,
            "enabled_skills": task.enabled_skills,
            "tags": list(task.tags) if task.tags else None,
        }
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(
                self._api(worker, "/api/tasks"),
                headers=self._headers(worker),
                json=payload,
            )
            # 不检查会卡死在 in_progress：422 字段校验失败 / 500 都要立刻暴露
            r.raise_for_status()
        logger.info("task %s forwarded to worker %s", task.id, worker.id)

    async def push_files(self, worker: Worker, paths: list[str]):
        """chat 附件推到 worker 同一绝对路径（worker 上 Claude 用 Read 读）。"""
        from backend.config import settings
        from backend.services.ssh_executor import SSHExecutor
        ssh = SSHExecutor(
            host=worker.private_ip,
            user=worker.ssh_user,
            key_path=worker.ssh_key_path or settings.worker_ssh_key_path,
        )
        for path in paths:
            await ssh.copy_file(path, path)

    # ------------------------------------------------------------------
    # 通用操作代理（设计 §6.4）
    # ------------------------------------------------------------------

    async def proxy_to_worker(self, task: Task, method: str, path: str, body=None):
        worker = await self.require_ready_worker(task.worker_id)
        await self.relay.subscribe_task(worker, task.id)
        async with httpx.AsyncClient(timeout=60) as c:
            try:
                r = await c.request(
                    method, self._api(worker, path),
                    headers=self._headers(worker), json=body,
                )
            except httpx.ConnectError:
                raise HTTPException(503, f"无法连接到 Worker {worker.name}，请检查 Worker 状态")
        if r.status_code >= 400:
            detail = r.text[:500]
            try:
                detail = r.json().get("detail", detail)
            except Exception:
                pass
            raise HTTPException(r.status_code, f"Worker: {detail}")
        try:
            return r.json()
        except Exception:
            return {"ok": True}
