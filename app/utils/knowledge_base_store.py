from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import threading
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

from utils.file_lock import ProcessSafeFileLock


sys.modules.setdefault("utils.knowledge_base_store", sys.modules[__name__])
sys.modules.setdefault("app.utils.knowledge_base_store", sys.modules[__name__])


KNOWLEDGE_BASE_SCHEMA_VERSION = 1
DEFAULT_KNOWLEDGE_BASE_ID = "default"
DEFAULT_KNOWLEDGE_BASE_NAME = "General"
LEGACY_DEFAULT_KNOWLEDGE_BASE_NAME = "Default"
KNOWLEDGE_BASE_STATUSES = {"active", "deleting", "delete_failed"}
KNOWLEDGE_BASE_ID_PATTERN = re.compile(r"^kb_[0-9a-f]{32}$")


class KnowledgeBaseCatalogError(RuntimeError):
    """Raised when a catalog exists but cannot be trusted."""


class KnowledgeBaseValidationError(ValueError):
    def __init__(self, message: str, *, code: str = "invalid_knowledge_base", status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status_code = status_code


class KnowledgeBaseStore:
    """Atomic JSON catalog for the knowledge bases in one workspace."""

    _locks: dict[str, ProcessSafeFileLock] = {}
    _locks_guard = threading.Lock()

    def __init__(self, path: str | Path, *, max_additional: int = 20):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.max_additional = max(0, int(max_additional))
        with self._locks_guard:
            lock_key = str(self.path.resolve())
            self._lock = self._locks.setdefault(
                lock_key,
                ProcessSafeFileLock(self.path.with_suffix(self.path.suffix + ".lock")),
            )

    def ensure_default(self) -> dict:
        with self._lock:
            if not self.path.exists():
                catalog = {
                    "schema_version": KNOWLEDGE_BASE_SCHEMA_VERSION,
                    "knowledge_bases": [_default_record()],
                }
                self._write_unlocked(catalog)
                return self._public_catalog(catalog)
            catalog = self._load_unlocked()
            if self._migrate_legacy_default_name(catalog):
                self._write_unlocked(catalog)
            return self._public_catalog(catalog)

    def list(self) -> list[dict]:
        with self._lock:
            catalog = self._load_unlocked()
            return [self._public_record(record) for record in catalog["knowledge_bases"]]

    def get(self, knowledge_base_id: str) -> dict | None:
        for record in self.list():
            if record["id"] == knowledge_base_id:
                return record
        return None

    def create(self, *, name: str, description: str = "") -> dict:
        normalized_name = validate_knowledge_base_name(name)
        normalized_description = validate_knowledge_base_description(description)
        with self._lock:
            catalog = self._load_unlocked()
            records = catalog["knowledge_bases"]
            additional_count = sum(record["id"] != DEFAULT_KNOWLEDGE_BASE_ID for record in records)
            if additional_count >= self.max_additional:
                raise KnowledgeBaseValidationError(
                    f"Limite di {self.max_additional} knowledge base aggiuntive raggiunto",
                    code="knowledge_base_limit_reached",
                    status_code=409,
                )
            self._ensure_unique_name(records, normalized_name)
            now = _now()
            record = {
                "id": f"kb_{uuid.uuid4().hex}",
                "name": normalized_name,
                "description": normalized_description,
                "status": "active",
                "created_at": now,
                "updated_at": now,
                "delete_error": "",
                "delete_requester_api_key_id": "",
            }
            records.append(record)
            self._write_unlocked(catalog)
            return self._public_record(record)

    def update(
        self,
        knowledge_base_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
    ) -> dict:
        with self._lock:
            catalog = self._load_unlocked()
            record = _find_record(catalog["knowledge_bases"], knowledge_base_id)
            if record is None:
                raise KnowledgeBaseValidationError(
                    "Knowledge base non trovata",
                    code="knowledge_base_not_found",
                    status_code=404,
                )
            if record["status"] == "deleting":
                raise KnowledgeBaseValidationError(
                    "Knowledge base in eliminazione",
                    code="knowledge_base_deleting",
                    status_code=409,
                )
            if name is not None:
                normalized_name = validate_knowledge_base_name(name)
                self._ensure_unique_name(
                    catalog["knowledge_bases"],
                    normalized_name,
                    exclude_id=knowledge_base_id,
                )
                record["name"] = normalized_name
            if description is not None:
                record["description"] = validate_knowledge_base_description(description)
            record["updated_at"] = _now()
            self._write_unlocked(catalog)
            return self._public_record(record)

    def set_status(
        self,
        knowledge_base_id: str,
        status: str,
        *,
        delete_error: str = "",
    ) -> dict:
        if status not in KNOWLEDGE_BASE_STATUSES:
            raise KnowledgeBaseValidationError("Stato knowledge base non valido")
        with self._lock:
            catalog = self._load_unlocked()
            record = _find_record(catalog["knowledge_bases"], knowledge_base_id)
            if record is None:
                raise KnowledgeBaseValidationError(
                    "Knowledge base non trovata",
                    code="knowledge_base_not_found",
                    status_code=404,
                )
            if knowledge_base_id == DEFAULT_KNOWLEDGE_BASE_ID and status != "active":
                raise KnowledgeBaseValidationError(
                    "La knowledge base default non può essere eliminata",
                    code="default_knowledge_base",
                    status_code=409,
                )
            record["status"] = status
            record["delete_error"] = str(delete_error or "")[:1000]
            if status == "active":
                record["delete_requester_api_key_id"] = ""
            record["updated_at"] = _now()
            self._write_unlocked(catalog)
            return self._public_record(record)

    def begin_delete(
        self,
        knowledge_base_id: str,
        *,
        requester_api_key_id: str = "",
    ) -> dict:
        """Atomically move a non-default knowledge base into deleting state."""
        if knowledge_base_id == DEFAULT_KNOWLEDGE_BASE_ID:
            raise KnowledgeBaseValidationError(
                "La knowledge base default non può essere eliminata",
                code="default_knowledge_base",
                status_code=409,
            )
        with self._lock:
            catalog = self._load_unlocked()
            record = _find_record(catalog["knowledge_bases"], knowledge_base_id)
            if record is None:
                raise KnowledgeBaseValidationError(
                    "Knowledge base non trovata",
                    code="knowledge_base_not_found",
                    status_code=404,
                )
            changed = False
            if record["status"] != "deleting":
                record["status"] = "deleting"
                record["delete_error"] = ""
                record["delete_requester_api_key_id"] = str(
                    requester_api_key_id or ""
                )
                changed = True
            elif (
                requester_api_key_id
                and not record.get("delete_requester_api_key_id")
            ):
                record["delete_requester_api_key_id"] = str(
                    requester_api_key_id
                )
                changed = True
            if changed:
                record["updated_at"] = _now()
                self._write_unlocked(catalog)
            return self._public_record(record)

    def delete_requester_api_key_id(
        self,
        knowledge_base_id: str,
    ) -> str:
        """Return the durable API-key owner of an in-progress deletion."""
        with self._lock:
            catalog = self._load_unlocked()
            record = _find_record(
                catalog["knowledge_bases"],
                knowledge_base_id,
            )
            if record is None:
                return ""
            return str(record.get("delete_requester_api_key_id") or "")

    def require_active(self, knowledge_base_id: str) -> dict:
        """Return a record only while it is safe for an ingestion worker to write."""
        with self._lock:
            catalog = self._load_unlocked()
            record = _find_record(catalog["knowledge_bases"], knowledge_base_id)
            if record is None:
                raise KnowledgeBaseValidationError(
                    "Knowledge base non trovata",
                    code="knowledge_base_not_found",
                    status_code=404,
                )
            if record["status"] != "active":
                code = (
                    "knowledge_base_deleting"
                    if record["status"] == "deleting"
                    else "knowledge_base_delete_failed"
                )
                raise KnowledgeBaseValidationError(
                    "Knowledge base non disponibile",
                    code=code,
                    status_code=409,
                )
            return self._public_record(record)

    def remove(self, knowledge_base_id: str) -> bool:
        if knowledge_base_id == DEFAULT_KNOWLEDGE_BASE_ID:
            raise KnowledgeBaseValidationError(
                "La knowledge base default non può essere eliminata",
                code="default_knowledge_base",
                status_code=409,
            )
        with self._lock:
            catalog = self._load_unlocked()
            original = catalog["knowledge_bases"]
            remaining = [record for record in original if record["id"] != knowledge_base_id]
            if len(remaining) == len(original):
                return False
            catalog["knowledge_bases"] = remaining
            self._write_unlocked(catalog)
            return True

    def _ensure_unique_name(
        self,
        records: list[dict],
        name: str,
        *,
        exclude_id: str | None = None,
    ) -> None:
        folded = name.casefold()
        if any(
            record["id"] != exclude_id and str(record.get("name") or "").casefold() == folded
            for record in records
        ):
            raise KnowledgeBaseValidationError(
                "Esiste già una knowledge base con questo nome",
                code="duplicate_knowledge_base_name",
                status_code=409,
            )

    @staticmethod
    def _migrate_legacy_default_name(catalog: dict) -> bool:
        records = catalog["knowledge_bases"]
        default_record = _find_record(records, DEFAULT_KNOWLEDGE_BASE_ID)
        if (
            default_record is None
            or default_record["name"] != LEGACY_DEFAULT_KNOWLEDGE_BASE_NAME
        ):
            return False
        if any(
            record["id"] != DEFAULT_KNOWLEDGE_BASE_ID
            and record["name"].casefold() == DEFAULT_KNOWLEDGE_BASE_NAME.casefold()
            for record in records
        ):
            return False
        default_record["name"] = DEFAULT_KNOWLEDGE_BASE_NAME
        default_record["updated_at"] = _now()
        return True

    def _load_unlocked(self) -> dict:
        if not self.path.exists():
            raise KnowledgeBaseCatalogError(f"Catalogo knowledge base mancante: {self.path}")
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                catalog = json.load(handle)
        except (json.JSONDecodeError, OSError) as exc:
            raise KnowledgeBaseCatalogError(
                f"Catalogo knowledge base non leggibile: {self.path}"
            ) from exc
        return _validate_catalog(catalog, self.path)

    def _write_unlocked(self, catalog: dict) -> None:
        fd, temporary_name = tempfile.mkstemp(
            prefix=".knowledge-bases.",
            suffix=".json",
            dir=str(self.path.parent),
            text=True,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(catalog, handle, indent=2, sort_keys=True)
                handle.write("\n")
            os.replace(temporary_name, self.path)
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
        finally:
            if os.path.exists(temporary_name):
                os.unlink(temporary_name)

    @staticmethod
    def _public_record(record: dict) -> dict:
        public_record = deepcopy(record)
        public_record.pop("delete_requester_api_key_id", None)
        return {
            **public_record,
            "is_default": record["id"] == DEFAULT_KNOWLEDGE_BASE_ID,
        }

    def _public_catalog(self, catalog: dict) -> dict:
        return {
            "schema_version": catalog["schema_version"],
            "knowledge_bases": [
                self._public_record(record) for record in catalog["knowledge_bases"]
            ],
        }


def validate_knowledge_base_id(value: str | None) -> str:
    if value is None:
        return DEFAULT_KNOWLEDGE_BASE_ID
    knowledge_base_id = str(value).strip()
    if knowledge_base_id == DEFAULT_KNOWLEDGE_BASE_ID:
        return knowledge_base_id
    if not KNOWLEDGE_BASE_ID_PATTERN.fullmatch(knowledge_base_id):
        raise KnowledgeBaseValidationError(
            "ID knowledge base non valido",
            code="invalid_knowledge_base_id",
            status_code=400,
        )
    return knowledge_base_id


def validate_knowledge_base_name(value: str) -> str:
    if not isinstance(value, str):
        raise KnowledgeBaseValidationError(
            "Il nome deve essere una stringa",
            code="invalid_knowledge_base_name",
        )
    name = value.strip()
    if not 1 <= len(name) <= 120:
        raise KnowledgeBaseValidationError(
            "Il nome deve contenere da 1 a 120 caratteri",
            code="invalid_knowledge_base_name",
        )
    return name


def validate_knowledge_base_description(value: str | None) -> str:
    if not isinstance(value, str):
        raise KnowledgeBaseValidationError(
            "La descrizione deve essere una stringa",
            code="invalid_knowledge_base_description",
        )
    description = value.strip()
    if len(description) > 1000:
        raise KnowledgeBaseValidationError(
            "La descrizione non può superare 1000 caratteri",
            code="invalid_knowledge_base_description",
        )
    return description


def _validate_catalog(catalog: object, path: Path) -> dict:
    if not isinstance(catalog, dict):
        raise KnowledgeBaseCatalogError(f"Catalogo knowledge base non valido: {path}")
    if catalog.get("schema_version") != KNOWLEDGE_BASE_SCHEMA_VERSION:
        raise KnowledgeBaseCatalogError(
            f"Versione catalogo knowledge base non supportata: {path}"
        )
    records = catalog.get("knowledge_bases")
    if not isinstance(records, list) or not records:
        raise KnowledgeBaseCatalogError(f"Catalogo knowledge base vuoto o non valido: {path}")
    ids: set[str] = set()
    names: set[str] = set()
    default_count = 0
    normalized_records = []
    for raw in records:
        if not isinstance(raw, dict):
            raise KnowledgeBaseCatalogError(f"Record knowledge base non valido: {path}")
        knowledge_base_id = str(raw.get("id") or "")
        try:
            validate_knowledge_base_id(knowledge_base_id)
            name = validate_knowledge_base_name(raw.get("name"))
            description = validate_knowledge_base_description(raw.get("description"))
        except KnowledgeBaseValidationError as exc:
            raise KnowledgeBaseCatalogError(
                f"Record knowledge base non valido in {path}: {exc.message}"
            ) from exc
        status = str(raw.get("status") or "")
        if status not in KNOWLEDGE_BASE_STATUSES:
            raise KnowledgeBaseCatalogError(f"Stato knowledge base non valido in {path}")
        if knowledge_base_id in ids or name.casefold() in names:
            raise KnowledgeBaseCatalogError(f"Knowledge base duplicate nel catalogo: {path}")
        ids.add(knowledge_base_id)
        names.add(name.casefold())
        default_count += int(knowledge_base_id == DEFAULT_KNOWLEDGE_BASE_ID)
        normalized_records.append(
            {
                "id": knowledge_base_id,
                "name": name,
                "description": description,
                "status": status,
                "created_at": _validate_timestamp(
                    raw.get("created_at"),
                    path=path,
                    field="created_at",
                ),
                "updated_at": _validate_timestamp(
                    raw.get("updated_at"),
                    path=path,
                    field="updated_at",
                ),
                "delete_error": str(raw.get("delete_error") or "")[:1000],
                "delete_requester_api_key_id": str(
                    raw.get("delete_requester_api_key_id") or ""
                ),
            }
        )
    if default_count != 1:
        raise KnowledgeBaseCatalogError(
            f"Il catalogo deve contenere esattamente una knowledge base default: {path}"
        )
    return {
        "schema_version": KNOWLEDGE_BASE_SCHEMA_VERSION,
        "knowledge_bases": normalized_records,
    }


def _default_record() -> dict:
    now = _now()
    return {
        "id": DEFAULT_KNOWLEDGE_BASE_ID,
        "name": DEFAULT_KNOWLEDGE_BASE_NAME,
        "description": "",
        "status": "active",
        "created_at": now,
        "updated_at": now,
        "delete_error": "",
        "delete_requester_api_key_id": "",
    }


def _find_record(records: list[dict], knowledge_base_id: str) -> dict | None:
    return next(
        (record for record in records if record.get("id") == knowledge_base_id),
        None,
    )


def _validate_timestamp(value, *, path: Path, field: str) -> str:
    timestamp = str(value or "").strip()
    if not timestamp:
        raise KnowledgeBaseCatalogError(
            f"Timestamp {field} mancante nel catalogo: {path}"
        )
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError as exc:
        raise KnowledgeBaseCatalogError(
            f"Timestamp {field} non valido nel catalogo: {path}"
        ) from exc
    if parsed.tzinfo is None:
        raise KnowledgeBaseCatalogError(
            f"Timestamp {field} senza timezone nel catalogo: {path}"
        )
    return timestamp


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
