"""Tests for backup storage and manager modules."""
import json
import os
import shutil
import tempfile
from pathlib import Path
from datetime import datetime, timezone

import pytest
from app.utils.vector_store.backup_manager import (
    BACKUP_DIR,
    BackupError,
    _sha256,
    _dir_size,
    _dir_checksum,
    list_backups,
    verify_backup,
    apply_retention,
    create_backup,
    restore_backup,
    BackupScheduler,
    start_scheduler,
    stop_scheduler,
    _default_schedule_hours,
    _scheduler,
)
from app.utils.vector_store.backup_storage import (
    LocalBackupStorage,
    create_backup_storage,
)


@pytest.fixture
def tmp_backup_dir(tmp_path):
    """Provide a temporary backup directory for isolated tests."""
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    return backup_dir


@pytest.fixture
def tmp_chroma_dir(tmp_path):
    """Simulate a minimal ChromaDB directory."""
    chroma_dir = tmp_path / "chroma_db"
    chroma_dir.mkdir()
    db_file = chroma_dir / "chromadb.sqlite3"
    db_file.write_bytes(b"fake-sqlite-db-content")
    return chroma_dir


@pytest.fixture
def mock_env(tmp_backup_dir, tmp_chroma_dir, monkeypatch):
    """Set up environment for backup tests to use temp paths."""
    monkeypatch.setenv("BACKUP_DIR", str(tmp_backup_dir))
    monkeypatch.setenv("CHROMA_PERSIST_DIR", str(tmp_chroma_dir))
    monkeypatch.setenv("RAG_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("BACKUP_RETENTION_DAYS", "7")

    # Force reload of config with new paths
    from config import Config
    monkeypatch.setattr(Config.paths, "chroma_persist_dir", str(tmp_chroma_dir))
    monkeypatch.setattr(Config.paths, "data_dir", str(tmp_path))
    monkeypatch.setattr(Config.paths, "upload_folder", str(tmp_path))

    return tmp_path


class TestSHA256:
    def test_deterministic(self, tmp_path):
        p = tmp_path / "test.txt"
        p.write_text("hello")
        hash1 = _sha256(p)
        hash2 = _sha256(p)
        assert hash1 == hash2
        assert len(hash1) == 64  # SHA-256

    def test_different_content(self, tmp_path):
        p1 = tmp_path / "a.txt"
        p2 = tmp_path / "b.txt"
        p1.write_text("foo")
        p2.write_text("bar")
        assert _sha256(p1) != _sha256(p2)


class TestDirChecksum:
    def test_empty_dir(self, tmp_path):
        d = tmp_path / "empty"
        d.mkdir()
        cs = _dir_checksum(d)
        assert len(cs) == 64  # SHA-256 hex

    def test_with_files(self, tmp_path):
        d = tmp_path / "sample"
        d.mkdir()
        (d / "a.txt").write_text("content")
        cs = _dir_checksum(d)
        assert len(cs) == 64

    def test_order_independent_filenames_are_sorted(self, tmp_path):
        d = tmp_path / "sorted"
        d.mkdir()
        (d / "z.txt").write_text("z")
        (d / "a.txt").write_text("a")
        cs = _dir_checksum(d)
        # Same files in different creation order should give same checksum
        d2 = tmp_path / "sorted2"
        d2.mkdir()
        (d2 / "a.txt").write_text("a")
        (d2 / "z.txt").write_text("z")
        cs2 = _dir_checksum(d2)
        assert cs == cs2


class TestDirSize:
    def test_empty(self, tmp_path):
        d = tmp_path / "empty"
        d.mkdir()
        assert _dir_size(d) == 0

    def test_with_content(self, tmp_path):
        d = tmp_path / "filled"
        d.mkdir()
        (d / "file.txt").write_text("A" * 100)
        assert _dir_size(d) == 100


class TestLocalBackupStorage:
    def test_list_empty(self, tmp_backup_dir):
        storage = LocalBackupStorage(backup_dir=str(tmp_backup_dir))
        assert storage.list() == []

    def test_upload(self, tmp_backup_dir, tmp_path):
        storage = LocalBackupStorage(backup_dir=str(tmp_backup_dir))
        source = tmp_path / "backup1"
        source.mkdir()
        (source / "data.txt").write_text("hello")
        result = storage.upload(source, "backup1")
        assert result is not None
        assert result["id"] == "backup1"

    def test_delete(self, tmp_backup_dir, tmp_path):
        storage = LocalBackupStorage(backup_dir=str(tmp_backup_dir))
        source = tmp_path / "bk"
        source.mkdir()
        (source / "f.txt").write_text("x")
        storage.upload(source, "bk")
        assert storage.list()
        storage.delete("bk")
        assert storage.list() == []

    def test_download(self, tmp_backup_dir, tmp_path):
        storage = LocalBackupStorage(backup_dir=str(tmp_backup_dir))
        source = tmp_path / "download1"
        source.mkdir()
        (source / "info.txt").write_text("hello")
        storage.upload(source, "download1")
        dest = tmp_path / "restored"
        assert storage.download("download1", dest)
        assert (dest / "info.txt").read_text() == "hello"


class TestBackupLifecycle:
    """Integration-style tests that exercise full backup flow with mock paths."""

    def test_list_backups_returns_list(self, tmp_backup_dir, monkeypatch):
        # Override BACKUP_DIR in the backup module at runtime
        import app.utils.vector_store.backup_manager as bm

        orig_dir = bm.BACKUP_DIR
        bm.BACKUP_DIR = tmp_backup_dir

        result = list_backups()
        # Must be a list even when empty
        assert isinstance(result, list)

        bm.BACKUP_DIR = orig_dir

    def test_verify_backup_404(self, tmp_backup_dir, monkeypatch):
        import app.utils.vector_store.backup_manager as bm
        orig_dir = bm.BACKUP_DIR
        bm.BACKUP_DIR = tmp_backup_dir

        result = verify_backup("nonexistent")
        assert result["status"] == "error"

        bm.BACKUP_DIR = orig_dir

    def test_apply_retention_no_backups(self, tmp_backup_dir, monkeypatch):
        import app.utils.vector_store.backup_manager as bm
        import app.utils.vector_store.backup_storage as bstorage

        orig_dir = bm.BACKUP_DIR
        bm.BACKUP_DIR = tmp_backup_dir

        deleted = apply_retention()
        assert deleted == []

        bm.BACKUP_DIR = orig_dir

    def test_create_backup_returns_dict(self, monkeypatch, tmp_path, tmp_backup_dir, tmp_chroma_dir):
        """Test that create_backup() produces structured output."""
        import app.utils.vector_store.backup_manager as bm

        orig_backup_dir = bm.BACKUP_DIR
        orig_chroma_dir = bm.CHROMA_DIR
        orig_data_dir = bm.DATA_DIR

        bm.BACKUP_DIR = tmp_backup_dir
        bm.CHROMA_DIR = tmp_chroma_dir
        bm.DATA_DIR = tmp_path / "data"
        bm.DATA_DIR.mkdir(parents=True, exist_ok=True)

        try:
            result = create_backup()
            assert isinstance(result, dict)
            assert "id" in result
            assert "status" in result
        finally:
            bm.BACKUP_DIR = orig_backup_dir
            bm.CHROMA_DIR = orig_chroma_dir
            bm.DATA_DIR = orig_data_dir

    def test_restore_backup_restores_chroma_and_data(self, monkeypatch, tmp_path, tmp_backup_dir, tmp_chroma_dir):
        """Full backup/restore smoke test for local Chroma files and metadata."""
        import app.utils.vector_store.backup_manager as bm

        orig_backup_dir = bm.BACKUP_DIR
        orig_chroma_dir = bm.CHROMA_DIR
        orig_data_dir = bm.DATA_DIR

        data_dir = tmp_path / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / "files.json").write_text('{"version": 1}', encoding="utf-8")
        (tmp_chroma_dir / "marker.txt").write_text("original", encoding="utf-8")

        bm.BACKUP_DIR = tmp_backup_dir
        bm.CHROMA_DIR = tmp_chroma_dir
        bm.DATA_DIR = data_dir

        try:
            result = create_backup()
            backup_id = result["id"]

            (tmp_chroma_dir / "marker.txt").write_text("mutated", encoding="utf-8")
            (data_dir / "files.json").write_text('{"version": 2}', encoding="utf-8")

            restore = restore_backup(backup_id)

            assert restore["status"] == "success"
            assert (tmp_chroma_dir / "marker.txt").read_text(encoding="utf-8") == "original"
            assert (data_dir / "files.json").read_text(encoding="utf-8") == '{"version": 1}'
            assert list(tmp_path.glob("chroma_db.bak.*"))
        finally:
            bm.BACKUP_DIR = orig_backup_dir
            bm.CHROMA_DIR = orig_chroma_dir
            bm.DATA_DIR = orig_data_dir

    def test_retention_zero_keeps_backups(self, tmp_backup_dir, monkeypatch):
        import app.utils.vector_store.backup_manager as bm

        orig_dir = bm.BACKUP_DIR
        bm.BACKUP_DIR = tmp_backup_dir
        monkeypatch.setenv("BACKUP_RETENTION_DAYS", "0")

        old_backup = tmp_backup_dir / "old"
        old_backup.mkdir()
        (old_backup / "manifest.json").write_text(
            json.dumps({"created_at_epoch": 1}),
            encoding="utf-8",
        )

        try:
            assert apply_retention() == []
            assert old_backup.exists()
        finally:
            bm.BACKUP_DIR = orig_dir


class TestBackupStorageFactory:
    def test_returns_local_by_default(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BACKUP_REMOTE_TYPE", "local")
        monkeypatch.setenv("BACKUP_DIR", str(tmp_path))
        storage = create_backup_storage()
        assert isinstance(storage, LocalBackupStorage)

    def test_remote_env_still_returns_local(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BACKUP_REMOTE_TYPE", "http")
        monkeypatch.setenv("BACKUP_DIR", str(tmp_path))
        storage = create_backup_storage()
        assert isinstance(storage, LocalBackupStorage)

    def test_schedule_hours_default(self, monkeypatch):
        # Default is "2,20"
        assert _default_schedule_hours() == [2, 20]

    def test_schedule_hours_custom(self, monkeypatch):
        monkeypatch.setenv("BACKUP_SCHEDULE_HOURS", "3,15")
        assert _default_schedule_hours() == [3, 15]

    def test_schedule_hours_invalid(self, monkeypatch):
        monkeypatch.setenv("BACKUP_SCHEDULE_HOURS", "abc")
        # Falls back to default
        assert _default_schedule_hours() == [2]

    def test_scheduler_creation(self, monkeypatch):
        monkeypatch.setenv("BACKUP_ENABLED", "1")
        scheduler = BackupScheduler(enabled=True)
        assert not scheduler.is_running

    def test_scheduler_start_stop(self, monkeypatch):
        monkeypatch.setenv("BACKUP_ENABLED", "1")
        monkeypatch.setenv("BACKUP_SCHEDULE_HOURS", "6")
        scheduler = BackupScheduler(enabled=True)
        scheduler.start()
        assert scheduler.is_running
        scheduler.stop()
        assert not scheduler.is_running

    def test_scheduler_disabled(self, monkeypatch):
        scheduler = BackupScheduler(enabled=False)
        scheduler.start()
        # With enabled=False it should not actually start threads
        # but the public API should still be callable
        assert not scheduler.is_running
        scheduler.stop()  # Should be safe even when not running
