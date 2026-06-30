"""Worker 管理 API（elastic-worker 设计 §18）。

长流程（创建/开关机/销毁）全部 fire-and-forget 后台执行，
进度经 "workers" WS channel 实时广播，API 立即返回当前记录。
"""

from __future__ import annotations

import asyncio
import logging
import socket
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import settings
from backend.database import get_db
from backend.models.worker import Worker
from backend.schemas.worker import WorkerCreate, WorkerLogsResponse, WorkerResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/workers", tags=["workers"])

# 后台任务强引用：event loop 只持弱引用，长耗时 bootstrap 任务可能被 GC
# 掐死在半路（asyncio 文档明确的坑）
_background_tasks: set[asyncio.Task] = set()


def _spawn(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


def _provisioner():
    from backend.main import worker_provisioner

    if worker_provisioner is None:
        raise HTTPException(503, "Worker 功能未启用（WORKER_ENABLED=false 或缺少 boto3）")
    return worker_provisioner


@router.get("", response_model=list[WorkerResponse])
async def list_workers(request: Request, db: AsyncSession = Depends(get_db)):
    from backend.api.deps import get_current_user_id, get_current_user_role
    user_id = get_current_user_id(request)
    user_role = get_current_user_role(request)
    stmt = select(Worker).where(Worker.status != "terminated").order_by(desc(Worker.created_at))
    if user_role != "admin":
        stmt = stmt.where(Worker.owner_user_id == user_id)
    result = await db.execute(stmt)
    return result.scalars().all()


@router.post("", response_model=WorkerResponse)
async def create_worker(body: WorkerCreate, db: AsyncSession = Depends(get_db)):
    prov = _provisioner()
    if not body.name or not body.name.strip():
        raise HTTPException(400, "请填写 Worker 名称")
    worker = Worker(
        name=body.name.strip(),
        status="creating",
        ssh_user=settings.worker_ssh_user,
        ssh_key_path=settings.worker_ssh_key_path,
        accounts=[{"email": a.email, "token": a.token or "", "status": "pending"} for a in body.accounts],
    )
    db.add(worker)
    await db.commit()
    await db.refresh(worker)

    accounts = [a.model_dump() for a in body.accounts]
    _spawn(
        prov.create_worker(worker.id, accounts=accounts)
    )
    return worker


@router.get("/{worker_id}", response_model=WorkerResponse)
async def get_worker(worker_id: int, db: AsyncSession = Depends(get_db)):
    worker = await db.get(Worker, worker_id)
    if not worker:
        raise HTTPException(404, "Worker not found")
    return worker


@router.get("/{worker_id}/logs", response_model=WorkerLogsResponse)
async def get_worker_logs(worker_id: int, db: AsyncSession = Depends(get_db)):
    worker = await db.get(Worker, worker_id)
    if not worker:
        raise HTTPException(404, "Worker not found")
    return WorkerLogsResponse(id=worker.id, bootstrap_log=worker.bootstrap_log)


async def _require_worker(db: AsyncSession, worker_id: int, allowed_statuses: tuple[str, ...]) -> Worker:
    worker = await db.get(Worker, worker_id)
    if not worker:
        raise HTTPException(404, "Worker not found")
    if worker.status not in allowed_statuses:
        raise HTTPException(409, f"Worker 当前状态 {worker.status}，不允许该操作")
    return worker


@router.post("/{worker_id}/stop", response_model=WorkerResponse)
async def stop_worker(worker_id: int, db: AsyncSession = Depends(get_db)):
    prov = _provisioner()
    worker = await _require_worker(db, worker_id, ("ready", "error"))
    # 同步置过渡态：双击/并发请求第二发直接 409，不会起两个后台任务
    worker.status = "stopping"
    await db.commit()
    await db.refresh(worker)
    _spawn(prov.stop_worker(worker.id))
    return worker


@router.post("/{worker_id}/start", response_model=WorkerResponse)
async def start_worker(worker_id: int, db: AsyncSession = Depends(get_db)):
    prov = _provisioner()
    worker = await _require_worker(db, worker_id, ("stopped", "error"))
    worker.status = "starting"
    await db.commit()
    await db.refresh(worker)
    _spawn(prov.start_worker(worker.id))
    return worker


@router.post("/{worker_id}/destroy", response_model=WorkerResponse)
async def destroy_worker(worker_id: int, db: AsyncSession = Depends(get_db)):
    prov = _provisioner()
    worker = await db.get(Worker, worker_id)
    if not worker:
        raise HTTPException(404, "Worker not found")
    if worker.status in ("terminated", "destroying"):
        raise HTTPException(409, f"Worker 状态 {worker.status}")
    worker.status = "destroying"
    await db.commit()
    await db.refresh(worker)
    # 先把该 worker 的 task 全部迁回本机（执行态无损），再销毁实例
    _spawn(_migrate_back_then_destroy(prov, worker.id))
    return worker


async def _migrate_back_then_destroy(prov, worker_id: int, db_factory=None):
    """销毁 = 批量 migrate(task, 本机) + terminate（设计 §10.3）。

    单个 task 迁移失败不阻塞销毁（日志/状态在 Manager 本就完整，丢的只是
    session 续聊能力），但要记到 task.error_message 让用户知情。"""
    from backend.main import task_migrator, worker_relay
    from backend.models.task import Task
    from sqlalchemy import select

    if db_factory is None:
        from backend.database import async_session as db_factory

    # TaskMigrator 已接受 destroying 状态作为迁移源，无需临时改 ready
    async with db_factory() as db:
        result = await db.execute(select(Task).where(Task.worker_id == worker_id))
        tasks = result.scalars().all()
    # Stop executing tasks before migrating — running sessions can't be migrated
    for task in tasks:
        if task.status in ("executing", "in_progress"):
            try:
                from backend.services.worker_proxy import WorkerProxy
                proxy = WorkerProxy(db_factory, worker_relay)
                await proxy.proxy_to_worker(task, "POST", f"/api/tasks/{task.id}/stop-session")
                logger.info("destroy: stopped executing task %s before migration", task.id)
                await asyncio.sleep(2)
            except Exception as e:
                logger.warning("destroy: failed to stop task %s: %s", task.id, e)
    # Refresh task statuses after stopping
    async with db_factory() as db:
        result = await db.execute(select(Task).where(Task.worker_id == worker_id))
        tasks = result.scalars().all()
    for task in tasks:
        try:
            if task_migrator is not None:
                await task_migrator.migrate(task.id, None)
        except Exception as e:
            logger.warning("destroy: migrate task %s back failed: %s", task.id, e)
            async with db_factory() as db:
                t = await db.get(Task, task.id)
                if t:
                    t.worker_id = None  # 指针总要切回，否则 task 永远指向死 worker
                    t.error_message = (t.error_message or "") + f"\n[销毁迁移失败: {e}]"
                    await db.commit()
    if worker_relay is not None:
        await worker_relay.stop_worker(worker_id)
    await prov.destroy_worker(worker_id)


@router.post("/{worker_id}/retry", response_model=WorkerResponse)
async def retry_bootstrap(worker_id: int, db: AsyncSession = Depends(get_db)):
    """error 状态下重跑创建/bootstrap 流程（实例已存在则等效于收养重 bootstrap）。"""
    prov = _provisioner()
    worker = await _require_worker(db, worker_id, ("error",))
    worker.status = "creating"
    await db.commit()
    await db.refresh(worker)
    # 从 DB 读已有账号信息（创建时存的 email/token），retry 时重新登录
    saved_accounts = worker.accounts or []
    accounts = [{"email": a.get("email", ""), "token": a.get("token", "")} for a in saved_accounts if a.get("email")]
    _spawn(
        prov.create_worker(worker.id, accounts=accounts)
    )
    return worker


@router.get("/{worker_id}/pool")
async def get_worker_pool(worker_id: int, db: AsyncSession = Depends(get_db)):
    """实时拉取 worker 上配置的 CC 账号池状态（转发其 /api/pool/status）。"""
    worker = await db.get(Worker, worker_id)
    if not worker:
        raise HTTPException(404, "Worker not found")
    if worker.status != "ready" or not worker.private_ip:
        raise HTTPException(409, f"Worker 未就绪（{worker.status}）")
    import httpx
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                f"http://{worker.private_ip}:{worker.ccm_port}/api/pool/status",
                headers={"Authorization": f"Bearer {worker.auth_token}"},
            )
            if r.status_code == 404:
                # worker 端 POOL_ENABLED=false：单账号模式。
                # 老版 worker 没有账号查询端点，经 SSH 读 ~/.claude.json
                # 的 oauthAccount.emailAddress 兜底，让用户知道用的是哪个号
                email = None
                try:
                    from backend.services.ssh_executor import SSHExecutor
                    ssh = SSHExecutor(
                        host=worker.private_ip,
                        user=worker.ssh_user,
                        key_path=worker.ssh_key_path,
                    )
                    code, out = await ssh.run(
                        "python3 -c \"import json;"
                        "print(json.load(open('/home/'+__import__('getpass').getuser()+'/.claude.json'))"
                        ".get('oauthAccount',{}).get('emailAddress',''))\"",
                        timeout=15,
                    )
                    if code == 0 and out.strip():
                        email = out.strip().splitlines()[-1]
                except Exception:
                    email = None
                accounts = (
                    [{"id": "default", "email": email, "enabled": True,
                      "available": True, "cooldown_remaining": 0}]
                    if email else []
                )
                return {"enabled": True, "total": len(accounts),
                        "available": len(accounts), "accounts": accounts}
            r.raise_for_status()
            return r.json()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"无法连接 worker 号池: {e}")


@router.post("/{worker_id}/pool/add")
async def add_worker_account(worker_id: int, body: dict, db: AsyncSession = Depends(get_db)):
    """在 worker 上添加账号（跑 auto_login.py）。body: {email, token}"""
    worker = await db.get(Worker, worker_id)
    if not worker:
        raise HTTPException(404, "Worker not found")
    if worker.status != "ready" or not worker.private_ip:
        raise HTTPException(409, f"Worker 未就绪（{worker.status}）")

    email = body.get("email", "").strip()
    token = body.get("token", "").strip()
    if not email or not token:
        raise HTTPException(400, "email 和 token 必填")

    from backend.config import settings
    from backend.services.ssh_executor import SSHExecutor
    ssh = SSHExecutor(host=worker.private_ip, user=worker.ssh_user,
                      key_path=worker.ssh_key_path or settings.worker_ssh_key_path)

    # 算 slot 名：查 worker 现有账号数
    import httpx
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"http://{worker.private_ip}:{worker.ccm_port}/api/pool/status",
                            headers={"Authorization": f"Bearer {worker.auth_token}"})
            existing = len(r.json().get("accounts", [])) if r.status_code == 200 else 0
    except Exception:
        existing = 0

    slot = f"account-{existing + 1}" if existing > 0 else "default"
    remote_dir = settings.worker_remote_dir

    # 后台跑 auto_login（xvfb-run 包装）
    cmd = (
        f"cd {remote_dir} && export PATH=\"$HOME/.local/bin:$PATH\" && "
        f"xvfb-run --auto-servernum --server-args='-screen 0 1920x1080x24' "
        f"python3 scripts/auto_login.py --email {email} --token {token} "
        f"--add-to-pool {slot} --save-token"
    )

    # 这个任务可能跑 1-2 分钟，用 fire-and-forget
    _worker_login_state[f"{worker_id}:{email}"] = {"status": "running", "started_at": time.time()}

    async def _run():
        code, out = await ssh.run(cmd, timeout=600)
        _worker_login_state[f"{worker_id}:{email}"] = {
            "status": "success" if code == 0 else "failed",
            "detail": out[-1000:],
        }

    task = asyncio.create_task(_run())
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return {"ok": True, "status": "running", "slot": slot}


# worker 登录状态
_worker_login_state: dict[str, dict] = {}


@router.get("/{worker_id}/pool/add/{email}")
async def worker_add_status(worker_id: int, email: str):
    return _worker_login_state.get(f"{worker_id}:{email}") or {"status": "idle"}


@router.delete("/{worker_id}/pool/{account_id}")
async def delete_worker_account(worker_id: int, account_id: str, db: AsyncSession = Depends(get_db)):
    """从 worker 的号池中删除账号。"""
    worker = await db.get(Worker, worker_id)
    if not worker:
        raise HTTPException(404, "Worker not found")
    if worker.status != "ready" or not worker.private_ip:
        raise HTTPException(409, f"Worker 未就绪（{worker.status}）")

    # 转发到 worker 的 pool delete API
    import httpx
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.delete(
            f"http://{worker.private_ip}:{worker.ccm_port}/api/pool/accounts/{account_id}",
            headers={"Authorization": f"Bearer {worker.auth_token}"},
        )
    if r.status_code >= 400:
        raise HTTPException(r.status_code, r.text[:300])
    return r.json()


@router.get("/{worker_id}/pool/usage")
async def get_worker_pool_usage(worker_id: int, db: AsyncSession = Depends(get_db)):
    """拉取 worker 的号池额度（转发 /api/pool/usage，和本机 PoolDrawer 同格式）。"""
    worker = await db.get(Worker, worker_id)
    if not worker:
        raise HTTPException(404, "Worker not found")
    if worker.status != "ready" or not worker.private_ip:
        raise HTTPException(409, f"Worker 未就绪（{worker.status}）")
    import httpx
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(
                f"http://{worker.private_ip}:{worker.ccm_port}/api/pool/usage",
                headers={"Authorization": f"Bearer {worker.auth_token}"},
            )
            if r.status_code == 200:
                return r.json()
            # pool 未启用时走 status fallback
            r2 = await client.get(
                f"http://{worker.private_ip}:{worker.ccm_port}/api/pool/status",
                headers={"Authorization": f"Bearer {worker.auth_token}"},
            )
            return r2.json() if r2.status_code == 200 else {"accounts": []}
    except Exception as e:
        raise HTTPException(503, f"Worker 不可达: {e}")


@router.get("/{worker_id}/settings/runtime")
async def get_worker_runtime_settings(worker_id: int, db: AsyncSession = Depends(get_db)):
    worker = await db.get(Worker, worker_id)
    if not worker:
        raise HTTPException(404, "Worker not found")
    if worker.status != "ready" or not worker.private_ip:
        raise HTTPException(409, f"Worker 未就绪（{worker.status}）")
    import httpx
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get(
            f"http://{worker.private_ip}:{worker.ccm_port}/api/settings/runtime",
            headers={"Authorization": f"Bearer {worker.auth_token}"},
        )
    if r.status_code >= 400:
        raise HTTPException(r.status_code, r.text[:300])
    return r.json()


@router.put("/{worker_id}/settings/runtime")
async def update_worker_runtime_settings(worker_id: int, body: dict, db: AsyncSession = Depends(get_db)):
    worker = await db.get(Worker, worker_id)
    if not worker:
        raise HTTPException(404, "Worker not found")
    if worker.status != "ready" or not worker.private_ip:
        raise HTTPException(409, f"Worker 未就绪（{worker.status}）")
    import httpx
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.put(
            f"http://{worker.private_ip}:{worker.ccm_port}/api/settings/runtime",
            headers={"Authorization": f"Bearer {worker.auth_token}"},
            json=body,
        )
    if r.status_code >= 400:
        raise HTTPException(r.status_code, r.text[:300])
    return r.json()


# --- Team CCM: Worker assignment ---

from pydantic import BaseModel as _BaseModel


class AssignWorkerBody(_BaseModel):
    owner_user_id: int | None = None


@router.put("/{worker_id}/assign", response_model=WorkerResponse)
async def assign_worker(worker_id: int, body: AssignWorkerBody, request: Request, db: AsyncSession = Depends(get_db)):
    """Assign a worker to a user (admin only). Set owner_user_id=null for public pool."""
    from backend.api.deps import get_current_user_role
    if get_current_user_role(request) != "admin":
        raise HTTPException(403, "Only admin can assign workers")
    worker = await db.get(Worker, worker_id)
    if not worker:
        raise HTTPException(404, "Worker not found")
    worker.owner_user_id = body.owner_user_id
    await db.commit()
    await db.refresh(worker)
    return worker
