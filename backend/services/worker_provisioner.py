"""Worker 生命周期编排（elastic-worker 设计 §3/§14/§16.4）。

- 创建：开新 EC2（配置自举继承 Manager）或收养已有实例 → bootstrap → ready
- 部署走 rsync（Manager 本地仓库 → Worker），天然实现"版本锁定到 Manager
  当前 commit"，且 Worker 无需任何 GitHub 凭证
- 关机/开机：EC2 stop/start，数据零迁移（private IP 在 VPC 内保持不变）
- 销毁：terminate（任务迁移由上层 TaskMigrator 先行完成；收养实例只 stop）
- 状态变化广播到 "workers" channel
"""

from __future__ import annotations

import asyncio
import logging
import secrets as pysecrets
import socket
import subprocess
from datetime import datetime

import httpx
from sqlalchemy import select

from backend.config import settings
from backend.models.worker import Worker
from backend.services.cloud_provider import CloudProvider
from backend.services.ssh_executor import SSHExecutor

logger = logging.getLogger(__name__)

# rsync 部署时排除（机器本地状态，不属于代码）
DEPLOY_EXCLUDES = [
    ".venv", "node_modules", "frontend/dist", "__pycache__", ".pytest_cache",
    "*.db", "*.db-shm", "*.db-wal", ".env", ".env.*", "uploads/",
    ".claude-manager/", "archive-do-not-use/",
]


class BootstrapError(Exception):
    def __init__(self, step: str, detail: str):
        super().__init__(f"[{step}] {detail}")
        self.step = step
        self.detail = detail


class WorkerProvisioner:
    def __init__(self, db_factory, cloud: CloudProvider, broadcaster=None):
        self.db_factory = db_factory
        self.cloud = cloud
        self.broadcaster = broadcaster
        self._repo_dir = settings.worker_deploy_source_dir

    # ------------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------------

    async def _update(self, worker_id: int, **fields) -> Worker:
        async with self.db_factory() as db:
            worker = await db.get(Worker, worker_id)
            for k, v in fields.items():
                setattr(worker, k, v)
            await db.commit()
            await db.refresh(worker)
        await self._broadcast(worker)
        return worker

    async def _broadcast(self, worker: Worker):
        if self.broadcaster:
            await self.broadcaster.broadcast("workers", {
                "event_type": "worker_update",
                "worker_id": worker.id,
                "status": worker.status,
                "bootstrap_step": worker.bootstrap_step,
                "bootstrap_error": worker.bootstrap_error,
                "private_ip": worker.private_ip,
            })

    async def _log(self, worker_id: int, line: str):
        logger.info("worker %s: %s", worker_id, line.strip())
        async with self.db_factory() as db:
            worker = await db.get(Worker, worker_id)
            stamp = datetime.utcnow().strftime("%H:%M:%S")
            worker.bootstrap_log = (worker.bootstrap_log or "") + f"[{stamp}] {line}\n"
            await db.commit()
            await db.refresh(worker)
        await self._broadcast(worker)

    def _ssh(self, worker: Worker) -> SSHExecutor:
        return SSHExecutor(
            host=worker.private_ip,
            user=worker.ssh_user,
            key_path=worker.ssh_key_path or settings.worker_ssh_key_path,
        )

    @staticmethod
    def _manager_hostname() -> str:
        return socket.gethostname()

    @staticmethod
    def _manager_commit() -> str:
        try:
            r = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=settings.worker_deploy_source_dir,
                capture_output=True, text=True, timeout=10,
            )
            return r.stdout.strip() if r.returncode == 0 else ""
        except Exception:
            return ""

    # ------------------------------------------------------------------
    # 创建 / 收养
    # ------------------------------------------------------------------

    async def create_worker(
        self,
        worker_id: int,
        accounts: list[dict] | None = None,
        adopt_instance_id: str | None = None,
    ):
        """完整创建流程（后台任务）。失败 → status=error + 记录步骤与原因。"""
        step = "provision"
        try:
            worker = await self._update(
                worker_id, status="creating", bootstrap_step=step, bootstrap_error=None
            )

            if adopt_instance_id:
                await self._log(worker_id, f"adopting existing instance {adopt_instance_id}")
                info = await self.cloud.describe_instance(adopt_instance_id)
                if info["state"] == "stopped":
                    await self.cloud.start_instance(adopt_instance_id)
                private_ip = await self.cloud.wait_until_running(adopt_instance_id)
                worker = await self._update(
                    worker_id, cloud_instance_id=adopt_instance_id,
                    private_ip=private_ip, adopted=True,
                )
            else:
                await self._log(worker_id, "creating EC2 instance (config inherited from manager)")
                iid = await self.cloud.create_instance(worker.name)
                worker = await self._update(worker_id, cloud_instance_id=iid)
                private_ip = await self.cloud.wait_until_running(iid)
                worker = await self._update(worker_id, private_ip=private_ip)

            await self._log(worker_id, f"instance running, private_ip={private_ip}")
            await self._bootstrap(worker_id, accounts or [])

            worker = await self._update(
                worker_id, status="ready", bootstrap_step=None,
                last_heartbeat=datetime.utcnow(),
            )
            await self._log(worker_id, "worker ready")
        except BootstrapError as e:
            await self._update(
                worker_id, status="error", bootstrap_step=e.step, bootstrap_error=e.detail
            )
            await self._log(worker_id, f"FAILED at {e.step}: {e.detail}")
        except Exception as e:
            await self._update(
                worker_id, status="error", bootstrap_step=step, bootstrap_error=str(e)
            )
            await self._log(worker_id, f"FAILED: {e}")

    # ------------------------------------------------------------------
    # Bootstrap pipeline
    # ------------------------------------------------------------------

    async def _bootstrap(self, worker_id: int, accounts: list[dict]):
        async with self.db_factory() as db:
            worker = await db.get(Worker, worker_id)

        ssh = self._ssh(worker)

        async def run_step(step: str, coro):
            await self._update(worker_id, status="bootstrapping", bootstrap_step=step)
            await self._log(worker_id, f"step: {step}")
            try:
                await coro
            except BootstrapError:
                raise
            except Exception as e:
                raise BootstrapError(step, str(e))

        await run_step("ssh-wait", self._step_ssh_wait(ssh))
        await run_step("system-init", self._step_system_init(ssh, worker_id))
        await run_step("ccm-deploy", self._step_ccm_deploy(ssh, worker, worker_id))
        await run_step("ccm-config", self._step_ccm_config(ssh, worker_id))
        await run_step("account-login", self._step_account_login(ssh, worker_id, accounts))
        await run_step("ccm-service", self._step_ccm_service(ssh))
        await run_step("health-check", self._step_health_check(worker_id))

    async def _step_ssh_wait(self, ssh: SSHExecutor, timeout: int = 180):
        deadline = asyncio.get_event_loop().time() + timeout
        while not await ssh.check_alive():
            if asyncio.get_event_loop().time() > deadline:
                raise BootstrapError("ssh-wait", f"SSH 不可达: {ssh.host}")
            await asyncio.sleep(5)

    async def _step_system_init(self, ssh: SSHExecutor, worker_id: int):
        # 幂等：已装则跳过；node 走 nodesource，uv 走官方脚本
        script = r"""
set -e
export DEBIAN_FRONTEND=noninteractive
command -v git >/dev/null || sudo apt-get update -qq
sudo apt-get install -y -qq git curl rsync python3-venv > /dev/null 2>&1 || true
if ! command -v node >/dev/null; then
  curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash - > /dev/null
  sudo apt-get install -y -qq nodejs > /dev/null
fi
command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh > /dev/null
sudo npm ls -g @anthropic-ai/claude-code --depth=0 >/dev/null 2>&1 || sudo npm install -g @anthropic-ai/claude-code@latest > /dev/null
echo "node=$(node --version) uv=$($HOME/.local/bin/uv --version 2>/dev/null || uv --version) claude=$(claude --version 2>/dev/null | head -1)"
"""
        code, out = await ssh.run(script, timeout=900)
        if code != 0:
            raise BootstrapError("system-init", out[-2000:])
        await self._log(worker_id, out.strip().splitlines()[-1] if out.strip() else "system-init done")

    async def _step_ccm_deploy(self, ssh: SSHExecutor, worker: Worker, worker_id: int):
        remote_dir = settings.worker_remote_dir
        commit = self._manager_commit()
        await self._log(worker_id, f"rsync repo @ {commit[:8]} -> {ssh.host}:{remote_dir}")
        await ssh.run(f"mkdir -p {remote_dir}")
        # 版本锁定：直接同步 Manager 工作区（含 .git），Worker 上即 Manager 同款 commit
        await ssh.rsync_to(
            self._repo_dir.rstrip("/") + "/", remote_dir, excludes=DEPLOY_EXCLUDES,
            timeout=1200,
        )
        script = f"""
set -e
cd {remote_dir}
export PATH="$HOME/.local/bin:$PATH"
uv sync --quiet
cd frontend && npm install --silent > /dev/null && npm run build > /dev/null 2>&1
echo deploy-ok
"""
        code, out = await ssh.run(script, timeout=1800)
        if code != 0:
            raise BootstrapError("ccm-deploy", out[-2000:])
        await self._update(worker_id, ccm_commit=commit)

    async def _step_ccm_config(self, ssh: SSHExecutor, worker_id: int):
        async with self.db_factory() as db:
            worker = await db.get(Worker, worker_id)
        token = worker.auth_token or pysecrets.token_hex(24)
        await self._update(worker_id, auth_token=token)
        remote_dir = settings.worker_remote_dir
        env = "\n".join([
            f"AUTH_TOKEN={token}",
            f"PORT={worker.ccm_port}",
            "HOST=0.0.0.0",
            "AUTO_START_DISPATCHER=true",
            f"WORKSPACE_DIR={settings.workspace_dir}",  # 必须与 Manager 一致（session 路径对齐）
            "POOL_ENABLED=true",
            f"USE_PTY_MODE={'true' if settings.use_pty_mode else 'false'}",
        ])
        code, out = await ssh.run(f"cat > {remote_dir}/.env << 'EOF'\n{env}\nEOF")
        if code != 0:
            raise BootstrapError("ccm-config", out[-1000:])

    async def _step_account_login(self, ssh: SSHExecutor, worker_id: int, accounts: list[dict]):
        if not accounts:
            await self._log(worker_id, "no accounts given, skipping login (worker 已有凭证或稍后手动登录)")
            return
        remote_dir = settings.worker_remote_dir
        results = []
        for i, acct in enumerate(accounts):
            email = acct.get("email", "")
            name = "default" if i == 0 else f"account-{i + 1}"
            await self._log(worker_id, f"login {email} -> pool slot {name}")
            cmd = (
                f"cd {remote_dir} && export PATH=\"$HOME/.local/bin:$PATH\" && "
                f"uv run python scripts/auto_login.py --email {email} --add-to-pool {name}"
            )
            code, out = await ssh.run(cmd, timeout=600)
            status = "logged_in" if code == 0 else "failed"
            results.append({"email": email, "status": status})
            await self._log(worker_id, f"login {email}: {status}")
        await self._update(worker_id, accounts=results)
        if all(r["status"] == "failed" for r in results):
            raise BootstrapError("account-login", "全部账号登录失败")

    async def _step_ccm_service(self, ssh: SSHExecutor):
        remote_dir = settings.worker_remote_dir
        unit = f"""
[Unit]
Description=Claude Code Manager (worker)
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory={remote_dir}
ExecStart={remote_dir}/.venv/bin/python -m uvicorn backend.main:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
"""
        script = f"""
set -e
sudo tee /etc/systemd/system/ccm-worker.service > /dev/null << 'UNIT'
{unit}
UNIT
sudo systemctl daemon-reload
sudo systemctl enable ccm-worker > /dev/null 2>&1
sudo systemctl restart ccm-worker
"""
        code, out = await ssh.run(script, timeout=120)
        if code != 0:
            raise BootstrapError("ccm-service", out[-2000:])

    async def _step_health_check(self, worker_id: int, timeout: int = 120):
        async with self.db_factory() as db:
            worker = await db.get(Worker, worker_id)
        url = f"http://{worker.private_ip}:{worker.ccm_port}/api/system/health"
        deadline = asyncio.get_event_loop().time() + timeout
        last_err = ""
        async with httpx.AsyncClient(timeout=10) as c:
            while asyncio.get_event_loop().time() < deadline:
                try:
                    r = await c.get(url, headers={"Authorization": f"Bearer {worker.auth_token}"})
                    if r.status_code == 200:
                        return
                    last_err = f"HTTP {r.status_code}"
                except Exception as e:
                    last_err = str(e)
                await asyncio.sleep(5)
        raise BootstrapError(
            "health-check",
            f"{last_err}（若连接被拒，检查安全组是否放行 Manager→Worker:{worker.ccm_port}）",
        )

    # ------------------------------------------------------------------
    # 关机 / 开机 / 销毁
    # ------------------------------------------------------------------

    async def stop_worker(self, worker_id: int):
        worker = await self._update(worker_id, status="stopping")
        try:
            ssh = self._ssh(worker)
            await ssh.run("sudo systemctl stop ccm-worker", timeout=60)
        except Exception as e:
            logger.warning("worker %s: graceful service stop failed: %s", worker_id, e)
        await self.cloud.stop_instance(worker.cloud_instance_id)
        # 等到真正 stopped
        for _ in range(60):
            info = await self.cloud.describe_instance(worker.cloud_instance_id)
            if info["state"] == "stopped":
                break
            await asyncio.sleep(5)
        await self._update(worker_id, status="stopped")

    async def start_worker(self, worker_id: int):
        worker = await self._update(worker_id, status="starting")
        try:
            await self.cloud.start_instance(worker.cloud_instance_id)
            private_ip = await self.cloud.wait_until_running(worker.cloud_instance_id)
            worker = await self._update(worker_id, private_ip=private_ip)
            ssh = self._ssh(worker)
            await self._step_ssh_wait(ssh)
            # systemd enable 过，等服务自启
            await self._step_health_check(worker_id, timeout=180)
            await self._update(
                worker_id, status="ready", last_heartbeat=datetime.utcnow(),
                bootstrap_error=None,
            )
        except Exception as e:
            await self._update(worker_id, status="error", bootstrap_error=str(e))

    async def destroy_worker(self, worker_id: int):
        """销毁实例。任务迁移由调用方先行完成（Phase 3 接 TaskMigrator）。"""
        worker = await self._update(worker_id, status="destroying")
        try:
            if worker.cloud_instance_id:
                if worker.adopted:
                    # 收养的机器不是我们创建的，只关机不销毁
                    await self.cloud.stop_instance(worker.cloud_instance_id)
                else:
                    await self.cloud.terminate_instance(worker.cloud_instance_id)
        except Exception as e:
            logger.warning("worker %s destroy: %s", worker_id, e)
        await self._update(worker_id, status="terminated")

    # ------------------------------------------------------------------
    # 健康监控（lifespan 起一个循环）
    # ------------------------------------------------------------------

    async def health_check_loop(self, interval: int = 30):
        fail_counts: dict[int, int] = {}
        while True:
            try:
                await self._health_check_once(fail_counts)
            except Exception:
                logger.exception("worker health check loop error")
            await asyncio.sleep(interval)

    async def _health_check_once(self, fail_counts: dict[int, int]):
        async with self.db_factory() as db:
            result = await db.execute(
                select(Worker).where(Worker.status.in_(["ready", "error"]))
            )
            workers = result.scalars().all()
        for worker in workers:
            url = f"http://{worker.private_ip}:{worker.ccm_port}/api/system/health"
            try:
                async with httpx.AsyncClient(timeout=10) as c:
                    r = await c.get(url, headers={"Authorization": f"Bearer {worker.auth_token}"})
                r.raise_for_status()
                fail_counts.pop(worker.id, None)
                fields = {"last_heartbeat": datetime.utcnow()}
                commit = r.json().get("commit")
                if commit:
                    fields["ccm_commit"] = commit
                if worker.status == "error":
                    # 自动恢复（error 不是终态）
                    fields["status"] = "ready"
                    fields["bootstrap_error"] = None
                await self._update(worker.id, **fields)
            except Exception:
                fail_counts[worker.id] = fail_counts.get(worker.id, 0) + 1
                if fail_counts[worker.id] >= 3 and worker.status == "ready":
                    await self._update(
                        worker.id, status="error",
                        bootstrap_error="健康检查连续 3 次失败",
                    )
