"""Regression tests for GlobalDispatcher._resolve_resume_config_dir.

Pins the fix for prod tasks #734/#740: when every pool account is rate-limited
(``select`` returns None), a resume must still be anchored to the account dir
that actually holds the session JSONL — otherwise the launch falls through to an
inherited ``CLAUDE_CONFIG_DIR`` that lacks the file and ``claude --resume`` dies
with "No conversation found with session ID", hard-failing the task and losing
the session.
"""
import json
import time
import pytest
from pathlib import Path
from unittest.mock import MagicMock

from backend.services.claude_pool import ClaudePool
from backend.services.dispatcher import GlobalDispatcher


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
    async def test_pool_disabled_returns_none(self, tmp_path, monkeypatch):
        """No pool → use the inherited/default account (return None)."""
        monkeypatch.setenv("HOME", str(tmp_path))
        disp = GlobalDispatcher(
            db_factory=MagicMock(), instance_manager=MagicMock(), broadcaster=MagicMock()
        )
        disp.pool = None
        assert await disp._resolve_resume_config_dir("sess-x") is None
