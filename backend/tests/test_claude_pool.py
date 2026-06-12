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

    @pytest.mark.asyncio
    async def test_pool_select_returns_none_when_no_pool(self):
        from backend.services.dispatcher import GlobalDispatcher
        from backend.services.instance_manager import InstanceManager
        from backend.services.ws_broadcaster import WebSocketBroadcaster

        mock_db = MagicMock()
        broadcaster = WebSocketBroadcaster()
        im = InstanceManager(db_factory=mock_db, broadcaster=broadcaster)
        dispatcher = GlobalDispatcher(db_factory=mock_db, instance_manager=im, broadcaster=broadcaster)
        assert await dispatcher._pool_select() is None


# ---------- 2026-06-10 生产事故回归：新版 CC 限流文案未被识别 ----------

class TestRateLimitWordingVariants:
    """CC 2.1.x 的实际限流文案必须全部命中（task 79 撞限未换号的根因）。"""

    def test_session_limit_wording(self):
        from backend.services.claude_pool import is_rate_limited
        # 2026-06-10 生产实际文案
        assert is_rate_limited("You've hit your session limit · resets 5:50pm (UTC)")

    def test_weekly_limit_wording(self):
        from backend.services.claude_pool import is_rate_limited
        assert is_rate_limited("You've hit your weekly limit · resets 8am (America/Los_Angeles)")

    def test_legacy_wordings_still_match(self):
        from backend.services.claude_pool import is_rate_limited
        assert is_rate_limited("You've hit your limit")
        assert is_rate_limited("usage limit reached")
        assert is_rate_limited("resets 5pm (America/New_York)")

    def test_resets_with_minutes_any_timezone(self):
        from backend.services.claude_pool import is_rate_limited
        assert is_rate_limited("resets 11:30am (UTC)")
        assert is_rate_limited("resets 5:50pm (Asia/Shanghai)")

    def test_normal_text_not_matched(self):
        from backend.services.claude_pool import is_rate_limited
        assert not is_rate_limited("I implemented the rate limiter middleware as requested")
        assert not is_rate_limited("the function resets the counter")


# ---------- 2026-06-11 修复回归：chat 路径 pool 切换 + probe + 额度 ----------

class TestChatPoolRotationRegression:
    """回归：_try_chat_pool_rotation 曾用位置参数调用 keyword-only 的
    migrate_session，TypeError 被吞掉导致 chat 路径切换从未生效。"""

    @pytest.mark.asyncio
    async def test_chat_rotation_succeeds_and_migrates_session(self, pool, tmp_path, monkeypatch):
        from backend.services.instance_manager import InstanceManager

        # session jsonl 存在于 acc-1 的 config dir 下
        old_dir = tmp_path / "claude-1"
        proj = old_dir / "projects" / "-home-user-repo"
        proj.mkdir(parents=True)
        (proj / "sess-123.jsonl").write_text("{}")

        import backend.main
        fake_dispatcher = MagicMock()
        fake_dispatcher.pool = pool
        monkeypatch.setattr(backend.main, "dispatcher", fake_dispatcher)

        task = MagicMock()
        task.session_id = "sess-123"
        task.last_cwd = "/home/user/repo"
        task.target_repo = None

        db = AsyncMock()
        db.get = AsyncMock(return_value=task)
        ctx = MagicMock()
        ctx.__aenter__ = AsyncMock(return_value=db)
        ctx.__aexit__ = AsyncMock(return_value=False)
        db_factory = MagicMock(return_value=ctx)

        broadcaster = MagicMock()
        broadcaster.broadcast = AsyncMock()

        im = InstanceManager(db_factory=db_factory, broadcaster=broadcaster)
        im._config_dirs[1] = str(old_dir)
        im._launch_params[1] = {"prompt": "hello"}
        im.get_recent_log_contents = AsyncMock(return_value=["You've hit your limit"])
        im.launch = AsyncMock()

        ok = await im._try_chat_pool_rotation(1, 42, 1, "You've hit your limit")

        assert ok is True
        im.launch.assert_awaited_once()
        # session 已硬链接到新账号 (acc-2)
        new_jsonl = tmp_path / "claude-2" / "projects" / "-home-user-repo" / "sess-123.jsonl"
        assert new_jsonl.exists()
        assert im.launch.await_args.kwargs["config_dir"] == str(tmp_path / "claude-2")


class TestLocateSessionConfigDir:
    def test_finds_session_under_account_dir(self, pool, tmp_path):
        proj = tmp_path / "claude-2" / "projects" / "-x"
        proj.mkdir(parents=True)
        (proj / "sid-9.jsonl").write_text("{}")
        assert pool.locate_session_config_dir("sid-9") == str(tmp_path / "claude-2")

    def test_returns_none_when_not_found(self, pool):
        assert pool.locate_session_config_dir("nope") is None


class TestSelectAsync:
    @pytest.mark.asyncio
    async def test_select_async_without_validate(self, pool, tmp_path):
        result = await pool.select_async()
        assert result in [str(tmp_path / "claude-1"), str(tmp_path / "claude-2")]


class TestProbeEnvCleanup:
    def test_probe_strips_nested_session_vars(self, pool, monkeypatch):
        monkeypatch.setenv("CLAUDECODE", "1")
        monkeypatch.setenv("CLAUDE_CODE", "1")
        captured = {}

        def fake_run(cmd, *, env, **kwargs):
            captured["env"] = env
            r = MagicMock()
            r.returncode = 0
            r.stdout, r.stderr = "ok", ""
            return r

        monkeypatch.setattr("backend.services.claude_pool.subprocess.run", fake_run)
        account = pool._accounts[0]
        assert pool._probe_account(account) is True
        assert "CLAUDECODE" not in captured["env"]
        assert "CLAUDE_CODE" not in captured["env"]
        assert captured["env"]["CLAUDE_CONFIG_DIR"] == account.config_dir


class TestFetchUsage:
    def _write_creds(self, config_dir, expires_in_ms=10**15, refresh_token=None):
        config_dir.mkdir(parents=True, exist_ok=True)
        creds = {
            "accessToken": "sk-ant-oat01-test",
            "expiresAt": expires_in_ms,
            "subscriptionType": "max",
        }
        if refresh_token:
            creds["refreshToken"] = refresh_token
        (config_dir / ".credentials.json").write_text(json.dumps({"claudeAiOauth": creds}))

    def _fake_client(self, payload, status_code=200, counter=None,
                     post_payload=None, post_status=200):
        class FakeResp:
            def __init__(self, status, body):
                self.status_code = status
                self._body = body
            def json(self):
                return self._body

        class FakeClient:
            def __init__(self, *a, **k):
                pass
            async def __aenter__(self):
                return self
            async def __aexit__(self, *a):
                return False
            async def get(self, url, headers=None):
                if counter is not None:
                    counter["n"] += 1
                return FakeResp(status_code, payload)
            async def post(self, url, json=None, headers=None):
                return FakeResp(post_status, post_payload or {})

        return FakeClient

    @pytest.mark.asyncio
    async def test_fetch_usage_returns_utilization(self, pool, tmp_path, monkeypatch):
        self._write_creds(tmp_path / "claude-1")
        payload = {
            "five_hour": {"utilization": 14.0, "resets_at": "2026-06-11T06:59:59+00:00"},
            "seven_day": {"utilization": 38.0, "resets_at": "2026-06-12T17:59:59+00:00"},
        }
        monkeypatch.setattr("httpx.AsyncClient", self._fake_client(payload))
        results = await pool.fetch_usage()
        by_id = {r["id"]: r for r in results}
        assert by_id["acc-1"]["usage"]["five_hour"]["utilization"] == 14.0
        assert by_id["acc-1"]["usage"]["seven_day"]["utilization"] == 38.0
        assert by_id["acc-1"]["subscription_type"] == "max"
        # acc-2 没有 credentials 文件
        assert by_id["acc-2"]["error"] == "no_credentials"
        assert by_id["acc-2"]["usage"] is None

    @pytest.mark.asyncio
    async def test_fetch_usage_expired_token(self, pool, tmp_path, monkeypatch):
        # 无 refreshToken → 无法刷新，报 token_expired
        self._write_creds(tmp_path / "claude-1", expires_in_ms=1000)
        monkeypatch.setattr("httpx.AsyncClient", self._fake_client({}))
        results = await pool.fetch_usage()
        by_id = {r["id"]: r for r in results}
        assert by_id["acc-1"]["error"] == "token_expired"

    @pytest.mark.asyncio
    async def test_fetch_usage_expired_token_auto_refresh(self, pool, tmp_path, monkeypatch):
        # 过期但有 refreshToken → 自动刷新成功，正常返回 usage 并写回新凭证
        self._write_creds(tmp_path / "claude-1", expires_in_ms=1000, refresh_token="sk-ant-ort01-old")
        payload = {"five_hour": {"utilization": 5.0, "resets_at": None}}
        monkeypatch.setattr("httpx.AsyncClient", self._fake_client(
            payload,
            post_payload={"access_token": "sk-ant-oat01-new",
                          "refresh_token": "sk-ant-ort01-new", "expires_in": 28800},
        ))
        results = await pool.fetch_usage()
        by_id = {r["id"]: r for r in results}
        assert by_id["acc-1"]["error"] is None
        assert by_id["acc-1"]["usage"]["five_hour"]["utilization"] == 5.0
        # refresh token 轮换后必须落盘
        saved = json.loads((tmp_path / "claude-1" / ".credentials.json").read_text())["claudeAiOauth"]
        assert saved["accessToken"] == "sk-ant-oat01-new"
        assert saved["refreshToken"] == "sk-ant-ort01-new"
        assert saved["expiresAt"] / 1000 > __import__("time").time()

    @pytest.mark.asyncio
    async def test_fetch_usage_expired_token_refresh_fails(self, pool, tmp_path, monkeypatch):
        # refresh 被拒（refreshToken 失效）→ 才真正报 token_expired
        self._write_creds(tmp_path / "claude-1", expires_in_ms=1000, refresh_token="sk-ant-ort01-revoked")
        monkeypatch.setattr("httpx.AsyncClient", self._fake_client({}, post_status=401))
        results = await pool.fetch_usage()
        by_id = {r["id"]: r for r in results}
        assert by_id["acc-1"]["error"] == "token_expired"

    @pytest.mark.asyncio
    async def test_fetch_usage_cached(self, pool, tmp_path, monkeypatch):
        self._write_creds(tmp_path / "claude-1")
        counter = {"n": 0}
        payload = {"five_hour": {"utilization": 1.0, "resets_at": None}}
        monkeypatch.setattr("httpx.AsyncClient", self._fake_client(payload, counter=counter))
        await pool.fetch_usage()
        first = counter["n"]
        await pool.fetch_usage()
        assert counter["n"] == first  # 第二次走缓存


# ---------- 手动切号（preferred account）+ PTY 模式 limit 检测 ----------

class TestPreferredAccount:
    """手动切号：preferred 账号插队，不可用时回落自动轮换。"""

    def test_preferred_jumps_queue(self, pool):
        # 默认顺序会选 acc-1；置 preferred 后应选 acc-2
        assert pool.set_preferred("acc-2") is True
        assert pool.select() == pool._accounts[1].config_dir
        assert pool.status()["preferred"] == "acc-2"

    def test_preferred_falls_back_when_cooled(self, pool):
        pool.set_preferred("acc-2")
        pool.mark_rate_limited(pool._accounts[1].config_dir)
        # acc-2 冷却中 → 自动回落 acc-1
        assert pool.select() == pool._accounts[0].config_dir

    def test_preferred_respects_exclude(self, pool):
        pool.set_preferred("acc-2")
        assert pool.select(exclude={"acc-2"}) == pool._accounts[0].config_dir

    def test_clear_preferred(self, pool):
        pool.set_preferred("acc-2")
        assert pool.set_preferred(None) is True
        assert pool.status()["preferred"] is None
        assert pool.select() == pool._accounts[0].config_dir

    def test_unknown_account_rejected(self, pool):
        assert pool.set_preferred("nope") is False
        assert pool.preferred_account_id is None


class TestPtyModeRateLimitDetection:
    """PTY 模式 limit 后切号：PTY 框架以错误 message 事件结束 turn，
    其文案必须命中 is_rate_limited，使 _check_rate_limit_and_rotate 生效。"""

    def test_pty_banner_message_matches(self):
        # claude_pty session.py 在 rate-limit 时 yield 的固定文案
        msg = ("usage limit reached — account hit its rate limit "
               "(detected in PTY session)")
        assert is_rate_limited(msg)
        assert is_pool_rotatable(msg)

    def test_pty_jsonl_rate_limit_event_content(self):
        # jsonl_reader 对 rate_limit_event 的 normalize 内容
        assert is_pool_rotatable("usage limit reached") is True


class TestLastSelected:
    def test_select_records_last_selected(self, pool):
        pool.select()
        assert pool.status()["last_selected"] == "acc-1"

    def test_no_selection_yet(self, pool):
        assert pool.status()["last_selected"] is None
