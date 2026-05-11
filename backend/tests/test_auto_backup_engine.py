"""Tests for auto_backup SDK fixes (temp cleanup, path expansion, custom tmp_dir).

These tests verify the bug fixes applied to the auto_backup package:
1. _create_archive cleans up temp dir on failure
2. _create_archive supports custom tmp_base_dir
3. execute_backup always cleans temp dir (finally block)
4. run_once always cleans temp dir (finally block)
5. LocalBackend expands ~ in base_path
"""
import os
import shutil
import tarfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from auto_backup.backup_engine import _create_archive, execute_backup
from auto_backup.backends.local import LocalBackend


# ── _create_archive ──────────────────────────────────────────────────────────


class TestCreateArchive:
    def test_creates_archive_in_system_temp(self, tmp_path):
        src = tmp_path / "data.txt"
        src.write_text("hello")

        archive = _create_archive([str(src)], "test-task")
        try:
            assert archive.exists()
            assert archive.name.startswith("test-task_")
            assert archive.name.endswith(".tar.gz")
        finally:
            shutil.rmtree(str(archive.parent), ignore_errors=True)

    def test_custom_tmp_base_dir(self, tmp_path):
        src = tmp_path / "data.txt"
        src.write_text("hello")
        custom_tmp = tmp_path / "my_tmp"

        archive = _create_archive([str(src)], "test-task", tmp_base_dir=str(custom_tmp))
        try:
            assert archive.exists()
            assert str(custom_tmp) in str(archive.parent)
        finally:
            shutil.rmtree(str(archive.parent), ignore_errors=True)

    def test_custom_tmp_base_dir_created_if_missing(self, tmp_path):
        custom_tmp = tmp_path / "does" / "not" / "exist"
        src = tmp_path / "data.txt"
        src.write_text("hello")

        archive = _create_archive([str(src)], "test-task", tmp_base_dir=str(custom_tmp))
        try:
            assert custom_tmp.exists()
            assert archive.exists()
        finally:
            shutil.rmtree(str(archive.parent), ignore_errors=True)

    def test_cleanup_on_tarfile_failure(self, tmp_path):
        src = tmp_path / "data.txt"
        src.write_text("hello")

        with patch("auto_backup.backup_engine.tarfile.open", side_effect=OSError("disk full")):
            with pytest.raises(OSError, match="disk full"):
                _create_archive([str(src)], "test-task", tmp_base_dir=str(tmp_path / "tmp"))

        leftover = list((tmp_path / "tmp").glob("auto_backup_*"))
        assert len(leftover) == 0, f"Temp dirs not cleaned up: {leftover}"

    def test_missing_source_skipped(self, tmp_path):
        archive = _create_archive(["/nonexistent/path"], "test-task")
        try:
            assert archive.exists()
            with tarfile.open(str(archive)) as tar:
                assert len(tar.getnames()) == 0
        finally:
            shutil.rmtree(str(archive.parent), ignore_errors=True)


# ── execute_backup temp cleanup ──────────────────────────────────────────────


class TestExecuteBackupCleanup:
    def _setup_mock_db(self, tmp_path):
        """Create a source file and return a mock DB connection."""
        src = tmp_path / "test.db"
        src.write_text("data")

        import json
        row = {
            "enabled": 1,
            "name": "test-backup",
            "source_paths": json.dumps([str(src)]),
            "destinations": json.dumps([{"type": "local", "path": str(tmp_path / "dest")}]),
            "max_copies": 5,
            "max_retention_seconds": None,
        }

        mock_row = MagicMock()
        mock_row.__getitem__ = lambda self, key: row[key]

        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchone.return_value = mock_row
        mock_conn.execute.return_value.lastrowid = 1
        return mock_conn

    def test_temp_cleaned_after_success(self, tmp_path):
        mock_conn = self._setup_mock_db(tmp_path)
        dest_dir = tmp_path / "dest"
        dest_dir.mkdir()
        custom_tmp = tmp_path / "tmp"

        with patch("auto_backup.backup_engine.get_connection", return_value=mock_conn):
            execute_backup(1, tmp_base_dir=str(custom_tmp))

        leftover = list(custom_tmp.glob("auto_backup_*"))
        assert len(leftover) == 0, f"Temp dirs not cleaned: {leftover}"

    def test_temp_cleaned_after_upload_failure(self, tmp_path):
        mock_conn = self._setup_mock_db(tmp_path)
        custom_tmp = tmp_path / "tmp"

        with patch("auto_backup.backup_engine.get_connection", return_value=mock_conn), \
             patch("auto_backup.backup_engine._make_backend") as mock_backend:
            mock_backend.return_value.upload.side_effect = RuntimeError("upload failed")
            execute_backup(1, tmp_base_dir=str(custom_tmp))

        leftover = list(custom_tmp.glob("auto_backup_*"))
        assert len(leftover) == 0, f"Temp dirs not cleaned: {leftover}"

    def test_task_not_found(self, tmp_path):
        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchone.return_value = None

        with patch("auto_backup.backup_engine.get_connection", return_value=mock_conn):
            execute_backup(999)


# ── LocalBackend path expansion ──────────────────────────────────────────────


class TestLocalBackendPathExpansion:
    def test_tilde_expanded(self):
        backend = LocalBackend("~/backups")
        assert "~" not in str(backend.base_path)
        assert backend.base_path.is_absolute()

    def test_absolute_path_unchanged(self):
        backend = LocalBackend("/mnt/backup")
        assert str(backend.base_path) == "/mnt/backup"

    def test_upload_creates_dest(self, tmp_path):
        backend = LocalBackend(str(tmp_path / "dest"))
        src = tmp_path / "file.tar.gz"
        src.write_text("data")

        result = backend.upload(src, "backup_001.tar.gz")
        assert Path(result).exists()

    def test_list_and_delete(self, tmp_path):
        dest = tmp_path / "dest"
        dest.mkdir()
        (dest / "task_20250101.tar.gz").write_text("a")
        (dest / "task_20250102.tar.gz").write_text("b")
        (dest / "other_file.txt").write_text("c")

        backend = LocalBackend(str(dest))
        backups = backend.list_backups("task_")
        assert len(backups) == 2

        backend.delete("task_20250101.tar.gz")
        assert not (dest / "task_20250101.tar.gz").exists()


# ── AutoBackup.run_once cleanup ──────────────────────────────────────────────


class TestRunOnceCleanup:
    def test_temp_cleaned_after_run_once(self, tmp_path):
        src = tmp_path / "data.txt"
        src.write_text("hello")
        dest = tmp_path / "dest"
        dest.mkdir()
        custom_tmp = tmp_path / "tmp"

        from auto_backup.client import AutoBackup

        with patch.object(AutoBackup, "__init__", lambda self, **kw: setattr(self, "_tmp_base_dir", kw.get("tmp_base_dir"))):
            backup = AutoBackup.__new__(AutoBackup)
            backup._tmp_base_dir = str(custom_tmp)

        results = backup.run_once(
            source_paths=[str(src)],
            destinations=[{"type": "local", "path": str(dest)}],
            task_name="test",
            tmp_base_dir=str(custom_tmp),
        )

        assert results[0]["status"] == "success"
        leftover = list(custom_tmp.glob("auto_backup_*"))
        assert len(leftover) == 0, f"Temp dirs not cleaned: {leftover}"

    def test_temp_cleaned_on_upload_failure(self, tmp_path):
        src = tmp_path / "data.txt"
        src.write_text("hello")
        custom_tmp = tmp_path / "tmp"

        from auto_backup.client import AutoBackup

        with patch.object(AutoBackup, "__init__", lambda self, **kw: setattr(self, "_tmp_base_dir", kw.get("tmp_base_dir"))):
            backup = AutoBackup.__new__(AutoBackup)
            backup._tmp_base_dir = str(custom_tmp)

        with patch("auto_backup.backup_engine._make_backend") as mock_backend:
            mock_backend.return_value.upload.side_effect = RuntimeError("fail")
            results = backup.run_once(
                source_paths=[str(src)],
                destinations=[{"type": "local", "path": "/bad/path"}],
                task_name="test",
                tmp_base_dir=str(custom_tmp),
            )

        assert results[0]["status"] == "failed"
        leftover = list(custom_tmp.glob("auto_backup_*"))
        assert len(leftover) == 0, f"Temp dirs not cleaned: {leftover}"
