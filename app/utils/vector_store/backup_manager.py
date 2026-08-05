"""Backup manager – create, restore, verify and retain ChromaDB snapshots.

Public API:
  - create_backup()           → creates local snapshot + optional remote upload
  - restore_backup(backup_id) → swaps ChromaDB to a previous snapshot
  - list_backups()            → returns catalog of available backups
  - delete_backup(backup_id)  → removes a backup
  - verify_backup(backup_id)  → integrity check via checksum
  - apply_retention()         → prune old backups past retention policy
  - schedule_backup()         → trigger scheduled backup
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tarfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from config import Config
from utils.logging_config import APP_LOGGER as log
from utils.logging_config import CHROMA_LOGGER as chroma_log
from .backup_storage import create_backup_storage

# ── chromadb import (optional – used for live document count) ────
_chromadb_module = None
try:
    import chromadb as _chromadb_module
except ImportError:
    pass

# ── dirs ──────────────────────────────────────────────────────────
BACKUP_DIR = Path(os.getenv("BACKUP_DIR", "app/backups"))
CHROMA_DIR = Path(Config.paths.chroma_persist_dir)
DATA_DIR = Path(Config.paths.data_dir)
UPLOAD_DIR = Path(Config.paths.upload_folder)
WORKSPACE_DATA_DIR = Path(
    os.getenv("RAG_WORKSPACE_DATA_DIR", str(DATA_DIR / "workspaces"))
)
WORKSPACE_UPLOAD_DIR = Path(
    os.getenv("RAG_WORKSPACE_UPLOAD_DIR", str(UPLOAD_DIR / "workspaces"))
)
_INITIAL_WORKSPACE_DATA_DIR = WORKSPACE_DATA_DIR
_INITIAL_WORKSPACE_UPLOAD_DIR = WORKSPACE_UPLOAD_DIR


class BackupError(RuntimeError):
    """Raised on backup/restore failures."""


def _safe_backup_id(value: str) -> str:
    backup_id = str(value or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", backup_id):
        raise BackupError("Invalid backup id")
    return backup_id


# ======================================================================
# CHECKSUM HELPERS
# ======================================================================
def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


# ======================================================================
# SQLITE WAL FLUSH (must run before snapshot)
# ======================================================================
def _checkpoint_chroma(path: Path) -> None:
    """Force ChromaDB SQLite WAL → FULL so hot snapshot is consistent."""
    db_file = path / "chromadb.sqlite3"
    if not db_file.exists():
        return
    try:
        conn = sqlite3.connect(str(db_file))
        cur = conn.cursor()
        cur.execute("PRAGMA wal_checkpoint(PASSIVE)")
        result = cur.fetchone()
        conn.close()
        chroma_log.info("Chroma WAL checkpoint: %s", result)
    except sqlite3.Error as e:
        chroma_log.warning("WAL checkpoint failed: %s – snapshot may be stale", e)


def _checkpoint_conversation_db(workspace_data_dir: Path) -> dict[str, Any]:
    """Checkpoint the conversation history SQLite WAL before snapshot.

    Returns a small manifest summary (present, size, sha256) so callers can
    record it without re-statting the file later.
    """

    summary: dict[str, Any] = {
        "present": False,
        "size_bytes": 0,
        "sha256": "",
    }
    db_file = workspace_data_dir / "conversation_history.db"
    if not db_file.exists():
        return summary
    try:
        conn = sqlite3.connect(str(db_file))
        cur = conn.cursor()
        cur.execute("PRAGMA wal_checkpoint(PASSIVE)")
        cur.fetchone()
        conn.close()
        log.info("Conversation history WAL checkpointed")
    except sqlite3.Error as e:
        log.warning("Conversation history WAL checkpoint failed: %s", e)
    summary["present"] = True
    summary["size_bytes"] = db_file.stat().st_size
    summary["sha256"] = _sha256(db_file)
    return summary


# ======================================================================
# CREATE BACKUP
# ======================================================================
def create_backup(
    *,
    workspace_data_dir: str | os.PathLike[str] | None = None,
    workspace_upload_dir: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    from utils.index_lock import index_write_lock, lifecycle_read_lock

    with lifecycle_read_lock():
        with index_write_lock():
            return _create_backup_locked(
                workspace_data_dir=workspace_data_dir,
                workspace_upload_dir=workspace_upload_dir,
            )


def _create_backup_locked(
    *,
    workspace_data_dir: str | os.PathLike[str] | None = None,
    workspace_upload_dir: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Create a backup snapshot of ChromaDB + data JSON files."""
    from utils.index_lock import assert_distributed_locks_healthy
    from utils.metrics import get_metrics
    metrics = get_metrics()
    start_time = time.time()
    status = "success"
    backup_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    staging = BACKUP_DIR / f"__staging__{backup_id}"
    final_dir: Optional[Path] = None

    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)

    try:
        workspace_data_dir, workspace_upload_dir = _configured_workspace_dirs(
            workspace_data_dir=workspace_data_dir,
            workspace_upload_dir=workspace_upload_dir,
        )

        # ── 1. live document count (before any copy) ──────────────
        live_doc_count = _count_chroma_live(CHROMA_DIR)
        log.info("Live document count: %d", live_doc_count)

        # ── 2. checkpoint WAL ─────────────────────────────────────
        chroma_log.info("Flushing ChromaDB WAL before backup...")
        _checkpoint_chroma(CHROMA_DIR)
        conversation_db_summary = _checkpoint_conversation_db(workspace_data_dir)

        # ── 3. copy chromadb ───────────────────────────────────────
        chroma_staging = staging / "chroma_db"
        if CHROMA_DIR.exists():
            shutil.copytree(str(CHROMA_DIR), str(chroma_staging))
            chroma_log.info("ChromaDB copied to staging (%s)", chroma_staging)
        else:
            chroma_log.warning("ChromaDB directory does not exist – empty backup")

        # ── 4. copy all persistent application data and uploads ──
        data_staging = staging / "data"
        if DATA_DIR.exists():
            shutil.copytree(
                str(DATA_DIR),
                str(data_staging),
                ignore=shutil.ignore_patterns("*.lock"),
            )
        else:
            data_staging.mkdir()

        uploads_staging = staging / "uploads"
        if UPLOAD_DIR.exists():
            shutil.copytree(str(UPLOAD_DIR), str(uploads_staging))
        else:
            uploads_staging.mkdir()

        # Workspace roots can be configured outside the global data/upload
        # trees. Store them as separate archive components only when the
        # global component does not already contain them.
        workspace_data_staging, workspace_data_separate = (
            _stage_workspace_directory(
                workspace_data_dir,
                primary_source=DATA_DIR,
                primary_staging=data_staging,
                separate_staging=staging / "workspace_data",
                ignore=shutil.ignore_patterns("*.lock"),
            )
        )
        workspace_upload_staging, workspace_upload_separate = (
            _stage_workspace_directory(
                workspace_upload_dir,
                primary_source=UPLOAD_DIR,
                primary_staging=uploads_staging,
                separate_staging=staging / "workspace_uploads",
            )
        )
        assert_distributed_locks_healthy()

        # ── 5. build manifest ──────────────────────────────────────
        manifest_path = staging / "manifest.json"
        chroma_size = _dir_size(chroma_staging) if chroma_staging.exists() else 0
        data_size = _dir_size(data_staging) if data_staging.exists() else 0
        uploads_size = _dir_size(uploads_staging) if uploads_staging.exists() else 0
        workspace_data_size = _dir_size(workspace_data_staging)
        workspace_uploads_size = _dir_size(workspace_upload_staging)
        total_size = (
            chroma_size
            + data_size
            + uploads_size
            + (workspace_data_size if workspace_data_separate else 0)
            + (workspace_uploads_size if workspace_upload_separate else 0)
        )
        knowledge_base_summary = _knowledge_base_manifest_summary(
            data_staging,
            chroma_root=chroma_staging,
            uploads_root=uploads_staging,
            workspace_data_root=workspace_data_staging,
            workspace_uploads_root=workspace_upload_staging,
        )
        chat_agent_summary = _chat_agent_manifest_summary(
            data_staging,
            workspace_data_root=workspace_data_staging,
        )

        manifest = {
            "backup_id": backup_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "created_at_epoch": time.time(),
            "document_count": live_doc_count,
            "chroma_size_bytes": chroma_size,
            "data_size_bytes": data_size,
            "uploads_size_bytes": uploads_size,
            "workspace_data_size_bytes": workspace_data_size,
            "workspace_uploads_size_bytes": workspace_uploads_size,
            "total_size_bytes": total_size,
            "chroma_sha256": _dir_checksum(chroma_staging) if chroma_staging.exists() else "",
            "data_sha256": _dir_checksum(data_staging) if data_staging.exists() else "",
            "uploads_sha256": _dir_checksum(uploads_staging) if uploads_staging.exists() else "",
            "workspace_data_sha256": _dir_checksum(workspace_data_staging),
            "workspace_uploads_sha256": _dir_checksum(workspace_upload_staging),
            "source_chroma_dir": str(CHROMA_DIR),
            "source_data_dir": str(DATA_DIR),
            "source_upload_dir": str(UPLOAD_DIR),
            "source_workspace_data_dir": str(workspace_data_dir),
            "source_workspace_upload_dir": str(workspace_upload_dir),
            "workspace_data_backup_path": workspace_data_staging.relative_to(
                staging
            ).as_posix(),
            "workspace_uploads_backup_path": workspace_upload_staging.relative_to(
                staging
            ).as_posix(),
            "workspace_data_separate": workspace_data_separate,
            "workspace_uploads_separate": workspace_upload_separate,
            "knowledge_base_catalog_schema_version": 1,
            "knowledge_base_workspace_count": knowledge_base_summary[
                "workspace_count"
            ],
            "knowledge_base_count": knowledge_base_summary[
                "knowledge_base_count"
            ],
            "knowledge_base_collection_count": knowledge_base_summary[
                "collection_count"
            ],
            "knowledge_base_collection_scan_available": knowledge_base_summary[
                "collection_scan_available"
            ],
            "knowledge_bases": knowledge_base_summary["knowledge_bases"],
            "chat_agent_catalog_schema_version": 1,
            "chat_agent_count": chat_agent_summary["chat_agent_count"],
            "chat_agents": chat_agent_summary["chat_agents"],
            "conversation_db_present": conversation_db_summary["present"],
            "conversation_db_size_bytes": conversation_db_summary["size_bytes"],
            "conversation_db_sha256": conversation_db_summary["sha256"],
        }
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")

        # ── 6. compress to tar.gz ──────────────────────────────────
        compressed = staging / f"{backup_id}.tar.gz"
        with tarfile.open(str(compressed), "w:gz") as tar:
            for child in sorted(staging.iterdir()):
                if child == compressed:
                    continue
                tar.add(str(child), arcname=child.name)
        compressed_size = compressed.stat().st_size
        manifest["compressed_size_bytes"] = compressed_size
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
        assert_distributed_locks_healthy()

        # ── 7. move to final location ─────────────────────────────
        final_dir = BACKUP_DIR / backup_id
        final_dir.mkdir(parents=True)
        shutil.move(str(compressed), str(final_dir / f"{backup_id}.tar.gz"))
        # Copy decompressed files for restore convenience
        for child in sorted(staging.iterdir()):
            assert_distributed_locks_healthy()
            dest = final_dir / child.name
            if not dest.exists() or dest.is_dir():
                if child.is_dir():
                    shutil.copytree(str(child), str(dest))
                else:
                    shutil.copy2(str(child), str(dest))
        # Remove staging (ignore_errors as file may be briefly locked on Windows)
        shutil.rmtree(staging, ignore_errors=True)

        # ── 8. encrypt if configured ───────────────────────────────
        encryption_key = os.getenv("BACKUP_ENCRYPTION_KEY")
        final_archive = final_dir / f"{backup_id}.tar.gz"
        if encryption_key:
            final_archive = _encrypt_backup(final_dir, encryption_key)
            for component_name in (
                "chroma_db",
                "data",
                "uploads",
                "workspace_data",
                "workspace_uploads",
            ):
                shutil.rmtree(final_dir / component_name, ignore_errors=True)
        manifest["archive_filename"] = final_archive.name
        manifest["archive_sha256"] = _sha256(final_archive)
        (final_dir / "manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True),
            encoding="utf-8",
        )

        log.info(
            "Backup %s created: %d docs, %d bytes uncompressed, %d bytes compressed",
            backup_id, live_doc_count, total_size, compressed_size,
        )

        metrics.observe_backup("create", time.time() - start_time, status)

        return {
            "id": backup_id,
            "status": "success",
            "document_count": live_doc_count,
            "total_size_bytes": total_size,
            "compressed_size_bytes": compressed_size,
            "created_at": manifest["created_at"],
            "checksum": manifest["chroma_sha256"],
        }

    except Exception as e:
        status = "error"
        log.error("Backup failed: %s", e)
        metrics.observe_backup("create", time.time() - start_time, status)
        # Cleanup staging on failure
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        if final_dir and final_dir.exists():
            shutil.rmtree(final_dir, ignore_errors=True)
        raise BackupError(f"Backup failed: {e}") from e


# ======================================================================
# RESTORE BACKUP
# ======================================================================
def restore_backup(
    backup_id: str,
    *,
    workspace_data_dir: str | os.PathLike[str] | None = None,
    workspace_upload_dir: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    from utils.index_lock import index_write_lock, lifecycle_write_lock

    backup_id = _safe_backup_id(backup_id)
    with lifecycle_write_lock(publish=False):
        with index_write_lock():
            return _restore_backup_locked(
                backup_id,
                workspace_data_dir=workspace_data_dir,
                workspace_upload_dir=workspace_upload_dir,
            )


def _restore_backup_locked(
    backup_id: str,
    *,
    workspace_data_dir: str | os.PathLike[str] | None = None,
    workspace_upload_dir: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Restore ChromaDB + data JSON from a previous backup.

    This performs an atomic swap:
      1. Backup current to chroma_db.bak.<ts>
      2. Extract backup to chroma_db.restore
      3. Atomic rename restore → current
      4. Verify document count matches manifest
    """
    from utils.index_lock import (
        DistributedLockLeaseLostError,
        assert_distributed_locks_healthy,
    )
    from utils.metrics import get_metrics
    metrics = get_metrics()
    restore_start = time.time()
    restore_status = "success"
    extract_root: Optional[Path] = None
    restore_dirs: list[Path] = []
    swapped: list[tuple[Path, Optional[Path]]] = []
    chroma_clients_reset = False

    try:
        backup_path = BACKUP_DIR / backup_id
        if not backup_path.exists():
            raise BackupError(f"Backup {backup_id} not found")

        manifest_file = backup_path / "manifest.json"
        if not manifest_file.exists():
            raise BackupError(f"Backup {backup_id}: manifest.json missing")

        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
        expected_docs = manifest.get("document_count", 0)

        verification = verify_backup(backup_id)
        if verification.get("status") != "ok":
            raise BackupError(f"Backup {backup_id} failed integrity verification: {verification}")

        source_root = backup_path
        source_chroma = source_root / "chroma_db"
        source_data = source_root / "data"
        source_uploads = source_root / "uploads"

        if not source_chroma.exists():
            tar_path = backup_path / f"{backup_id}.tar.gz"
            encrypted_tar = backup_path / f"{backup_id}.tar.gz.enc"
            if tar_path.exists():
                extract_root = CHROMA_DIR.parent / f"__restore_extract__{backup_id}"
                if extract_root.exists():
                    shutil.rmtree(extract_root)
                extract_root.mkdir(parents=True)
                with tarfile.open(str(tar_path), "r:gz") as tar:
                    _safe_extract(tar, extract_root)
                source_root = extract_root
            elif encrypted_tar.exists():
                encryption_key = os.getenv("BACKUP_ENCRYPTION_KEY")
                if not encryption_key:
                    raise BackupError("BACKUP_ENCRYPTION_KEY is required to restore this backup")
                extract_root = CHROMA_DIR.parent / f"__restore_extract__{backup_id}"
                if extract_root.exists():
                    shutil.rmtree(extract_root)
                extract_root.mkdir(parents=True)
                decrypted_tar = extract_root / f"{backup_id}.tar.gz"
                _decrypt_backup_archive(encrypted_tar, decrypted_tar, encryption_key)
                with tarfile.open(str(decrypted_tar), "r:gz") as tar:
                    _safe_extract(tar, extract_root)
                decrypted_tar.unlink(missing_ok=True)
                source_root = extract_root

        source_chroma = source_root / "chroma_db"
        source_data = source_root / "data"
        source_uploads = source_root / "uploads"
        source_workspace_data = _manifest_component_path(
            source_root,
            manifest.get("workspace_data_backup_path"),
            fallback=source_data / "workspaces",
        )
        source_workspace_uploads = _manifest_component_path(
            source_root,
            manifest.get("workspace_uploads_backup_path"),
            fallback=source_uploads / "workspaces",
        )
        workspace_data_dir, workspace_upload_dir = _configured_workspace_dirs(
            workspace_data_dir=workspace_data_dir,
            workspace_upload_dir=workspace_upload_dir,
        )

        bak_suffix = f".bak.{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S_%f')}"
        components = _restore_components(
            source_chroma=source_chroma,
            source_data=source_data,
            source_uploads=source_uploads,
            source_workspace_data=source_workspace_data,
            source_workspace_uploads=source_workspace_uploads,
            workspace_data_dir=workspace_data_dir,
            workspace_upload_dir=workspace_upload_dir,
        )
        for target, source, overlays in components:
            target.parent.mkdir(parents=True, exist_ok=True)
            restore_dir = target.parent / f"{target.name}.restore.{backup_id}"
            if restore_dir.exists():
                shutil.rmtree(restore_dir)
            _copy_directory(source, restore_dir)
            for relative_path, overlay_source in overlays:
                overlay_target = restore_dir / relative_path
                if overlay_target.exists():
                    shutil.rmtree(overlay_target)
                overlay_target.parent.mkdir(parents=True, exist_ok=True)
                _copy_directory(overlay_source, overlay_target)
            restore_dirs.append(restore_dir)

        # Chroma caches one System per persistence path. Stop and evict the
        # live System before swapping directories, otherwise a client created
        # after the restore can keep serving the SQLite database moved to
        # ``.bak`` under the same path identifier.
        assert_distributed_locks_healthy()
        chroma_clients_reset = True
        _reset_chroma_system_cache()
        try:
            for (target, _source, _overlays), restore_dir in zip(
                components, restore_dirs
            ):
                assert_distributed_locks_healthy()
                previous = target.parent / f"{target.name}{bak_suffix}"
                if previous.exists():
                    assert_distributed_locks_healthy()
                    shutil.rmtree(previous)
                assert_distributed_locks_healthy()
                previous_path: Optional[Path] = None
                if target.exists():
                    try:
                        shutil.move(str(target), str(previous))
                    except Exception:
                        if previous.exists() and not target.exists():
                            swapped.append((target, previous))
                        raise
                    previous_path = previous
                swapped.append((target, previous_path))
                assert_distributed_locks_healthy()
                shutil.move(str(restore_dir), str(target))
                assert_distributed_locks_healthy()
        except DistributedLockLeaseLostError:
            raise
        except Exception:
            _rollback_restore_components(swapped)
            raise

        assert_distributed_locks_healthy()
        actual_docs = _count_chroma_live(CHROMA_DIR)
        _validate_restored_knowledge_bases(
            DATA_DIR,
            chroma_root=CHROMA_DIR,
            uploads_root=UPLOAD_DIR,
            manifest=manifest,
            workspace_data_root=workspace_data_dir,
            workspace_uploads_root=workspace_upload_dir,
        )
        _validate_restored_chat_agents(
            DATA_DIR,
            manifest=manifest,
            workspace_data_root=workspace_data_dir,
        )
        verify_ok = actual_docs == expected_docs

        if not verify_ok:
            raise BackupError(
                f"Restored document count mismatch: expected {expected_docs}, found {actual_docs}"
            )

        # Import registers RAG/provider invalidators; the Chroma invalidator is
        # registered by the vector-store module used during the reset above.
        import utils.rag_engine  # noqa: F401
        from utils.index_lock import (
            bump_lifecycle_generation,
            invalidate_lifecycle_caches,
        )

        assert_distributed_locks_healthy()
        invalidate_lifecycle_caches()
        chroma_log.info(
            "Restore complete: expected %d docs, actual %d, verify=%s",
            expected_docs, actual_docs, verify_ok,
        )

        result = {
            "status": "success",
            "backup_id": backup_id,
            "document_count": actual_docs,
            "verify_ok": verify_ok,
            "expected_documents": expected_docs,
            "previous_backups": {
                target.name: str(previous) if previous else ""
                for target, previous in swapped
            },
        }
        # Publish while the filesystem swap is still inside the rollback
        # transaction and both lifecycle/index locks are held. A publication
        # failure therefore restores the previous snapshot instead of leaving
        # other workers attached to stale Chroma systems.
        assert_distributed_locks_healthy()
        bump_lifecycle_generation()
        return result
    except DistributedLockLeaseLostError as e:
        restore_status = "error"
        preserved = {
            str(target): str(previous) if previous else ""
            for target, previous in swapped
        }
        if chroma_clients_reset:
            try:
                _reset_chroma_system_cache()
            except Exception as reset_error:
                log.error(
                    "Failed to reset Chroma clients after lost restore lease: %s",
                    reset_error,
                )
        log.critical(
            "Restore %s lost its distributed lease; live paths were not "
            "rolled back. Re-run the restore after lock recovery. "
            "Preserved snapshots: %s",
            backup_id,
            preserved,
        )
        raise
    except Exception as e:
        restore_status = "error"
        # Catalog validation happens after the atomic swap. Roll every
        # component back on any post-swap failure, not only on a Chroma count
        # mismatch, so an invalid KB catalog can never remain active.
        try:
            _rollback_restore_components(swapped)
        except DistributedLockLeaseLostError as lease_error:
            preserved = {
                str(target): str(previous) if previous else ""
                for target, previous in swapped
            }
            if chroma_clients_reset:
                try:
                    _reset_chroma_system_cache()
                except Exception as reset_error:
                    log.error(
                        "Failed to reset Chroma clients after skipped "
                        "restore rollback: %s",
                        reset_error,
                    )
            log.critical(
                "Restore %s failed and then lost its distributed lease; "
                "rollback was skipped. Re-run the restore after lock "
                "recovery. Preserved snapshots: %s",
                backup_id,
                preserved,
            )
            raise lease_error from e
        if chroma_clients_reset:
            try:
                _reset_chroma_system_cache()
            except Exception as reset_error:
                log.error(
                    "Failed to reset Chroma clients after restore rollback: %s",
                    reset_error,
                )
        log.error("Restore failed for backup %s: %s", backup_id, e)
        raise
    finally:
        for restore_dir in restore_dirs:
            if restore_dir.exists():
                shutil.rmtree(restore_dir, ignore_errors=True)
        if extract_root and extract_root.exists():
            shutil.rmtree(extract_root, ignore_errors=True)
        metrics.observe_backup("restore", time.time() - restore_start, restore_status)


def _rollback_restore_components(
    swapped: list[tuple[Path, Optional[Path]]],
) -> None:
    """Restore live components only while distributed ownership is valid."""

    if not swapped:
        return
    from utils.index_lock import assert_distributed_locks_healthy

    assert_distributed_locks_healthy()
    for target, previous in reversed(swapped):
        assert_distributed_locks_healthy()
        if target.exists():
            shutil.rmtree(target)
            assert_distributed_locks_healthy()
        if previous and previous.exists():
            assert_distributed_locks_healthy()
            shutil.move(str(previous), str(target))
            assert_distributed_locks_healthy()
    swapped.clear()


# ======================================================================
# LIST / DELETE / VERIFY
# ======================================================================
def list_backups() -> list[dict]:
    storage = create_backup_storage()
    return storage.list()


def delete_backup(backup_id: str) -> bool:
    storage = create_backup_storage()
    return storage.delete(_safe_backup_id(backup_id))


def verify_backup(backup_id: str) -> dict[str, Any]:
    """Verify a backup's integrity via SHA-256 checksums."""
    try:
        backup_id = _safe_backup_id(backup_id)
    except BackupError as exc:
        return {"status": "error", "error": str(exc)}
    backup_path = BACKUP_DIR / backup_id
    if not backup_path.exists():
        return {"status": "error", "error": f"Backup {backup_id} not found"}

    manifest_file = backup_path / "manifest.json"
    if not manifest_file.exists():
        return {"status": "error", "error": "manifest.json missing"}

    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    encrypted_only = (
        (backup_path / f"{backup_id}.tar.gz.enc").exists()
        and not (backup_path / "chroma_db").exists()
        and not (backup_path / "data").exists()
        and not (backup_path / "uploads").exists()
    )

    # Verify every persisted component checksum.
    chroma_dir = backup_path / "chroma_db"
    if encrypted_only:
        chroma_ok = True
    elif chroma_dir.exists():
        actual_chroma = _dir_checksum(chroma_dir)
        expected_chroma = manifest.get("chroma_sha256", "")
        chroma_ok = actual_chroma == expected_chroma
    else:
        chroma_ok = manifest.get("chroma_sha256", "") == ""

    # Verify data checksum
    data_dir = backup_path / "data"
    if encrypted_only:
        data_ok = True
    elif data_dir.exists():
        actual_data = _dir_checksum(data_dir)
        expected_data = manifest.get("data_sha256", "")
        data_ok = actual_data == expected_data
    else:
        data_ok = manifest.get("data_sha256", "") == ""

    uploads_dir = backup_path / "uploads"
    expected_uploads = manifest.get("uploads_sha256", "")
    if encrypted_only:
        uploads_ok = True
    elif uploads_dir.exists():
        uploads_ok = not expected_uploads or _dir_checksum(uploads_dir) == expected_uploads
    else:
        uploads_ok = not expected_uploads

    if encrypted_only:
        workspace_data_ok = True
        workspace_uploads_ok = True
    else:
        workspace_data_ok = _verify_manifest_directory(
            backup_path,
            manifest,
            path_key="workspace_data_backup_path",
            checksum_key="workspace_data_sha256",
        )
        workspace_uploads_ok = _verify_manifest_directory(
            backup_path,
            manifest,
            path_key="workspace_uploads_backup_path",
            checksum_key="workspace_uploads_sha256",
        )

    # Verify the compressed or encrypted archive and its checksum.
    tar_ok = (
        (backup_path / f"{backup_id}.tar.gz").exists()
        or (backup_path / f"{backup_id}.tar.gz.enc").exists()
    )
    archive_path = backup_path / str(
        manifest.get("archive_filename") or f"{backup_id}.tar.gz"
    )
    archive_checksum = manifest.get("archive_sha256", "")
    archive_checksum_ok = (
        not archive_checksum
        or (archive_path.exists() and _sha256(archive_path) == archive_checksum)
    )

    return {
        "status": "ok"
        if (
            chroma_ok
            and data_ok
            and uploads_ok
            and workspace_data_ok
            and workspace_uploads_ok
            and tar_ok
            and archive_checksum_ok
        )
        else "mismatch",
        "backup_id": backup_id,
        "chroma_checksum_ok": chroma_ok,
        "data_checksum_ok": data_ok,
        "uploads_checksum_ok": uploads_ok,
        "workspace_data_checksum_ok": workspace_data_ok,
        "workspace_uploads_checksum_ok": workspace_uploads_ok,
        "tar_archive_ok": tar_ok,
        "archive_checksum_ok": archive_checksum_ok,
        "document_count": manifest.get("document_count", 0),
    }


# ======================================================================
# RETENTION
# ======================================================================
def apply_retention() -> list[str]:
    """Delete backups older than BACKUP_RETENTION_DAYS days.

    Returns list of deleted backup IDs.
    """
    retention_days = int(os.getenv("BACKUP_RETENTION_DAYS", "7"))
    deleted: list[str] = []

    if not BACKUP_DIR.exists():
        return deleted

    if retention_days <= 0:
        return deleted

    cutoff = time.time() - (retention_days * 86400)

    for item in sorted(BACKUP_DIR.iterdir()):
        if not item.is_dir() or item.name.startswith("__"):
            continue

        manifest = item / "manifest.json"
        if not manifest.exists():
            continue

        try:
            info = json.loads(manifest.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue

        created = info.get("created_at_epoch", item.stat().st_ctime)
        if created < cutoff:
            delete_backup(item.name)
            deleted.append(item.name)
            log.info("Retention: deleted backup %s (%.1f days old)", item.name, (time.time() - created) / 86400)

    return deleted


# ======================================================================
# SCHEDULED BACKUP
# ======================================================================
def schedule_backup(
    *,
    workspace_data_dir: str | os.PathLike[str] | None = None,
    workspace_upload_dir: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Trigger for APScheduler or cron. Runs create_backup wrapped in retry."""
    try:
        result = create_backup(
            workspace_data_dir=workspace_data_dir,
            workspace_upload_dir=workspace_upload_dir,
        )
        # Auto-retention after successful backup
        try:
            apply_retention()
        except Exception as e:
            log.warning("Retention cleanup failed (backup ok): %s", e)
        return result
    except BackupError as e:
        log.error("Scheduled backup failed: %s", e)
        return {"status": "error", "error": str(e)}


# ======================================================================
# ENCRYPTION (openssl symmetric)
# ======================================================================
def _encrypt_backup(backup_dir: Path, key: str) -> Path:
    """Encrypt tar.gz with openssl AES-256-CBC if available."""
    tar_path = backup_dir / f"{backup_dir.name}.tar.gz"
    if not tar_path.exists():
        raise BackupError(f"Backup archive not found: {tar_path}")
    # Check if openssl is available
    try:
        encrypted = str(tar_path) + ".enc"
        subprocess.run(
            [
                "openssl", "enc", "-aes-256-cbc", "-salt", "-pbkdf2",
                "-in", str(tar_path),
                "-out", encrypted,
                "-pass", "env:RAG_BACKUP_PASSPHRASE",
            ],
            check=True,
            capture_output=True,
            timeout=120,
            env={
                "PATH": os.environ.get("PATH", ""),
                "RAG_BACKUP_PASSPHRASE": key,
            },
        )
        tar_path.unlink()  # Remove unencrypted copy
        log.info("Backup %s encrypted with openssl", tar_path.name)
        return Path(encrypted)
    except Exception as e:
        raise BackupError(f"Backup encryption failed: {e}") from e


def _decrypt_backup_archive(encrypted: Path, destination: Path, key: str) -> None:
    try:
        subprocess.run(
            [
                "openssl", "enc", "-d", "-aes-256-cbc", "-pbkdf2",
                "-in", str(encrypted),
                "-out", str(destination),
                "-pass", "env:RAG_BACKUP_PASSPHRASE",
            ],
            check=True,
            capture_output=True,
            timeout=120,
            env={
                "PATH": os.environ.get("PATH", ""),
                "RAG_BACKUP_PASSPHRASE": key,
            },
        )
    except Exception as exc:
        destination.unlink(missing_ok=True)
        raise BackupError(f"Backup decryption failed: {exc}") from exc



# ======================================================================
# BACKUP SCHEDULER (background thread, zero deps)
# ======================================================================
# Global single-instance state
_scheduler: Optional["BackupScheduler"] = None
_scheduler_lock = threading.Lock()


def _default_schedule_hours() -> list[int]:
    """Parse BACKUP_SCHEDULE_HOURS env var (default: ['2', '20'])."""
    raw = os.getenv("BACKUP_SCHEDULE_HOURS", "2,20").strip()
    if not raw:
        return [2, 20]
    parts = [h.strip() for h in raw.split(",")]
    valid: list[int] = []
    for p in parts:
        try:
            h = int(p)
            if 0 <= h < 24:
                valid.append(h)
        except ValueError:
            continue
    return valid if valid else [2]


def _seconds_until_hour(hour: int) -> float:
    """Seconds from now until the next occurrence of the given hour (local tz)."""
    import datetime
    now = datetime.datetime.now()
    target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if target <= now:
        target = target + datetime.timedelta(days=1)
    return (target - now).total_seconds()


class BackupScheduler:
    """Thread-safe background scheduler for periodic backups.

    Runs one timer thread per configured hour. On each trigger:
      1. create_backup() + retention cleanup + optional remote upload
      2. Reschedule next occurrence

    Usage:
        scheduler = BackupScheduler()
        scheduler.start()  # non-blocking
        ...
        scheduler.stop()   # graceful
    """

    def __init__(
        self,
        enabled: bool = True,
        *,
        workspace_data_dir: str | os.PathLike[str] | None = None,
        workspace_upload_dir: str | os.PathLike[str] | None = None,
    ) -> None:
        self._lock = threading.Lock()
        self._enabled = enabled
        self._workspace_data_dir = workspace_data_dir
        self._workspace_upload_dir = workspace_upload_dir
        self._threads: list[threading.Thread] = []
        self._stop_events: list[threading.Event] = []
        self._running = False

        log.info(
            "BackupScheduler init (enabled=%s, hours=%s)",
            enabled, os.getenv("BACKUP_SCHEDULE_HOURS", "2,20"),
        )

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._running

    def configure_workspace_dirs(
        self,
        *,
        workspace_data_dir: str | os.PathLike[str] | None = None,
        workspace_upload_dir: str | os.PathLike[str] | None = None,
    ) -> None:
        """Update the workspace roots used by future scheduled runs."""

        with self._lock:
            self._workspace_data_dir = workspace_data_dir
            self._workspace_upload_dir = workspace_upload_dir

    def start(self) -> None:
        """Start scheduled backup threads (non-blocking)."""
        with self._lock:
            if self._running:
                log.warning("BackupScheduler already running")
                return

        if not self._enabled:
            log.info("BackupScheduler: enabled=false, skipping start")
            return

        hours = _default_schedule_hours()
        for hour in hours:
            wait = _seconds_until_hour(hour)
            ev = threading.Event()
            t = threading.Thread(
                target=self._run, args=(hour, wait, ev),
                daemon=True, name=f"backup-scheduler-{hour:02d}",
            )
            with self._lock:
                self._threads.append(t)
                self._stop_events.append(ev)
            t.start()

        with self._lock:
            self._running = True

        log.info(
            "BackupScheduler started: %d hourly thread(s)", len(self._threads),
        )

    def stop(self) -> None:
        """Gracefully stop all scheduler threads."""
        with self._lock:
            if not self._running:
                return
            self._running = False

        for ev in self._stop_events:
            ev.set()

        for t in self._threads:
            t.join(timeout=5)

        with self._lock:
            self._threads.clear()
            self._stop_events.clear()

        log.info("BackupScheduler stopped")

    def _run(self, hour: int, initial_wait: float, stop: threading.Event) -> None:
        """Execute backup, wait for next cycle, repeat."""
        log.info("BackupScheduler: first run in %.0fs (hour %d)", initial_wait, hour)
        try:
            if not stop.wait(timeout=initial_wait):
                self._do_backup(hour)
        except Exception as e:
            log.error("BackupScheduler initial wait interrupted: %s", e)

        # Cycle: every 24h
        while not stop.is_set():
            wait = _seconds_until_hour(hour)
            if not stop.wait(timeout=max(1, wait)):
                self._do_backup(hour)

    def _do_backup(self, hour: int) -> None:
        """Perform a single scheduled backup + retention."""
        log.info("BackupScheduler: running scheduled backup (hour %d)", hour)
        try:
            with self._lock:
                workspace_data_dir = self._workspace_data_dir
                workspace_upload_dir = self._workspace_upload_dir
            result = schedule_backup(
                workspace_data_dir=workspace_data_dir,
                workspace_upload_dir=workspace_upload_dir,
            )
            status = result.get("status", "unknown")
            log.info("BackupScheduler: hour %d – backup %s", hour, status)
        except Exception as e:
            log.error("BackupScheduler: hour %d – backup failed: %s", hour, e)


def start_scheduler(
    *,
    workspace_data_dir: str | os.PathLike[str] | None = None,
    workspace_upload_dir: str | os.PathLike[str] | None = None,
) -> "BackupScheduler":
    """Return (starting if needed) the global scheduler instance."""
    global _scheduler

    with _scheduler_lock:
        if _scheduler is None:
            enabled = os.getenv("BACKUP_ENABLED", "0").lower() in {
                "1", "true", "yes", "on",
            }
            _scheduler = BackupScheduler(
                enabled=enabled,
                workspace_data_dir=workspace_data_dir,
                workspace_upload_dir=workspace_upload_dir,
            )
        else:
            _scheduler.configure_workspace_dirs(
                workspace_data_dir=workspace_data_dir,
                workspace_upload_dir=workspace_upload_dir,
            )

    if not _scheduler.is_running:
        _scheduler.start()

    return _scheduler


def stop_scheduler() -> None:
    """Signal the global scheduler to stop."""
    global _scheduler
    if _scheduler and _scheduler.is_running:
        _scheduler.stop()
    _scheduler = None


# ======================================================================
# LIVE CHROMA COUNT
# ======================================================================
def _reset_chroma_system_cache() -> None:
    """Stop cached Chroma systems so a path swap opens the replacement DB."""

    if _chromadb_module is None:
        return
    try:
        from utils.vector_store.chroma_persistent import (
            reset_chroma_system_cache,
        )

        reset_chroma_system_cache()
    except Exception as exc:
        raise BackupError(f"Unable to clear Chroma client cache: {exc}") from exc


def _count_chroma_live(path: Path) -> int:
    """Ask the ChromaDB client for its live document count.

    This avoids guessing internal SQLite table names and works regardless
    of the ChromaDB version.
    """
    if not path.exists():
        return 0
    if _chromadb_module is None:
        chroma_log.warning("chromadb not installed – cannot count documents live")
        return 0

    try:
        client = _chromadb_module.PersistentClient(path=str(path))
        total = 0
        for collection_info in client.list_collections():
            if hasattr(collection_info, "count"):
                total += collection_info.count()
            elif isinstance(collection_info, dict) and "collection" in collection_info:
                total += collection_info["collection"].count()
            elif isinstance(collection_info, str):
                total += client.get_collection(collection_info).count()
        return total
    except Exception as e:
        chroma_log.warning("Live ChromaDB count failed: %s", e)
        return 0


# ======================================================================
# HELPERS
# ======================================================================
def _configured_workspace_dirs(
    *,
    workspace_data_dir: str | os.PathLike[str] | None = None,
    workspace_upload_dir: str | os.PathLike[str] | None = None,
) -> tuple[Path, Path]:
    """Return the workspace roots used by the application.

    Environment values are resolved when each operation starts so a worker
    does not silently keep stale values after configuration is injected.
    The module globals remain patchable for tests and embedding callers.
    """

    workspace_data_env = os.getenv("RAG_WORKSPACE_DATA_DIR")
    workspace_upload_env = os.getenv("RAG_WORKSPACE_UPLOAD_DIR")
    if workspace_data_dir is not None:
        resolved_workspace_data_dir = Path(workspace_data_dir)
    elif workspace_data_env is not None:
        resolved_workspace_data_dir = Path(workspace_data_env)
    elif WORKSPACE_DATA_DIR != _INITIAL_WORKSPACE_DATA_DIR:
        resolved_workspace_data_dir = WORKSPACE_DATA_DIR
    else:
        resolved_workspace_data_dir = DATA_DIR / "workspaces"
    if workspace_upload_dir is not None:
        resolved_workspace_upload_dir = Path(workspace_upload_dir)
    elif workspace_upload_env is not None:
        resolved_workspace_upload_dir = Path(workspace_upload_env)
    elif WORKSPACE_UPLOAD_DIR != _INITIAL_WORKSPACE_UPLOAD_DIR:
        resolved_workspace_upload_dir = WORKSPACE_UPLOAD_DIR
    else:
        resolved_workspace_upload_dir = UPLOAD_DIR / "workspaces"
    return resolved_workspace_data_dir, resolved_workspace_upload_dir


def _relative_directory(path: Path, root: Path) -> Path | None:
    try:
        return path.resolve().relative_to(root.resolve())
    except ValueError:
        return None


def _copy_directory(
    source: Path,
    destination: Path,
    *,
    ignore: Any = None,
) -> None:
    if source.exists():
        shutil.copytree(str(source), str(destination), ignore=ignore)
    else:
        destination.mkdir(parents=True)


def _stage_workspace_directory(
    source: Path,
    *,
    primary_source: Path,
    primary_staging: Path,
    separate_staging: Path,
    ignore: Any = None,
) -> tuple[Path, bool]:
    relative_path = _relative_directory(source, primary_source)
    if relative_path is not None:
        staged = primary_staging / relative_path
        if not staged.exists():
            staged.mkdir(parents=True)
        return staged, False
    _copy_directory(source, separate_staging, ignore=ignore)
    return separate_staging, True


def _manifest_component_path(
    backup_root: Path,
    value: Any,
    *,
    fallback: Path,
) -> Path:
    """Resolve a manifest path without allowing it outside the backup."""

    if value is None:
        return fallback
    if not isinstance(value, str) or not value:
        raise BackupError("Invalid workspace component path in backup manifest")
    root = backup_root.resolve()
    candidate = (backup_root / value).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise BackupError("Unsafe workspace component path in backup manifest") from exc
    return candidate


def _verify_manifest_directory(
    backup_root: Path,
    manifest: dict,
    *,
    path_key: str,
    checksum_key: str,
) -> bool:
    """Verify an optional logical directory recorded by newer manifests."""

    if checksum_key not in manifest:
        return True
    try:
        component = _manifest_component_path(
            backup_root,
            manifest.get(path_key),
            fallback=backup_root / "__missing_workspace_component__",
        )
    except BackupError:
        return False
    expected = manifest.get(checksum_key)
    return (
        isinstance(expected, str)
        and component.is_dir()
        and _dir_checksum(component) == expected
    )


def _restore_components(
    *,
    source_chroma: Path,
    source_data: Path,
    source_uploads: Path,
    source_workspace_data: Path,
    source_workspace_uploads: Path,
    workspace_data_dir: Path,
    workspace_upload_dir: Path,
) -> list[tuple[Path, Path, list[tuple[Path, Path]]]]:
    """Build non-overlapping restore swaps and workspace overlays."""

    components: list[tuple[Path, Path, list[tuple[Path, Path]]]] = [
        (CHROMA_DIR, source_chroma, []),
        (DATA_DIR, source_data, []),
        (UPLOAD_DIR, source_uploads, []),
    ]

    def add_workspace_component(
        target: Path,
        source: Path,
        *,
        primary_index: int,
    ) -> None:
        primary_target, primary_source, overlays = components[primary_index]
        relative_path = _relative_directory(target, primary_target)
        if relative_path is not None:
            if relative_path == Path("."):
                components[primary_index] = (primary_target, source, overlays)
            else:
                overlays.append((relative_path, source))
            return
        if _relative_directory(primary_target, target) is not None:
            raise BackupError(
                f"Workspace restore target {target} cannot contain {primary_target}"
            )
        components.append((target, source, []))

    add_workspace_component(
        workspace_data_dir,
        source_workspace_data,
        primary_index=1,
    )
    add_workspace_component(
        workspace_upload_dir,
        source_workspace_uploads,
        primary_index=2,
    )

    # Overlapping top-level swaps can move a prepared restore tree out from
    # underneath a later swap. Reject such configurations before live state
    # is touched.
    for index, (target, _source, _overlays) in enumerate(components):
        for other_target, _other_source, _other_overlays in components[index + 1 :]:
            if (
                _relative_directory(target, other_target) is not None
                or _relative_directory(other_target, target) is not None
            ):
                raise BackupError(
                    f"Backup restore targets overlap: {target} and {other_target}"
                )
    return components


def _knowledge_base_manifest_summary(
    data_root: Path,
    *,
    chroma_root: Path | None = None,
    uploads_root: Path | None = None,
    workspace_data_root: Path | None = None,
    workspace_uploads_root: Path | None = None,
) -> dict:
    from utils.knowledge_base_store import (
        DEFAULT_KNOWLEDGE_BASE_NAME,
        KnowledgeBaseStore,
    )
    from utils.workspace import collection_for_knowledge_base

    workspaces_root = workspace_data_root or data_root / "workspaces"
    workspace_uploads_root = (
        workspace_uploads_root
        if workspace_uploads_root is not None
        else uploads_root / "workspaces"
        if uploads_root is not None
        else None
    )
    entries = []
    workspace_count = 0
    if not workspaces_root.exists():
        return {
            "workspace_count": 0,
            "knowledge_base_count": 0,
            "collection_count": 0,
            "collection_scan_available": True,
            "knowledge_bases": [],
        }
    for workspace_dir in sorted(path for path in workspaces_root.iterdir() if path.is_dir()):
        workspace_count += 1
        catalog_path = workspace_dir / "knowledge_bases.json"
        if catalog_path.exists():
            records = KnowledgeBaseStore(catalog_path).list()
        else:
            records = [
                {
                    "id": "default",
                    "name": DEFAULT_KNOWLEDGE_BASE_NAME,
                    "status": "active",
                }
            ]
        for record in records:
            knowledge_base_id = record["id"]
            if knowledge_base_id == "default":
                file_index = workspace_dir / "files.json"
                data_directory = workspace_dir
                upload_directory = (
                    workspace_uploads_root / workspace_dir.name
                    if workspace_uploads_root is not None
                    else None
                )
            else:
                data_directory = (
                    workspace_dir
                    / "knowledge_bases"
                    / knowledge_base_id
                )
                file_index = data_directory / "files.json"
                upload_directory = (
                    workspace_uploads_root
                    / workspace_dir.name
                    / "__knowledge_bases__"
                    / knowledge_base_id
                    if workspace_uploads_root is not None
                    else None
                )
            files = []
            if file_index.exists():
                try:
                    loaded = json.loads(file_index.read_text(encoding="utf-8"))
                    files = loaded if isinstance(loaded, list) else []
                except (json.JSONDecodeError, OSError):
                    files = []
            entries.append(
                {
                    "workspace_id": workspace_dir.name,
                    "knowledge_base_id": knowledge_base_id,
                    "status": record.get("status", "active"),
                    "collection": collection_for_knowledge_base(
                        workspace_dir.name,
                        knowledge_base_id,
                    ),
                    "tracked_files": len(files),
                    "indexed_files": sum(
                        item.get("status") == "indexed"
                        for item in files
                        if isinstance(item, dict)
                    ),
                    "chunks": sum(
                        max(0, int(item.get("chunks") or 0))
                        for item in files
                        if isinstance(item, dict)
                    ),
                    "data_directory_present": data_directory.exists(),
                    "upload_directory_present": (
                        upload_directory.exists()
                        if upload_directory is not None
                        else False
                    ),
                }
            )
    collection_counts, collection_scan_available = _chroma_collection_counts(
        chroma_root
    )
    for entry in entries:
        collection_name = entry["collection"]
        entry["collection_present"] = collection_name in collection_counts
        entry["documents"] = collection_counts.get(collection_name, 0)
    return {
        "workspace_count": workspace_count,
        "knowledge_base_count": len(entries),
        "collection_count": sum(
            entry["collection_present"] for entry in entries
        ),
        "collection_scan_available": collection_scan_available,
        "knowledge_bases": entries,
    }


def _validate_restored_knowledge_bases(
    data_root: Path,
    *,
    chroma_root: Path,
    uploads_root: Path,
    manifest: dict,
    workspace_data_root: Path | None = None,
    workspace_uploads_root: Path | None = None,
) -> None:
    from utils.knowledge_base_store import KnowledgeBaseCatalogError, KnowledgeBaseStore

    workspaces_root = workspace_data_root or data_root / "workspaces"
    if not workspaces_root.exists():
        return
    for workspace_dir in (path for path in workspaces_root.iterdir() if path.is_dir()):
        catalog_path = workspace_dir / "knowledge_bases.json"
        if not catalog_path.exists():
            continue
        try:
            KnowledgeBaseStore(catalog_path).list()
        except KnowledgeBaseCatalogError as exc:
            raise BackupError(
                f"Invalid restored knowledge base catalog for {workspace_dir.name}"
            ) from exc

    expected_entries = manifest.get("knowledge_bases")
    if not isinstance(expected_entries, list):
        return
    actual = _knowledge_base_manifest_summary(
        data_root,
        chroma_root=chroma_root,
        uploads_root=uploads_root,
        workspace_data_root=workspaces_root,
        workspace_uploads_root=workspace_uploads_root,
    )
    expected_by_key = {
        (item.get("workspace_id"), item.get("knowledge_base_id")): item
        for item in expected_entries
        if isinstance(item, dict)
    }
    actual_by_key = {
        (item.get("workspace_id"), item.get("knowledge_base_id")): item
        for item in actual["knowledge_bases"]
    }
    if set(expected_by_key) != set(actual_by_key):
        raise BackupError("Restored knowledge base catalog does not match the manifest")
    for key, expected in expected_by_key.items():
        restored = actual_by_key[key]
        for field in ("data_directory_present", "upload_directory_present"):
            if field in expected and bool(expected[field]) != bool(restored[field]):
                raise BackupError(
                    f"Restored knowledge base storage does not match the manifest: {key}"
                )
    if (
        manifest.get("knowledge_base_collection_scan_available")
        and actual["collection_scan_available"]
    ):
        for key, expected in expected_by_key.items():
            restored = actual_by_key[key]
            if (
                bool(expected.get("collection_present"))
                != bool(restored["collection_present"])
                or int(expected.get("documents") or 0)
                != int(restored["documents"])
            ):
                raise BackupError(
                    f"Restored knowledge base collection does not match the manifest: {key}"
                )


def _chat_agent_manifest_summary(
    data_root: Path,
    *,
    workspace_data_root: Path | None = None,
) -> dict:
    """Scan workspace dirs for chat_agents.json and return a manifest summary."""
    from utils.chat_agent_store import ChatAgentStore

    workspaces_root = workspace_data_root or data_root / "workspaces"
    entries: list[dict] = []
    if not workspaces_root.exists():
        return {
            "chat_agent_count": 0,
            "chat_agents": [],
        }
    for workspace_dir in sorted(path for path in workspaces_root.iterdir() if path.is_dir()):
        catalog_path = workspace_dir / "chat_agents.json"
        if not catalog_path.exists():
            continue
        try:
            agents = ChatAgentStore(catalog_path).list()
        except Exception:
            agents = []
        for agent in agents:
            entries.append(
                {
                    "workspace_id": workspace_dir.name,
                    "agent_id": agent.get("id"),
                    "name": agent.get("name"),
                }
            )
    return {
        "chat_agent_count": len(entries),
        "chat_agents": entries,
    }


def _validate_restored_chat_agents(
    data_root: Path,
    *,
    manifest: dict,
    workspace_data_root: Path | None = None,
) -> None:
    """Validate restored chat_agents.json catalogs against the manifest."""
    from utils.chat_agent_store import ChatAgentCatalogError, ChatAgentStore

    workspaces_root = workspace_data_root or data_root / "workspaces"
    if not workspaces_root.exists():
        return
    for workspace_dir in (path for path in workspaces_root.iterdir() if path.is_dir()):
        catalog_path = workspace_dir / "chat_agents.json"
        if not catalog_path.exists():
            continue
        try:
            ChatAgentStore(catalog_path).list()
        except ChatAgentCatalogError as exc:
            raise BackupError(
                f"Invalid restored chat agent catalog for {workspace_dir.name}"
            ) from exc

    expected_entries = manifest.get("chat_agents")
    if not isinstance(expected_entries, list):
        return
    actual = _chat_agent_manifest_summary(
        data_root,
        workspace_data_root=workspaces_root,
    )
    expected_by_key = {
        (item.get("workspace_id"), item.get("agent_id")): item
        for item in expected_entries
        if isinstance(item, dict)
    }
    actual_by_key = {
        (item.get("workspace_id"), item.get("agent_id")): item
        for item in actual["chat_agents"]
    }
    if set(expected_by_key) != set(actual_by_key):
        raise BackupError("Restored chat agent catalog does not match the manifest")


def _chroma_collection_counts(path: Path | None) -> tuple[dict[str, int], bool]:
    if path is None or not path.exists():
        return {}, True
    if _chromadb_module is None:
        return {}, False
    try:
        client = _chromadb_module.PersistentClient(path=str(path))
        counts: dict[str, int] = {}
        for item in client.list_collections():
            if isinstance(item, str):
                name = item
                collection = client.get_collection(name)
            elif isinstance(item, dict):
                name = str(item.get("name") or "")
                collection = item.get("collection")
                if collection is None and name:
                    collection = client.get_collection(name)
            else:
                name = str(getattr(item, "name", "") or "")
                collection = item
            if name and collection is not None:
                counts[name] = max(0, int(collection.count()))
        return counts, True
    except Exception as exc:
        chroma_log.warning("Knowledge base collection count failed: %s", exc)
        return {}, False


def _dir_size(path: Path) -> int:
    total = 0
    for f in path.rglob("*"):
        if f.is_file():
            total += f.stat().st_size
    return total


def _dir_checksum(path: Path) -> str:
    combined = hashlib.sha256()
    for f in sorted(path.rglob("*")):
        if f.is_file():
            combined.update(f"{f.relative_to(path)}:{_sha256(f)}\n".encode())
    return combined.hexdigest()


def _safe_extract(tar: tarfile.TarFile, destination: Path) -> None:
    destination = destination.resolve()
    for member in tar.getmembers():
        member_path = (destination / member.name).resolve()
        try:
            member_path.relative_to(destination)
        except ValueError:
            raise BackupError(f"Unsafe path in backup archive: {member.name}")
    try:
        tar.extractall(destination, filter="data")
    except TypeError:  # pragma: no cover - older Python 3.11 patch releases
        tar.extractall(destination)
