import logging
import re
import subprocess
import sys
from pathlib import Path

from sqlalchemy import event, inspect
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import DeclarativeBase

from backend.config import settings

logger = logging.getLogger(__name__)

# Project root (where alembic.ini lives)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent

def _is_sqlite(url: str) -> bool:
    """Check if the database URL is for SQLite."""
    return url.startswith("sqlite")


def _async_url_to_sync(url: str) -> str:
    """Convert an async database URL to its synchronous equivalent.

    Examples:
        sqlite+aiosqlite:///./db.sqlite  -> sqlite:///./db.sqlite
        postgresql+asyncpg://...         -> postgresql://...
        mysql+aiomysql://...             -> mysql+pymysql://...
    """
    if "+aiosqlite" in url:
        return url.replace("+aiosqlite", "")
    if "+asyncpg" in url:
        return url.replace("+asyncpg", "")
    if "+aiomysql" in url:
        return url.replace("+aiomysql", "+pymysql")
    # Generic fallback: strip async driver suffix
    return re.sub(r'\+\w+', '', url, count=1)


# Resolve relative SQLite paths against the project root so the engine always
# opens the correct database regardless of the process's working directory.
_db_url = settings.database_url
if _is_sqlite(_db_url):
    m = re.match(r"(sqlite\+?\w*:///)(\./.+)", _db_url)
    if m:
        _db_url = m.group(1) + str((_PROJECT_ROOT / m.group(2)).resolve())

# Build engine kwargs based on database type
_engine_kwargs: dict = {"echo": False}
if _is_sqlite(_db_url):
    # Wait for short SQLite write locks instead of surfacing intermittent HTTP 500s.
    _engine_kwargs.update(connect_args={"timeout": 30})
elif not _is_sqlite(_db_url):
    # Connection pool settings for external databases
    _engine_kwargs.update(pool_size=5, max_overflow=10, pool_pre_ping=True)

engine = create_async_engine(_db_url, **_engine_kwargs)

if _is_sqlite(_db_url):
    @event.listens_for(engine.sync_engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()

async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def init_db():
    """Apply all pending Alembic migrations.

    Behaviour:
    - Fresh install (no DB file / empty database): runs all migrations from scratch.
    - Legacy DB (tables exist but no alembic_version table): stamps as head so Alembic
      knows the schema is already current, then handles future migrations normally.
    - Already tracked DB: runs any pending migrations and is otherwise a no-op.

    Supports SQLite, PostgreSQL, and MySQL backends.
    Uses subprocess to run alembic to avoid deadlocks with uvicorn's event loop.
    """
    # Detect whether this is a legacy database (created before Alembic was introduced).
    async with engine.begin() as conn:
        def _check(sync_conn):
            inspector = inspect(sync_conn)
            tables = inspector.get_table_names()
            return 'tasks' in tables, 'alembic_version' in tables

        has_tables, has_alembic = await conn.run_sync(_check)

    # Dispose all pooled connections to avoid lock conflicts with alembic
    await engine.dispose()

    if has_tables and not has_alembic:
        # Legacy database: stamp initial revision, then upgrade
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "stamp", "6b3f8a1c2d9e"],
            cwd=str(_PROJECT_ROOT),
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            logger.error("Alembic stamp failed: %s", result.stderr)
            raise RuntimeError(f"Alembic stamp failed: {result.stderr}")

    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=str(_PROJECT_ROOT),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        logger.error("Alembic upgrade failed: %s", result.stderr)
        raise RuntimeError(f"Alembic upgrade failed: {result.stderr}")

    if result.stderr:
        # Log alembic migration info (it writes to stderr)
        for line in result.stderr.strip().splitlines():
            logger.info(line.strip())

    db_type = "SQLite" if _is_sqlite(settings.database_url) else settings.database_url.split("://")[0]
    logger.info("Database ready (backend: %s)", db_type)


async def get_db():
    async with async_session() as session:
        yield session
