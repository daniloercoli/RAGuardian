"""Local backup storage."""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path

from utils.logging_config import APP_LOGGER as log


class LocalBackupStorage:
    """Local disk backup storage with compressed tar archives."""

    def __init__(self, backup_dir: str = "app/backups") -> None:
        self.backup_dir = Path(backup_dir)
        self.backup_dir.mkdir(parents=True, exist_ok=True)

    # ── upload ──────────────────────────────────────────────────────
    def upload(self, backup_path: Path, backup_id: str) -> dict | None:
        """Copy a prepared backup folder into the backup store."""
        target = self.backup_dir / backup_id

        if target.exists():
            log.warning("Backup %s already exists, skipping upload", backup_id)
            return None

        try:
            shutil.copytree(backup_path, target)
            return {"id": backup_id, "path": str(target), "timestamp": time.time()}
        except shutil.Error as e:
            log.error("Failed to copy backup %s: %s", backup_id, e)
            return None

    # ── download ────────────────────────────────────────────────────
    def download(self, backup_id: str, destination: Path) -> bool:
        source = self.backup_dir / backup_id
        if not source.exists():
            log.error("Backup %s not found for download", backup_id)
            return False
        try:
            shutil.copytree(source, destination)
            return True
        except shutil.Error as e:
            log.error("Failed to download backup %s: %s", backup_id, e)
            return False

    # ── list ────────────────────────────────────────────────────────
    def list(self) -> list[dict]:
        entries: list[dict] = []
        if not self.backup_dir.exists():
            return entries
        for folder in sorted(self.backup_dir.iterdir()):
            if not folder.is_dir():
                continue
            manifest = folder / "manifest.json"
            if manifest.exists():
                try:
                    info = json.loads(manifest.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    info = {}
            else:
                info = {}
            size = sum(f.stat().st_size for f in folder.rglob("*") if f.is_file())
            entries.append({
                "id": folder.name,
                "type": "local",
                "size_bytes": size,
                "document_count": info.get("document_count", 0),
                "created_at": info.get("created_at", folder.stat().st_ctime),
                "checksum": info.get("checksum", ""),
                "path": str(folder),
            })
        return entries

    # ── delete ──────────────────────────────────────────────────────
    def delete(self, backup_id: str) -> bool:
        target = self.backup_dir / backup_id
        if not target.exists():
            return True
        try:
            shutil.rmtree(target)
            return True
        except shutil.Error as e:
            log.error("Failed to delete backup %s: %s", backup_id, e)
            return False


def create_backup_storage() -> LocalBackupStorage:
    """Create the local backup storage."""
    backup_dir = os.getenv("BACKUP_DIR", "app/backups")
    return LocalBackupStorage(backup_dir=backup_dir)
