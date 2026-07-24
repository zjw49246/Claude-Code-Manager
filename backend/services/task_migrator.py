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
import re
import shlex
import shutil
import tempfile
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import PurePosixPath

import httpx
from sqlalchemy import select, update

from backend.config import settings
from backend.models.project import Project
from backend.models.task import Task
from backend.models.worker import Worker
from backend.services.ssh_executor import SSHExecutor, worker_known_hosts_path
from backend.services.task_queue import (
    PR_REVIEW_SUPERSEDED_METADATA_KEY,
    task_retry_not_superseded_predicate,
)
from backend.services.worker_proxy import get_task_operation_lock

logger = logging.getLogger(__name__)
_CODEX_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_COPY_BUFFER_SIZE = 1024 * 1024


class MigrationError(Exception):
    pass


@dataclass(frozen=True)
class MigrationTaskGeneration:
    """Exact Manager-side Task generation owned by one migration attempt."""

    task_id: int
    worker_id: int | None
    status: str
    retry_count: int
    instance_id: int | None
    started_at: datetime | None
    completed_at: datetime | None


def migration_task_generation(task: Task) -> MigrationTaskGeneration:
    return MigrationTaskGeneration(
        task_id=task.id,
        worker_id=task.worker_id,
        status=task.status,
        retry_count=task.retry_count,
        instance_id=task.instance_id,
        started_at=task.started_at,
        completed_at=task.completed_at,
    )


def _nullable_eq(column, value):
    return column.is_(None) if value is None else column == value


def migration_generation_predicates(
    generation: MigrationTaskGeneration,
) -> tuple:
    return (
        Task.id == generation.task_id,
        (
            Task.worker_id.is_(None)
            if generation.worker_id is None
            else Task.worker_id == generation.worker_id
        ),
        Task.shared_from_id.is_(None),
        Task.status == generation.status,
        Task.retry_count == generation.retry_count,
        _nullable_eq(Task.instance_id, generation.instance_id),
        _nullable_eq(Task.started_at, generation.started_at),
        _nullable_eq(Task.completed_at, generation.completed_at),
    )


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
            # Migration keeps its fast duplicate-request guard above, but the
            # full workflow also shares WorkerProxy's mutation lock.  Chat,
            # retry and plan operations therefore cannot mutate the source
            # Worker while files/session state are being copied.
            async with get_task_operation_lock(task_id):
                await self._migrate_locked(task_id, target_worker_id)

    async def _migrate_locked(self, task_id: int, target: int | None):
        async with self.db_factory() as db:
            task = await db.get(Task, task_id)
            if not task:
                raise MigrationError("task 不存在")
            if task.worker_id == target:
                return  # 已在目标位置
            if (
                (task.metadata_ or {}).get(
                    PR_REVIEW_SUPERSEDED_METADATA_KEY
                )
                is True
            ):
                raise MigrationError("已被新 push 取代的 PR review task 不可迁移")
            if task.status in ("in_progress", "executing", "migrating"):
                raise MigrationError(f"task 状态 {task.status}，先停止再切换")
            observed = migration_task_generation(task)
            prev_status = observed.status
            src_worker_id = observed.worker_id

        src = await self._get_worker(src_worker_id) if src_worker_id else None
        dst = await self._get_worker(target) if target else None
        if target and (not dst or dst.status != "ready"):
            raise MigrationError(f"目标 Worker {dst.name if dst else target} 不可用")
        if src_worker_id and (not src or src.status not in ("ready", "destroying")):
            raise MigrationError(
                f"源 Worker {src.name if src else src_worker_id} 不可用（{src.status if src else '不存在'}）——"
                "无法取回执行态。可先启动该 Worker 再切换"
            )

        # Worker validation contains awaits, so the snapshot above is not a
        # claim.  Atomically transition the exact original state to migrating;
        # a dispatcher/user update which wins the race makes this CAS fail.
        claimed = await self._claim_migration(observed)
        await self._broadcast_status(task_id, prev_status, "migrating")

        local_codex_target_home: str | None = None
        src_unsubscribed = False
        dst_subscribed = False
        claim_active = True
        try:
            # 1. 源是 worker：先把 relay 收不到的字段同步回来（session_id/last_cwd）
            if src is not None:
                await self._sync_task_fields_from_worker(
                    src,
                    claimed,
                    expected_remote_status=prev_status,
                )

            task = await self._read_claimed_task(claimed)
            session_id = task.session_id
            project_id = task.project_id
            provider = (task.provider or "claude").lower()

            # 2. 工作目录搬运（含 .git + 未提交改动，无过滤全量 rsync）
            local_path = None
            if project_id:
                async with self.db_factory() as db:
                    project = await db.get(Project, project_id)
                local_path = project.local_path if project else None
            if local_path:
                await self._sync_workspace(src, dst, local_path)

            # 3. session 文件搬运（claude 落目标机 ~/.claude；codex 落 ~/.codex/sessions）
            if session_id:
                if provider == "codex":
                    moved_codex_home = await self._move_codex_session(
                        src, dst, session_id
                    )
                    if dst is None:
                        local_codex_target_home = moved_codex_home
                else:
                    await self._move_session(src, dst, session_id)

            # 4. 目标是 worker：确保项目记录 + 用同 ID 重建 task
            if dst is not None:
                from backend.main import worker_proxy
                task = await self._read_claimed_task(claimed)
                worker_project_id = await worker_proxy.ensure_worker_project(dst, task)
                await self._ensure_worker_task(dst, task, worker_project_id)

            # 5. relay 订阅切换
            if src is not None:
                self.relay.unsubscribe_task(src.id, task_id)
                src_unsubscribed = True
            if dst is not None:
                await self.relay.subscribe_task(dst, task_id)
                dst_subscribed = True

            # 6. 切指针 + 状态复原。仍以 migrating + 原 worker_id 为 CAS
            # 条件；并发取消/认领不能被迁移完成阶段覆盖。
            await self._finish_migration(
                claimed=claimed,
                target_worker_id=target,
                restored_status=prev_status,
                provider=provider,
                local_codex_target_home=local_codex_target_home,
            )
            claim_active = False
            await self._broadcast_status(task_id, "migrating", prev_status)
            logger.info("task %s migrated: %s -> %s", task_id, src_worker_id, target)
        except Exception:
            # 复制式搬运：源机文件未动，失败无害，状态复原可重试
            if claim_active:
                restored = await self._restore_migration_claim(
                    claimed,
                    prev_status,
                )
                if restored:
                    await self._broadcast_status(task_id, "migrating", prev_status)
                else:
                    logger.warning(
                        "task %s migration rollback skipped: claim no longer owned",
                        task_id,
                    )

            # Keep relay routing aligned with the unchanged source pointer when
            # a failure happens after subscription switching.
            try:
                if dst_subscribed and dst is not None:
                    self.relay.unsubscribe_task(dst.id, task_id)
                if src_unsubscribed and src is not None:
                    await self.relay.subscribe_task(src, task_id)
            except Exception:
                logger.exception("task %s relay rollback failed", task_id)
            raise

    # ------------------------------------------------------------------
    # 子操作
    # ------------------------------------------------------------------

    async def _get_worker(self, worker_id: int) -> Worker | None:
        async with self.db_factory() as db:
            return await db.get(Worker, worker_id)

    async def _read_claimed_task(
        self,
        claimed: MigrationTaskGeneration,
    ) -> Task:
        async with self.db_factory() as db:
            task = (
                await db.execute(
                    select(Task).where(
                        *migration_generation_predicates(claimed)
                    )
                )
            ).scalar_one_or_none()
            if task is None:
                raise MigrationError(
                    "task 迁移 generation 已被并发修改，拒绝继续使用旧状态"
                )
            return task

    async def _claim_migration(
        self,
        observed: MigrationTaskGeneration,
    ) -> MigrationTaskGeneration:
        async with self.db_factory() as db:
            result = await db.execute(
                update(Task)
                .where(
                    *migration_generation_predicates(observed),
                    task_retry_not_superseded_predicate(),
                )
                .values(status="migrating")
            )
            if result.rowcount != 1:
                await db.rollback()
                current = await db.get(Task, observed.task_id)
                if current is None:
                    raise MigrationError("task 不存在")
                raise MigrationError(
                    "task 在迁移认领前已被并发修改"
                    f"（status={current.status}, worker_id={current.worker_id}）"
                )
            await db.commit()
        return replace(observed, status="migrating")

    async def _restore_migration_claim(
        self,
        claimed: MigrationTaskGeneration,
        restored_status: str,
    ) -> bool:
        """Restore only a claim that this migration still owns."""
        async with self.db_factory() as db:
            result = await db.execute(
                update(Task)
                .where(*migration_generation_predicates(claimed))
                .values(status=restored_status)
            )
            await db.commit()
            return result.rowcount == 1

    async def _finish_migration(
        self,
        *,
        claimed: MigrationTaskGeneration,
        target_worker_id: int | None,
        restored_status: str,
        provider: str,
        local_codex_target_home: str | None,
    ) -> None:
        async with self.db_factory() as db:
            task = (
                await db.execute(
                    select(Task)
                    .where(*migration_generation_predicates(claimed))
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if task is None:
                raise MigrationError(
                    "task 迁移状态或 generation 已被并发修改，拒绝覆盖"
                )

            values: dict = {
                "worker_id": target_worker_id,
                "status": restored_status,
            }
            if provider == "codex" and target_worker_id is None and local_codex_target_home:
                values["metadata_"] = self._local_codex_account_metadata(
                    task.metadata_, local_codex_target_home
                )

            # last_cwd 防护：失败启动会把 os.getcwd() 写进 last_cwd（污染），
            # 且它优先于 target_repo——切回本机时不存在/不在项目内的一律清掉，
            # 让 cwd 解析回落到 target_repo。
            if target_worker_id is None and task.last_cwd:
                valid = os.path.isdir(task.last_cwd) and (
                    not task.target_repo
                    or task.last_cwd.startswith(task.target_repo)
                )
                if not valid:
                    values["last_cwd"] = None

            result = await db.execute(
                update(Task)
                .where(*migration_generation_predicates(claimed))
                .values(**values)
            )
            if result.rowcount != 1:
                await db.rollback()
                raise MigrationError("task 迁移状态已被并发修改，拒绝覆盖")
            await db.commit()

    def _ssh(self, worker: Worker) -> SSHExecutor:
        return SSHExecutor(
            host=worker.private_ip,
            user=worker.ssh_user,
            key_path=worker.ssh_key_path or settings.worker_ssh_key_path,
            known_hosts_path=(
                worker_known_hosts_path(worker.cloud_instance_id)
                if worker.cloud_instance_id else None
            ),
        )

    async def _broadcast_status(self, task_id: int, old: str, new: str):
        if self.broadcaster:
            try:
                await self.broadcaster.broadcast("tasks", {
                    "event": "status_change", "task_id": task_id,
                    "old_status": old, "new_status": new,
                })
            except Exception:
                logger.exception("task %s status broadcast failed", task_id)

    async def _sync_task_fields_from_worker(
        self,
        worker: Worker,
        claimed: MigrationTaskGeneration,
        *,
        expected_remote_status: str,
    ):
        """worker 广播会 pop session_id、last_cwd 只写 worker DB——迁移前必须拉全。"""
        task_id = claimed.task_id
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.get(
                f"http://{worker.private_ip}:{worker.ccm_port}/api/tasks/{task_id}",
                headers={"Authorization": f"Bearer {worker.auth_token}"},
            )
            if r.status_code != 200:
                raise MigrationError(f"从 worker 拉取 task 详情失败: HTTP {r.status_code}")
            wt = r.json()
        if (
            not isinstance(wt, dict)
            or wt.get("id") != task_id
            # Destination imports are deliberately inert ("cancelled") while
            # the Manager mirror restores the pre-migration status.  A later
            # move back must accept that intentional mismatch, but no other
            # unexpected source status may be borrowed.
            or wt.get("status") not in {
                expected_remote_status,
                "cancelled",
            }
            or wt.get("retry_count") != claimed.retry_count
        ):
            raise MigrationError(
                "源 Worker task generation 已变化，拒绝迁移旧状态"
            )
        async with self.db_factory() as db:
            task = (
                await db.execute(
                    select(Task)
                    .where(*migration_generation_predicates(claimed))
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if task is None:
                await db.rollback()
                raise MigrationError(
                    "task 在 Worker 状态同步期间已被并发修改"
                )
            remote_metadata = wt.get("metadata_") or {}
            if (
                isinstance(remote_metadata, dict)
                and remote_metadata.get(
                    PR_REVIEW_SUPERSEDED_METADATA_KEY
                )
                is True
            ):
                # A lost hidden-termination response can leave the source
                # Worker gated while the Manager mirror is still stale. Mirror
                # that durable proof before aborting migration; otherwise the
                # destination TaskCreate payload would drop the gate and make
                # the obsolete review retryable again.
                metadata = dict(task.metadata_ or {})
                metadata[PR_REVIEW_SUPERSEDED_METADATA_KEY] = True
                changed = await db.execute(
                    update(Task)
                    .where(*migration_generation_predicates(claimed))
                    .values(metadata_=metadata)
                )
                if changed.rowcount != 1:
                    await db.rollback()
                    raise MigrationError(
                        "task 在 Worker 状态同步期间已被并发修改"
                    )
                await db.commit()
                raise MigrationError(
                    "源 Worker task 已被新 push 取代，拒绝迁移"
                )
            values = {
                field: wt[field]
                for field in (
                    "session_id",
                    "last_cwd",
                    "target_repo",
                    "error_message",
                )
                if wt.get(field)
            }
            # Even an empty response must prove that the claimed generation is
            # still current after the network await.
            if not values:
                values["status"] = claimed.status
            changed = await db.execute(
                update(Task)
                .where(*migration_generation_predicates(claimed))
                .values(**values)
            )
            if changed.rowcount != 1:
                await db.rollback()
                raise MigrationError(
                    "task 在 Worker 状态同步期间已被并发修改"
                )
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

    # -- codex session 搬运 ---------------------------------------------
    # codex 的 session 是 rollout 文件：~/.codex/sessions/YYYY/MM/DD/
    # rollout-<timestamp>-<session_id>.jsonl（本机 CLI 0.144.6 实证）。
    # `codex exec resume <id>` 按 id 扫描 sessions 树，故目标机保持源机的
    # 相对路径（含日期目录）落盘即可。

    @staticmethod
    def _local_codex_session_glob(session_id: str) -> list[str]:
        if not _CODEX_SESSION_ID_RE.fullmatch(session_id):
            raise MigrationError("无效 Codex session id")
        home = os.path.expanduser("~")
        escaped_session_id = glob.escape(session_id)
        return sorted(
            glob.glob(
                f"{home}/.codex*/sessions/*/*/*/rollout-*-{escaped_session_id}.jsonl"
            )
        )

    @staticmethod
    def _codex_sessions_root_and_relative(rollout_file: str) -> tuple[str, str]:
        """Return the matched account's sessions root and safe date/file path."""
        rollout = PurePosixPath(rollout_file)
        try:
            sessions_root = rollout.parents[3]
            relative = rollout.relative_to(sessions_root)
        except (IndexError, ValueError) as exc:
            raise MigrationError(f"无效 Codex rollout 路径: {rollout_file}") from exc

        if (
            sessions_root.name != "sessions"
            or len(relative.parts) != 4
            or any(part in ("", ".", "..") for part in relative.parts)
            or not relative.name.startswith("rollout-")
            or not relative.name.endswith(".jsonl")
        ):
            raise MigrationError(f"无效 Codex rollout 路径: {rollout_file}")
        return str(sessions_root), relative.as_posix()

    @staticmethod
    def _file_is_prefix(prefix_file: str, full_file: str) -> bool:
        """Whether one rollout is a byte-prefix of another rollout."""

        if os.path.getsize(prefix_file) > os.path.getsize(full_file):
            return False
        with open(prefix_file, "rb") as prefix_stream, open(full_file, "rb") as full_stream:
            while True:
                chunk = prefix_stream.read(_COPY_BUFFER_SIZE)
                if not chunk:
                    return True
                if full_stream.read(len(chunk)) != chunk:
                    return False

    @classmethod
    def _select_authoritative_codex_rollout(cls, candidates: list[str]) -> str:
        """Choose the longest rollout only when every other copy is its prefix.

        Account rotation intentionally keeps recovery copies.  Picking the
        lexicographically first home loses later turns, while picking by mtime
        can choose a touched stale file.  Prefix validation proves that the
        selected file contains all known history; divergent histories fail
        closed and require manual reconciliation.
        """

        if not candidates:
            raise MigrationError("未找到 Codex rollout")
        ordered = sorted(candidates, key=lambda path: (-os.path.getsize(path), path))
        selected = ordered[0]
        for candidate in ordered[1:]:
            if not cls._file_is_prefix(candidate, selected):
                raise MigrationError(
                    "Codex session 存在分叉 rollout，拒绝猜测并迁移可能过期的上下文"
                )
        return selected

    @staticmethod
    def _local_codex_account_metadata(
        current_metadata: dict | None,
        target_home: str,
    ) -> dict:
        """Return metadata aligned with the local account receiving rollout.

        Worker and manager account IDs are machine-local.  Keeping the worker's
        old ID (or an earlier local ID) after copying into ``~/.codex`` makes
        the resolver trust a stale recovery copy.  Persist the actual local
        account when it is registered; otherwise clear the foreign binding so
        it is never treated as authoritative.
        """

        account_id: str | None = None
        try:
            from backend.main import codex_pool

            if codex_pool is not None:
                resolved = codex_pool.account_id_for_home(target_home)
                if isinstance(resolved, str) and resolved:
                    account_id = resolved
        except Exception:
            logger.exception(
                "Failed to map migrated CODEX_HOME %s to a local account",
                target_home,
            )

        metadata = dict(current_metadata or {})
        if account_id:
            metadata["codex_account_id"] = account_id
        else:
            metadata.pop("codex_account_id", None)
        return metadata

    @classmethod
    def _sync_local_codex_account_binding(cls, task: Task, target_home: str) -> None:
        """Compatibility wrapper for callers mutating an ORM task directly."""
        task.metadata_ = cls._local_codex_account_metadata(
            task.metadata_, target_home
        )

    async def _move_codex_session(
        self,
        src: Worker | None,
        dst: Worker | None,
        session_id: str,
    ) -> str | None:
        codex_home = os.path.expanduser("~/.codex")
        codex_root = os.path.join(codex_home, "sessions")
        if not _CODEX_SESSION_ID_RE.fullmatch(session_id):
            raise MigrationError("无效 Codex session id")
        temporary_dir: str | None = None
        target_home: str | None = None
        try:
            if src is None:
                matches = self._local_codex_session_glob(session_id)
                if not matches:
                    logger.warning("codex session %s 本机未找到，跳过 session 搬运", session_id)
                    return
                src_file = self._select_authoritative_codex_rollout(matches)
                _, rel = self._codex_sessions_root_and_relative(src_file)
            else:
                ssh = self._ssh(src)
                quoted_name = shlex.quote(f"rollout-*-{session_id}.jsonl")
                _code, out = await ssh.run(
                    "find ~/.codex*/sessions -mindepth 4 -maxdepth 4 -type f "
                    f"-name {quoted_name} -print 2>/dev/null"
                )
                remote_files = [line.strip() for line in out.splitlines() if line.strip()]
                if not remote_files:
                    logger.warning("codex session %s 在 worker %s 未找到，跳过", session_id, src.id)
                    return
                temporary_dir = tempfile.mkdtemp(prefix="ccm-codex-sess-")
                local_to_remote: dict[str, str] = {}
                for index, remote_file in enumerate(remote_files):
                    # Prefix the basename because migrated account copies
                    # usually have the exact same rollout filename.
                    local_file = os.path.join(
                        temporary_dir,
                        f"{index:04d}-{os.path.basename(remote_file)}",
                    )
                    await ssh.rsync_from(remote_file, local_file, delete=False)
                    local_to_remote[local_file] = remote_file
                src_file = self._select_authoritative_codex_rollout(
                    list(local_to_remote)
                )
                _, rel = self._codex_sessions_root_and_relative(
                    local_to_remote[src_file]
                )

            if dst is None:
                target = os.path.join(codex_root, rel)
                os.makedirs(os.path.dirname(target), exist_ok=True)
                if os.path.abspath(src_file) != os.path.abspath(target):
                    shutil.copy2(src_file, target)
                target_home = codex_home
            else:
                target = f"/home/{dst.ssh_user}/.codex/sessions/{rel}"
                await self._ssh(dst).copy_file(src_file, target)
                target_home = f"/home/{dst.ssh_user}/.codex"
            return target_home
        finally:
            if temporary_dir is not None:
                shutil.rmtree(temporary_dir, ignore_errors=True)

    # -- 目标 worker 上重建 task ----------------------------------------

    async def _ensure_worker_task(self, dst: Worker, task: Task, worker_project_id: int):
        """Atomically import an inert same-ID task on the destination Worker."""
        headers = {"Authorization": f"Bearer {dst.auth_token}"}
        base = f"http://{dst.private_ip}:{dst.ccm_port}/api/tasks"
        payload = {
            "id": task.id,
            "worker_id": None,
            "title": task.title,
            "description": task.description or task.title or "migrated task",
            "project_id": worker_project_id,
            "target_repo": task.target_repo,
            "target_branch": task.target_branch or "main",
            "priority": task.priority,
            "retry_count": task.retry_count,
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
            "system_prompt_mode": task.system_prompt_mode,
            "timeout_hours": task.timeout_hours,
            "sort_order": task.sort_order,
            "enable_workflows": task.enable_workflows,
            "enabled_skills": task.enabled_skills,
            "selected_user_skills": task.selected_user_skills,
            "tags": task.tags,
            "starred": task.starred,
            "session_id": task.session_id,
            "last_cwd": task.last_cwd,
        }
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(
                f"{base}/migration-import",
                headers=headers,
                json=payload,
            )
            if r.status_code == 409:
                try:
                    detail = r.json().get("detail", r.text)
                except ValueError:
                    detail = r.text
                raise MigrationError(f"目标 Worker 导入 task 冲突: {detail}")
            r.raise_for_status()
            if r.json().get("status") != "cancelled":
                raise MigrationError("目标 Worker 导入 task 未保持不可调度状态")
