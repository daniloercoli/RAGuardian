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

    def test_backup_ids_reject_path_traversal(self, tmp_backup_dir):
        import app.utils.vector_store.backup_manager as bm

        original = bm.BACKUP_DIR
        bm.BACKUP_DIR = tmp_backup_dir
        try:
            assert verify_backup("..") == {"status": "error", "error": "Invalid backup id"}
            with pytest.raises(BackupError, match="Invalid backup id"):
                restore_backup("..")
        finally:
            bm.BACKUP_DIR = original

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
        orig_upload_dir = bm.UPLOAD_DIR

        bm.BACKUP_DIR = tmp_backup_dir
        bm.CHROMA_DIR = tmp_chroma_dir
        bm.DATA_DIR = tmp_path / "data"
        bm.DATA_DIR.mkdir(parents=True, exist_ok=True)
        bm.UPLOAD_DIR = tmp_path / "uploads"
        bm.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

        try:
            result = create_backup()
            assert isinstance(result, dict)
            assert "id" in result
            assert "status" in result
        finally:
            bm.BACKUP_DIR = orig_backup_dir
            bm.CHROMA_DIR = orig_chroma_dir
            bm.DATA_DIR = orig_data_dir
            bm.UPLOAD_DIR = orig_upload_dir

    def test_backup_manifest_lists_each_workspace_knowledge_base(
        self,
        tmp_path,
        tmp_backup_dir,
        tmp_chroma_dir,
    ):
        import app.utils.vector_store.backup_manager as bm
        from app.utils.knowledge_base_store import KnowledgeBaseStore

        original = (bm.BACKUP_DIR, bm.CHROMA_DIR, bm.DATA_DIR, bm.UPLOAD_DIR)
        data_dir = tmp_path / "data"
        workspace_dir = data_dir / "workspaces" / "alice"
        workspace_dir.mkdir(parents=True)
        upload_dir = tmp_path / "uploads"
        upload_dir.mkdir()
        store = KnowledgeBaseStore(workspace_dir / "knowledge_bases.json")
        store.ensure_default()
        secondary = store.create(name="Legal")
        store.set_status(
            secondary["id"],
            "delete_failed",
            delete_error="cleanup interrupted",
        )
        (workspace_dir / "files.json").write_text(
            json.dumps(
                [{"filename": "default.txt", "status": "indexed", "chunks": 2}]
            ),
            encoding="utf-8",
        )
        secondary_dir = workspace_dir / "knowledge_bases" / secondary["id"]
        secondary_dir.mkdir(parents=True)
        (secondary_dir / "files.json").write_text(
            json.dumps(
                [{"filename": "legal.txt", "status": "indexed", "chunks": 3}]
            ),
            encoding="utf-8",
        )
        bm.BACKUP_DIR = tmp_backup_dir
        bm.CHROMA_DIR = tmp_chroma_dir
        bm.DATA_DIR = data_dir
        bm.UPLOAD_DIR = upload_dir

        try:
            backup_id = create_backup()["id"]
            manifest = json.loads(
                (tmp_backup_dir / backup_id / "manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            assert manifest["knowledge_base_catalog_schema_version"] == 1
            assert manifest["knowledge_base_workspace_count"] == 1
            assert manifest["knowledge_base_count"] == 2
            assert "knowledge_base_collection_count" in manifest
            assert {
                item["knowledge_base_id"]: item["chunks"]
                for item in manifest["knowledge_bases"]
            } == {"default": 2, secondary["id"]: 3}
            assert next(
                item
                for item in manifest["knowledge_bases"]
                if item["knowledge_base_id"] == secondary["id"]
            )["status"] == "delete_failed"
        finally:
            bm.BACKUP_DIR, bm.CHROMA_DIR, bm.DATA_DIR, bm.UPLOAD_DIR = original

    def test_backup_manifest_includes_conversation_db_fields(
        self,
        tmp_path,
        tmp_backup_dir,
        tmp_chroma_dir,
    ):
        """Backup manifest records conversation_history.db presence, size and hash."""
        import sqlite3

        import app.utils.vector_store.backup_manager as bm

        original = (bm.BACKUP_DIR, bm.CHROMA_DIR, bm.DATA_DIR, bm.UPLOAD_DIR)
        data_dir = tmp_path / "data"
        workspace_data_dir = data_dir / "workspaces"
        workspace_data_dir.mkdir(parents=True)
        upload_dir = tmp_path / "uploads"
        upload_dir.mkdir()

        db_file = workspace_data_dir / "conversation_history.db"
        conn = sqlite3.connect(str(db_file))
        conn.execute(
            "CREATE TABLE conversations (id TEXT PRIMARY KEY, scope_key TEXT)"
        )
        conn.execute(
            "INSERT INTO conversations VALUES (?, ?)",
            ("conv-1", "ws:default:conv-1"),
        )
        conn.commit()
        conn.close()
        expected_size = db_file.stat().st_size

        bm.BACKUP_DIR = tmp_backup_dir
        bm.CHROMA_DIR = tmp_chroma_dir
        bm.DATA_DIR = data_dir
        bm.UPLOAD_DIR = upload_dir

        try:
            backup_id = create_backup()["id"]
            manifest = json.loads(
                (tmp_backup_dir / backup_id / "manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            assert manifest["conversation_db_present"] is True
            assert manifest["conversation_db_size_bytes"] == expected_size
            assert len(manifest["conversation_db_sha256"]) == 64
            assert all(
                c in "0123456789abcdef"
                for c in manifest["conversation_db_sha256"]
            )
        finally:
            bm.BACKUP_DIR, bm.CHROMA_DIR, bm.DATA_DIR, bm.UPLOAD_DIR = original

    def test_backup_manifest_conversation_db_absent_when_missing(
        self,
        tmp_path,
        tmp_backup_dir,
        tmp_chroma_dir,
    ):
        """Manifest reports conversation_db_present=False when no DB exists."""
        import app.utils.vector_store.backup_manager as bm

        original = (bm.BACKUP_DIR, bm.CHROMA_DIR, bm.DATA_DIR, bm.UPLOAD_DIR)
        data_dir = tmp_path / "data"
        workspace_data_dir = data_dir / "workspaces"
        workspace_data_dir.mkdir(parents=True)
        upload_dir = tmp_path / "uploads"
        upload_dir.mkdir()

        bm.BACKUP_DIR = tmp_backup_dir
        bm.CHROMA_DIR = tmp_chroma_dir
        bm.DATA_DIR = data_dir
        bm.UPLOAD_DIR = upload_dir

        try:
            backup_id = create_backup()["id"]
            manifest = json.loads(
                (tmp_backup_dir / backup_id / "manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            assert manifest["conversation_db_present"] is False
            assert manifest["conversation_db_size_bytes"] == 0
            assert manifest["conversation_db_sha256"] == ""
        finally:
            bm.BACKUP_DIR, bm.CHROMA_DIR, bm.DATA_DIR, bm.UPLOAD_DIR = original

    def test_restore_backup_restores_chroma_and_data(self, monkeypatch, tmp_path, tmp_backup_dir, tmp_chroma_dir):
        """Full backup/restore smoke test for local Chroma files and metadata."""
        import app.utils.vector_store.backup_manager as bm
        import utils.rag_engine as runtime_rag_engine

        orig_backup_dir = bm.BACKUP_DIR
        orig_chroma_dir = bm.CHROMA_DIR
        orig_data_dir = bm.DATA_DIR
        orig_upload_dir = bm.UPLOAD_DIR
        cache_clears = []
        monkeypatch.setattr(
            runtime_rag_engine,
            "clear_cache",
            lambda: cache_clears.append(True),
        )

        data_dir = tmp_path / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / "files.json").write_text('{"version": 1}', encoding="utf-8")
        (data_dir / "workspaces" / "alice").mkdir(parents=True)
        (data_dir / "workspaces" / "alice" / "settings.json").write_text('{"workspace": 1}', encoding="utf-8")
        upload_dir = tmp_path / "uploads"
        (upload_dir / "workspaces" / "alice").mkdir(parents=True)
        (upload_dir / "workspaces" / "alice" / "source.pdf").write_bytes(b"original upload")
        (tmp_chroma_dir / "marker.txt").write_text("original", encoding="utf-8")

        bm.BACKUP_DIR = tmp_backup_dir
        bm.CHROMA_DIR = tmp_chroma_dir
        bm.DATA_DIR = data_dir
        bm.UPLOAD_DIR = upload_dir

        try:
            result = create_backup()
            backup_id = result["id"]

            (tmp_chroma_dir / "marker.txt").write_text("mutated", encoding="utf-8")
            (data_dir / "files.json").write_text('{"version": 2}', encoding="utf-8")
            (data_dir / "workspaces" / "alice" / "settings.json").write_text('{"workspace": 2}', encoding="utf-8")
            (upload_dir / "workspaces" / "alice" / "source.pdf").write_bytes(b"mutated upload")

            restore = restore_backup(backup_id)

            assert restore["status"] == "success"
            assert (tmp_chroma_dir / "marker.txt").read_text(encoding="utf-8") == "original"
            assert (data_dir / "files.json").read_text(encoding="utf-8") == '{"version": 1}'
            assert (data_dir / "workspaces" / "alice" / "settings.json").read_text(encoding="utf-8") == '{"workspace": 1}'
            assert (upload_dir / "workspaces" / "alice" / "source.pdf").read_bytes() == b"original upload"
            assert list(tmp_path.glob("chroma_db.bak.*"))
            assert cache_clears == [True]
        finally:
            bm.BACKUP_DIR = orig_backup_dir
            bm.CHROMA_DIR = orig_chroma_dir
            bm.DATA_DIR = orig_data_dir
            bm.UPLOAD_DIR = orig_upload_dir

    def test_restore_holds_lifecycle_write_lock_before_index_lock(
        self,
        monkeypatch,
        tmp_path,
    ):
        from contextlib import contextmanager

        import app.utils.index_lock as locks
        import app.utils.vector_store.backup_manager as bm

        events = []

        @contextmanager
        def lifecycle_lock(*, publish=True):
            assert publish is False
            events.append("lifecycle-enter")
            try:
                yield
            finally:
                events.append("lifecycle-exit")

        @contextmanager
        def index_lock():
            events.append("index-enter")
            try:
                yield
            finally:
                events.append("index-exit")

        def restore_locked(backup_id, **kwargs):
            events.append(("restore", backup_id, kwargs))
            return {"status": "success"}

        monkeypatch.setattr(locks, "lifecycle_write_lock", lifecycle_lock)
        monkeypatch.setattr(locks, "index_write_lock", index_lock)
        monkeypatch.setattr(bm, "_restore_backup_locked", restore_locked)
        workspace_data = tmp_path / "workspace_data"
        workspace_uploads = tmp_path / "workspace_uploads"

        result = bm.restore_backup(
            "backup_1",
            workspace_data_dir=workspace_data,
            workspace_upload_dir=workspace_uploads,
        )

        assert result == {"status": "success"}
        assert events == [
            "lifecycle-enter",
            "index-enter",
            (
                "restore",
                "backup_1",
                {
                    "workspace_data_dir": workspace_data,
                    "workspace_upload_dir": workspace_uploads,
                },
            ),
            "index-exit",
            "lifecycle-exit",
        ]

    def test_restore_rolls_back_when_epoch_publish_fails(
        self,
        monkeypatch,
        tmp_path,
        tmp_backup_dir,
        tmp_chroma_dir,
    ):
        import app.utils.index_lock as locks
        import app.utils.vector_store.backup_manager as bm

        original = (bm.BACKUP_DIR, bm.CHROMA_DIR, bm.DATA_DIR, bm.UPLOAD_DIR)
        data_dir = tmp_path / "data"
        upload_dir = tmp_path / "uploads"
        data_dir.mkdir()
        upload_dir.mkdir()
        marker = data_dir / "marker.txt"
        marker.write_text("backup", encoding="utf-8")
        bm.BACKUP_DIR = tmp_backup_dir
        bm.CHROMA_DIR = tmp_chroma_dir
        bm.DATA_DIR = data_dir
        bm.UPLOAD_DIR = upload_dir

        try:
            backup_id = create_backup()["id"]
            marker.write_text("live", encoding="utf-8")
            monkeypatch.setattr(
                locks,
                "bump_lifecycle_generation",
                lambda: (_ for _ in ()).throw(OSError("epoch unavailable")),
            )

            with pytest.raises(OSError, match="epoch unavailable"):
                restore_backup(backup_id)

            assert marker.read_text(encoding="utf-8") == "live"
        finally:
            bm.BACKUP_DIR, bm.CHROMA_DIR, bm.DATA_DIR, bm.UPLOAD_DIR = original

    def test_restore_reopens_real_chroma_after_directory_swap(
        self,
        tmp_path,
        tmp_backup_dir,
    ):
        chromadb = pytest.importorskip("chromadb")
        import app.utils.vector_store.backup_manager as bm

        original = (bm.BACKUP_DIR, bm.CHROMA_DIR, bm.DATA_DIR, bm.UPLOAD_DIR)
        chroma_dir = tmp_path / "real_chroma"
        data_dir = tmp_path / "data"
        upload_dir = tmp_path / "uploads"
        data_dir.mkdir()
        upload_dir.mkdir()
        bm.BACKUP_DIR = tmp_backup_dir
        bm.CHROMA_DIR = chroma_dir
        bm.DATA_DIR = data_dir
        bm.UPLOAD_DIR = upload_dir
        bm._reset_chroma_system_cache()

        try:
            client = chromadb.PersistentClient(path=str(chroma_dir))
            collection = client.get_or_create_collection("documents")
            collection.add(
                ids=["backed-up"],
                documents=["backup state"],
                embeddings=[[1.0, 0.0]],
            )
            backup_id = bm.create_backup()["id"]

            collection.add(
                ids=["live-only"],
                documents=["new live state"],
                embeddings=[[0.0, 1.0]],
            )
            assert collection.count() == 2

            restored = bm.restore_backup(backup_id)

            assert restored["status"] == "success"
            assert restored["document_count"] == 1
            fresh = chromadb.PersistentClient(path=str(chroma_dir))
            ids = fresh.get_collection("documents").get()["ids"]
            assert ids == ["backed-up"]
        finally:
            bm._reset_chroma_system_cache()
            bm.BACKUP_DIR, bm.CHROMA_DIR, bm.DATA_DIR, bm.UPLOAD_DIR = original

    def test_restore_does_not_rollback_live_paths_after_lease_loss(
        self,
        monkeypatch,
        tmp_path,
        tmp_backup_dir,
        tmp_chroma_dir,
    ):
        import app.utils.index_lock as locks
        import app.utils.vector_store.backup_manager as bm

        original = (bm.BACKUP_DIR, bm.CHROMA_DIR, bm.DATA_DIR, bm.UPLOAD_DIR)
        data_dir = tmp_path / "data"
        upload_dir = tmp_path / "uploads"
        data_dir.mkdir()
        upload_dir.mkdir()
        (data_dir / "settings.json").write_text("backup", encoding="utf-8")
        (upload_dir / "source.txt").write_text("backup", encoding="utf-8")
        bm.BACKUP_DIR = tmp_backup_dir
        bm.CHROMA_DIR = tmp_chroma_dir
        bm.DATA_DIR = data_dir
        bm.UPLOAD_DIR = upload_dir

        try:
            backup_id = create_backup()["id"]
            (tmp_chroma_dir / "live-only.txt").write_text(
                "live",
                encoding="utf-8",
            )
            lease_lost = False
            real_move = bm.shutil.move

            def move_and_lose_lease(source, destination):
                nonlocal lease_lost
                result = real_move(source, destination)
                if (
                    Path(source) == bm.CHROMA_DIR
                    and ".bak." in Path(destination).name
                ):
                    bm.CHROMA_DIR.mkdir()
                    (bm.CHROMA_DIR / "concurrent.txt").write_text(
                        "new owner",
                        encoding="utf-8",
                    )
                    lease_lost = True
                return result

            def reject_lost_lease():
                if lease_lost:
                    raise locks.DistributedLockLeaseLostError(
                        "simulated lease loss"
                    )

            monkeypatch.setattr(bm.shutil, "move", move_and_lose_lease)
            monkeypatch.setattr(
                locks,
                "assert_distributed_locks_healthy",
                reject_lost_lease,
            )

            with pytest.raises(
                locks.DistributedLockLeaseLostError,
                match="simulated lease loss",
            ):
                restore_backup(backup_id)

            assert (
                bm.CHROMA_DIR / "concurrent.txt"
            ).read_text(encoding="utf-8") == "new owner"
            previous = list(tmp_path.glob("chroma_db.bak.*"))
            assert len(previous) == 1
            assert (previous[0] / "live-only.txt").read_text(
                encoding="utf-8"
            ) == "live"
        finally:
            bm.BACKUP_DIR, bm.CHROMA_DIR, bm.DATA_DIR, bm.UPLOAD_DIR = original

    def test_restore_rollback_stops_between_remove_and_move_after_lease_loss(
        self,
        monkeypatch,
        tmp_path,
    ):
        import app.utils.index_lock as locks
        import app.utils.vector_store.backup_manager as bm

        target = tmp_path / "live"
        previous = tmp_path / "live.bak"
        target.mkdir()
        previous.mkdir()
        (target / "value.txt").write_text("replacement", encoding="utf-8")
        (previous / "value.txt").write_text("original", encoding="utf-8")
        lease_lost = False
        real_rmtree = bm.shutil.rmtree

        def remove_and_lose_lease(path, *args, **kwargs):
            nonlocal lease_lost
            result = real_rmtree(path, *args, **kwargs)
            if Path(path) == target:
                lease_lost = True
            return result

        def reject_lost_lease():
            if lease_lost:
                raise locks.DistributedLockLeaseLostError(
                    "simulated rollback lease loss"
                )

        monkeypatch.setattr(bm.shutil, "rmtree", remove_and_lose_lease)
        monkeypatch.setattr(
            locks,
            "assert_distributed_locks_healthy",
            reject_lost_lease,
        )

        with pytest.raises(
            locks.DistributedLockLeaseLostError,
            match="simulated rollback lease loss",
        ):
            bm._rollback_restore_components([(target, previous)])

        assert not target.exists()
        assert (
            previous / "value.txt"
        ).read_text(encoding="utf-8") == "original"

    def test_backup_restores_custom_workspace_roots(
        self,
        monkeypatch,
        tmp_path,
        tmp_backup_dir,
        tmp_chroma_dir,
    ):
        import app.utils.vector_store.backup_manager as bm
        from app.utils.knowledge_base_store import KnowledgeBaseStore

        original = (bm.BACKUP_DIR, bm.CHROMA_DIR, bm.DATA_DIR, bm.UPLOAD_DIR)
        data_dir = tmp_path / "global_data"
        upload_dir = tmp_path / "global_uploads"
        workspace_data_dir = tmp_path / "tenant_state"
        workspace_upload_dir = tmp_path / "tenant_files"
        workspace_dir = workspace_data_dir / "alice"
        workspace_files = workspace_upload_dir / "alice"
        data_dir.mkdir()
        upload_dir.mkdir()
        workspace_dir.mkdir(parents=True)
        workspace_files.mkdir(parents=True)
        (data_dir / "settings.json").write_text("global backup", encoding="utf-8")
        (workspace_dir / "settings.json").write_text(
            "workspace backup",
            encoding="utf-8",
        )
        (workspace_dir / "files.json").write_text(
            json.dumps(
                [{"filename": "source.pdf", "status": "indexed", "chunks": 2}]
            ),
            encoding="utf-8",
        )
        KnowledgeBaseStore(workspace_dir / "knowledge_bases.json").ensure_default()
        (workspace_files / "source.pdf").write_bytes(b"workspace upload")

        bm.BACKUP_DIR = tmp_backup_dir
        bm.CHROMA_DIR = tmp_chroma_dir
        bm.DATA_DIR = data_dir
        bm.UPLOAD_DIR = upload_dir
        monkeypatch.setenv("RAG_WORKSPACE_DATA_DIR", str(workspace_data_dir))
        monkeypatch.setenv("RAG_WORKSPACE_UPLOAD_DIR", str(workspace_upload_dir))

        try:
            backup_id = create_backup()["id"]
            backup_path = tmp_backup_dir / backup_id
            manifest = json.loads(
                (backup_path / "manifest.json").read_text(encoding="utf-8")
            )

            assert manifest["source_workspace_data_dir"] == str(workspace_data_dir)
            assert manifest["source_workspace_upload_dir"] == str(
                workspace_upload_dir
            )
            assert manifest["workspace_data_backup_path"] == "workspace_data"
            assert manifest["workspace_uploads_backup_path"] == "workspace_uploads"
            assert manifest["workspace_data_separate"] is True
            assert manifest["workspace_uploads_separate"] is True
            assert manifest["knowledge_base_workspace_count"] == 1
            assert manifest["knowledge_bases"][0]["workspace_id"] == "alice"
            assert (backup_path / "workspace_data" / "alice" / "settings.json").exists()
            assert (backup_path / "workspace_uploads" / "alice" / "source.pdf").exists()
            verification = verify_backup(backup_id)
            assert verification["status"] == "ok"
            assert verification["workspace_data_checksum_ok"] is True
            assert verification["workspace_uploads_checksum_ok"] is True

            (data_dir / "settings.json").write_text("global live", encoding="utf-8")
            (workspace_dir / "settings.json").write_text(
                "workspace live",
                encoding="utf-8",
            )
            (workspace_data_dir / "live-only.txt").write_text(
                "remove me",
                encoding="utf-8",
            )
            (workspace_files / "source.pdf").write_bytes(b"live upload")

            restored = restore_backup(backup_id)

            assert restored["status"] == "success"
            assert (data_dir / "settings.json").read_text(
                encoding="utf-8"
            ) == "global backup"
            assert (workspace_dir / "settings.json").read_text(
                encoding="utf-8"
            ) == "workspace backup"
            assert not (workspace_data_dir / "live-only.txt").exists()
            assert (workspace_files / "source.pdf").read_bytes() == b"workspace upload"
        finally:
            bm.BACKUP_DIR, bm.CHROMA_DIR, bm.DATA_DIR, bm.UPLOAD_DIR = original

    def test_explicit_workspace_roots_override_environment(
        self,
        monkeypatch,
        tmp_path,
        tmp_backup_dir,
        tmp_chroma_dir,
    ):
        import app.utils.vector_store.backup_manager as bm

        original = (bm.BACKUP_DIR, bm.CHROMA_DIR, bm.DATA_DIR, bm.UPLOAD_DIR)
        data_dir = tmp_path / "global_data"
        upload_dir = tmp_path / "global_uploads"
        explicit_data = tmp_path / "configured_workspace_data"
        explicit_uploads = tmp_path / "configured_workspace_uploads"
        env_data = tmp_path / "environment_workspace_data"
        env_uploads = tmp_path / "environment_workspace_uploads"
        for path in (
            data_dir,
            upload_dir,
            explicit_data,
            explicit_uploads,
            env_data,
            env_uploads,
        ):
            path.mkdir()
        (explicit_data / "marker.txt").write_text("configured", encoding="utf-8")
        (explicit_uploads / "source.pdf").write_bytes(b"configured upload")
        (env_data / "marker.txt").write_text("environment", encoding="utf-8")
        (env_uploads / "source.pdf").write_bytes(b"environment upload")

        bm.BACKUP_DIR = tmp_backup_dir
        bm.CHROMA_DIR = tmp_chroma_dir
        bm.DATA_DIR = data_dir
        bm.UPLOAD_DIR = upload_dir
        monkeypatch.setenv("RAG_WORKSPACE_DATA_DIR", str(env_data))
        monkeypatch.setenv("RAG_WORKSPACE_UPLOAD_DIR", str(env_uploads))

        try:
            backup_id = create_backup(
                workspace_data_dir=explicit_data,
                workspace_upload_dir=explicit_uploads,
            )["id"]
            manifest = json.loads(
                (tmp_backup_dir / backup_id / "manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            assert manifest["source_workspace_data_dir"] == str(explicit_data)
            assert manifest["source_workspace_upload_dir"] == str(explicit_uploads)

            (explicit_data / "marker.txt").write_text("mutated", encoding="utf-8")
            (explicit_uploads / "source.pdf").write_bytes(b"mutated upload")
            restored = restore_backup(
                backup_id,
                workspace_data_dir=explicit_data,
                workspace_upload_dir=explicit_uploads,
            )

            assert restored["status"] == "success"
            assert (explicit_data / "marker.txt").read_text(
                encoding="utf-8"
            ) == "configured"
            assert (explicit_uploads / "source.pdf").read_bytes() == (
                b"configured upload"
            )
            assert (env_data / "marker.txt").read_text(
                encoding="utf-8"
            ) == "environment"
            assert (env_uploads / "source.pdf").read_bytes() == (
                b"environment upload"
            )
        finally:
            bm.BACKUP_DIR, bm.CHROMA_DIR, bm.DATA_DIR, bm.UPLOAD_DIR = original

    def test_verify_detects_corrupted_custom_workspace_component(
        self,
        monkeypatch,
        tmp_path,
        tmp_backup_dir,
        tmp_chroma_dir,
    ):
        import app.utils.vector_store.backup_manager as bm

        original = (bm.BACKUP_DIR, bm.CHROMA_DIR, bm.DATA_DIR, bm.UPLOAD_DIR)
        data_dir = tmp_path / "global_data"
        upload_dir = tmp_path / "global_uploads"
        workspace_data_dir = tmp_path / "tenant_state"
        workspace_upload_dir = tmp_path / "tenant_files"
        data_dir.mkdir()
        upload_dir.mkdir()
        workspace_data_dir.mkdir()
        workspace_upload_dir.mkdir()
        (workspace_data_dir / "settings.json").write_text("backup", encoding="utf-8")
        bm.BACKUP_DIR = tmp_backup_dir
        bm.CHROMA_DIR = tmp_chroma_dir
        bm.DATA_DIR = data_dir
        bm.UPLOAD_DIR = upload_dir
        monkeypatch.setenv("RAG_WORKSPACE_DATA_DIR", str(workspace_data_dir))
        monkeypatch.setenv("RAG_WORKSPACE_UPLOAD_DIR", str(workspace_upload_dir))

        try:
            backup_id = create_backup()["id"]
            (workspace_data_dir / "settings.json").write_text("live", encoding="utf-8")
            (
                tmp_backup_dir
                / backup_id
                / "workspace_data"
                / "settings.json"
            ).write_text("corrupted", encoding="utf-8")

            verification = verify_backup(backup_id)
            assert verification["status"] == "mismatch"
            assert verification["workspace_data_checksum_ok"] is False
            with pytest.raises(BackupError, match="integrity verification"):
                restore_backup(backup_id)
            assert (workspace_data_dir / "settings.json").read_text(
                encoding="utf-8"
            ) == "live"
        finally:
            bm.BACKUP_DIR, bm.CHROMA_DIR, bm.DATA_DIR, bm.UPLOAD_DIR = original

    def test_restore_validates_custom_workspace_catalog_and_rolls_back(
        self,
        monkeypatch,
        tmp_path,
        tmp_backup_dir,
        tmp_chroma_dir,
    ):
        import app.utils.vector_store.backup_manager as bm
        from app.utils.knowledge_base_store import KnowledgeBaseStore

        original = (bm.BACKUP_DIR, bm.CHROMA_DIR, bm.DATA_DIR, bm.UPLOAD_DIR)
        data_dir = tmp_path / "global_data"
        upload_dir = tmp_path / "global_uploads"
        workspace_data_dir = tmp_path / "tenant_state"
        workspace_upload_dir = tmp_path / "tenant_files"
        workspace_dir = workspace_data_dir / "alice"
        data_dir.mkdir()
        upload_dir.mkdir()
        workspace_dir.mkdir(parents=True)
        workspace_upload_dir.mkdir()
        KnowledgeBaseStore(workspace_dir / "knowledge_bases.json").ensure_default()
        marker = workspace_dir / "marker.txt"
        marker.write_text("backup", encoding="utf-8")
        bm.BACKUP_DIR = tmp_backup_dir
        bm.CHROMA_DIR = tmp_chroma_dir
        bm.DATA_DIR = data_dir
        bm.UPLOAD_DIR = upload_dir
        monkeypatch.setenv("RAG_WORKSPACE_DATA_DIR", str(workspace_data_dir))
        monkeypatch.setenv("RAG_WORKSPACE_UPLOAD_DIR", str(workspace_upload_dir))

        try:
            backup_id = create_backup()["id"]
            marker.write_text("live", encoding="utf-8")
            (
                tmp_backup_dir
                / backup_id
                / "workspace_data"
                / "alice"
                / "knowledge_bases.json"
            ).write_text("{invalid", encoding="utf-8")
            monkeypatch.setattr(
                bm,
                "verify_backup",
                lambda _backup_id: {"status": "ok"},
            )

            with pytest.raises(BackupError, match="Invalid restored knowledge base"):
                restore_backup(backup_id)

            assert marker.read_text(encoding="utf-8") == "live"
            KnowledgeBaseStore(
                workspace_dir / "knowledge_bases.json"
            ).list()
        finally:
            bm.BACKUP_DIR, bm.CHROMA_DIR, bm.DATA_DIR, bm.UPLOAD_DIR = original

    def test_restore_rejects_corrupted_backup_without_touching_live_data(
        self, tmp_path, tmp_backup_dir, tmp_chroma_dir
    ):
        import app.utils.vector_store.backup_manager as bm

        original = (bm.BACKUP_DIR, bm.CHROMA_DIR, bm.DATA_DIR, bm.UPLOAD_DIR)
        data_dir = tmp_path / "data"
        upload_dir = tmp_path / "uploads"
        data_dir.mkdir()
        upload_dir.mkdir()
        (data_dir / "settings.json").write_text('{"state": "backup"}', encoding="utf-8")
        bm.BACKUP_DIR = tmp_backup_dir
        bm.CHROMA_DIR = tmp_chroma_dir
        bm.DATA_DIR = data_dir
        bm.UPLOAD_DIR = upload_dir

        try:
            backup_id = create_backup()["id"]
            (data_dir / "settings.json").write_text('{"state": "live"}', encoding="utf-8")
            (tmp_backup_dir / backup_id / "data" / "settings.json").write_text("corrupted", encoding="utf-8")

            with pytest.raises(BackupError, match="integrity verification"):
                restore_backup(backup_id)

            assert (data_dir / "settings.json").read_text(encoding="utf-8") == '{"state": "live"}'
        finally:
            bm.BACKUP_DIR, bm.CHROMA_DIR, bm.DATA_DIR, bm.UPLOAD_DIR = original

    def test_restore_rolls_back_when_restored_kb_catalog_is_invalid(
        self,
        tmp_path,
        tmp_backup_dir,
        tmp_chroma_dir,
        monkeypatch,
    ):
        import app.utils.vector_store.backup_manager as bm
        from app.utils.knowledge_base_store import KnowledgeBaseStore

        original = (bm.BACKUP_DIR, bm.CHROMA_DIR, bm.DATA_DIR, bm.UPLOAD_DIR)
        data_dir = tmp_path / "data"
        workspace_dir = data_dir / "workspaces" / "alice"
        workspace_dir.mkdir(parents=True)
        KnowledgeBaseStore(workspace_dir / "knowledge_bases.json").ensure_default()
        marker = data_dir / "marker.txt"
        marker.write_text("backup", encoding="utf-8")
        upload_dir = tmp_path / "uploads"
        upload_dir.mkdir()
        bm.BACKUP_DIR = tmp_backup_dir
        bm.CHROMA_DIR = tmp_chroma_dir
        bm.DATA_DIR = data_dir
        bm.UPLOAD_DIR = upload_dir

        try:
            backup_id = create_backup()["id"]
            marker.write_text("live", encoding="utf-8")
            backup_catalog = (
                tmp_backup_dir
                / backup_id
                / "data"
                / "workspaces"
                / "alice"
                / "knowledge_bases.json"
            )
            backup_catalog.write_text("{invalid", encoding="utf-8")
            monkeypatch.setattr(
                bm,
                "verify_backup",
                lambda _backup_id: {"status": "ok"},
            )

            with pytest.raises(BackupError, match="Invalid restored knowledge base"):
                restore_backup(backup_id)

            assert marker.read_text(encoding="utf-8") == "live"
            KnowledgeBaseStore(
                data_dir / "workspaces" / "alice" / "knowledge_bases.json"
            ).list()
        finally:
            bm.BACKUP_DIR, bm.CHROMA_DIR, bm.DATA_DIR, bm.UPLOAD_DIR = original

    def test_encrypted_backup_has_no_plaintext_snapshot_and_can_restore(
        self, tmp_path, tmp_backup_dir, tmp_chroma_dir, monkeypatch
    ):
        if not shutil.which("openssl"):
            pytest.skip("openssl is required for encrypted backup test")
        import app.utils.vector_store.backup_manager as bm

        original = (bm.BACKUP_DIR, bm.CHROMA_DIR, bm.DATA_DIR, bm.UPLOAD_DIR)
        data_dir = tmp_path / "data"
        upload_dir = tmp_path / "uploads"
        data_dir.mkdir()
        upload_dir.mkdir()
        (data_dir / "secret.txt").write_text("sensitive backup data", encoding="utf-8")
        bm.BACKUP_DIR = tmp_backup_dir
        bm.CHROMA_DIR = tmp_chroma_dir
        bm.DATA_DIR = data_dir
        bm.UPLOAD_DIR = upload_dir
        monkeypatch.setenv("BACKUP_ENCRYPTION_KEY", "correct horse battery staple")

        try:
            backup_id = create_backup()["id"]
            backup_path = tmp_backup_dir / backup_id
            assert (backup_path / f"{backup_id}.tar.gz.enc").exists()
            assert not (backup_path / "data").exists()
            assert verify_backup(backup_id)["status"] == "ok"

            (data_dir / "secret.txt").write_text("mutated", encoding="utf-8")
            assert restore_backup(backup_id)["status"] == "success"
            assert (data_dir / "secret.txt").read_text(encoding="utf-8") == "sensitive backup data"
        finally:
            bm.BACKUP_DIR, bm.CHROMA_DIR, bm.DATA_DIR, bm.UPLOAD_DIR = original

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

    def test_schedule_backup_forwards_workspace_roots(self, monkeypatch, tmp_path):
        import app.utils.vector_store.backup_manager as bm

        captured = []
        workspace_data = tmp_path / "workspace_data"
        workspace_uploads = tmp_path / "workspace_uploads"

        def fake_create_backup(**kwargs):
            captured.append(kwargs)
            return {"status": "success", "id": "scheduled"}

        monkeypatch.setattr(bm, "create_backup", fake_create_backup)
        monkeypatch.setattr(bm, "apply_retention", lambda: [])

        result = bm.schedule_backup(
            workspace_data_dir=workspace_data,
            workspace_upload_dir=workspace_uploads,
        )

        assert result["id"] == "scheduled"
        assert captured == [
            {
                "workspace_data_dir": workspace_data,
                "workspace_upload_dir": workspace_uploads,
            }
        ]

    def test_start_scheduler_updates_workspace_roots(self, monkeypatch, tmp_path):
        import app.utils.vector_store.backup_manager as bm

        previous_scheduler = bm._scheduler
        scheduler = bm.BackupScheduler(enabled=False)
        captured = []
        workspace_data = tmp_path / "workspace_data"
        workspace_uploads = tmp_path / "workspace_uploads"
        monkeypatch.setattr(
            bm,
            "schedule_backup",
            lambda **kwargs: captured.append(kwargs) or {"status": "success"},
        )
        bm._scheduler = scheduler

        try:
            returned = bm.start_scheduler(
                workspace_data_dir=workspace_data,
                workspace_upload_dir=workspace_uploads,
            )
            returned._do_backup(2)
        finally:
            bm._scheduler = previous_scheduler

        assert returned is scheduler
        assert captured == [
            {
                "workspace_data_dir": workspace_data,
                "workspace_upload_dir": workspace_uploads,
            }
        ]
