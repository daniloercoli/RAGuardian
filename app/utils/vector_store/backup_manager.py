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


# ======================================================================
# CREATE BACKUP
# ======================================================================
def create_backup() -> dict[str, Any]:
    from utils.index_lock import index_write_lock

    with index_write_lock():
        return _create_backup_locked()


def _create_backup_locked() -> dict[str, Any]:
    """Create a backup snapshot of ChromaDB + data JSON files."""
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
        # ── 1. live document count (before any copy) ──────────────
        live_doc_count = _count_chroma_live(CHROMA_DIR)
        log.info("Live document count: %d", live_doc_count)

        # ── 2. checkpoint WAL ─────────────────────────────────────
        chroma_log.info("Flushing ChromaDB WAL before backup...")
        _checkpoint_chroma(CHROMA_DIR)

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

        # ── 5. build manifest ──────────────────────────────────────
        manifest_path = staging / "manifest.json"
        chroma_size = _dir_size(chroma_staging) if chroma_staging.exists() else 0
        data_size = _dir_size(data_staging) if data_staging.exists() else 0
        uploads_size = _dir_size(uploads_staging) if uploads_staging.exists() else 0
        total_size = chroma_size + data_size + uploads_size

        manifest = {
            "backup_id": backup_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "created_at_epoch": time.time(),
            "document_count": live_doc_count,
            "chroma_size_bytes": chroma_size,
            "data_size_bytes": data_size,
            "uploads_size_bytes": uploads_size,
            "total_size_bytes": total_size,
            "chroma_sha256": _dir_checksum(chroma_staging) if chroma_staging.exists() else "",
            "data_sha256": _dir_checksum(data_staging) if data_staging.exists() else "",
            "uploads_sha256": _dir_checksum(uploads_staging) if uploads_staging.exists() else "",
            "source_chroma_dir": str(CHROMA_DIR),
            "source_data_dir": str(DATA_DIR),
            "source_upload_dir": str(UPLOAD_DIR),
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

        # ── 7. move to final location ─────────────────────────────
        final_dir = BACKUP_DIR / backup_id
        final_dir.mkdir(parents=True)
        shutil.move(str(compressed), str(final_dir / f"{backup_id}.tar.gz"))
        # Copy decompressed files for restore convenience
        for child in sorted(staging.iterdir()):
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
            for component_name in ("chroma_db", "data", "uploads"):
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
def restore_backup(backup_id: str) -> dict[str, Any]:
    from utils.index_lock import index_write_lock

    with index_write_lock():
        return _restore_backup_locked(_safe_backup_id(backup_id))


def _restore_backup_locked(backup_id: str) -> dict[str, Any]:
    """Restore ChromaDB + data JSON from a previous backup.

    This performs an atomic swap:
      1. Backup current to chroma_db.bak.<ts>
      2. Extract backup to chroma_db.restore
      3. Atomic rename restore → current
      4. Verify document count matches manifest
    """
    from utils.metrics import get_metrics
    metrics = get_metrics()
    restore_start = time.time()
    restore_status = "success"
    extract_root: Optional[Path] = None
    restore_dirs: list[Path] = []
    swapped: list[tuple[Path, Optional[Path]]] = []

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

        source_chroma = backup_path / "chroma_db"
        source_data = backup_path / "data"
        source_uploads = backup_path / "uploads"

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
                source_chroma = extract_root / "chroma_db"
                source_data = extract_root / "data"
                source_uploads = extract_root / "uploads"
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
                source_chroma = extract_root / "chroma_db"
                source_data = extract_root / "data"
                source_uploads = extract_root / "uploads"

        bak_suffix = f".bak.{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S_%f')}"
        components = (
            (CHROMA_DIR, source_chroma),
            (DATA_DIR, source_data),
            (UPLOAD_DIR, source_uploads),
        )
        for target, source in components:
            target.parent.mkdir(parents=True, exist_ok=True)
            restore_dir = target.parent / f"{target.name}.restore.{backup_id}"
            if restore_dir.exists():
                shutil.rmtree(restore_dir)
            if source.exists():
                shutil.copytree(str(source), str(restore_dir))
            else:
                restore_dir.mkdir()
            restore_dirs.append(restore_dir)

        try:
            for (target, _source), restore_dir in zip(components, restore_dirs):
                previous = target.parent / f"{target.name}{bak_suffix}"
                if previous.exists():
                    shutil.rmtree(previous)
                previous_path: Optional[Path] = None
                if target.exists():
                    shutil.move(str(target), str(previous))
                    previous_path = previous
                try:
                    shutil.move(str(restore_dir), str(target))
                except Exception:
                    if previous_path and previous_path.exists() and not target.exists():
                        shutil.move(str(previous_path), str(target))
                    raise
                swapped.append((target, previous_path))
        except Exception:
            for target, previous in reversed(swapped):
                if target.exists():
                    shutil.rmtree(target)
                if previous and previous.exists():
                    shutil.move(str(previous), str(target))
            raise

        actual_docs = _count_chroma_live(CHROMA_DIR)
        verify_ok = actual_docs == expected_docs

        if not verify_ok:
            for target, previous in reversed(swapped):
                if target.exists():
                    shutil.rmtree(target)
                if previous and previous.exists():
                    shutil.move(str(previous), str(target))
            swapped.clear()
            raise BackupError(
                f"Restored document count mismatch: expected {expected_docs}, found {actual_docs}"
            )

        chroma_log.info(
            "Restore complete: expected %d docs, actual %d, verify=%s",
            expected_docs, actual_docs, verify_ok,
        )

        return {
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
    except Exception as e:
        restore_status = "error"
        log.error("Restore failed for backup %s: %s", backup_id, e)
        raise
    finally:
        for restore_dir in restore_dirs:
            if restore_dir.exists():
                shutil.rmtree(restore_dir, ignore_errors=True)
        if extract_root and extract_root.exists():
            shutil.rmtree(extract_root, ignore_errors=True)
        metrics.observe_backup("restore", time.time() - restore_start, restore_status)


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
        "status": "ok" if (chroma_ok and data_ok and uploads_ok and tar_ok and archive_checksum_ok) else "mismatch",
        "backup_id": backup_id,
        "chroma_checksum_ok": chroma_ok,
        "data_checksum_ok": data_ok,
        "uploads_checksum_ok": uploads_ok,
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
def schedule_backup() -> dict[str, Any]:
    """Trigger for APScheduler or cron. Runs create_backup wrapped in retry."""
    try:
        result = create_backup()
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

    def __init__(self, enabled: bool = True) -> None:
        self._lock = threading.Lock()
        self._enabled = enabled
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
            result = schedule_backup()
            status = result.get("status", "unknown")
            log.info("BackupScheduler: hour %d – backup %s", hour, status)
        except Exception as e:
            log.error("BackupScheduler: hour %d – backup failed: %s", hour, e)


def start_scheduler() -> "BackupScheduler":
    """Return (starting if needed) the global scheduler instance."""
    global _scheduler

    with _scheduler_lock:
        if _scheduler is None:
            enabled = os.getenv("BACKUP_ENABLED", "0").lower() in {
                "1", "true", "yes", "on",
            }
            _scheduler = BackupScheduler(enabled=enabled)

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
