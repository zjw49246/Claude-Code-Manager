"""Regression tests for session recovery on resume (prod task #725).

Symptom: a follow-up message that resumed a session living under a pool
account dir hard-failed the task on the FIRST message ("No conversation
found"), and only the SECOND message recovered — because both the on-disk
lookup and the recovery clone only searched ~/.claude / CLAUDE_CONFIG_DIR
(with the exact last_cwd encoding), missing pool-account-resident sessions.

These tests pin the pool-aware lookup (`_find_session_jsonl`) that lets the
first message detect the gone/relocated session and clone the real JSONL.
"""
import json
import types
from pathlib import Path

import pytest

from backend.services.claude_pool import ClaudePool
from backend.api.tasks import _find_session_jsonl, _clone_session
from backend.models.task import Task


def _write_session(config_dir: Path, encoded_cwd: str, session_id: str) -> Path:
    proj = config_dir / "projects" / encoded_cwd
    proj.mkdir(parents=True, exist_ok=True)
    jsonl = proj / f"{session_id}.jsonl"
    jsonl.write_text('{"type":"summary"}\n')
    return jsonl


@pytest.fixture
def pool_with_dirs(tmp_path):
    config = {
        "accounts": [
            {"id": "acc-1", "config_dir": str(tmp_path / "claude-1"), "email": "a@test.com", "enabled": True},
            {"id": "acc-2", "config_dir": str(tmp_path / "claude-2"), "email": "b@test.com", "enabled": True},
        ],
    }
    config_path = tmp_path / "accounts.json"
    config_path.write_text(json.dumps(config))
    return ClaudePool(config_path=config_path, cooldown_seconds=60)


@pytest.fixture
def patched_dispatcher(monkeypatch, pool_with_dirs):
    """Point backend.main.dispatcher.pool at our tmp pool for _find_session_jsonl."""
    import backend.main as main_mod
    monkeypatch.setattr(main_mod, "dispatcher", types.SimpleNamespace(pool=pool_with_dirs), raising=False)
    return pool_with_dirs


class TestFindSessionJsonl:
    def test_finds_session_under_pool_account_dir(self, tmp_path, patched_dispatcher, monkeypatch):
        # session lives ONLY under pool account-2, NOT under ~/.claude / env dir
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "nonexistent-default"))
        jsonl = _write_session(tmp_path / "claude-2", "-Users-matter-repo", "sid-pool")
        assert _find_session_jsonl("sid-pool") == jsonl

    def test_returns_none_when_session_absent(self, tmp_path, patched_dispatcher, monkeypatch):
        # This is exactly the "session_gone" trigger that makes the first message recover.
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "nonexistent-default"))
        assert _find_session_jsonl("ghost-sid") is None

    def test_globs_across_project_subdirs_without_pool(self, tmp_path, monkeypatch):
        # No pool → fall back to CLAUDE_CONFIG_DIR, but still find the session even
        # though the project subdir name matches no last_cwd encoding (old code
        # required the exact encoded subdir and would have missed it).
        import backend.main as main_mod
        monkeypatch.setattr(main_mod, "dispatcher", None, raising=False)
        cfg = tmp_path / "default"
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
        jsonl = _write_session(cfg, "-unrelated-encoding", "sid-x")
        assert _find_session_jsonl("sid-x") == jsonl

    def test_finds_codex_rollout_under_codex_home(self, tmp_path, monkeypatch):
        """Regression: every valid Codex follow-up was treated as session_gone."""
        import backend.main as main_mod
        monkeypatch.setattr(main_mod, "codex_pool", None, raising=False)
        monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
        sid = "019f7991-3ef9-75a1-8441-8bfd420ab006"
        day = tmp_path / "codex-home" / "sessions" / "2026" / "07" / "19"
        day.mkdir(parents=True)
        rollout = day / f"rollout-2026-07-19T08-49-49-{sid}.jsonl"
        rollout.write_text('{"type":"session_meta"}\n')

        assert _find_session_jsonl(sid, provider="codex") == rollout

    def test_finds_codex_rollout_under_pool_account_home(self, tmp_path, monkeypatch):
        import backend.main as main_mod

        pool_home = tmp_path / "pool-account-2"
        sid = "019f7991-3ef9-75a1-8441-8bfd420ab007"
        day = pool_home / "sessions" / "2026" / "07" / "20"
        day.mkdir(parents=True)
        rollout = day / f"rollout-2026-07-20T08-49-49-{sid}.jsonl"
        rollout.write_text('{"type":"session_meta"}\n')
        pool = types.SimpleNamespace(
            list_accounts=lambda: [
                {"codex_home": str(tmp_path / "pool-account-1")},
                {"codex_home": str(pool_home)},
            ]
        )
        monkeypatch.setattr(main_mod, "codex_pool", pool, raising=False)
        monkeypatch.setenv("CODEX_HOME", str(tmp_path / "missing-default"))

        assert _find_session_jsonl(sid, provider="codex") == rollout

    def test_finds_codex_rollout_under_legacy_codex_star_home(self, tmp_path, monkeypatch):
        import backend.main as main_mod

        monkeypatch.setattr(main_mod, "codex_pool", None, raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("CODEX_HOME", raising=False)
        sid = "019f7991-3ef9-75a1-8441-8bfd420ab008"
        day = tmp_path / ".codex-account-old" / "sessions" / "2026" / "07" / "20"
        day.mkdir(parents=True)
        rollout = day / f"rollout-2026-07-20T09-49-49-{sid}.jsonl"
        rollout.write_text('{"type":"session_meta"}\n')

        assert _find_session_jsonl(sid, provider="codex") == rollout

    def test_codex_lookup_does_not_accept_claude_session(self, tmp_path, monkeypatch):
        import backend.main as main_mod
        monkeypatch.setattr(main_mod, "codex_pool", None, raising=False)
        monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
        _write_session(tmp_path / "claude", "-repo", "same-sid")

        assert _find_session_jsonl("same-sid", provider="codex") is None


@pytest.mark.asyncio
class TestCloneSessionPoolAware:
    async def test_clone_finds_pool_resident_session(self, tmp_path, db_session, patched_dispatcher, monkeypatch):
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "nonexistent-default"))
        _write_session(tmp_path / "claude-1", "-Users-matter-repo", "orig-sid")
        task = Task(status="failed", session_id="orig-sid", last_cwd="/Users/matter/repo")
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        result = await _clone_session(task.id, db_session)
        assert result is not None
        assert result["session_id"] != "orig-sid"
        assert result["last_cwd"] == "/Users/matter/repo"
        cloned = tmp_path / "claude-1" / "projects" / "-Users-matter-repo" / f'{result["session_id"]}.jsonl'
        assert cloned.exists()

    async def test_clone_returns_none_when_session_gone(self, tmp_path, db_session, patched_dispatcher, monkeypatch):
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "nonexistent-default"))
        task = Task(status="failed", session_id="vanished-sid", last_cwd="/Users/matter/repo")
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)
        assert await _clone_session(task.id, db_session) is None

    async def test_codex_rollout_is_never_cloned_by_filename(self, tmp_path, db_session, monkeypatch):
        monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
        task = Task(
            status="failed", provider="codex", session_id="codex-sid",
            last_cwd="/repo",
        )
        db_session.add(task)
        await db_session.commit()
        await db_session.refresh(task)

        assert await _clone_session(task.id, db_session) is None
