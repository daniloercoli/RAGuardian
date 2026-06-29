"""Backup storage abstraction.

Supports:
  - local: file-system copy/restore with tar.gz compression
  - http: upload/download to a generic HTTP multipart endpoint
"""

from __future__ import annotations

import glob as glob_module
import hashlib
import hmac
import json
import os
import shutil
import tarfile
import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from utils.logging_config import APP_LOGGER as log


class BackupStorage(ABC):
    """Contract for backup storage backends."""

    @abstractmethod
    def upload(self, backup_path: Path, backup_id: str) -> dict | None: ...

    @abstractmethod
    def download(self, backup_id: str, destination: Path) -> bool: ...

    @abstractmethod
    def list(self) -> list[dict]: ...

    @abstractmethod
    def delete(self, backup_id: str) -> bool: ...


class LocalBackupStorage(BackupStorage):
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


class HttpBackupStorage(BackupStorage):
    """Upload/download to a generic HTTP endpoint.

    Expected server semantics (must be implemented by the remote side):
      GET  <url>/list           → JSON array of backup descriptors
      POST <url>/upload        → multipart/form-data with file + metadata JSON
      GET  <url>/download/<id> → file download
      DELETE <url>/delete/<id> → delete backup

    All calls honour BACKUP_REMOTE_AUTH (Bearer token or empty string).
    """

    def __init__(self, url: str = "", secret: Optional[str] = None) -> None:
        self.url = url.rstrip("/")
        self._secret = secret

    # ── _request helper ─────────────────────────────────────────────
    def _headers(self) -> dict[str, str]:
        headers: dict[str, str] = {"Accept": "application/json"}
        if self._secret:
            headers["Authorization"] = f"Bearer {self._secret}"
        return headers

    # ── upload ──────────────────────────────────────────────────────
    def upload(self, backup_path: Path, backup_id: str) -> dict | None:
        """Upload tar.gz to remote HTTP endpoint."""
        import requests

        tar_path = backup_path / f"{backup_id}.tar.gz"
        if not tar_path.exists():
            log.error("HTTP upload: tar %s not found", tar_path)
            return None

        with tar_path.open("rb") as f:
            res = requests.post(
                f"{self.url}/upload",
                headers=self._headers(),
                files={"file": (f"{backup_id}.tar.gz", f, "application/gzip")},
                data={"backup_id": backup_id},
                timeout=300,
            )
        if res.status_code >= 400:
            log.error("HTTP upload failed %s: %s", res.status_code, res.text[:200])
            return None
        try:
            return res.json()
        except ValueError:
            return {"id": backup_id, "status": "uploaded"}

    # ── download ────────────────────────────────────────────────────
    def download(self, backup_id: str, destination: Path) -> bool:
        import requests

        res = requests.get(
            f"{self.url}/download/{backup_id}",
            headers=self._headers(),
            timeout=300,
        )
        if res.status_code != 200:
            log.error("HTTP download failed %s: %s", res.status_code, res.text[:200])
            return False
        with (destination / f"{backup_id}.tar.gz").open("wb") as out:
            out.write(res.content)
        return True

    # ── list ────────────────────────────────────────────────────────
    def list(self) -> list[dict]:
        import requests

        res = requests.get(f"{self.url}/list", headers=self._headers(), timeout=30)
        if res.status_code != 200:
            return []
        try:
            data = res.json()
            return data if isinstance(data, list) else []
        except ValueError:
            return []

    # ── delete ──────────────────────────────────────────────────────
    def delete(self, backup_id: str) -> bool:
        import requests

        res = requests.delete(
            f"{self.url}/delete/{backup_id}",
            headers=self._headers(),
            timeout=30,
        )
        return res.status_code < 400


def create_backup_storage() -> BackupStorage:
    """Factory that creates a backup storage backend from env vars."""
    storage_type = os.getenv("BACKUP_REMOTE_TYPE", "local").strip().lower()
    if storage_type == "http":
        url = os.getenv("BACKUP_REMOTE_URL", "")
        secret = os.getenv("BACKUP_REMOTE_AUTH", "")
        return HttpBackupStorage(url=url or "http://127.0.0.1:8080", secret=secret)
    backup_dir = os.getenv("BACKUP_DIR", "app/backups")
    return LocalBackupStorage(backup_dir=backup_dir)
