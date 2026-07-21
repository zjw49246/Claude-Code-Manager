"""Safe local migration of a Codex rollout between account homes.

Codex sessions are individual rollout files under::

    CODEX_HOME/sessions/YYYY/MM/DD/rollout-<timestamp>-<session_id>.jsonl

This helper deliberately copies only that rollout file.  In particular it
never copies ``auth.json`` and never removes or hard-links the source file.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from pathlib import Path


_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_COPY_BUFFER_SIZE = 1024 * 1024


class CodexSessionMigrationError(RuntimeError):
    """Base error for a Codex rollout migration failure."""


class InvalidCodexSessionIdError(CodexSessionMigrationError):
    """The requested session ID cannot safely be used in a glob pattern."""


class CodexSessionNotFoundError(CodexSessionMigrationError):
    """No rollout for the requested session exists in the source home."""


class AmbiguousCodexSessionError(CodexSessionMigrationError):
    """More than one source rollout matched the requested session ID."""


class CodexSessionConflictError(CodexSessionMigrationError):
    """The target path already contains different or aliased content."""


def _normalise_home(codex_home: str | os.PathLike[str]) -> Path:
    try:
        return Path(codex_home).expanduser().resolve(strict=False)
    except (OSError, RuntimeError, TypeError) as exc:
        raise CodexSessionMigrationError(f"Invalid CODEX_HOME {codex_home!r}: {exc}") from exc


def _validate_session_id(session_id: str) -> None:
    if not isinstance(session_id, str) or not _SESSION_ID_RE.fullmatch(session_id):
        raise InvalidCodexSessionIdError(
            "Invalid Codex session ID; expected only letters, digits, '.', '_' or '-'"
        )


def find_codex_rollout_session(
    session_id: str,
    codex_home: str | os.PathLike[str],
) -> Path:
    """Return the unique rollout path for ``session_id`` in ``codex_home``.

    Raises a specific error when the source is missing or ambiguous instead of
    silently picking one rollout and potentially resuming the wrong history.
    """

    _validate_session_id(session_id)
    home = _normalise_home(codex_home)
    sessions_dir = home / "sessions"
    pattern = f"*/*/*/rollout-*-{session_id}.jsonl"
    matches = sorted(path for path in sessions_dir.glob(pattern) if path.is_file())

    if not matches:
        raise CodexSessionNotFoundError(
            f"Codex session {session_id!r} not found under {sessions_dir}"
        )
    if len(matches) > 1:
        locations = ", ".join(str(path) for path in matches)
        raise AmbiguousCodexSessionError(
            f"Codex session {session_id!r} has multiple rollouts: {locations}"
        )
    return matches[0]


def _ensure_private_directory(path: Path) -> None:
    """Create every missing directory in ``path`` with owner-only access."""

    missing: list[Path] = []
    cursor = path
    while not cursor.exists():
        missing.append(cursor)
        parent = cursor.parent
        if parent == cursor:
            break
        cursor = parent

    if not cursor.is_dir():
        raise CodexSessionMigrationError(f"Cannot create directory below non-directory {cursor}")

    for directory in reversed(missing):
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError:
            # A concurrent migration may have created it after the existence
            # check.  Accept only a directory, never a file or broken link.
            if not directory.is_dir():
                raise CodexSessionMigrationError(
                    f"Target path component is not a directory: {directory}"
                )
        except OSError as exc:
            raise CodexSessionMigrationError(
                f"Unable to create target directory {directory}: {exc}"
            ) from exc


def _compare_rollouts(source: Path, target: Path) -> str:
    """Describe the byte-prefix relationship between two rollout files."""

    try:
        source_size = source.stat().st_size
        target_size = target.stat().st_size
        remaining = min(source_size, target_size)
        with source.open("rb") as source_file, target.open("rb") as target_file:
            while remaining:
                chunk_size = min(_COPY_BUFFER_SIZE, remaining)
                if source_file.read(chunk_size) != target_file.read(chunk_size):
                    return "diverged"
                remaining -= chunk_size
    except OSError as exc:
        raise CodexSessionMigrationError(
            f"Unable to compare rollout files {source} and {target}: {exc}"
        ) from exc

    if source_size == target_size:
        return "equal"
    if source_size > target_size:
        return "source_extends_target"
    return "target_extends_source"


def _copy_file_exclusive(source: Path, target: Path) -> None:
    """Create an independent copy at ``target`` without replacing anything."""

    source_stat = source.stat()
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with source.open("rb") as source_file, os.fdopen(descriptor, "wb") as target_file:
            descriptor = -1  # fdopen owns it now.
            shutil.copyfileobj(source_file, target_file, length=_COPY_BUFFER_SIZE)
            target_file.flush()
            os.fsync(target_file.fileno())
        os.chmod(target, 0o600)
        os.utime(
            target,
            ns=(source_stat.st_atime_ns, source_stat.st_mtime_ns),
            follow_symlinks=False,
        )
    except Exception:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            target.unlink()
        except OSError:
            pass
        raise


def _create_recoverable_backup(target: Path) -> Path:
    """Copy the current target to a unique owner-only backup beside it."""

    for index in range(10_000):
        suffix = ".pre-migration.bak" if index == 0 else f".pre-migration.{index}.bak"
        backup = target.with_name(target.name + suffix)
        try:
            _copy_file_exclusive(target, backup)
        except FileExistsError:
            if backup.is_file():
                try:
                    aliases_target = target.samefile(backup)
                except OSError:
                    aliases_target = False
                if not aliases_target and _compare_rollouts(target, backup) == "equal":
                    return backup
            continue

        if _compare_rollouts(target, backup) != "equal":
            raise CodexSessionMigrationError(
                f"Target rollout changed while creating recoverable backup {backup}"
            )
        return backup

    raise CodexSessionMigrationError(
        f"Unable to allocate a recoverable backup name beside {target}"
    )


def _copy_source_to_temporary_file(source: Path, target: Path) -> Path:
    """Create and fsync a private replacement file in the target directory."""

    descriptor, name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=target.parent,
    )
    temporary = Path(name)
    try:
        source_stat = source.stat()
        with source.open("rb") as source_file, os.fdopen(descriptor, "wb") as target_file:
            descriptor = -1
            shutil.copyfileobj(source_file, target_file, length=_COPY_BUFFER_SIZE)
            target_file.flush()
            os.fsync(target_file.fileno())
        os.chmod(temporary, 0o600)
        os.utime(
            temporary,
            ns=(source_stat.st_atime_ns, source_stat.st_mtime_ns),
            follow_symlinks=False,
        )
    except Exception:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except OSError:
            pass
        raise
    return temporary


def _fsync_directory(directory: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(directory, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        # Some filesystems do not support fsync on directories.  The rollout
        # replacement itself is already atomic; directory fsync is durability
        # hardening where supported.
        pass
    finally:
        os.close(descriptor)


def _replace_older_target(source: Path, target: Path) -> Path:
    """Back up an older target and atomically replace it with ``source``."""

    backup = _create_recoverable_backup(target)
    temporary = _copy_source_to_temporary_file(source, target)
    try:
        if _compare_rollouts(source, temporary) != "equal":
            raise CodexSessionMigrationError(
                f"Source rollout changed while preparing migration: {source}"
            )

        relationship = _compare_rollouts(source, target)
        if relationship in {"equal", "target_extends_source"}:
            # A concurrent writer already brought the target up to date (or
            # beyond it).  Preserve that newer target.
            return target
        if relationship == "diverged":
            raise CodexSessionConflictError(
                f"Target rollout diverged while preparing migration: {target}"
            )
        if _compare_rollouts(target, backup) != "equal":
            raise CodexSessionMigrationError(
                f"Target rollout changed after backup {backup}; retry migration"
            )

        os.replace(temporary, target)
        _fsync_directory(target.parent)
        return target
    finally:
        try:
            temporary.unlink()
        except OSError:
            pass


def _reconcile_existing_target(source: Path, target: Path) -> Path:
    if not target.is_file():
        raise CodexSessionConflictError(f"Target rollout is not a regular file: {target}")

    try:
        aliases_source = source.samefile(target)
    except OSError as exc:
        raise CodexSessionMigrationError(f"Unable to inspect target rollout {target}: {exc}") from exc

    if aliases_source and source != target:
        raise CodexSessionConflictError(
            f"Target rollout aliases the source instead of containing an independent copy: {target}"
        )

    relationship = _compare_rollouts(source, target)
    if relationship in {"equal", "target_extends_source"}:
        return target
    if relationship == "source_extends_target":
        return _replace_older_target(source, target)
    raise CodexSessionConflictError(
        f"Target rollout already exists with diverged content: {target}"
    )


def migrate_codex_rollout_session(
    session_id: str,
    source_codex_home: str | os.PathLike[str],
    target_codex_home: str | os.PathLike[str],
) -> Path:
    """Copy one Codex rollout to the same relative path in another home.

    The operation is idempotent when an independent target file already has
    identical content.  If one rollout is a strict byte-prefix of the other,
    the longer history wins: a newer target is preserved, while an older target
    is backed up and atomically replaced.  Diverged histories are never
    overwritten.  New directories and copied files are owner-only (0700/0600).
    """

    source_home = _normalise_home(source_codex_home)
    target_home = _normalise_home(target_codex_home)
    source = find_codex_rollout_session(session_id, source_home)
    source_sessions = source_home / "sessions"

    try:
        relative_path = source.relative_to(source_sessions)
    except ValueError as exc:  # Defensive: the fixed-depth glob should guarantee this.
        raise CodexSessionMigrationError(
            f"Source rollout escaped its CODEX_HOME sessions directory: {source}"
        ) from exc

    target = target_home / "sessions" / relative_path
    if source == target:
        return target

    try:
        _ensure_private_directory(target.parent)
        if target.exists():
            return _reconcile_existing_target(source, target)

        try:
            _copy_file_exclusive(source, target)
        except FileExistsError:
            # Preserve no-overwrite semantics when another process wins the
            # target creation race.
            return _reconcile_existing_target(source, target)
    except CodexSessionMigrationError:
        raise
    except OSError as exc:
        raise CodexSessionMigrationError(
            f"Unable to copy Codex session {session_id!r} from {source} to {target}: {exc}"
        ) from exc

    return target


__all__ = [
    "AmbiguousCodexSessionError",
    "CodexSessionConflictError",
    "CodexSessionMigrationError",
    "CodexSessionNotFoundError",
    "InvalidCodexSessionIdError",
    "find_codex_rollout_session",
    "migrate_codex_rollout_session",
]
