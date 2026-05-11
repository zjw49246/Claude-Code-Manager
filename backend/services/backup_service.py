"""Backup service: wraps auto-backup SDK to periodically back up the SQLite database."""
import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class BackupService:
    """Schedules periodic database backups using the auto-backup SDK.

    Supports local filesystem, AWS S3, and Alibaba Cloud OSS destinations.
    The backup is only started when a valid destination is configured.

    Args:
        db_path: SQLAlchemy database URL (e.g. ``sqlite+aiosqlite:///./claude_manager.db``).
        backup_type: Destination type — ``"local"``, ``"s3"``, or ``"oss"``.
        interval_seconds: How often to run a backup (default 3600).
        max_copies: How many backup copies to keep per destination (default 10).
        destination_path: Local directory path (required when *backup_type* is ``"local"``).
        temp_dir: Custom directory for temporary archive files (avoids filling /tmp).
        s3_bucket / s3_region / s3_access_key / s3_secret_key: S3 credentials.
        oss_endpoint / oss_bucket / oss_access_key / oss_secret_key: OSS credentials.
        _auto_backup_cls: Injectable AutoBackup class (for testing).
    """

    def __init__(
        self,
        db_path: str,
        backup_type: str = "local",
        interval_seconds: int = 3600,
        max_copies: int = 10,
        destination_path: str = "",
        temp_dir: str = "",
        s3_bucket: str = "",
        s3_region: str = "",
        s3_access_key: str = "",
        s3_secret_key: str = "",
        oss_endpoint: str = "",
        oss_bucket: str = "",
        oss_access_key: str = "",
        oss_secret_key: str = "",
        _auto_backup_cls=None,
    ):
        self._db_path = db_path
        self._backup_type = backup_type
        self._interval_seconds = interval_seconds
        self._max_copies = max_copies
        self._destination_path = destination_path
        self._temp_dir = temp_dir
        self._s3_bucket = s3_bucket
        self._s3_region = s3_region
        self._s3_access_key = s3_access_key
        self._s3_secret_key = s3_secret_key
        self._oss_endpoint = oss_endpoint
        self._oss_bucket = oss_bucket
        self._oss_access_key = oss_access_key
        self._oss_secret_key = oss_secret_key
        self._auto_backup_cls = _auto_backup_cls
        self._backup = None

    def _build_destination(self) -> Optional[dict]:
        t = self._backup_type
        if t == "local":
            if not self._destination_path:
                return None
            resolved = str(Path(self._destination_path).expanduser().resolve())
            return {"type": "local", "path": resolved}
        elif t == "s3":
            if not self._s3_bucket:
                return None
            return {
                "type": "s3",
                "bucket": self._s3_bucket,
                "region": self._s3_region,
                "access_key": self._s3_access_key,
                "secret_key": self._s3_secret_key,
            }
        elif t == "oss":
            if not self._oss_endpoint or not self._oss_bucket:
                return None
            return {
                "type": "oss",
                "endpoint": self._oss_endpoint,
                "bucket": self._oss_bucket,
                "access_key": self._oss_access_key,
                "secret_key": self._oss_secret_key,
            }
        logger.warning(f"Unknown backup type: {t!r}")
        return None

    def _resolve_db_path(self) -> str:
        raw = self._db_path
        for prefix in ("sqlite+aiosqlite:///", "sqlite:///"):
            if raw.startswith(prefix):
                raw = raw[len(prefix):]
                break
        return str(Path(raw).resolve())

    def start(self) -> bool:
        """Start the background backup scheduler. Returns True if started."""
        destination = self._build_destination()
        if destination is None:
            logger.info(
                "Backup destination not fully configured (backup_type=%r), skipping backup service",
                self._backup_type,
            )
            return False

        cls = self._auto_backup_cls
        if cls is None:
            from auto_backup import AutoBackup  # noqa: PLC0415 — lazy import
            cls = AutoBackup

        db_file = self._resolve_db_path()
        tmp_dir = str(Path(self._temp_dir).expanduser().resolve()) if self._temp_dir else None
        self._backup = cls(tmp_base_dir=tmp_dir)
        self._backup.add_task(
            name="claude-manager-db",
            source_paths=[db_file],
            destinations=[destination],
            interval_seconds=self._interval_seconds,
            max_copies=self._max_copies,
        )
        self._backup.start()
        logger.info(
            "Backup service started (type=%r, db=%r, interval=%ds)",
            self._backup_type,
            db_file,
            self._interval_seconds,
        )
        return True

    def stop(self):
        """Stop the background backup scheduler."""
        if self._backup is not None:
            self._backup.stop()
            self._backup = None
            logger.info("Backup service stopped")
