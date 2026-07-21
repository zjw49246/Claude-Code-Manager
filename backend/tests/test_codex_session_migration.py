import os
import stat
from pathlib import Path

import pytest

from backend.services.codex_session_migration import (
    AmbiguousCodexSessionError,
    CodexSessionConflictError,
    CodexSessionNotFoundError,
    InvalidCodexSessionIdError,
    find_codex_rollout_session,
    migrate_codex_rollout_session,
)


def _write_rollout(
    codex_home: Path,
    session_id: str,
    *,
    date: tuple[str, str, str] = ("2026", "07", "21"),
    content: str = '{"type":"session_meta"}\n',
) -> Path:
    directory = codex_home / "sessions" / date[0] / date[1] / date[2]
    directory.mkdir(parents=True)
    path = directory / f"rollout-2026-07-21T10-20-30-{session_id}.jsonl"
    path.write_text(content, encoding="utf-8")
    return path


def test_migrate_copies_only_rollout_at_same_relative_path(tmp_path: Path):
    session_id = "019f0000-aaaa-bbbb-cccc-000000000001"
    source_home = tmp_path / "source-account"
    target_home = tmp_path / "target-account"
    source = _write_rollout(source_home, session_id)
    source.chmod(0o640)
    (source_home / "auth.json").write_text('{"secret":"must-not-copy"}', encoding="utf-8")

    target = migrate_codex_rollout_session(session_id, source_home, target_home)

    expected = target_home / source.relative_to(source_home)
    assert target == expected.resolve()
    assert target.read_bytes() == source.read_bytes()
    assert source.exists()
    assert not source.samefile(target)
    assert not (target_home / "auth.json").exists()
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    for directory in [
        target_home,
        target_home / "sessions",
        target_home / "sessions" / "2026",
        target_home / "sessions" / "2026" / "07",
        target.parent,
    ]:
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700


def test_existing_identical_target_is_idempotent_and_not_replaced(tmp_path: Path):
    session_id = "019f0000-aaaa-bbbb-cccc-000000000002"
    source_home = tmp_path / "source"
    target_home = tmp_path / "target"
    source = _write_rollout(source_home, session_id, content="same\n")
    target = _write_rollout(target_home, session_id, content="same\n")
    target_inode = target.stat().st_ino
    target.chmod(0o640)

    result = migrate_codex_rollout_session(session_id, source_home, target_home)

    assert result == target.resolve()
    assert result.stat().st_ino == target_inode
    assert stat.S_IMODE(result.stat().st_mode) == 0o640
    assert not source.samefile(result)


def test_round_trip_updates_old_target_atomically_with_backup(tmp_path: Path):
    session_id = "019f0000-aaaa-bbbb-cccc-000000000003"
    home_a = tmp_path / "account-a"
    home_b = tmp_path / "account-b"
    rollout_a = _write_rollout(home_a, session_id, content="turn-1\n")

    rollout_b = migrate_codex_rollout_session(session_id, home_a, home_b)
    with rollout_b.open("a", encoding="utf-8") as stream:
        stream.write("turn-2\n")
    old_a_inode = rollout_a.stat().st_ino

    result = migrate_codex_rollout_session(session_id, home_b, home_a)

    backup = rollout_a.with_name(rollout_a.name + ".pre-migration.bak")
    assert result == rollout_a.resolve()
    assert result.read_text(encoding="utf-8") == "turn-1\nturn-2\n"
    assert result.stat().st_ino != old_a_inode
    assert backup.read_text(encoding="utf-8") == "turn-1\n"
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    assert rollout_b.exists()
    assert not rollout_b.samefile(result)
    assert not backup.samefile(result)


def test_newer_target_is_preserved_without_backup(tmp_path: Path):
    session_id = "019f0000-aaaa-bbbb-cccc-000000000006"
    source_home = tmp_path / "source"
    target_home = tmp_path / "target"
    _write_rollout(source_home, session_id, content="turn-1\n")
    target = _write_rollout(target_home, session_id, content="turn-1\nturn-2\n")
    target_inode = target.stat().st_ino

    result = migrate_codex_rollout_session(session_id, source_home, target_home)

    assert result.read_text(encoding="utf-8") == "turn-1\nturn-2\n"
    assert result.stat().st_ino == target_inode
    assert list(target.parent.glob(target.name + ".pre-migration*.bak")) == []


def test_diverged_target_is_preserved_and_reported(tmp_path: Path):
    session_id = "019f0000-aaaa-bbbb-cccc-000000000007"
    source_home = tmp_path / "source"
    target_home = tmp_path / "target"
    _write_rollout(source_home, session_id, content="common\nsource\n")
    target = _write_rollout(target_home, session_id, content="common\ntarget\n")
    before = target.read_bytes()

    with pytest.raises(CodexSessionConflictError, match="diverged content"):
        migrate_codex_rollout_session(session_id, source_home, target_home)

    assert target.read_bytes() == before


def test_missing_session_raises_explicit_error(tmp_path: Path):
    with pytest.raises(CodexSessionNotFoundError, match="not found"):
        migrate_codex_rollout_session("missing-session", tmp_path / "source", tmp_path / "target")


def test_multiple_source_rollouts_raise_ambiguous_error(tmp_path: Path):
    session_id = "019f0000-aaaa-bbbb-cccc-000000000004"
    source_home = tmp_path / "source"
    _write_rollout(source_home, session_id, date=("2026", "07", "20"))
    _write_rollout(source_home, session_id, date=("2026", "07", "21"))

    with pytest.raises(AmbiguousCodexSessionError, match="multiple rollouts"):
        find_codex_rollout_session(session_id, source_home)


@pytest.mark.parametrize("session_id", ["../escape", "bad*glob", "", "space id"])
def test_invalid_session_id_is_rejected(session_id: str, tmp_path: Path):
    with pytest.raises(InvalidCodexSessionIdError):
        migrate_codex_rollout_session(session_id, tmp_path / "source", tmp_path / "target")


def test_existing_hardlink_is_not_accepted_as_a_copy(tmp_path: Path):
    session_id = "019f0000-aaaa-bbbb-cccc-000000000005"
    source_home = tmp_path / "source"
    target_home = tmp_path / "target"
    source = _write_rollout(source_home, session_id)
    target = target_home / source.relative_to(source_home)
    target.parent.mkdir(parents=True)
    os.link(source, target)

    with pytest.raises(CodexSessionConflictError, match="aliases the source"):
        migrate_codex_rollout_session(session_id, source_home, target_home)
