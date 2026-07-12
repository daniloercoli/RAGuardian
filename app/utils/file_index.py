import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from utils.file_lock import ProcessSafeFileLock


class FileIndex:
    _locks: Dict[str, ProcessSafeFileLock] = {}
    _locks_guard = threading.Lock()

    def __init__(self, path: Optional[str] = None):
        configured = path or os.getenv("RAG_FILE_INDEX", "app/data/files.json")
        self.path = Path(configured)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_key = str(self.path.resolve())
        with self._locks_guard:
            self._lock = self._locks.setdefault(
                lock_key,
                ProcessSafeFileLock(self.path.with_suffix(self.path.suffix + ".lock")),
            )

    def list(self) -> List[dict]:
        with self._lock:
            if not self.path.exists():
                return []
            try:
                with self.path.open("r", encoding="utf-8") as f:
                    data = json.load(f)
            except (json.JSONDecodeError, OSError):
                return []
            return data if isinstance(data, list) else []

    def record(
        self,
        filename: str,
        path: str,
        chunks: int,
        status: str = "indexed",
        error: str = "",
        metadata: Optional[dict] = None,
    ) -> dict:
        entry = {
            "filename": filename,
            "path": path,
            "chunks": chunks,
            "status": status,
            "error": error,
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        if metadata:
            entry.update(metadata)
        with self._lock:
            entries = [item for item in self._list_unlocked() if item.get("filename") != filename]
            entries.insert(0, entry)
            self._save_unlocked(entries)
        return entry

    def get(self, filename: str) -> Optional[dict]:
        for item in self.list():
            if item.get("filename") == filename:
                return item
        return None

    def remove(self, filename: str) -> Optional[dict]:
        removed = None
        entries = []
        with self._lock:
            for item in self._list_unlocked():
                if item.get("filename") == filename:
                    removed = item
                    continue
                entries.append(item)
            if removed:
                self._save_unlocked(entries)
        return removed

    def _save(self, entries: List[dict]) -> None:
        with self._lock:
            self._save_unlocked(entries)

    def _list_unlocked(self) -> List[dict]:
        if not self.path.exists():
            return []
        try:
            with self.path.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            return []
        return data if isinstance(data, list) else []

    def _save_unlocked(self, entries: List[dict]) -> None:
        fd, tmp_name = tempfile.mkstemp(
            prefix=".files.",
            suffix=".json",
            dir=str(self.path.parent),
            text=True,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(entries, f, indent=2, sort_keys=True)
                f.write("\n")
            os.replace(tmp_name, self.path)
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
