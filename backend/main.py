import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from sqlalchemy import select

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from backend.config import settings
from backend.database import init_db, async_session
from backend.api.tasks import router as tasks_router
from backend.api.instances import router as instances_router, dispatcher_router
from backend.api.system import router as system_router
from backend.api.ws import router as ws_router
from backend.api.voice import router as voice_router
from backend.api.auth import router as auth_router
from backend.api.chat import router as chat_router
from backend.api.projects import router as projects_router
from backend.api.settings import router as settings_router
from backend.api.uploads import router as uploads_router
from backend.api.secrets import router as secrets_router
from backend.api.tags import router as tags_router
from backend.api.files import router as files_router
from backend.api.pool import router as pool_router
from backend.api.monitor import router as monitor_router
from backend.api.sub_agents import router as sub_agents_router
from backend.api.discussions import router as discussions_router
from backend.api.quick_phrases import router as quick_phrases_router
from backend.api.pr_monitor import router as pr_monitor_router, webhook_router as pr_webhook_router
from backend.api.workers import router as workers_router
from backend.middleware.auth import TokenAuthMiddleware
from backend.services.ws_broadcaster import WebSocketBroadcaster
from backend.services.instance_manager import InstanceManager
from backend.services.ralph_loop import RalphLoop
from backend.services.dispatcher import GlobalDispatcher

# Logging: surface INFO from our services AND claude_pty in the server log.
# Without this, PTY delivery/turn diagnostics are invisible (learned the
# hard way while debugging silent message loss).
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

# Global singletons
logger = logging.getLogger(__name__)
broadcaster = WebSocketBroadcaster()
instance_manager = InstanceManager(db_factory=async_session, broadcaster=broadcaster)
ralph_loop = RalphLoop(
    db_factory=async_session,
    instance_manager=instance_manager,
    broadcaster=broadcaster,
)
dispatcher = GlobalDispatcher(
    db_factory=async_session,
    instance_manager=instance_manager,
    broadcaster=broadcaster,
)

# 分布式 Worker（可选，WORKER_ENABLED=true 且装了 boto3 才启用）
worker_provisioner = None
worker_relay = None
worker_proxy = None
if settings.worker_enabled:
    try:
        from backend.services.cloud_provider import get_cloud_provider
        from backend.services.worker_provisioner import WorkerProvisioner
        from backend.services.worker_relay import WorkerRelay
        from backend.services.worker_proxy import WorkerProxy

        worker_relay = WorkerRelay(db_factory=async_session, broadcaster=broadcaster)
        worker_proxy = WorkerProxy(db_factory=async_session, relay=worker_relay)
        worker_provisioner = WorkerProvisioner(
            db_factory=async_session,
            cloud=get_cloud_provider(settings.worker_cloud_provider),
            broadcaster=broadcaster,
            relay=worker_relay,
        )
    except Exception:
        logger.exception("Worker provisioner init failed — workers disabled")


async def _sync_tags():
    """Ensure all project tags have corresponding Tag records."""
    from sqlalchemy import select
    from backend.models.project import Project
    from backend.models.tag import Tag
    async with async_session() as db:
        result = await db.execute(select(Project.tags))
        all_tag_names: set[str] = set()
        for (tags,) in result:
            if tags:
                all_tag_names.update(tags)
        if not all_tag_names:
            return
        existing = await db.execute(select(Tag.name))
        existing_names = {row[0] for row in existing}
        for name in all_tag_names - existing_names:
            db.add(Tag(name=name))
        await db.commit()


async def _reset_stale_discussion_agents():
    from backend.models.discussion import DiscussionAgent
    async with async_session() as db:
        result = await db.execute(
            select(DiscussionAgent).where(DiscussionAgent.status == "running")
        )
        stale = result.scalars().all()
        for agent in stale:
            agent.status = "idle"
            agent.pid = None
        if stale:
            await db.commit()
            logger.info("Reset %d stale discussion agents to idle", len(stale))


async def _recover_worker_relays():
    """Manager 重启后为 ready worker 上的活跃 task 重建中继 + 补缺失日志。"""
    from backend.models.worker import Worker
    try:
        async with async_session() as db:
            result = await db.execute(
                select(Worker).where(Worker.status == "ready")
            )
            workers = result.scalars().all()
        for w in workers:
            try:
                await worker_relay.recover(w)
            except Exception:
                logger.exception("recover relay for worker %s failed", w.id)
    except Exception:
        logger.exception("worker relay recovery failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    # PTY 权限透传：bridge HTTP 线程需要往主循环调度协程
    instance_manager._loop = asyncio.get_running_loop()
    # Apply persisted runtime-settings override (frontend PTY toggle)
    from backend.models.global_settings import GlobalSettings
    async with async_session() as db:
        row = await db.get(GlobalSettings, 1)
        if row is not None and row.use_pty_mode is not None:
            instance_manager.set_pty_mode(row.use_pty_mode)
    await _reset_stale_discussion_agents()
    await _sync_tags()
    if settings.auto_start_dispatcher:
        await dispatcher.start()

    # Worker 健康监控循环 + Manager 重启后恢复所有 relay 连接
    worker_health_task = None
    if worker_provisioner is not None:
        import asyncio as _asyncio
        worker_health_task = _asyncio.create_task(worker_provisioner.health_check_loop())
        _asyncio.create_task(_recover_worker_relays())

    # Start periodic database backup (optional — requires BACKUP_ENABLED=true in .env)
    backup_svc = None
    if settings.backup_enabled:
        from backend.services.backup_service import BackupService
        backup_svc = BackupService(
            db_path=settings.database_url,
            backup_type=settings.backup_type,
            interval_seconds=settings.backup_interval_seconds,
            max_copies=settings.backup_max_copies,
            destination_path=settings.backup_destination_path,
            temp_dir=settings.backup_temp_dir,
            s3_bucket=settings.backup_s3_bucket,
            s3_region=settings.backup_s3_region,
            s3_access_key=settings.backup_s3_access_key,
            s3_secret_key=settings.backup_s3_secret_key,
            oss_endpoint=settings.backup_oss_endpoint,
            oss_bucket=settings.backup_oss_bucket,
            oss_access_key=settings.backup_oss_access_key,
            oss_secret_key=settings.backup_oss_secret_key,
        )
        backup_svc.start()

    from backend.api.uploads import start_upload_cleanup_loop
    upload_cleanup_task = await start_upload_cleanup_loop()

    yield

    # Stop all PTY sessions on shutdown — orphaned CC processes keep holding
    # their session files and break cold resume after restart.
    if instance_manager._pty_backend is not None:
        try:
            await instance_manager._pty_backend.shutdown()
        except Exception:
            logger.exception("PTY backend shutdown failed")

    if worker_health_task is not None:
        worker_health_task.cancel()
    upload_cleanup_task.cancel()
    # Stop all running Claude processes before shutdown
    for inst_id in list(instance_manager.processes.keys()):
        await instance_manager.stop(inst_id)

    if backup_svc:
        backup_svc.stop()
    await dispatcher.stop()


app = FastAPI(title="Claude Code Manager", version="0.1.0", lifespan=lifespan)

app.add_middleware(TokenAuthMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(tasks_router)
app.include_router(instances_router)
app.include_router(system_router)
app.include_router(ws_router)
app.include_router(voice_router)
app.include_router(auth_router)
app.include_router(chat_router)
app.include_router(projects_router)
app.include_router(settings_router)
app.include_router(dispatcher_router)
app.include_router(uploads_router)
app.include_router(secrets_router)
app.include_router(tags_router)
app.include_router(files_router)
app.include_router(pool_router)
app.include_router(discussions_router)
app.include_router(quick_phrases_router)
app.include_router(monitor_router)
app.include_router(sub_agents_router)
app.include_router(pr_monitor_router)
app.include_router(pr_webhook_router)
app.include_router(workers_router)

# Serve frontend static files in production
FRONTEND_DIST = Path(__file__).resolve().parent.parent / "frontend" / "dist"
if FRONTEND_DIST.is_dir():
    app.mount("/assets", StaticFiles(directory=str(FRONTEND_DIST / "assets")), name="static")

    @app.get("/{full_path:path}")
    async def serve_spa(full_path: str):
        """Serve index.html for all non-API routes (SPA fallback)."""
        file_path = FRONTEND_DIST / full_path
        if file_path.is_file():
            return FileResponse(str(file_path))
        return FileResponse(str(FRONTEND_DIST / "index.html"), headers={"Cache-Control": "no-cache"})
