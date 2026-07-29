"""Materialize external source snapshots under the configured upload folder."""

from __future__ import annotations

import hashlib
import os
import re
import uuid
from pathlib import Path

from utils.data_ingestion.base import IngestionAttachment, IngestionItem
from utils.document_indexer import DOCUMENT_INDEX_EXTENSIONS


class IngestionStorage:
    def __init__(self, upload_folder: str):
        self.upload_root = Path(upload_folder).resolve()

    def materialize_item(
        self,
        data_source_id: str,
        item: IngestionItem,
        *,
        staged: bool = False,
    ) -> dict:
        extension = _safe_extension(item.extension)
        filename = stable_filename(data_source_id, item.remote_id, item.filename, extension)
        path = self._source_dir(data_source_id) / filename
        write_path = _staged_path(path) if staged else path
        if item.content_bytes is not None:
            write_path.write_bytes(item.content_bytes)
        else:
            write_path.write_text(item.content or "", encoding="utf-8")
        result = {
            "filename": filename,
            "file_path": str(path),
            "extension": extension,
        }
        if staged:
            result["staged_file_path"] = str(write_path)
        return result

    def materialize_attachment(
        self,
        data_source_id: str,
        parent: IngestionItem,
        attachment: IngestionAttachment,
        *,
        staged: bool = False,
    ) -> dict | None:
        extension = _safe_extension(attachment.extension)
        if extension not in DOCUMENT_INDEX_EXTENSIONS:
            return None
        remote_id = attachment.remote_id or f"{parent.remote_id}:{attachment.filename}"
        filename = stable_filename(data_source_id, remote_id, attachment.filename, extension)
        path = self._source_dir(data_source_id) / filename
        write_path = _staged_path(path) if staged else path
        write_path.write_bytes(attachment.content or b"")
        result = {
            "filename": filename,
            "file_path": str(path),
            "extension": extension,
        }
        if staged:
            result["staged_file_path"] = str(write_path)
        return result

    def _source_dir(self, data_source_id: str) -> Path:
        source_dir = (self.upload_root / "external" / _slug(data_source_id)).resolve()
        if not str(source_dir).startswith(str(self.upload_root) + os.sep):
            raise ValueError("Unsafe data source path")
        source_dir.mkdir(parents=True, exist_ok=True)
        return source_dir


def stable_filename(source_id: str, remote_id: str, original_name: str, extension: str) -> str:
    extension = _safe_extension(extension)
    remote_hash = hashlib.sha256(str(remote_id or original_name).encode("utf-8")).hexdigest()[:16]
    source_slug = _slug(source_id) or "source"
    name = _slug(Path(original_name or "document").stem) or "document"
    return f"{source_slug}__{remote_hash}__{name}.{extension}"


def _staged_path(path: Path) -> Path:
    return path.with_name(
        f".{path.name}.{uuid.uuid4().hex}.staged-ingestion"
    )


def _safe_extension(extension: str) -> str:
    extension = str(extension or "").lower().lstrip(".")
    return extension if re.fullmatch(r"[a-z0-9]{1,12}", extension) else "txt"


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_.-]+", "-", str(value or "").strip().lower()).strip("-._")
    return slug[:80]
