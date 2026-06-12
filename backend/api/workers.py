"""Worker 管理 API（elastic-worker 设计 §18）。

长流程（创建/开关机/销毁）全部 fire-and-forget 后台执行，
进度经 "workers" WS channel 实时广播，API 立即返回当前记录。
"""

from __future__ import annotations

import asyncio
import logging
import socket

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config import settings
from backend.database import get_db
from backend.models.worker import Worker
from backend.schemas.worker import WorkerCreate, WorkerLogsResponse, WorkerResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/workers", tags=["workers"])

ACTIVE_STATUSES = ("creating", "bootstrapping", "ready", "error", "stopping", "stopped", "starting", "destroying")


def _provisioner():
    from backend.main import worker_provisioner

    if worker_provisioner is None:
        raise HTTPException(503, "Worker 功能未启用（WORKER_ENABLED=false 或缺少 boto3）")
    return worker_provisioner


@router.get("", response_model=list[WorkerResponse])
async def list_workers(db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Worker).where(Worker.status != "terminated").order_by(desc(Worker.created_at))
    )
    return result.scalars().all()


@router.post("", response_model=WorkerResponse)
async def create_worker(body: WorkerCreate, db: AsyncSession = Depends(get_db)):
    prov = _provisioner()
    worker = Worker(
        name=body.name or "pending",
        status="creating",
        ssh_user=settings.worker_ssh_user,
        ssh_key_path=settings.worker_ssh_key_path,
        accounts=[{"email": a.email, "status": "pending"} for a in body.accounts],
    )
    db.add(worker)
    await db.commit()
    await db.refresh(worker)
    if not body.name:
        worker.name = f"{socket.gethostname()}-worker-{worker.id}"
        await db.commit()
        await db.refresh(worker)

    accounts = [a.model_dump() for a in body.accounts]
    asyncio.create_task(
        prov.create_worker(worker.id, accounts=accounts, adopt_instance_id=body.adopt_instance_id)
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
    asyncio.create_task(prov.stop_worker(worker.id))
    return worker


@router.post("/{worker_id}/start", response_model=WorkerResponse)
async def start_worker(worker_id: int, db: AsyncSession = Depends(get_db)):
    prov = _provisioner()
    worker = await _require_worker(db, worker_id, ("stopped", "error"))
    asyncio.create_task(prov.start_worker(worker.id))
    return worker


@router.post("/{worker_id}/destroy", response_model=WorkerResponse)
async def destroy_worker(worker_id: int, db: AsyncSession = Depends(get_db)):
    prov = _provisioner()
    worker = await db.get(Worker, worker_id)
    if not worker:
        raise HTTPException(404, "Worker not found")
    if worker.status == "terminated":
        raise HTTPException(409, "Worker 已销毁")
    # Phase 3 接 TaskMigrator：销毁前把该 worker 的 task 全部迁回本机
    asyncio.create_task(prov.destroy_worker(worker.id))
    return worker


@router.post("/{worker_id}/retry", response_model=WorkerResponse)
async def retry_bootstrap(worker_id: int, db: AsyncSession = Depends(get_db)):
    """error 状态下重跑创建/bootstrap 流程（实例已存在则等效于收养重 bootstrap）。"""
    prov = _provisioner()
    worker = await _require_worker(db, worker_id, ("error",))
    asyncio.create_task(
        prov.create_worker(worker.id, accounts=[], adopt_instance_id=worker.cloud_instance_id)
    )
    return worker
