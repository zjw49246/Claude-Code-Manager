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
from backend.api.discussions import router as discussions_router
from backend.api.quick_phrases import router as quick_phrases_router
from backend.middleware.auth import TokenAuthMiddleware
from backend.services.ws_broadcaster import WebSocketBroadcaster
from backend.services.instance_manager import InstanceManager
from backend.services.ralph_loop import RalphLoop
from backend.services.dispatcher import GlobalDispatcher

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


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await _reset_stale_discussion_agents()
    await _sync_tags()
    if settings.auto_start_dispatcher:
        await dispatcher.start()

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
