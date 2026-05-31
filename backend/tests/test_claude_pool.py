"""Tests for Claude account pool — rotation, detection, session migration."""
import json
import os
import time
import pytest
from pathlib import Path
from unittest.mock import patch, AsyncMock, MagicMock

from backend.services.claude_pool import (
    ClaudePool,
    PoolAccount,
    is_rate_limited,
    is_auth_failure,
    is_pool_rotatable,
    migrate_session,
    collect_process_output_for_detection,
)


# ---------------------------------------------------------------------------
# Rate-limit / auth-failure detection
# ---------------------------------------------------------------------------

class TestRateLimitDetection:
    def test_hit_your_limit(self):
        assert is_rate_limited("You've hit your limit, you can continue using Claude at 3pm")

    def test_usage_limit_reached(self):
        assert is_rate_limited("Claude AI usage limit reached")

    def test_resets_timestamp(self):
        assert is_rate_limited("resets 3pm (America/New_York)")

    def test_organization_disabled(self):
        assert is_rate_limited("Your organization has been disabled")

    def test_account_disabled(self):
        assert is_rate_limited("Your account has been disabled")

    def test_chinese_rate_limit(self):
        assert is_rate_limited("当前限速，请稍后再试")

    def test_case_insensitive(self):
        assert is_rate_limited("YOU'VE HIT YOUR LIMIT")

    def test_no_false_positive_on_generic_429(self):
        assert not is_rate_limited("The API returned a 429 rate limit error")

    def test_no_false_positive_on_tool_output(self):
        assert not is_rate_limited("Semantic Scholar returned: Too many requests")

    def test_empty_string(self):
        assert not is_rate_limited("")

    def test_none(self):
        assert not is_rate_limited(None)

    def test_normal_output(self):
        assert not is_rate_limited("Task completed successfully. All tests pass.")


class TestAuthFailureDetection:
    def test_not_logged_in(self):
        assert is_auth_failure("Error: Not logged in. Please run `claude login`")

    def test_please_run_login(self):
        assert is_auth_failure("please run /login to authenticate")

    def test_not_authenticated(self):
        assert is_auth_failure("not authenticated, please sign in")

    def test_please_log_in(self):
        assert is_auth_failure("Please log in first")

    def test_failed_to_authenticate(self):
        assert is_auth_failure("Failed to authenticate with the server")

    def test_no_false_positive(self):
        assert not is_auth_failure("User login endpoint created")

    def test_empty(self):
        assert not is_auth_failure("")


class TestPoolRotatable:
    def test_rate_limit(self):
        assert is_pool_rotatable("You've hit your limit")

    def test_auth_failure(self):
        assert is_pool_rotatable("Not logged in")

    def test_normal_error(self):
        assert not is_pool_rotatable("SyntaxError: unexpected token")


# ---------------------------------------------------------------------------
# Pool configuration and selection
# ---------------------------------------------------------------------------

@pytest.fixture
def pool_config(tmp_path):
    config = {
        "accounts": [
            {"id": "acc-1", "config_dir": str(tmp_path / "claude-1"), "email": "a@test.com", "enabled": True},
            {"id": "acc-2", "config_dir": str(tmp_path / "claude-2"), "email": "b@test.com", "enabled": True},
            {"id": "acc-3", "config_dir": str(tmp_path / "claude-3"), "email": "c@test.com", "enabled": False},
        ],
    }
    config_path = tmp_path / "accounts.json"
    config_path.write_text(json.dumps(config))
    return config_path


@pytest.fixture
def pool(pool_config):
    return ClaudePool(config_path=pool_config, cooldown_seconds=60)


class TestClaudePool:
    def test_load_accounts(self, pool):
        assert pool.enabled is True
        accounts = pool.list_accounts()
        assert len(accounts) == 3

    def test_disabled_pool_when_single_account(self, tmp_path):
        config = {"accounts": [{"id": "only", "config_dir": "/tmp/x", "enabled": True}]}
        path = tmp_path / "accounts.json"
        path.write_text(json.dumps(config))
        p = ClaudePool(config_path=path)
        assert p.enabled is False

    def test_missing_config_file(self, tmp_path):
        p = ClaudePool(config_path=tmp_path / "nonexistent.json")
        assert p.enabled is False
        assert p.list_accounts() == []

    def test_select_returns_config_dir(self, pool, tmp_path):
        result = pool.select()
        assert result in [str(tmp_path / "claude-1"), str(tmp_path / "claude-2")]

    def test_select_excludes_disabled(self, pool, tmp_path):
        # acc-3 is disabled, should never be selected
        for _ in range(20):
            result = pool.select()
            assert result != str(tmp_path / "claude-3")

    def test_select_excludes_specified(self, pool, tmp_path):
        result = pool.select(exclude={"acc-1"})
        assert result == str(tmp_path / "claude-2")

    def test_select_returns_none_when_all_excluded(self, pool):
        result = pool.select(exclude={"acc-1", "acc-2"})
        assert result is None

    def test_mark_rate_limited(self, pool, tmp_path):
        config_dir = str(tmp_path / "claude-1")
        pool.mark_rate_limited(config_dir, duration=60)
        # acc-1 should be unavailable
        accounts = pool.list_accounts()
        acc1 = next(a for a in accounts if a["id"] == "acc-1")
        assert acc1["available"] is False
        assert acc1["cooldown_remaining"] > 0
        # select should skip acc-1
        result = pool.select()
        assert result == str(tmp_path / "claude-2")

    def test_mark_auth_failure(self, pool, tmp_path):
        config_dir = str(tmp_path / "claude-1")
        pool.mark_auth_failure(config_dir)
        accounts = pool.list_accounts()
        acc1 = next(a for a in accounts if a["id"] == "acc-1")
        assert acc1["available"] is False
        # Should have a very long cooldown
        assert acc1["cooldown_remaining"] > 86000

    def test_clear_cooldown(self, pool, tmp_path):
        config_dir = str(tmp_path / "claude-1")
        pool.mark_rate_limited(config_dir)
        pool.clear_cooldown("acc-1")
        accounts = pool.list_accounts()
        acc1 = next(a for a in accounts if a["id"] == "acc-1")
        assert acc1["available"] is True

    def test_select_none_when_all_rate_limited(self, pool, tmp_path):
        pool.mark_rate_limited(str(tmp_path / "claude-1"))
        pool.mark_rate_limited(str(tmp_path / "claude-2"))
        assert pool.select() is None

    def test_cooldown_expires(self, pool, tmp_path):
        config_dir = str(tmp_path / "claude-1")
        pool.mark_rate_limited(config_dir, duration=1)
        # Immediately after, still in cooldown
        assert pool.select(exclude=set()) is not None  # acc-2 is available
        # Manually expire the cooldown
        pool._cooldowns["acc-1"] = time.time() - 1
        result = pool.select()
        # acc-1 should be available again
        assert result is not None

    def test_account_id_from_config_dir(self, pool, tmp_path):
        assert pool.account_id_from_config_dir(str(tmp_path / "claude-1")) == "acc-1"
        assert pool.account_id_from_config_dir("/nonexistent") is None

    def test_status(self, pool, tmp_path):
        pool.mark_rate_limited(str(tmp_path / "claude-1"))
        status = pool.status()
        assert status["enabled"] is True
        assert status["total"] == 3
        assert status["available"] == 1  # acc-2
        assert status["cooldown"] == 1  # acc-1
        assert status["disabled"] == 1  # acc-3

    def test_reload(self, pool, pool_config):
        pool.reload()
        assert len(pool.list_accounts()) == 3

    def test_select_round_robin(self, pool, tmp_path):
        """Accounts with earlier cooldown expiry are preferred → distributes load."""
        pool.mark_rate_limited(str(tmp_path / "claude-1"), duration=1)
        pool._cooldowns["acc-1"] = time.time() - 10  # expired 10s ago
        pool.mark_rate_limited(str(tmp_path / "claude-2"), duration=1)
        pool._cooldowns["acc-2"] = time.time() - 5   # expired 5s ago
        # acc-1 expired earlier, should be selected first
        result = pool.select()
        assert result == str(tmp_path / "claude-1")


# ---------------------------------------------------------------------------
# Session migration
# ---------------------------------------------------------------------------

class TestSessionMigration:
    def test_successful_hardlink(self, tmp_path):
        old_dir = tmp_path / "old-account"
        new_dir = tmp_path / "new-account"
        session_id = "test-session-123"

        # Create the session file in old_config_dir
        session_dir = old_dir / "projects" / "encoded-cwd"
        session_dir.mkdir(parents=True)
        session_file = session_dir / f"{session_id}.jsonl"
        session_file.write_text('{"event": "init"}\n')

        result = migrate_session(
            old_config_dir=str(old_dir),
            new_config_dir=str(new_dir),
            session_id=session_id,
        )
        assert result is True

        new_file = new_dir / "projects" / "encoded-cwd" / f"{session_id}.jsonl"
        assert new_file.exists()
        # Verify it's a hardlink (same inode)
        assert session_file.stat().st_ino == new_file.stat().st_ino

    def test_already_hardlinked(self, tmp_path):
        old_dir = tmp_path / "old"
        new_dir = tmp_path / "new"
        session_id = "sid-abc"

        session_dir = old_dir / "projects" / "cwd"
        session_dir.mkdir(parents=True)
        session_file = session_dir / f"{session_id}.jsonl"
        session_file.write_text("data\n")

        # First migration
        migrate_session(old_config_dir=str(old_dir), new_config_dir=str(new_dir), session_id=session_id)
        # Second migration should return True (idempotent)
        result = migrate_session(old_config_dir=str(old_dir), new_config_dir=str(new_dir), session_id=session_id)
        assert result is True

    def test_missing_session_file(self, tmp_path):
        result = migrate_session(
            old_config_dir=str(tmp_path / "old"),
            new_config_dir=str(tmp_path / "new"),
            session_id="nonexistent",
        )
        assert result is False

    def test_different_inode_refuses(self, tmp_path):
        old_dir = tmp_path / "old"
        new_dir = tmp_path / "new"
        session_id = "sid-conflict"

        # Create in old
        (old_dir / "projects" / "cwd").mkdir(parents=True)
        (old_dir / "projects" / "cwd" / f"{session_id}.jsonl").write_text("old\n")

        # Create a different file at the target
        (new_dir / "projects" / "cwd").mkdir(parents=True)
        (new_dir / "projects" / "cwd" / f"{session_id}.jsonl").write_text("different\n")

        result = migrate_session(
            old_config_dir=str(old_dir),
            new_config_dir=str(new_dir),
            session_id=session_id,
        )
        assert result is False


# ---------------------------------------------------------------------------
# collect_process_output_for_detection
# ---------------------------------------------------------------------------

class TestCollectProcessOutput:
    def test_combines_stderr_and_logs(self):
        result = collect_process_output_for_detection(
            "stderr line",
            ["log line 1", "log line 2"],
        )
        assert "stderr line" in result
        assert "log line 1" in result
        assert "log line 2" in result

    def test_empty_inputs(self):
        result = collect_process_output_for_detection("", [])
        assert result == ""

    def test_none_logs_filtered(self):
        result = collect_process_output_for_detection("err", [None, "content", None])
        assert "content" in result


# ---------------------------------------------------------------------------
# Dispatcher pool integration (unit-level)
# ---------------------------------------------------------------------------

class TestDispatcherPoolIntegration:
    """Test that the dispatcher correctly initializes and uses the pool."""

    @pytest.mark.asyncio
    async def test_pool_not_initialized_when_disabled(self):
        """Pool should be None when pool_enabled is False."""
        from backend.services.dispatcher import GlobalDispatcher
        from backend.services.instance_manager import InstanceManager
        from backend.services.ws_broadcaster import WebSocketBroadcaster

        mock_db = MagicMock()
        broadcaster = WebSocketBroadcaster()
        im = InstanceManager(db_factory=mock_db, broadcaster=broadcaster)
        dispatcher = GlobalDispatcher(db_factory=mock_db, instance_manager=im, broadcaster=broadcaster)
        assert dispatcher.pool is None

    def test_pool_select_returns_none_when_no_pool(self):
        from backend.services.dispatcher import GlobalDispatcher
        from backend.services.instance_manager import InstanceManager
        from backend.services.ws_broadcaster import WebSocketBroadcaster

        mock_db = MagicMock()
        broadcaster = WebSocketBroadcaster()
        im = InstanceManager(db_factory=mock_db, broadcaster=broadcaster)
        dispatcher = GlobalDispatcher(db_factory=mock_db, instance_manager=im, broadcaster=broadcaster)
        assert dispatcher._pool_select() is None
