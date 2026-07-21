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
from backend.api.project_todos import router as project_todos_router
from backend.api.settings import router as settings_router
from backend.api.uploads import router as uploads_router
from backend.api.secrets import router as secrets_router
from backend.api.tags import router as tags_router
from backend.api.files import router as files_router
from backend.api.pool import router as pool_router
from backend.api.codex_pool import router as codex_pool_router
from backend.api.monitor import router as monitor_router
from backend.api.sub_agents import router as sub_agents_router
from backend.api.sub_agent_tasks import router as sub_agent_tasks_router
from backend.api.discussions import router as discussions_router
from backend.api.quick_phrases import router as quick_phrases_router
from backend.api.pr_monitor import router as pr_monitor_router, webhook_router as pr_webhook_router
from backend.api.workers import router as workers_router
from backend.api.feishu import router as feishu_router
from backend.api.org import router as org_router
from backend.api.ask_user import router as ask_user_router
from backend.api.user_skills import router as user_skills_router
from backend.api.team_sharing import router as team_sharing_router
from backend.middleware.auth import TokenAuthMiddleware
from backend.services.ws_broadcaster import WebSocketBroadcaster
from backend.services.instance_manager import InstanceManager
from backend.services.ralph_loop import RalphLoop
from backend.services.dispatcher import GlobalDispatcher
from backend.services.update_service import UpdateService
from backend.services.sub_agent_watcher import SubAgentWatcher

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
broadcaster.db_factory = async_session
instance_manager = InstanceManager(db_factory=async_session, broadcaster=broadcaster)

shared_relay = None
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

sub_agent_watcher = SubAgentWatcher(db_factory=async_session, broadcaster=broadcaster)

# Codex account pool (optional, CODEX_POOL_ENABLED=true)
codex_pool = None
if settings.codex_pool_enabled:
    try:
        from backend.services.codex_pool import CodexPool
        codex_pool = CodexPool(
            config_path=settings.codex_pool_config_path,
            cooldown_seconds=settings.codex_pool_cooldown_seconds,
        )
        dispatcher.codex_pool = codex_pool
        logger.info("Codex pool enabled with %d accounts", len(codex_pool._accounts))
    except Exception:
        logger.debug("Codex pool init failed — codex pool disabled")

update_service = UpdateService(
    broadcaster=broadcaster,
    port=settings.port,
    project_dir=str(Path(__file__).resolve().parent.parent),
)

# 分布式 Worker（可选，WORKER_ENABLED=true 且装了 boto3 才启用）
worker_provisioner = None
worker_relay = None
worker_proxy = None
task_migrator = None
if settings.worker_enabled:
    try:
        from backend.services.cloud_provider import get_cloud_provider
        from backend.services.worker_provisioner import WorkerProvisioner
        from backend.services.worker_relay import WorkerRelay
        from backend.services.worker_proxy import WorkerProxy

        from backend.services.task_migrator import TaskMigrator

        worker_relay = WorkerRelay(db_factory=async_session, broadcaster=broadcaster)
        worker_proxy = WorkerProxy(db_factory=async_session, relay=worker_relay)
        task_migrator = TaskMigrator(
            db_factory=async_session, relay=worker_relay, broadcaster=broadcaster,
        )
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


async def _cleanup_stale_sub_agents():
    """Mark running sub-agents as completed if their parent task is already done."""
    from backend.models.sub_agent import SubAgentSession
    from backend.models.task import Task
    from datetime import datetime
    async with async_session() as db:
        result = await db.execute(
            select(SubAgentSession).where(SubAgentSession.status == "running")
        )
        stale = []
        for sa in result.scalars().all():
            task = await db.get(Task, sa.task_id)
            if task and task.status in ("completed", "failed", "cancelled"):
                sa.status = "completed"
                sa.completed_at = datetime.utcnow()
                stale.append(sa)
        if stale:
            await db.commit()
            logger.info("Cleaned up %d stale sub-agents from completed tasks", len(stale))


async def _ensure_claude_warmup():
    """Ensure all Claude config dirs have completed onboarding.

    Fresh CC installs show interactive onboarding dialogs (theme picker, trust
    directory, etc.) that block PTY mode — the MCP/channel server never starts,
    so inject gets ConnectionRefused.  Running a quick `claude -p` in each
    config_dir completes the onboarding and writes hasCompletedOnboarding into
    .claude.json.  This is the same idea as worker_provisioner._step_claude_warmup
    but for the local machine at startup.
    """
    from pathlib import Path
    import json as _json
    import subprocess

    config_dirs: list[str] = []
    if settings.pool_enabled:
        try:
            from backend.services.claude_pool import ClaudePool
            pool = ClaudePool(
                config_path=settings.pool_config_path,
                cooldown_seconds=settings.pool_cooldown_seconds,
            )
            for acct in pool._accounts:
                if acct.enabled:
                    config_dirs.append(acct.config_dir)
        except Exception:
            logger.debug("Could not load pool for warmup, using default config dir")
    if not config_dirs:
        config_dirs.append(str(Path.home() / ".claude"))

    for config_dir in config_dirs:
        try:
            claude_json_path = Path(config_dir) / ".claude.json"
            needs_warmup = True
            existing = {}
            if claude_json_path.exists():
                try:
                    existing = _json.loads(claude_json_path.read_text(encoding="utf-8"))
                    if existing.get("hasCompletedOnboarding"):
                        needs_warmup = False
                except Exception:
                    pass

            if not needs_warmup:
                continue

            logger.info("Claude warmup needed for %s, running claude -p ...", config_dir)

            env = os.environ.copy()
            env["CLAUDE_CONFIG_DIR"] = config_dir
            env.pop("CLAUDECODE", None)
            env.pop("CLAUDE_CODE", None)
            try:
                subprocess.run(
                    ["claude", "-p", "reply ok", "--dangerously-skip-permissions"],
                    env=env, capture_output=True, text=True, timeout=30,
                )
            except Exception as exc:
                logger.warning("claude -p warmup failed for %s: %s", config_dir, exc)

            # Ensure hasCompletedOnboarding is set regardless of -p result
            try:
                if claude_json_path.exists():
                    existing = _json.loads(claude_json_path.read_text(encoding="utf-8"))
                existing["hasCompletedOnboarding"] = True
                claude_json_path.parent.mkdir(parents=True, exist_ok=True)
                claude_json_path.write_text(
                    _json.dumps(existing, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
                logger.info("Claude warmup completed for %s", config_dir)
            except Exception as exc:
                logger.warning("Failed to write .claude.json for %s: %s", config_dir, exc)

        except Exception:
            logger.warning("Claude warmup failed for %s", config_dir, exc_info=True)


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
    # Create default admin on first startup
    from backend.models.user import User
    from backend.api.auth import _hash_password
    async with async_session() as db:
        result = await db.execute(select(User))
        if result.scalars().first() is None:
            db.add(User(
                email="admin@apexin.ai",
                name="Admin",
                password_hash=_hash_password("admin123456"),
                role="super_admin",
            ))
            await db.commit()
            logger.info("Default admin account created: admin@apexin.ai")
    # Build Docker sandbox image if Docker is available (for shared project isolation)
    try:
        from backend.services.container_manager import ContainerManager, build_sandbox_image
        if ContainerManager.is_docker_available():
            asyncio.create_task(build_sandbox_image())
    except Exception:
        logger.debug("Docker not available, container isolation disabled")
    # PTY 权限透传：bridge HTTP 线程需要往主循环调度协程
    instance_manager._loop = asyncio.get_running_loop()
    # Apply persisted runtime-settings override (frontend PTY toggle)
    from backend.models.global_settings import GlobalSettings
    async with async_session() as db:
        row = await db.get(GlobalSettings, 1)
        if row is not None and row.use_pty_mode is not None:
            instance_manager.set_pty_mode(row.use_pty_mode)
    update_service.recover_from_status_file()
    await _reset_stale_discussion_agents()
    await _cleanup_stale_sub_agents()
    await _sync_tags()
    sub_agent_watcher.start()
    await _ensure_claude_warmup()
    if settings.auto_start_dispatcher:
        await dispatcher.start()

    # Worker 健康监控循环 + Manager 重启后恢复所有 relay 连接
    worker_health_task = None
    if worker_provisioner is not None:
        import asyncio as _asyncio
        worker_health_task = _asyncio.create_task(worker_provisioner.health_check_loop())
        _asyncio.create_task(_recover_worker_relays())

    # Recover shared task relays

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

    # Org registry heartbeat — periodically re-register with the registry
    heartbeat_task = None

    yield

    if heartbeat_task is not None:
        heartbeat_task.cancel()

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
    try:
        await instance_manager.shutdown_codex_app_server()
    except Exception:
        logger.exception("Codex app-server shutdown failed")

    sub_agent_watcher.stop()
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
    expose_headers=["X-Refreshed-Token"],
)

app.include_router(tasks_router)
app.include_router(instances_router)
app.include_router(system_router)
app.include_router(ws_router)
app.include_router(voice_router)
app.include_router(auth_router)
app.include_router(chat_router)
app.include_router(projects_router)
app.include_router(project_todos_router)
app.include_router(settings_router)
app.include_router(dispatcher_router)
app.include_router(uploads_router)
app.include_router(secrets_router)
app.include_router(tags_router)
app.include_router(files_router)
app.include_router(pool_router)
app.include_router(codex_pool_router)
app.include_router(discussions_router)
app.include_router(quick_phrases_router)
app.include_router(monitor_router)
app.include_router(sub_agents_router)
app.include_router(sub_agent_tasks_router)
app.include_router(pr_monitor_router)
app.include_router(pr_webhook_router)
app.include_router(workers_router)
app.include_router(feishu_router)
app.include_router(org_router)
app.include_router(ask_user_router)
app.include_router(user_skills_router)
app.include_router(team_sharing_router)

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
