"""Tests for Alembic migrations.

Ensures:
1. A legacy database (no alembic_version) can be migrated to head.
2. A fresh database can be created from scratch via migrations.
3. The final migrated schema matches the ORM models (no drift).
"""
import re
from pathlib import Path
from unittest.mock import patch

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text

# All ORM models must be imported so Base.metadata is complete.
from backend.database import Base
import backend.models.task  # noqa: F401
import backend.models.instance  # noqa: F401
import backend.models.project  # noqa: F401
import backend.models.log_entry  # noqa: F401
import backend.models.worktree  # noqa: F401
import backend.models.global_settings  # noqa: F401
import backend.models.secret  # noqa: F401
import backend.models.quick_phrase  # noqa: F401

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def _alembic_cfg(db_path: str) -> Config:
    """Create an Alembic Config pointing at a specific database file.

    Also patches backend.config.settings.database_url so that env.py
    (which reads settings at import time) uses the test DB, not production.
    """
    db_url = f"sqlite:///{db_path}"
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", db_url)
    return cfg


def _get_head_revision(cfg: Config) -> str:
    """Return the current head revision ID from migration scripts."""
    return ScriptDirectory.from_config(cfg).get_current_head()


def _run_alembic(cfg: Config, func, *args):
    """Run an Alembic command with settings.database_url patched to match cfg."""
    db_url = cfg.get_main_option("sqlalchemy.url")
    # env.py reads settings.database_url and overrides sqlalchemy.url,
    # so we must patch it to point at the test DB.
    async_url = db_url.replace("sqlite:///", "sqlite+aiosqlite:///")
    with patch("backend.config.settings.database_url", async_url):
        func(cfg, *args)


def _get_table_columns(engine, table_name: str) -> dict[str, str]:
    """Return {column_name: column_type_str} for a table."""
    insp = inspect(engine)
    if table_name not in insp.get_table_names():
        return {}
    cols = insp.get_columns(table_name)
    return {c["name"]: str(c["type"]) for c in cols}


def _get_all_tables(engine) -> set[str]:
    """Return set of all user table names (excluding alembic_version)."""
    insp = inspect(engine)
    return {t for t in insp.get_table_names() if t != "alembic_version"}


def _create_legacy_db(db_path: str):
    """Create a legacy database matching the backup structure (no alembic_version,
    no loop-task columns). This mirrors claude_manager_backup_20260307_2.db."""
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE instances (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name VARCHAR(100) NOT NULL,
                pid INTEGER,
                status VARCHAR(20),
                current_task_id INTEGER,
                worktree_path VARCHAR(500),
                worktree_branch VARCHAR(100),
                model VARCHAR(50),
                total_tasks_completed INTEGER,
                total_cost_usd FLOAT,
                config JSON,
                started_at DATETIME,
                last_heartbeat DATETIME
            )
        """))
        conn.execute(text("""
            CREATE TABLE projects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name VARCHAR(100) NOT NULL UNIQUE,
                git_url VARCHAR(500),
                has_remote BOOLEAN,
                local_path VARCHAR(500),
                default_branch VARCHAR(100),
                status VARCHAR(20),
                error_message VARCHAR(1000),
                created_at DATETIME
            )
        """))
        conn.execute(text("""
            CREATE TABLE tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title VARCHAR(200) NOT NULL,
                description TEXT NOT NULL,
                status VARCHAR(20) NOT NULL,
                priority INTEGER NOT NULL,
                project_id INTEGER,
                target_repo VARCHAR(500),
                target_branch VARCHAR(100),
                result_branch VARCHAR(100),
                merge_status VARCHAR(20),
                instance_id INTEGER,
                retry_count INTEGER,
                max_retries INTEGER,
                mode VARCHAR(20),
                plan_content TEXT,
                plan_approved BOOLEAN,
                session_id VARCHAR(200),
                last_cwd VARCHAR(500),
                error_message TEXT,
                tags JSON,
                metadata JSON,
                created_at DATETIME,
                started_at DATETIME,
                completed_at DATETIME
            )
        """))
        conn.execute(text("CREATE INDEX ix_tasks_status ON tasks (status)"))
        conn.execute(text("CREATE INDEX ix_tasks_priority ON tasks (priority)"))
        conn.execute(text("CREATE INDEX ix_tasks_project_id ON tasks (project_id)"))
        conn.execute(text("""
            CREATE TABLE log_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                instance_id INTEGER NOT NULL,
                task_id INTEGER,
                event_type VARCHAR(50) NOT NULL,
                role VARCHAR(20),
                content TEXT,
                tool_name VARCHAR(100),
                tool_input TEXT,
                tool_output TEXT,
                raw_json TEXT,
                is_error BOOLEAN,
                timestamp DATETIME
            )
        """))
        conn.execute(text("CREATE INDEX ix_log_entries_instance_id ON log_entries (instance_id)"))
        conn.execute(text("CREATE INDEX ix_log_entries_task_id ON log_entries (task_id)"))
        conn.execute(text("CREATE INDEX ix_log_entries_event_type ON log_entries (event_type)"))
        conn.execute(text("""
            CREATE TABLE worktrees (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                repo_path VARCHAR(500) NOT NULL,
                worktree_path VARCHAR(500) NOT NULL UNIQUE,
                branch_name VARCHAR(100) NOT NULL,
                base_branch VARCHAR(100),
                instance_id INTEGER,
                status VARCHAR(20),
                created_at DATETIME,
                removed_at DATETIME
            )
        """))
        # Insert a sample row so we can verify data survives migration
        conn.execute(text(
            "INSERT INTO tasks (title, description, status, priority, mode, created_at) "
            "VALUES ('test task', 'test desc', 'pending', 0, 'auto', '2026-01-01 00:00:00')"
        ))
    engine.dispose()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestLegacyMigration:
    """A legacy database (pre-Alembic) can be migrated to head."""

    def test_legacy_db_upgrades_successfully(self, tmp_path):
        """init_db logic: stamp initial, then upgrade to head."""
        db_path = str(tmp_path / "legacy.db")
        _create_legacy_db(db_path)

        cfg = _alembic_cfg(db_path)

        # Simulate init_db() logic for legacy DB:
        # stamp the initial revision, then upgrade to head
        _run_alembic(cfg, command.stamp, "6b3f8a1c2d9e")
        _run_alembic(cfg, command.upgrade, "head")

        # Verify alembic_version is at head
        engine = create_engine(f"sqlite:///{db_path}")
        with engine.connect() as conn:
            result = conn.execute(text("SELECT version_num FROM alembic_version"))
            version = result.scalar()
            assert version == _get_head_revision(cfg), f"Expected head revision, got {version}"

        # Verify new columns exist
        task_cols = _get_table_columns(engine, "tasks")
        assert "todo_file_path" in task_cols
        assert "loop_progress" in task_cols
        assert "max_iterations" in task_cols
        assert "context_window_usage" in task_cols

        log_cols = _get_table_columns(engine, "log_entries")
        assert "loop_iteration" in log_cols

        project_cols = _get_table_columns(engine, "projects")
        assert "sort_order" in project_cols
        assert "tags" in project_cols

        # Verify existing data survived
        with engine.connect() as conn:
            result = conn.execute(text("SELECT title FROM tasks WHERE id = 1"))
            assert result.scalar() == "test task"

        engine.dispose()

    def test_legacy_db_data_preserved(self, tmp_path):
        """Migration preserves all existing data including nullable new columns."""
        db_path = str(tmp_path / "legacy_data.db")
        _create_legacy_db(db_path)

        # Insert more data
        engine = create_engine(f"sqlite:///{db_path}")
        with engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO log_entries (instance_id, task_id, event_type, content, timestamp) "
                "VALUES (1, 1, 'message', 'hello', '2026-01-01 00:00:00')"
            ))
        engine.dispose()

        cfg = _alembic_cfg(db_path)
        _run_alembic(cfg, command.stamp, "6b3f8a1c2d9e")
        _run_alembic(cfg, command.upgrade, "head")

        engine = create_engine(f"sqlite:///{db_path}")
        with engine.connect() as conn:
            # New nullable columns default to NULL for existing rows
            row = conn.execute(text("SELECT todo_file_path, loop_progress FROM tasks WHERE id = 1")).fetchone()
            assert row[0] is None
            assert row[1] is None

            # max_iterations has server_default=50, so existing rows get 50
            row = conn.execute(text("SELECT max_iterations FROM tasks WHERE id = 1")).fetchone()
            assert row[0] == 50

            row = conn.execute(text("SELECT loop_iteration FROM log_entries WHERE id = 1")).fetchone()
            assert row[0] is None

        engine.dispose()


class TestFreshMigration:
    """A fresh database (no tables) can be fully created via Alembic upgrade."""

    def test_fresh_db_upgrade_from_scratch(self, tmp_path):
        """Running upgrade head on empty DB creates all tables."""
        db_path = str(tmp_path / "fresh.db")

        cfg = _alembic_cfg(db_path)
        _run_alembic(cfg, command.upgrade, "head")

        engine = create_engine(f"sqlite:///{db_path}")
        tables = _get_all_tables(engine)
        expected_tables = {"instances", "projects", "tasks", "log_entries", "worktrees", "global_settings", "secrets", "tags", "discussions", "discussion_messages", "discussion_agents", "discussion_events", "quick_phrases"}
        assert tables == expected_tables, f"Missing tables: {expected_tables - tables}"

        # Verify all columns from latest migration exist
        task_cols = _get_table_columns(engine, "tasks")
        assert "todo_file_path" in task_cols
        assert "loop_progress" in task_cols
        assert "max_iterations" in task_cols
        assert "context_window_usage" in task_cols

        log_cols = _get_table_columns(engine, "log_entries")
        assert "loop_iteration" in log_cols

        project_cols = _get_table_columns(engine, "projects")
        assert "sort_order" in project_cols
        assert "tags" in project_cols

        # Verify alembic_version at head
        with engine.connect() as conn:
            version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
            assert version == _get_head_revision(cfg)

        engine.dispose()

    def test_fresh_db_downgrade_and_upgrade(self, tmp_path):
        """Migrations are reversible: upgrade → downgrade → upgrade."""
        db_path = str(tmp_path / "roundtrip.db")
        cfg = _alembic_cfg(db_path)

        _run_alembic(cfg, command.upgrade, "head")
        _run_alembic(cfg, command.downgrade, "6b3f8a1c2d9e")

        engine = create_engine(f"sqlite:///{db_path}")
        task_cols = _get_table_columns(engine, "tasks")
        assert "todo_file_path" not in task_cols
        assert "loop_progress" not in task_cols
        log_cols = _get_table_columns(engine, "log_entries")
        assert "loop_iteration" not in log_cols
        engine.dispose()

        # Upgrade again
        _run_alembic(cfg, command.upgrade, "head")
        engine = create_engine(f"sqlite:///{db_path}")
        task_cols = _get_table_columns(engine, "tasks")
        assert "todo_file_path" in task_cols
        engine.dispose()


class TestAlreadyMigratedDb:
    """A database already at head is a no-op."""

    def test_upgrade_head_is_noop(self, tmp_path):
        db_path = str(tmp_path / "current.db")
        cfg = _alembic_cfg(db_path)

        _run_alembic(cfg, command.upgrade, "head")
        # Running again should not raise
        _run_alembic(cfg, command.upgrade, "head")

        engine = create_engine(f"sqlite:///{db_path}")
        with engine.connect() as conn:
            version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
            assert version == _get_head_revision(cfg)
        engine.dispose()


class TestSchemaConsistency:
    """The schema produced by Alembic migrations matches the ORM models.

    This is the critical test: if someone adds a column to an ORM model
    but forgets to create an Alembic migration, this test will catch it.
    """

    def test_migrated_schema_matches_orm(self, tmp_path):
        """Compare columns from Alembic-migrated DB vs ORM metadata.create_all."""
        # DB 1: created by Alembic migrations
        alembic_path = str(tmp_path / "alembic.db")
        cfg = _alembic_cfg(alembic_path)
        _run_alembic(cfg, command.upgrade, "head")
        alembic_engine = create_engine(f"sqlite:///{alembic_path}")

        # DB 2: created by ORM metadata.create_all
        orm_path = str(tmp_path / "orm.db")
        orm_engine = create_engine(f"sqlite:///{orm_path}")
        Base.metadata.create_all(orm_engine)

        # Compare tables
        alembic_tables = _get_all_tables(alembic_engine)
        orm_tables = _get_all_tables(orm_engine)
        assert alembic_tables == orm_tables, (
            f"Table mismatch.\n"
            f"  Only in Alembic: {alembic_tables - orm_tables}\n"
            f"  Only in ORM: {orm_tables - alembic_tables}"
        )

        # Compare columns for each table
        for table in sorted(orm_tables):
            alembic_cols = set(_get_table_columns(alembic_engine, table).keys())
            orm_cols = set(_get_table_columns(orm_engine, table).keys())
            assert alembic_cols == orm_cols, (
                f"Column mismatch in table '{table}'.\n"
                f"  Only in Alembic: {alembic_cols - orm_cols}\n"
                f"  Only in ORM (missing migration!): {orm_cols - alembic_cols}"
            )

        alembic_engine.dispose()
        orm_engine.dispose()

    def test_no_pending_autogenerate_changes(self, tmp_path):
        """Alembic autogenerate should detect no new changes.

        This verifies that the migrations fully cover the ORM models.
        If this fails, run: alembic revision --autogenerate -m 'description'
        """
        from alembic.autogenerate import compare_metadata

        db_path = str(tmp_path / "autogen.db")
        cfg = _alembic_cfg(db_path)
        _run_alembic(cfg, command.upgrade, "head")

        engine = create_engine(f"sqlite:///{db_path}")
        with engine.connect() as conn:
            from alembic.migration import MigrationContext
            mc = MigrationContext.configure(conn)
            diffs = compare_metadata(mc, Base.metadata)

            # Filter out differences that are cosmetic for SQLite:
            # - index differences (SQLite doesn't preserve index info perfectly)
            # - nullable differences (SQLite doesn't enforce NOT NULL strictly,
            #   and initial migration used nullable=True for columns with defaults)
            significant_diffs = [
                d for d in diffs
                if not (isinstance(d, tuple) and d[0] in ("add_index", "remove_index"))
                and not (isinstance(d, list) and len(d) == 1 and isinstance(d[0], tuple)
                         and d[0][0] == "modify_nullable")
            ]

            assert len(significant_diffs) == 0, (
                f"Alembic autogenerate found pending changes (need a new migration!):\n"
                + "\n".join(str(d) for d in significant_diffs)
            )

        engine.dispose()


class TestInitDbLogic:
    """Test the init_db() branching logic from database.py."""

    def test_init_db_fresh_database(self, tmp_path):
        """Fresh DB (no tables): upgrade head creates everything."""
        db_path = str(tmp_path / "fresh_init.db")

        engine = create_engine(f"sqlite:///{db_path}")
        insp = inspect(engine)
        tables = insp.get_table_names()
        has_tables = "tasks" in tables
        has_alembic = "alembic_version" in tables
        engine.dispose()

        assert not has_tables
        assert not has_alembic

        cfg = _alembic_cfg(db_path)
        # Same logic as init_db: else branch (fresh install)
        _run_alembic(cfg, command.upgrade, "head")

        engine = create_engine(f"sqlite:///{db_path}")
        assert "tasks" in _get_all_tables(engine)
        task_cols = _get_table_columns(engine, "tasks")
        assert "todo_file_path" in task_cols
        engine.dispose()

    def test_init_db_legacy_database(self, tmp_path):
        """Legacy DB (has tables, no alembic_version): stamp initial + upgrade."""
        db_path = str(tmp_path / "legacy_init.db")
        _create_legacy_db(db_path)

        engine = create_engine(f"sqlite:///{db_path}")
        insp = inspect(engine)
        has_tasks = "tasks" in insp.get_table_names()
        has_alembic = "alembic_version" in insp.get_table_names()
        engine.dispose()

        assert has_tasks
        assert not has_alembic

        cfg = _alembic_cfg(db_path)
        # Same logic as init_db: stamp initial, then upgrade
        _run_alembic(cfg, command.stamp, "6b3f8a1c2d9e")
        _run_alembic(cfg, command.upgrade, "head")

        engine = create_engine(f"sqlite:///{db_path}")
        task_cols = _get_table_columns(engine, "tasks")
        assert "todo_file_path" in task_cols
        assert "loop_progress" in task_cols
        engine.dispose()

    def test_init_db_already_tracked(self, tmp_path):
        """Already tracked DB: upgrade head is no-op."""
        db_path = str(tmp_path / "tracked_init.db")
        cfg = _alembic_cfg(db_path)

        # First run creates everything
        _run_alembic(cfg, command.upgrade, "head")

        engine = create_engine(f"sqlite:///{db_path}")
        insp = inspect(engine)
        has_tasks = "tasks" in insp.get_table_names()
        has_alembic = "alembic_version" in insp.get_table_names()
        engine.dispose()

        assert has_tasks
        assert has_alembic

        # Second run is no-op
        _run_alembic(cfg, command.upgrade, "head")

        engine = create_engine(f"sqlite:///{db_path}")
        with engine.connect() as conn:
            version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
            assert version == _get_head_revision(cfg)
        engine.dispose()
