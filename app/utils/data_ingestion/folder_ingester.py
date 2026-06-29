"""Folder ingestion plugin - watches a local/UNC folder for files."""

from __future__ import annotations

import fnmatch
from datetime import datetime, timezone
from pathlib import Path

from utils.data_ingestion.base import IngestionItem, SyncContext, SyncResult
from utils.document_indexer import DOCUMENT_INDEX_EXTENSIONS
from utils.validators import ValidationError


class FolderIngester:
    plugin_id = "folder_watch"
    display_name = "Cartella Locale"

    def validate_config(self, config: dict) -> dict:
        config = dict(config or {})
        folder_path = str(config.get("folder_path") or "").strip()

        if not folder_path:
            raise ValidationError("Percorso cartella obbligatorio", "folder_path")

        path = Path(folder_path)
        if not path.is_absolute():
            raise ValidationError("Percorso cartella deve essere assoluto", "folder_path")
        if not path.exists():
            raise ValidationError("Cartella non esiste", "folder_path")
        if not path.is_dir():
            raise ValidationError("Percorso non è una cartella", "folder_path")

        extensions = self._normalize_extensions(
            config.get("include_extensions", "pdf,txt,md,csv")
        )
        if not extensions:
            raise ValidationError(
                "Nessuna estensione supportata configurata",
                "include_extensions",
            )

        return {
            "folder_path": str(path.resolve()),
            "recursive": _as_bool(config.get("recursive"), False),
            "include_extensions": extensions,
            "exclude_patterns": self._normalize_patterns(
                config.get("exclude_patterns", "")
            ),
            "max_files": max(1, min(_int(config.get("max_files"), 100), 500)),
        }

    def sync(self, context: SyncContext, source_config: dict, cursor: dict | None = None) -> SyncResult:
        config = self.validate_config(source_config)

        folder_path = Path(config["folder_path"])
        files = self._scan_files(folder_path, config)

        items = []
        errors = []

        for file_path in files:
            try:
                item = self._create_item(file_path, config)
                if item:
                    items.append(item)
            except Exception as exc:
                errors.append({"remote_id": str(file_path), "error": str(exc)})

        return SyncResult(
            items=items,
            cursor={"last_sync": datetime.now(timezone.utc).isoformat()},
            errors=errors,
        )

    def _scan_files(self, folder_path: Path, config: dict) -> list[Path]:
        files = []
        extensions = config["include_extensions"]
        exclude_patterns = config["exclude_patterns"]

        if config["recursive"]:
            iterator = folder_path.rglob("*")
        else:
            iterator = folder_path.iterdir()

        for path in iterator:
            if not path.is_file():
                continue

            ext = path.suffix.lower().lstrip(".")
            if ext not in extensions:
                continue

            if self._matches_exclude(str(path), exclude_patterns):
                continue

            files.append(path)

        return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)[: config["max_files"]]

    def _create_item(self, file_path: Path, config: dict) -> IngestionItem | None:
        stat = file_path.stat()
        filename = file_path.name
        extension = file_path.suffix.lower().lstrip(".")

        metadata = {
            "source_type": "folder",
            "ingestion_plugin": FolderIngester.plugin_id,
            "remote_id": str(file_path),
            "remote_url": "",
            "remote_updated_at": datetime.fromtimestamp(
                stat.st_mtime, tz=timezone.utc
            ).isoformat(timespec="seconds"),
            "file_path": str(file_path),
            "file_size": stat.st_size,
        }

        content_bytes = file_path.read_bytes()
        content = ""
        if extension in {"txt", "md", "csv"}:
            try:
                content = content_bytes.decode("utf-8-sig")
            except UnicodeDecodeError:
                content = content_bytes.decode("latin-1", errors="replace")

        return IngestionItem(
            content=content,
            content_bytes=content_bytes,
            filename=filename,
            extension=extension,
            remote_id=str(file_path),
            updated_at=metadata["remote_updated_at"],
            metadata=metadata,
            attachments=[],
        )

    def _normalize_extensions(self, value) -> set:
        if isinstance(value, set):
            parts = value
        elif isinstance(value, list):
            parts = value
        else:
            parts = str(value or "pdf,txt,md,csv").replace(";", ",").split(",")
        return {
            str(part).strip().lower().lstrip(".")
            for part in parts
            if str(part).strip().lower().lstrip(".") in DOCUMENT_INDEX_EXTENSIONS
        }

    def _normalize_patterns(self, value) -> list:
        if not value:
            return []
        if isinstance(value, list):
            return [str(p).strip() for p in value if p]
        return [p.strip() for p in str(value).split(",") if p.strip()]

    def _matches_exclude(self, path: str, patterns: list) -> bool:
        path_lower = path.lower()
        name_lower = Path(path).name.lower()
        for pattern in patterns:
            pattern_lower = pattern.lower()
            if fnmatch.fnmatch(path_lower, pattern_lower) or fnmatch.fnmatch(name_lower, pattern_lower):
                return True
        return False


def _int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_bool(value, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}
