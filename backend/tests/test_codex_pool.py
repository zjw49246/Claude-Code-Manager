import asyncio
import json
import os
import threading
import time
from pathlib import Path

import pytest

from backend.services import claude_pool
from backend.services import codex_pool as codex_pool_module
from backend.services.codex_pool import (
    AmbiguousCodexSessionHomeError,
    CodexPool,
    canonical_codex_home,
    is_auth_failure,
    is_pool_rotatable,
    is_rate_limited,
    is_transient,
    quota_at_or_above,
    quota_cooldown_seconds,
)


@pytest.fixture
def pool_config(tmp_path: Path) -> Path:
    config = {
        "accounts": [
            {
                "id": "codex-1",
                "codex_home": str(tmp_path / "codex-1"),
                "email": "one@example.com",
                "enabled": True,
            },
            {
                "id": "codex-2",
                "codex_home": str(tmp_path / "codex-2"),
                "email": "two@example.com",
                "enabled": True,
            },
            {
                "id": "codex-3",
                "codex_home": str(tmp_path / "codex-3"),
                "email": "three@example.com",
                "enabled": False,
            },
        ]
    }
    for account in config["accounts"][:2]:
        home = Path(account["codex_home"])
        home.mkdir(parents=True)
        (home / "auth.json").write_text(
            json.dumps({"tokens": {"access_token": "test-access-token"}}),
            encoding="utf-8",
        )
    path = tmp_path / "accounts.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return path


@pytest.fixture
def pool(pool_config: Path) -> CodexPool:
    return CodexPool(config_path=pool_config, cooldown_seconds=60)


def _rollout(home: Path, session_id: str, timestamp: str = "2026-07-21T00-00-00") -> Path:
    path = home / "sessions" / "2026" / "07" / "21" / f"rollout-{timestamp}-{session_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}\n", encoding="utf-8")
    return path


def _quota_rollout(
    home: Path,
    session_id: str,
    used_percent: int,
    *,
    event_timestamp: str | None = "2026-07-21T00:00:00Z",
    mtime: float | None = None,
) -> Path:
    path = _rollout(home, session_id)
    path.write_text(json.dumps({
        "timestamp": event_timestamp,
        "payload": {
            "type": "token_count",
            "rate_limits": {
                "primary": {
                    "used_percent": used_percent,
                    "window_minutes": 10080,
                    "resets_at": 1_800_000_000,
                },
                "secondary": None,
                "plan_type": "pro",
                "rate_limit_reached_type": (
                    "rate_limit_reached" if used_percent >= 100 else None
                ),
                "credits": {"has_credits": False},
            },
        }
    }) + "\n")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


class TestSharedDetectors:
    def test_imports_the_canonical_codex_detectors(self):
        assert (
            codex_pool_module.is_codex_usage_limited
            is claude_pool.is_codex_usage_limited
        )
        assert codex_pool_module.is_codex_auth_failure is claude_pool.is_codex_auth_failure
        assert codex_pool_module.is_codex_transient is claude_pool.is_codex_transient

    def test_compatibility_aliases_keep_failure_classes_mutually_exclusive(self):
        assert is_rate_limited("You have hit your usage limit")
        assert is_auth_failure("The refresh token was revoked")
        assert is_transient("request timed out")
        assert is_pool_rotatable("You have hit your usage limit")
        assert is_pool_rotatable("The refresh token was revoked")
        assert not is_pool_rotatable("request timed out")


class TestSelection:
    def test_round_robin_skips_disabled_accounts(self, pool: CodexPool, tmp_path: Path):
        assert pool.select() == str((tmp_path / "codex-1").resolve())
        assert pool.select() == str((tmp_path / "codex-2").resolve())
        assert pool.select() == str((tmp_path / "codex-1").resolve())

    def test_preferred_stays_pinned_while_available(self, pool: CodexPool, tmp_path: Path):
        assert pool.set_preferred("codex-2")
        expected = str((tmp_path / "codex-2").resolve())
        assert pool.select() == expected
        assert pool.select() == expected

    def test_preferred_respects_exclude_and_round_robin_fallback(
        self, pool: CodexPool, tmp_path: Path
    ):
        pool.set_preferred("codex-2")
        assert pool.select() == str((tmp_path / "codex-2").resolve())
        assert pool.select(exclude={"codex-2"}) == str((tmp_path / "codex-1").resolve())

    def test_preferred_respects_cooldown(self, pool: CodexPool, tmp_path: Path):
        pool.set_preferred("codex-2")
        pool.mark_rate_limited(str(tmp_path / "codex-2"))
        assert pool.select() == str((tmp_path / "codex-1").resolve())

    def test_returns_none_when_every_enabled_account_is_unavailable(
        self, pool: CodexPool, tmp_path: Path
    ):
        pool.mark_rate_limited(str(tmp_path / "codex-1"))
        pool.mark_rate_limited(str(tmp_path / "codex-2"))
        assert pool.select() is None


class TestAccountHomeHelpers:
    def test_canonical_home_resolves_alias_and_drives_lookup(
        self, pool_config: Path, tmp_path: Path
    ):
        real_home = tmp_path / "real-codex-home"
        real_home.mkdir()
        alias = tmp_path / "codex-home-alias"
        alias.symlink_to(real_home, target_is_directory=True)
        pool_config.write_text(
            json.dumps(
                {
                    "accounts": [
                        {
                            "id": "aliased",
                            "codex_home": str(alias),
                            "enabled": True,
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        pool = CodexPool(config_path=pool_config)

        canonical = str(real_home.resolve())
        assert canonical_codex_home(alias) == canonical
        assert pool.canonical_home(alias) == canonical
        assert pool.account_for_home(alias).id == "aliased"
        assert pool.account_id_for_home(real_home) == "aliased"
        assert pool.account_id_from_codex_home(alias) == "aliased"
        assert pool.home_for_account("aliased") == canonical
        assert pool.is_known_account(alias)

    def test_home_state_distinguishes_enabled_disabled_and_cooled(
        self, pool: CodexPool, tmp_path: Path
    ):
        enabled = tmp_path / "codex-1"
        disabled = tmp_path / "codex-3"
        unknown = tmp_path / "unknown"

        assert pool.is_home_enabled(enabled)
        assert pool.is_home_available(enabled)
        assert pool.is_disabled(disabled)
        assert not pool.is_home_available(disabled)
        assert pool.home_status(unknown) is None
        assert not pool.is_known_account(unknown)

        pool.mark_rate_limited(str(enabled), duration=60)
        assert pool.is_in_cooldown(str(enabled))
        assert not pool.is_home_available(enabled)
        assert pool.home_status(enabled)["cooldown_remaining"] > 0


class TestSessionLookup:
    def test_locates_unique_registered_account_home(
        self, pool: CodexPool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("HOME", str(tmp_path))
        _rollout(tmp_path / "codex-2", "thread-123")

        expected = str((tmp_path / "codex-2").resolve())
        assert pool.locate_session_homes("thread-123") == [expected]
        assert pool.locate_session_home("thread-123") == expected

    def test_finds_orphaned_account_home_on_disk(
        self, pool: CodexPool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("HOME", str(tmp_path))
        orphan = tmp_path / ".codex-retired"
        _rollout(orphan, "thread-orphan")

        assert pool.locate_session_home("thread-orphan") == str(orphan.resolve())

    def test_multiple_rollout_copies_are_reported_and_single_lookup_raises(
        self, pool: CodexPool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("HOME", str(tmp_path))
        first = tmp_path / "codex-1"
        second = tmp_path / "codex-2"
        _rollout(first, "thread-copied")
        _rollout(second, "thread-copied")

        expected = [str(first.resolve()), str(second.resolve())]
        assert pool.locate_session_homes("thread-copied") == expected
        with pytest.raises(AmbiguousCodexSessionHomeError) as exc_info:
            pool.locate_session_home("thread-copied")
        assert exc_info.value.homes == expected

    def test_extra_home_alias_is_canonicalized_and_deduplicated(
        self, pool: CodexPool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("HOME", str(tmp_path))
        account_home = tmp_path / "codex-1"
        account_home.mkdir(exist_ok=True)
        alias = tmp_path / "alias"
        alias.symlink_to(account_home, target_is_directory=True)
        _rollout(account_home, "thread-alias")

        assert pool.locate_session_homes(
            "thread-alias", extra_homes=[str(alias)]
        ) == [str(account_home.resolve())]

    def test_invalid_session_id_is_rejected(self, pool: CodexPool):
        with pytest.raises(ValueError):
            pool.locate_session_homes("../auth")


class TestReload:
    def test_clears_removed_runtime_references_and_quota_cache(
        self, pool: CodexPool, pool_config: Path
    ):
        pool.set_preferred("codex-2")
        pool.select()
        pool._cooldowns["removed"] = time.time() + 60
        pool._quota_cache = {"codex-2": {"quota": "stale"}}
        pool._quota_cache_at = time.time()
        pool._quota_cache_live_until = time.time() + 60
        pool._selection_quota_cache = {"codex-2": {"quota": "stale"}}

        existing = json.loads(pool_config.read_text(encoding="utf-8"))["accounts"]
        pool_config.write_text(
            json.dumps({"accounts": [a for a in existing if a["id"] != "codex-2"]}),
            encoding="utf-8",
        )
        pool.reload()

        assert pool.preferred_account_id is None
        assert pool.status()["last_selected"] is None
        assert "removed" not in pool._cooldowns
        assert pool._quota_cache is None
        assert pool._quota_cache_at == 0.0
        assert pool._quota_cache_live_until == 0.0
        assert pool._selection_quota_cache is None

    def test_keeps_valid_preferred_but_still_invalidates_quota_cache(
        self, pool: CodexPool
    ):
        pool.set_preferred("codex-1")
        pool._quota_cache = {"codex-1": {"quota": "stale"}}
        pool._quota_cache_at = time.time()
        pool._selection_quota_cache = {"codex-1": {"quota": "stale"}}

        pool.reload()

        assert pool.preferred_account_id == "codex-1"
        assert pool._quota_cache is None
        assert pool._quota_cache_at == 0.0
        assert pool._selection_quota_cache is None


def test_duplicate_canonical_homes_are_rejected(tmp_path: Path):
    shared = tmp_path / "shared-home"
    config = tmp_path / "duplicate-homes.json"
    config.write_text(json.dumps({"accounts": [
        {"id": "codex-1", "codex_home": str(shared)},
        {"id": "codex-2", "codex_home": str(shared / ".." / "shared-home")},
    ]}))

    pool = CodexPool(config_path=config)

    assert pool.list_accounts() == []


@pytest.mark.asyncio
async def test_fetch_quota_tracks_each_account_from_its_own_latest_rollout(
    pool: CodexPool, tmp_path: Path,
):
    for account_id, used in (("codex-1", 37), ("codex-2", 82)):
        rollout = _rollout(tmp_path / account_id, f"quota-{account_id}")
        rollout.write_text(json.dumps({
            "timestamp": (
                "2026-07-21T00:00:01Z"
                if account_id == "codex-1"
                else "2026-07-21T00:00:02Z"
            ),
            "payload": {
                "type": "token_count",
                "rate_limits": {
                    "primary": {
                        "used_percent": used,
                        "window_minutes": 300,
                        "resets_at": 1_800_000_000,
                    },
                    "secondary": {
                        "used_percent": used // 2,
                        "window_minutes": 10080,
                        "resets_at": 1_800_100_000,
                    },
                    "plan_type": "pro",
                    "rate_limit_reached_type": None,
                    "credits": {"has_credits": True},
                },
            }
        }) + "\n")

    result = {item["id"]: item for item in await pool.fetch_quota(force=True)}

    assert result["codex-1"]["quota"]["primary_used_percent"] == 37
    assert result["codex-2"]["quota"]["primary_used_percent"] == 82
    assert result["codex-2"]["quota"]["secondary_window_minutes"] == 10080
    assert result["codex-1"]["plan_type"] == "pro"
    assert result["codex-1"]["error"] is None


@pytest.mark.asyncio
async def test_fetch_quota_falls_back_to_older_rollout_with_rate_limits(
    pool: CodexPool, tmp_path: Path,
):
    _quota_rollout(
        tmp_path / "codex-1", "older-with-quota", 100, mtime=100,
    )
    newest = _rollout(tmp_path / "codex-1", "newest-without-quota")
    newest.write_text('{"payload":{"type":"task_started"}}\nnot-json\n')
    os.utime(newest, (200, 200))

    result = {item["id"]: item for item in await pool.fetch_quota(force=True)}

    assert result["codex-1"]["quota"]["primary_used_percent"] == 100
    assert result["codex-1"]["quota"]["is_rate_limited"] is True
    assert result["codex-1"]["error"] is None


@pytest.mark.asyncio
async def test_rollout_quota_uses_event_timestamp_instead_of_file_mtime(
    pool: CodexPool, tmp_path: Path,
):
    _quota_rollout(
        tmp_path / "codex-1",
        "actual-latest-event",
        100,
        event_timestamp="2026-07-22T07:15:00Z",
        mtime=100,
    )
    # A migrated session copy can have a fresh mtime but carry an older event.
    _quota_rollout(
        tmp_path / "codex-1",
        "newer-migrated-file",
        3,
        event_timestamp="2026-07-22T06:00:00Z",
        mtime=200,
    )

    result = {item["id"]: item for item in await pool.fetch_quota(force=True)}

    assert result["codex-1"]["quota"]["primary_used_percent"] == 100
    assert result["codex-1"]["quota"]["is_rate_limited"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("event_timestamp", [None, "not-a-timestamp"])
async def test_rollout_quota_falls_back_to_mtime_for_legacy_event_timestamp(
    pool: CodexPool, tmp_path: Path, event_timestamp: str | None,
):
    _quota_rollout(
        tmp_path / "codex-1",
        "legacy-older-file",
        20,
        event_timestamp=event_timestamp,
        mtime=100,
    )
    _quota_rollout(
        tmp_path / "codex-1",
        "legacy-newer-file",
        40,
        event_timestamp=event_timestamp,
        mtime=200,
    )

    result = {item["id"]: item for item in await pool.fetch_quota(force=True)}

    assert result["codex-1"]["quota"]["primary_used_percent"] == 40


@pytest.mark.asyncio
async def test_rollout_quota_ignores_events_before_account_activation_cutoff(
    tmp_path: Path,
):
    home = tmp_path / "codex-1"
    home.mkdir()
    config = tmp_path / "accounts.json"
    config.write_text(json.dumps({"accounts": [{
        "id": "codex-1",
        "codex_home": str(home),
        "email": "replacement@example.com",
        "enabled": True,
        "quota_valid_after": 200.0,
    }]}))
    _quota_rollout(
        home, "previous-identity", 100,
        event_timestamp=100.0,
        mtime=1_000.0,
    )
    _quota_rollout(
        home, "legacy-with-fresh-mtime", 99,
        event_timestamp="not-a-timestamp",
        mtime=2_000.0,
    )
    pool = CodexPool(config_path=config)

    before_new_turn = {
        item["id"]: item for item in await pool.fetch_quota(force=True)
    }

    assert before_new_turn["codex-1"]["quota"] is None
    assert before_new_turn["codex-1"]["error"] == "no_rollout_data"

    _quota_rollout(
        home, "replacement-identity", 12,
        event_timestamp=201.0,
        mtime=50.0,
    )
    after_new_turn = {
        item["id"]: item for item in await pool.fetch_quota(force=True)
    }

    assert after_new_turn["codex-1"]["quota"]["primary_used_percent"] == 12


@pytest.mark.parametrize("event_timestamp", [None, float("nan"), float("inf"), 200.0])
def test_rollout_quota_cutoff_rejects_missing_nonfinite_and_equal_timestamp(
    tmp_path: Path, event_timestamp,
):
    home = tmp_path / "codex-1"
    _quota_rollout(
        home, "not-strictly-after", 100,
        event_timestamp=event_timestamp,
        mtime=10_000.0,
    )

    quota = codex_pool_module._read_quota_from_rollout(
        str(home), min_event_timestamp=200.0,
    )

    assert quota is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_cutoff",
    [None, "bad", True, 0, float("nan"), float("inf"), 10**1000],
)
async def test_explicit_invalid_quota_cutoff_fails_closed(
    tmp_path: Path, invalid_cutoff,
):
    home = tmp_path / "codex-1"
    _quota_rollout(home, "old-identity", 100, event_timestamp=300.0)
    config = tmp_path / "accounts.json"
    config.write_text(json.dumps({"accounts": [{
        "id": "codex-1", "codex_home": str(home),
        "email": "replacement@example.com", "enabled": True,
        "quota_valid_after": invalid_cutoff,
    }]}))
    pool = CodexPool(config_path=config)

    result = {item["id"]: item for item in await pool.fetch_quota(force=True)}

    assert result["codex-1"]["quota"] is None
    assert result["codex-1"]["error"] == "invalid_quota_cutoff"


@pytest.mark.asyncio
async def test_rollout_scan_skips_newer_snapshot_without_usage_data(
    pool: CodexPool, tmp_path: Path,
):
    rollout = _quota_rollout(
        tmp_path / "codex-1", "empty-tail", 64,
        event_timestamp="2026-07-22T06:00:00Z",
    )
    with rollout.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({
            "timestamp": "2026-07-22T07:00:00Z",
            "payload": {
                "type": "token_count",
                "rate_limits": {
                    "primary": {"window_minutes": 300},
                    "plan_type": "pro",
                },
            },
        }) + "\n")

    result = {item["id"]: item for item in await pool.fetch_quota(force=True)}

    assert result["codex-1"]["quota"]["primary_used_percent"] == 64


@pytest.mark.asyncio
async def test_live_quota_refresh_prefers_account_rpc_and_maps_camel_case(
    pool_config: Path, tmp_path: Path,
):
    _quota_rollout(tmp_path / "codex-1", "stale", 10)
    calls: list[str] = []

    async def live_reader(codex_home: str) -> dict:
        calls.append(codex_home)
        used = 100 if codex_home.endswith("codex-1") else 25
        snapshot = {
            "limitId": "codex",
            "primary": {
                "usedPercent": used,
                "windowDurationMins": 300,
                "resetsAt": 1_800_000_100,
            },
            "secondary": {
                "usedPercent": 91,
                "windowDurationMins": 10080,
                "resetsAt": 1_800_100_000,
            },
            "planType": "pro",
            "rateLimitReachedType": (
                "rate_limit_reached" if used >= 100 else None
            ),
            "credits": {"hasCredits": True, "unlimited": False},
        }
        if used == 25:
            return {
                "rateLimits": {},
                "rateLimitsByLimitId": {"codex": snapshot},
            }
        return {"rateLimits": snapshot}

    live_pool = CodexPool(
        config_path=pool_config,
        cooldown_seconds=60,
        quota_reader=live_reader,
    )
    result = {
        item["id"]: item
        for item in await live_pool.fetch_quota(force=True, live=True)
    }

    first = result["codex-1"]["quota"]
    assert first == {
        "primary_used_percent": 100,
        "primary_window_minutes": 300,
        "primary_resets_at": 1_800_000_100,
        "secondary_used_percent": 91,
        "secondary_window_minutes": 10080,
        "secondary_resets_at": 1_800_100_000,
        "plan_type": "pro",
        "is_rate_limited": True,
        "has_credits": True,
    }
    assert result["codex-1"]["error"] is None
    assert result["codex-2"]["quota"]["primary_used_percent"] == 25
    assert set(calls) == {
        str((tmp_path / "codex-1").resolve()),
        str((tmp_path / "codex-2").resolve()),
    }


@pytest.mark.asyncio
async def test_quota_fetch_discards_result_started_before_pool_reload(
    tmp_path: Path,
):
    home = tmp_path / "codex-1"
    home.mkdir()
    config_path = tmp_path / "accounts.json"
    config_path.write_text(json.dumps({"accounts": [{
        "id": "codex-1", "codex_home": str(home),
        "email": "old@example.com", "enabled": True,
    }]}))
    first_read_started = asyncio.Event()
    release_first_read = asyncio.Event()
    calls = 0

    async def live_reader(_codex_home: str) -> dict:
        nonlocal calls
        calls += 1
        if calls == 1:
            first_read_started.set()
            await release_first_read.wait()
            used = 100
        else:
            used = 7
        return {"rateLimits": {
            "primary": {
                "usedPercent": used,
                "windowDurationMins": 300,
                "resetsAt": 1_800_000_100,
            },
            "planType": "pro",
        }}

    pool = CodexPool(config_path=config_path, quota_reader=live_reader)
    fetch_task = asyncio.create_task(
        pool.fetch_quota(force=True, live=True)
    )
    await first_read_started.wait()
    config_path.write_text(json.dumps({"accounts": [{
        "id": "codex-1", "codex_home": str(home),
        "email": "replacement@example.com", "enabled": True,
        "quota_valid_after": time.time(),
    }]}))
    pool.reload()
    release_first_read.set()

    result = await fetch_task

    assert calls == 2
    assert result[0]["email"] == "replacement@example.com"
    assert result[0]["quota"]["primary_used_percent"] == 7
    assert pool._quota_cache["codex-1"]["quota"]["primary_used_percent"] == 7


@pytest.mark.asyncio
async def test_background_quota_fetch_restarts_after_pool_reload(
    monkeypatch, tmp_path: Path,
):
    home = tmp_path / "codex-1"
    home.mkdir()
    config_path = tmp_path / "accounts.json"
    config_path.write_text(json.dumps({"accounts": [{
        "id": "codex-1", "codex_home": str(home),
        "email": "old@example.com", "enabled": True,
    }]}))
    first_read_started = threading.Event()
    release_first_read = threading.Event()
    cutoffs: list[float | None] = []

    def rollout_reader(
        _codex_home: str, *, min_event_timestamp: float | None = None,
    ) -> dict:
        cutoffs.append(min_event_timestamp)
        if len(cutoffs) == 1:
            first_read_started.set()
            assert release_first_read.wait(timeout=5)
            used = 100
        else:
            used = 8
        return {
            "primary_used_percent": used,
            "primary_window_minutes": 300,
            "plan_type": "pro",
            "is_rate_limited": used >= 100,
        }

    monkeypatch.setattr(
        codex_pool_module, "_read_quota_from_rollout", rollout_reader,
    )
    pool = CodexPool(config_path=config_path)
    fetch_task = asyncio.create_task(pool.fetch_quota(force=True))
    assert await asyncio.to_thread(first_read_started.wait, 2)
    replacement_cutoff = time.time()
    config_path.write_text(json.dumps({"accounts": [{
        "id": "codex-1", "codex_home": str(home),
        "email": "replacement@example.com", "enabled": True,
        "quota_valid_after": replacement_cutoff,
    }]}))
    pool.reload()
    release_first_read.set()

    result = await fetch_task

    assert cutoffs == [None, replacement_cutoff]
    assert result[0]["email"] == "replacement@example.com"
    assert result[0]["quota"]["primary_used_percent"] == 8
    assert pool._quota_cache["codex-1"]["quota"]["primary_used_percent"] == 8


@pytest.mark.asyncio
async def test_live_quota_failure_never_uses_migrated_rollout_history(
    pool_config: Path, tmp_path: Path,
):
    _quota_rollout(tmp_path / "codex-1", "fallback", 77)
    _quota_rollout(tmp_path / "codex-2", "copied-history", 88)

    async def live_reader(codex_home: str) -> dict:
        if codex_home.endswith("codex-1"):
            raise RuntimeError("live RPC unavailable")
        return {"rateLimits": {}}

    live_pool = CodexPool(
        config_path=pool_config,
        quota_reader=live_reader,
    )
    result = {
        item["id"]: item
        for item in await live_pool.fetch_quota(live=True)
    }

    assert result["codex-1"]["quota"] is None
    assert result["codex-1"]["error"] == "live_unavailable"
    assert result["codex-2"]["quota"] is None
    assert result["codex-2"]["error"] == "live_unavailable"


@pytest.mark.asyncio
async def test_live_quota_failure_without_history_reports_clear_error(
    pool_config: Path,
):
    async def unavailable(_codex_home: str) -> dict:
        raise RuntimeError("account RPC unavailable")

    live_pool = CodexPool(config_path=pool_config, quota_reader=unavailable)
    result = await live_pool.fetch_quota(live=True)

    assert {item["error"] for item in result} == {"live_unavailable"}
    assert all(item["quota"] is None for item in result)


@pytest.mark.asyncio
async def test_background_scan_cannot_overwrite_recent_live_quota_cache(
    pool_config: Path, tmp_path: Path,
):
    async def live_reader(_codex_home: str) -> dict:
        return {"rateLimits": {"primary": {"usedPercent": 100}}}

    live_pool = CodexPool(config_path=pool_config, quota_reader=live_reader)
    live = {
        item["id"]: item for item in await live_pool.fetch_quota(live=True)
    }
    _quota_rollout(tmp_path / "codex-1", "later-background", 20)

    background = {
        item["id"]: item
        for item in await live_pool.fetch_quota(force=True)
    }
    cached = {item["id"]: item for item in await live_pool.fetch_quota()}

    assert live["codex-1"]["quota"]["primary_used_percent"] == 100
    assert background["codex-1"]["quota"]["primary_used_percent"] == 20
    assert cached["codex-1"]["quota"]["primary_used_percent"] == 100


@pytest.mark.asyncio
async def test_selection_snapshot_survives_failed_live_ui_cache(
    pool_config: Path, tmp_path: Path,
):
    async def live_reader(codex_home: str) -> dict:
        if codex_home.endswith("codex-1"):
            return {"rateLimits": {}}
        return {"rateLimits": {"primary": {"usedPercent": 12}}}

    live_pool = CodexPool(config_path=pool_config, quota_reader=live_reader)
    live = {
        item["id"]: item for item in await live_pool.fetch_quota(live=True)
    }
    assert live["codex-1"]["quota"] is None

    _quota_rollout(tmp_path / "codex-1", "current-high", 95)
    _quota_rollout(tmp_path / "codex-2", "alternative-low", 20)

    selected = await live_pool.select_quota_alternative(
        str(tmp_path / "codex-1")
    )
    selection_quota = live_pool.cached_quota_for_home(
        str(tmp_path / "codex-1")
    )
    still_live = {
        item["id"]: item for item in await live_pool.fetch_quota()
    }

    assert selected == str((tmp_path / "codex-2").resolve())
    assert selection_quota["primary_used_percent"] == 95
    assert selection_quota["primary_resets_at"] == 1_800_000_000
    assert still_live["codex-1"]["quota"] is None
    assert still_live["codex-1"]["error"] == "live_unavailable"


@pytest.mark.asyncio
async def test_background_force_refresh_does_not_start_live_account_readers(
    pool_config: Path, tmp_path: Path,
):
    _quota_rollout(tmp_path / "codex-1", "background", 64)

    async def unexpected_live_reader(_codex_home: str) -> dict:
        raise AssertionError("background refresh must stay rollout-only")

    background_pool = CodexPool(
        config_path=pool_config,
        quota_reader=unexpected_live_reader,
    )
    result = {
        item["id"]: item
        for item in await background_pool.fetch_quota(force=True)
    }

    assert result["codex-1"]["quota"]["primary_used_percent"] == 64


@pytest.mark.asyncio
@pytest.mark.parametrize("force", [False, True])
async def test_usage_api_only_requests_live_quota_for_explicit_force(
    monkeypatch: pytest.MonkeyPatch, force: bool,
):
    from unittest.mock import AsyncMock

    from backend.api import codex_pool as codex_pool_api

    fake_pool = type("FakePool", (), {})()
    fake_pool.status = lambda: {"accounts": []}
    fake_pool.fetch_quota = AsyncMock(return_value=[])
    monkeypatch.setattr(codex_pool_api, "_get_pool", lambda: fake_pool)

    assert await codex_pool_api.codex_pool_usage(force=force) == {"accounts": []}
    fake_pool.fetch_quota.assert_awaited_once_with(force=force, live=force)


class TestQuotaAwareSelection:
    def test_threshold_checks_primary_or_secondary(self):
        assert quota_at_or_above({"primary_used_percent": 90})
        assert quota_at_or_above({"secondary_used_percent": 91})
        assert not quota_at_or_above({
            "primary_used_percent": 89.9,
            "secondary_used_percent": 40,
        })

    def test_cooldown_uses_later_reset_of_all_high_windows(self):
        now = 1_700_000_000
        assert quota_cooldown_seconds({
            "primary_used_percent": 95,
            "primary_resets_at": now + 300,
            "secondary_used_percent": 90,
            "secondary_resets_at": (now + 7200) * 1000,
        }, now=now) == 7200

        # A low weekly window does not extend a high 5-hour window's cooldown.
        assert quota_cooldown_seconds({
            "primary_used_percent": 95,
            "primary_resets_at": now + 300,
            "secondary_used_percent": 89,
            "secondary_resets_at": now + 7200,
        }, now=now) == 300

    @pytest.mark.asyncio
    async def test_current_high_selects_low_alternative(
        self, pool: CodexPool, tmp_path: Path,
    ):
        async def quota(force=False):
            assert force is True
            return [
                {"id": "codex-1", "quota": {"primary_used_percent": 95}},
                {"id": "codex-2", "quota": {"secondary_used_percent": 30}},
            ]

        pool.fetch_quota = quota
        assert await pool.select_quota_alternative(
            str(tmp_path / "codex-1")
        ) == str((tmp_path / "codex-2").resolve())

    @pytest.mark.asyncio
    async def test_below_threshold_or_no_low_alternative_keeps_current(
        self, pool: CodexPool, tmp_path: Path,
    ):
        async def below(force=False):
            return [
                {"id": "codex-1", "quota": {"primary_used_percent": 89}},
                {"id": "codex-2", "quota": {"primary_used_percent": 20}},
            ]

        pool.fetch_quota = below
        assert await pool.select_quota_alternative(
            str(tmp_path / "codex-1")
        ) is None

        async def all_high(force=False):
            return [
                {"id": "codex-1", "quota": {"primary_used_percent": 95}},
                {"id": "codex-2", "quota": {"secondary_used_percent": 90}},
            ]

        pool.fetch_quota = all_high
        assert await pool.select_quota_alternative(
            str(tmp_path / "codex-1")
        ) is None
        assert not pool.is_in_cooldown(str(tmp_path / "codex-1"))

    @pytest.mark.asyncio
    async def test_unknown_alternative_is_eligible(
        self, pool: CodexPool, tmp_path: Path,
    ):
        async def quota(force=False):
            return [
                {"id": "codex-1", "quota": {"primary_used_percent": 95}},
                {"id": "codex-2", "quota": None, "error": "no_rollout_data"},
            ]

        pool.fetch_quota = quota
        assert await pool.select_quota_alternative(
            str(tmp_path / "codex-1")
        ) == str((tmp_path / "codex-2").resolve())

    @pytest.mark.asyncio
    async def test_explicitly_logged_out_alternative_is_rejected(
        self, pool: CodexPool, tmp_path: Path,
    ):
        (tmp_path / "codex-2" / "auth.json").unlink()

        async def quota(force=False):
            return [
                {"id": "codex-1", "quota": {"primary_used_percent": 95}},
                {"id": "codex-2", "quota": {"primary_used_percent": 10}},
            ]

        pool.fetch_quota = quota
        assert await pool.select_quota_alternative(
            str(tmp_path / "codex-1")
        ) is None
