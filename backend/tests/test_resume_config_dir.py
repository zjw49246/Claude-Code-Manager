"""Regression tests for GlobalDispatcher._resolve_resume_config_dir.

Pins the fix for prod tasks #734/#740: when every pool account is rate-limited
(``select`` returns None), a resume must still be anchored to the account dir
that actually holds the session JSONL — otherwise the launch falls through to an
inherited ``CLAUDE_CONFIG_DIR`` that lacks the file and ``claude --resume`` dies
with "No conversation found with session ID", hard-failing the task and losing
the session.
"""
import asyncio
import json
import time
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, call

from backend.services.claude_pool import ClaudePool
from backend.services.codex_pool import CodexPool
from backend.services.dispatcher import (
    CodexAccountRoutingError,
    GlobalDispatcher,
    TaskLifecycleSupersededError,
    _TaskStatusGeneration,
)


@pytest.fixture
def pool_config(tmp_path):
    config = {
        "accounts": [
            {"id": "acc-1", "config_dir": str(tmp_path / "claude-1"), "email": "a@test.com", "enabled": True},
            {"id": "acc-2", "config_dir": str(tmp_path / "claude-2"), "email": "b@test.com", "enabled": True},
        ],
    }
    config_path = tmp_path / "accounts.json"
    config_path.write_text(json.dumps(config))
    return config_path


@pytest.fixture
def pool(pool_config):
    return ClaudePool(config_path=pool_config, cooldown_seconds=60)


@pytest.fixture
def dispatcher(pool):
    # The helper only touches self.pool; the rest can be inert.
    disp = GlobalDispatcher(
        db_factory=MagicMock(),
        instance_manager=MagicMock(),
        broadcaster=MagicMock(),
    )
    disp.pool = pool
    return disp


def _seed_session(config_dir: Path, session_id: str, encoded_cwd: str = "-home-user-repo") -> Path:
    proj = config_dir / "projects" / encoded_cwd
    proj.mkdir(parents=True)
    jsonl = proj / f"{session_id}.jsonl"
    jsonl.write_text("{}")
    return jsonl


class TestResolveResumeConfigDir:
    @pytest.mark.asyncio
    async def test_pool_exhausted_anchors_to_resident_dir(self, dispatcher, pool, tmp_path, monkeypatch):
        """The bug: all accounts rate-limited → must return the session's dir, not None."""
        monkeypatch.setenv("HOME", str(tmp_path))  # isolate the ~/.claude* home scan
        _seed_session(tmp_path / "claude-2", "sess-734")
        # Every account in cooldown → select() returns None without probing.
        future = time.time() + 999
        pool._cooldowns = {"acc-1": future, "acc-2": future}

        result = await dispatcher._resolve_resume_config_dir("sess-734")

        assert result == str(tmp_path / "claude-2")

    @pytest.mark.asyncio
    async def test_pool_exhausted_no_session_returns_none(self, dispatcher, pool, tmp_path, monkeypatch):
        """Fresh launch (no session) + exhausted pool → None (don't fabricate a dir)."""
        monkeypatch.setenv("HOME", str(tmp_path))
        future = time.time() + 999
        pool._cooldowns = {"acc-1": future, "acc-2": future}

        assert await dispatcher._resolve_resume_config_dir(None) is None

    @pytest.mark.asyncio
    async def test_pool_exhausted_unknown_session_returns_none(self, dispatcher, pool, tmp_path, monkeypatch):
        """Exhausted pool + session JSONL nowhere on disk → None (recovery handles it)."""
        monkeypatch.setenv("HOME", str(tmp_path))
        future = time.time() + 999
        pool._cooldowns = {"acc-1": future, "acc-2": future}

        assert await dispatcher._resolve_resume_config_dir("ghost-sid") is None

    @pytest.mark.asyncio
    async def test_healthy_account_migrates_session(self, dispatcher, pool, tmp_path, monkeypatch):
        """Happy path preserved: a healthy account is chosen and the session hardlinked into it."""
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setattr(pool, "_probe_account", lambda acc: True)  # avoid real `claude -p`
        old_jsonl = _seed_session(tmp_path / "claude-1", "sess-1")
        # Make acc-2 the only selectable account so we get a deterministic migration target.
        pool._cooldowns = {"acc-1": time.time() + 999}

        result = await dispatcher._resolve_resume_config_dir("sess-1")

        assert result == str(tmp_path / "claude-2")
        new_jsonl = tmp_path / "claude-2" / "projects" / "-home-user-repo" / "sess-1.jsonl"
        assert new_jsonl.exists()
        # Hardlinked, not copied — same inode.
        assert new_jsonl.stat().st_ino == old_jsonl.stat().st_ino

    @pytest.mark.asyncio
    async def test_healthy_resident_reused_without_probe(self, dispatcher, pool, tmp_path, monkeypatch):
        """Hot path: session lives on a healthy account → reuse it as-is.

        No ``claude -p`` probe (the old per-message latency) and no migration /
        config_dir drift (which would drop the PTY hot session). We prove the
        probe is never reached by making it explode if called.
        """
        monkeypatch.setenv("HOME", str(tmp_path))
        def _boom(acc):
            raise AssertionError("probe must not run on the resume hot path")
        monkeypatch.setattr(pool, "_probe_account", _boom)
        _seed_session(tmp_path / "claude-1", "sess-hot")
        # acc-1 healthy (no cooldown) — must be returned untouched.

        result = await dispatcher._resolve_resume_config_dir("sess-hot")

        assert result == str(tmp_path / "claude-1")
        # Session NOT copied into the other account (no drift).
        assert not (tmp_path / "claude-2" / "projects").exists()

    @pytest.mark.asyncio
    async def test_disabled_resident_migrates_off(self, dispatcher, pool, tmp_path, monkeypatch):
        """enabled=false is a hard guarantee: a session sitting on a disabled
        account is migrated off it on resume, never reused — even though the
        account is healthy (no cooldown) and still holds the JSONL."""
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setattr(pool, "_probe_account", lambda acc: True)
        # Retire acc-1 (where the session lives); acc-2 stays enabled.
        for a in pool._accounts:
            if a.id == "acc-1":
                a.enabled = False
        _seed_session(tmp_path / "claude-1", "sess-dis")

        result = await dispatcher._resolve_resume_config_dir("sess-dis")

        # Must NOT reuse the disabled resident — migrated to the enabled account.
        assert result == str(tmp_path / "claude-2")
        assert (tmp_path / "claude-2" / "projects" / "-home-user-repo" / "sess-dis.jsonl").exists()

    @pytest.mark.asyncio
    async def test_pool_disabled_returns_none(self, tmp_path, monkeypatch):
        """No pool → use the inherited/default account (return None)."""
        monkeypatch.setenv("HOME", str(tmp_path))
        disp = GlobalDispatcher(
            db_factory=MagicMock(), instance_manager=MagicMock(), broadcaster=MagicMock()
        )
        disp.pool = None
        assert await disp._resolve_resume_config_dir("sess-x") is None


def _codex_db_factory(task):
    db = MagicMock()
    db.get = AsyncMock(return_value=task)
    locked = MagicMock()
    locked.scalar_one_or_none.return_value = task
    db.execute = AsyncMock(return_value=locked)
    db.commit = AsyncMock()
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=db)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return MagicMock(return_value=ctx)


def _codex_rollout(home: Path, session_id: str, text: str = '{"type":"session_meta"}\n') -> Path:
    path = home / "sessions" / "2026" / "07" / "21" / f"rollout-now-{session_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


class TestResolveResumeConfigDirCodex:
    def _dispatcher(self, tmp_path: Path, task):
        config_path = tmp_path / "codex-accounts.json"
        config_path.write_text(json.dumps({"accounts": [
            {"id": "codex-1", "codex_home": str(tmp_path / "codex-1"), "enabled": True},
            {"id": "codex-2", "codex_home": str(tmp_path / "codex-2"), "enabled": True},
        ]}))
        manager = MagicMock()
        manager.rebind_codex_thread = AsyncMock()
        manager.clear_codex_thread_owner_for_recovery = AsyncMock(
            return_value=True
        )
        disp = GlobalDispatcher(
            db_factory=_codex_db_factory(task),
            instance_manager=manager,
            broadcaster=MagicMock(),
        )
        disp.codex_pool = CodexPool(config_path=config_path, cooldown_seconds=60)
        return disp

    @pytest.mark.asyncio
    async def test_fresh_task_selects_and_persists_account_binding(self, tmp_path):
        task = MagicMock(id=42, metadata_={})
        disp = self._dispatcher(tmp_path, task)

        result = await disp._resolve_resume_config_dir(None, "codex", task_id=42)

        assert result == str((tmp_path / "codex-1").resolve())
        assert task.metadata_["codex_account_id"] == "codex-1"

    @pytest.mark.asyncio
    async def test_fresh_task_pool_exhaustion_never_falls_back_to_default_home(
        self, tmp_path,
    ):
        task = MagicMock(id=42, metadata_={})
        disp = self._dispatcher(tmp_path, task)
        future = time.time() + 999
        disp.codex_pool._cooldowns = {"codex-1": future, "codex-2": future}

        with pytest.raises(RuntimeError, match="no available account"):
            await disp._resolve_resume_config_dir(None, "codex", task_id=42)

    @pytest.mark.asyncio
    async def test_cooldown_migrates_rollout_rebinds_and_updates_owner(self, tmp_path):
        task = MagicMock(id=42, metadata_={"codex_account_id": "codex-1"})
        disp = self._dispatcher(tmp_path, task)
        source = tmp_path / "codex-1"
        target = tmp_path / "codex-2"
        old = _codex_rollout(source, "thread-switch", "one\ntwo\n")
        disp.codex_pool.mark_rate_limited(str(source), duration=999)

        result = await disp._resolve_resume_config_dir(
            "thread-switch", "codex", task_id=42
        )

        assert result == str(target.resolve())
        copied = target / old.relative_to(source)
        assert copied.read_text() == old.read_text()
        assert copied.stat().st_ino != old.stat().st_ino
        assert task.metadata_["codex_account_id"] == "codex-2"
        disp.instance_manager.rebind_codex_thread.assert_awaited_once_with(
            "thread-switch",
            source_codex_home=str(source.resolve()),
            target_codex_home=str(target.resolve()),
        )

    @pytest.mark.asyncio
    async def test_rebind_rolls_back_when_generation_loses_binding_cas(
        self, tmp_path,
    ):
        task = MagicMock(id=42, metadata_={"codex_account_id": "codex-1"})
        disp = self._dispatcher(tmp_path, task)
        source = str((tmp_path / "codex-1").resolve())
        target = str((tmp_path / "codex-2").resolve())
        _codex_rollout(Path(source), "thread-stale-rebind")
        disp.codex_pool.mark_rate_limited(source, duration=999)
        generation = _TaskStatusGeneration(
            task_id=42,
            worker_id=None,
            shared_from_id=None,
            status="completed",
            retry_count=0,
            instance_id=None,
            started_at=None,
            completed_at=None,
        )
        disp._set_codex_task_binding = AsyncMock(return_value=False)

        with pytest.raises(TaskLifecycleSupersededError):
            await disp._resolve_resume_config_dir(
                "thread-stale-rebind",
                "codex",
                task_id=42,
                expected_generation=generation,
            )

        assert disp.instance_manager.rebind_codex_thread.await_args_list == [
            call(
                "thread-stale-rebind",
                source_codex_home=source,
                target_codex_home=target,
            ),
            call(
                "thread-stale-rebind",
                source_codex_home=target,
                target_codex_home=source,
            ),
        ]
        disp.instance_manager.clear_codex_thread_owner_for_recovery.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cancel_at_forward_rebind_settles_binding_and_rollback(
        self, tmp_path,
    ):
        task = MagicMock(id=42, metadata_={"codex_account_id": "codex-1"})
        disp = self._dispatcher(tmp_path, task)
        source = str((tmp_path / "codex-1").resolve())
        target = str((tmp_path / "codex-2").resolve())
        _codex_rollout(Path(source), "thread-cancel-rebind")
        disp.codex_pool.mark_rate_limited(source, duration=999)
        generation = _TaskStatusGeneration(
            task_id=42,
            worker_id=None,
            shared_from_id=None,
            status="completed",
            retry_count=0,
            instance_id=None,
            started_at=None,
            completed_at=None,
        )
        disp._set_codex_task_binding = AsyncMock(return_value=False)
        calls = []
        resolver_task = None

        async def cancel_outer_after_owner_move(*args, **kwargs):
            calls.append(call(*args, **kwargs))
            if len(calls) == 1:
                resolver_task.cancel()

        disp.instance_manager.rebind_codex_thread = AsyncMock(
            side_effect=cancel_outer_after_owner_move
        )
        resolver_task = asyncio.create_task(
            disp._resolve_resume_config_dir(
                "thread-cancel-rebind",
                "codex",
                task_id=42,
                expected_generation=generation,
            )
        )
        with pytest.raises(asyncio.CancelledError):
            await resolver_task

        assert calls == [
            call(
                "thread-cancel-rebind",
                source_codex_home=source,
                target_codex_home=target,
            ),
            call(
                "thread-cancel-rebind",
                source_codex_home=target,
                target_codex_home=source,
            ),
        ]

    @pytest.mark.asyncio
    async def test_real_usage_limit_rotation_preserves_rollout_and_binding(self, tmp_path):
        """Exercise the real detector -> cooldown -> copy -> rebind path.

        Mode/chat unit tests mock the rotation helper.  This integration anchor
        proves a production usage-limit message moves the native Codex thread
        between two isolated homes without aliasing or losing task ownership.
        """
        task = MagicMock(
            id=42,
            session_id="thread-real-rotation",
            metadata_={"codex_account_id": "codex-1"},
        )
        disp = self._dispatcher(tmp_path, task)
        source = tmp_path / "codex-1"
        target = tmp_path / "codex-2"
        old = _codex_rollout(
            source,
            task.session_id,
            '{"type":"session_meta"}\n{"type":"response_item"}\n',
        )
        disp.instance_manager.get_config_dir.return_value = str(source)
        disp.broadcaster = MagicMock()
        disp.broadcaster.broadcast = AsyncMock()

        result = await disp._check_codex_rate_limit_and_rotate(
            instance_id=7,
            task_id=task.id,
            combined="You've hit your usage limit. Try again later.",
        )

        assert result == {
            "config_dir": str(target.resolve()),
            "session_id": task.session_id,
            "excluded": {"codex-1"},
        }
        assert not disp.codex_pool.is_home_available(source)
        copied = target / old.relative_to(source)
        assert copied.read_text() == old.read_text()
        assert copied.stat().st_ino != old.stat().st_ino
        assert task.metadata_["codex_account_id"] == "codex-2"
        disp.instance_manager.rebind_codex_thread.assert_awaited_once_with(
            task.session_id,
            source_codex_home=str(source.resolve()),
            target_codex_home=str(target.resolve()),
        )

    @pytest.mark.asyncio
    async def test_usage_limit_with_no_alternative_is_retryable_backpressure(self, tmp_path):
        task = MagicMock(
            id=42,
            session_id="thread-no-alternative",
            metadata_={"codex_account_id": "codex-1"},
        )
        disp = self._dispatcher(tmp_path, task)
        source = tmp_path / "codex-1"
        _codex_rollout(source, task.session_id)
        disp.instance_manager.get_config_dir.return_value = str(source)
        disp.codex_pool.mark_rate_limited(str(tmp_path / "codex-2"), duration=999)

        with pytest.raises(CodexAccountRoutingError) as exc_info:
            await disp._check_codex_rate_limit_and_rotate(
                instance_id=7,
                task_id=task.id,
                combined="You've hit your usage limit. Try again later.",
            )

        assert exc_info.value.retry_after is not None
        assert exc_info.value.retry_after > 0
        assert task.metadata_["codex_account_id"] == "codex-1"
        disp.instance_manager.rebind_codex_thread.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_binding_disambiguates_retained_source_copy(self, tmp_path):
        task = MagicMock(id=42, metadata_={"codex_account_id": "codex-2"})
        disp = self._dispatcher(tmp_path, task)
        _codex_rollout(tmp_path / "codex-1", "thread-copied")
        _codex_rollout(tmp_path / "codex-2", "thread-copied")

        result = await disp._resolve_resume_config_dir(
            "thread-copied", "codex", task_id=42
        )

        assert result == str((tmp_path / "codex-2").resolve())
        disp.instance_manager.rebind_codex_thread.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_stale_binding_is_repaired_from_unique_physical_rollout(self, tmp_path):
        task = MagicMock(id=42, metadata_={"codex_account_id": "codex-2"})
        disp = self._dispatcher(tmp_path, task)
        _codex_rollout(tmp_path / "codex-1", "thread-stale-binding")

        result = await disp._resolve_resume_config_dir(
            "thread-stale-binding", "codex", task_id=42
        )

        assert result == str((tmp_path / "codex-1").resolve())
        assert task.metadata_["codex_account_id"] == "codex-1"

    @pytest.mark.asyncio
    async def test_failed_migration_never_returns_unavailable_resident(
        self, tmp_path, monkeypatch,
    ):
        from backend.services.codex_session_migration import CodexSessionMigrationError

        task = MagicMock(id=42, metadata_={"codex_account_id": "codex-1"})
        disp = self._dispatcher(tmp_path, task)
        source = tmp_path / "codex-1"
        _codex_rollout(source, "thread-migrate-fail")
        disp.codex_pool.mark_rate_limited(str(source), duration=999)

        def fail_migration(*_args, **_kwargs):
            raise CodexSessionMigrationError("disk full")

        monkeypatch.setattr(
            "backend.services.codex_session_migration.migrate_codex_rollout_session",
            fail_migration,
        )

        with pytest.raises(RuntimeError, match="could not be migrated"):
            await disp._resolve_resume_config_dir(
                "thread-migrate-fail", "codex", task_id=42
            )

    @pytest.mark.asyncio
    async def test_unbound_multiple_copies_fail_instead_of_guessing(self, tmp_path):
        task = MagicMock(id=42, metadata_={})
        disp = self._dispatcher(tmp_path, task)
        _codex_rollout(tmp_path / "codex-1", "thread-ambiguous")
        _codex_rollout(tmp_path / "codex-2", "thread-ambiguous")

        with pytest.raises(RuntimeError, match="multiple account homes"):
            await disp._resolve_resume_config_dir(
                "thread-ambiguous", "codex", task_id=42
            )

    @pytest.mark.asyncio
    async def test_claude_provider_unaffected(self, dispatcher, pool, tmp_path):
        _seed_session(tmp_path / "claude-1", "sess-claude")
        result = await dispatcher._resolve_resume_config_dir("sess-claude", "claude")
        assert result == str(tmp_path / "claude-1")
