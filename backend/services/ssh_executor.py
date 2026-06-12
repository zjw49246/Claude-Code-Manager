"""SSH 远程执行（elastic-worker 设计 §16.3）。

paramiko 是同步库，统一 asyncio.to_thread 包装。每次 run/rsync 建独立连接，
bootstrap 场景下命令少且长耗时，连接复用收益小、状态管理成本高。
"""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
import subprocess

logger = logging.getLogger(__name__)


class SSHExecutor:
    def __init__(self, host: str, user: str, key_path: str):
        self.host = host
        self.user = user
        self.key_path = os.path.expanduser(key_path)

    def _run_sync(self, command: str, timeout: int) -> tuple[int, str]:
        import paramiko

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            client.connect(
                self.host,
                username=self.user,
                key_filename=self.key_path,
                timeout=15,
            )
            _, stdout, stderr = client.exec_command(command, timeout=timeout)
            exit_code = stdout.channel.recv_exit_status()
            out = stdout.read().decode(errors="replace")
            err = stderr.read().decode(errors="replace")
            return exit_code, out + (("\n" + err) if err.strip() else "")
        finally:
            client.close()

    async def run(self, command: str, timeout: int = 300) -> tuple[int, str]:
        """执行远程命令，返回 (exit_code, output)。"""
        logger.debug("ssh %s: %s", self.host, command[:200])
        return await asyncio.to_thread(self._run_sync, command, timeout)

    async def check_alive(self, timeout: int = 10) -> bool:
        try:
            code, _ = await self.run("true", timeout=timeout)
            return code == 0
        except Exception:
            return False

    async def copy_file(self, local_path: str, remote_path: str, timeout: int = 120) -> None:
        """复制单个文件到远端同路径（先 mkdir -p 再 rsync，无 filter）。"""
        import os as _os
        remote_dir = _os.path.dirname(remote_path)
        code, out = await self.run(f"mkdir -p {shlex.quote(remote_dir)}", timeout=30)
        if code != 0:
            raise RuntimeError(f"mkdir failed: {out[-500:]}")
        ssh_opt = (
            f"ssh -i {shlex.quote(self.key_path)} "
            "-o StrictHostKeyChecking=accept-new -o ConnectTimeout=15"
        )
        cmd = ["rsync", "-az", "-e", ssh_opt, local_path,
               f"{self.user}@{self.host}:{remote_path}"]

        def _sync() -> None:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            if r.returncode != 0:
                raise RuntimeError(f"rsync file failed ({r.returncode}): {r.stderr[-1000:]}")

        await asyncio.to_thread(_sync)

    async def rsync_from(
        self,
        remote_path: str,
        local_path: str,
        timeout: int = 600,
        delete: bool = True,
    ) -> None:
        """rsync 远端目录/文件到本地（迁移用：全量含 .git 与未提交改动，无过滤）。"""
        import os as _os
        _os.makedirs(_os.path.dirname(local_path.rstrip("/")) or "/", exist_ok=True)
        ssh_opt = (
            f"ssh -i {shlex.quote(self.key_path)} "
            "-o StrictHostKeyChecking=accept-new -o ConnectTimeout=15"
        )
        cmd = ["rsync", "-az"] + (["--delete"] if delete else []) + [
            "-e", ssh_opt, f"{self.user}@{self.host}:{remote_path}", local_path]

        def _sync() -> None:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            if r.returncode != 0:
                raise RuntimeError(f"rsync from failed ({r.returncode}): {r.stderr[-2000:]}")

        await asyncio.to_thread(_sync)

    async def rsync_to(
        self,
        local_path: str,
        remote_path: str,
        excludes: list[str] | None = None,
        timeout: int = 600,
    ) -> None:
        """rsync 本地目录到远端（用系统 rsync over ssh，增量+保权限）。"""
        # .gitignore 的忽略规则自动生效（.venv/node_modules/*.db 等），
        # 与仓库保持同步，避免手工 exclude 列表漂移；excludes 只补充
        # git 跟踪之外必须排除的内容
        cmd = ["rsync", "-az", "--delete", "--filter", ":- .gitignore"]
        for ex in excludes or []:
            cmd += ["--exclude", ex]
        ssh_opt = (
            f"ssh -i {shlex.quote(self.key_path)} "
            "-o StrictHostKeyChecking=accept-new -o ConnectTimeout=15"
        )
        cmd += ["-e", ssh_opt, local_path, f"{self.user}@{self.host}:{remote_path}"]

        def _sync() -> None:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            if r.returncode != 0:
                raise RuntimeError(f"rsync failed ({r.returncode}): {r.stderr[-2000:]}")

        await asyncio.to_thread(_sync)
