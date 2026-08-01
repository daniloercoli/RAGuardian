from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import threading
import uuid
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar

from utils.file_lock import ProcessSafeFileLock
from utils.knowledge_base_store import validate_knowledge_base_id

sys.modules.setdefault("utils.chat_agent_store", sys.modules[__name__])
sys.modules.setdefault("app.utils.chat_agent_store", sys.modules[__name__])


CHAT_AGENT_SCHEMA_VERSION = 1
CHAT_AGENT_ID_PATTERN = re.compile(r"^agent_[0-9a-f]{32}$")
PROMPT_SCOPES = {"personal", "shared"}

KNOWN_RECORD_FIELDS = frozenset(
    {
        "id",
        "name",
        "description",
        "provider_id",
        "model_id",
        "knowledge_base_ids",
        "prompt_ref",
        "created_at",
        "updated_at",
    }
)


class ChatAgentCatalogError(RuntimeError):
    """Raised when a catalog exists but cannot be trusted."""


class ChatAgentValidationError(ValueError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "invalid_chat_agent",
        status_code: int = 400,
    ):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status_code = status_code


class ChatAgentStore:
    """Atomic JSON catalog for the chat agents in one workspace."""

    _locks: ClassVar[dict[str, ProcessSafeFileLock]] = {}
    _locks_guard: ClassVar[threading.Lock] = threading.Lock()

    def __init__(
        self,
        path: str | Path,
        *,
        max_additional: int = 20,
        max_query_knowledge_bases: int = 5,
    ):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.max_additional = max(0, int(max_additional))
        self.max_query_knowledge_bases = max(1, int(max_query_knowledge_bases))
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
                    "schema_version": CHAT_AGENT_SCHEMA_VERSION,
                    "agents": [],
                }
                self._write_unlocked(catalog)
                return self._public_catalog(catalog)
            return self._public_catalog(self._load_unlocked())

    def list(self) -> list[dict]:
        with self._lock:
            catalog = self._load_unlocked()
            return [self._public_record(record) for record in catalog["agents"]]

    def get(self, agent_id: str) -> dict | None:
        for record in self.list():
            if record["id"] == agent_id:
                return record
        return None

    def create(
        self,
        *,
        name: str,
        description: str = "",
        provider_id: str,
        model_id: str,
        knowledge_base_ids: list[str],
        prompt_ref: dict | None,
    ) -> dict:
        normalized_name = validate_chat_agent_name(name)
        normalized_description = validate_chat_agent_description(description)
        normalized_provider_id = validate_provider_id(provider_id)
        normalized_model_id = validate_model_id(model_id)
        normalized_knowledge_base_ids = validate_knowledge_base_ids(
            knowledge_base_ids,
            limit=self.max_query_knowledge_bases,
        )
        normalized_prompt_ref = validate_prompt_ref_required(prompt_ref)
        with self._lock:
            catalog = self._load_unlocked()
            records = catalog["agents"]
            if len(records) >= self.max_additional:
                raise ChatAgentValidationError(
                    f"Limite di {self.max_additional} agent raggiunto",
                    code="chat_agent_limit_reached",
                    status_code=409,
                )
            self._ensure_unique_name(records, normalized_name)
            now = _now()
            record = {
                "id": f"agent_{uuid.uuid4().hex}",
                "name": normalized_name,
                "description": normalized_description,
                "provider_id": normalized_provider_id,
                "model_id": normalized_model_id,
                "knowledge_base_ids": normalized_knowledge_base_ids,
                "prompt_ref": normalized_prompt_ref,
                "created_at": now,
                "updated_at": now,
            }
            records.append(record)
            self._write_unlocked(catalog)
            return self._public_record(record)

    def update(
        self,
        agent_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
        provider_id: str | None = None,
        model_id: str | None = None,
        knowledge_base_ids: list[str] | None = None,
        prompt_ref: dict | None = None,
    ) -> dict:
        with self._lock:
            catalog = self._load_unlocked()
            record = _find_record(catalog["agents"], agent_id)
            if record is None:
                raise ChatAgentValidationError(
                    "Agent non trovato",
                    code="chat_agent_not_found",
                    status_code=404,
                )
            if name is not None:
                normalized_name = validate_chat_agent_name(name)
                self._ensure_unique_name(
                    catalog["agents"],
                    normalized_name,
                    exclude_id=agent_id,
                )
                record["name"] = normalized_name
            if description is not None:
                record["description"] = validate_chat_agent_description(description)
            if provider_id is not None:
                record["provider_id"] = validate_provider_id(provider_id)
            if model_id is not None:
                record["model_id"] = validate_model_id(model_id)
            if knowledge_base_ids is not None:
                record["knowledge_base_ids"] = validate_knowledge_base_ids(
                    knowledge_base_ids,
                    limit=self.max_query_knowledge_bases,
                )
            if prompt_ref is not None:
                record["prompt_ref"] = validate_prompt_ref_required(prompt_ref)
            record["updated_at"] = _now()
            self._write_unlocked(catalog)
            return self._public_record(record)

    def remove(self, agent_id: str) -> bool:
        with self._lock:
            catalog = self._load_unlocked()
            original = catalog["agents"]
            remaining = [record for record in original if record["id"] != agent_id]
            if len(remaining) == len(original):
                return False
            catalog["agents"] = remaining
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
            record["id"] != exclude_id
            and str(record.get("name") or "").casefold() == folded
            for record in records
        ):
            raise ChatAgentValidationError(
                "Esiste già un agent con questo nome",
                code="duplicate_chat_agent_name",
                status_code=409,
            )

    def _load_unlocked(self) -> dict:
        if not self.path.exists():
            raise ChatAgentCatalogError(f"Catalogo agent mancante: {self.path}")
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                catalog = json.load(handle)
        except (json.JSONDecodeError, OSError) as exc:
            raise ChatAgentCatalogError(
                f"Catalogo agent non leggibile: {self.path}"
            ) from exc
        return _validate_catalog(catalog, self.path)

    def _write_unlocked(self, catalog: dict) -> None:
        fd, temporary_name = tempfile.mkstemp(
            prefix=".chat-agents.",
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
        return deepcopy(record)

    def _public_catalog(self, catalog: dict) -> dict:
        return {
            "schema_version": catalog["schema_version"],
            "agents": [
                self._public_record(record) for record in catalog["agents"]
            ],
        }


def validate_chat_agent_id(value: str | None) -> str:
    if value is None:
        raise ChatAgentValidationError(
            "ID agent non valido",
            code="invalid_chat_agent_id",
            status_code=400,
        )
    agent_id = str(value).strip()
    if not CHAT_AGENT_ID_PATTERN.fullmatch(agent_id):
        raise ChatAgentValidationError(
            "ID agent non valido",
            code="invalid_chat_agent_id",
            status_code=400,
        )
    return agent_id


def validate_chat_agent_name(value: str) -> str:
    if not isinstance(value, str):
        raise ChatAgentValidationError(
            "Il nome deve essere una stringa",
            code="invalid_chat_agent_name",
        )
    name = value.strip()
    if not 1 <= len(name) <= 120:
        raise ChatAgentValidationError(
            "Il nome deve contenere da 1 a 120 caratteri",
            code="invalid_chat_agent_name",
        )
    return name


def validate_chat_agent_description(value: str | None) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ChatAgentValidationError(
            "La descrizione deve essere una stringa",
            code="invalid_chat_agent_description",
        )
    description = value.strip()
    if len(description) > 500:
        raise ChatAgentValidationError(
            "La descrizione non può superare 500 caratteri",
            code="invalid_chat_agent_description",
        )
    return description


def validate_provider_id(value: str | None) -> str:
    if not isinstance(value, str):
        raise ChatAgentValidationError(
            "Provider non valido",
            code="invalid_provider_id",
        )
    provider_id = value.strip()
    if not 1 <= len(provider_id) <= 120:
        raise ChatAgentValidationError(
            "Provider non valido",
            code="invalid_provider_id",
        )
    return provider_id


def validate_model_id(value: str | None) -> str:
    if not isinstance(value, str):
        raise ChatAgentValidationError(
            "Modello non valido",
            code="invalid_model_id",
        )
    model_id = value.strip()
    if not 1 <= len(model_id) <= 200:
        raise ChatAgentValidationError(
            "Modello non valido",
            code="invalid_model_id",
        )
    return model_id


def validate_knowledge_base_ids(
    value: list[str] | None,
    *,
    limit: int,
) -> list[str]:
    if not isinstance(value, list) or not value:
        raise ChatAgentValidationError(
            "knowledge_base_ids deve contenere almeno un elemento",
            code="invalid_knowledge_base_ids",
        )
    if len(value) > limit:
        raise ChatAgentValidationError(
            f"knowledge_base_ids non può superare {limit} elementi",
            code="knowledge_base_limit_exceeded",
            status_code=409,
        )
    normalized: list[str] = []
    try:
        for raw in value:
            if not isinstance(raw, str) or not raw.strip():
                raise ValueError
            normalized.append(validate_knowledge_base_id(raw.strip()))
    except (ValueError, TypeError):
        raise ChatAgentValidationError(
            "Selezione knowledge base non valida",
            code="invalid_knowledge_base_ids",
        ) from None
    if len(set(normalized)) != len(normalized):
        raise ChatAgentValidationError(
            "knowledge_base_ids non può contenere duplicati",
            code="invalid_knowledge_base_ids",
        )
    # The first KB is the primary retrieval target, so preserve the order
    # chosen by the user instead of treating this as an unordered set.
    return normalized


def validate_prompt_ref(value: dict | None) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ChatAgentValidationError(
            "prompt_ref non valido",
            code="invalid_prompt_ref",
        )
    if not value:
        return {}
    raw_id = value.get("id")
    if not isinstance(raw_id, str):
        raise ChatAgentValidationError(
            "prompt_ref.id non valido",
            code="invalid_prompt_ref",
        )
    prompt_id = raw_id.strip()
    if not 1 <= len(prompt_id) <= 200:
        raise ChatAgentValidationError(
            "prompt_ref.id non valido",
            code="invalid_prompt_ref",
        )
    scope = str(value.get("scope") or "").strip()
    if scope not in PROMPT_SCOPES:
        raise ChatAgentValidationError(
            "prompt_ref.scope non valido",
            code="invalid_prompt_ref",
        )
    return {"id": prompt_id, "scope": scope}


def validate_prompt_ref_required(value: dict | None) -> dict:
    if not value or not isinstance(value, dict) or not value.get("id"):
        raise ChatAgentValidationError(
            "prompt_ref è obbligatorio",
            code="invalid_prompt_ref",
            status_code=400,
        )
    return validate_prompt_ref(value)


def _validate_catalog(catalog: object, path: Path) -> dict:
    if not isinstance(catalog, dict):
        raise ChatAgentCatalogError(f"Catalogo agent non valido: {path}")
    if catalog.get("schema_version") != CHAT_AGENT_SCHEMA_VERSION:
        raise ChatAgentCatalogError(
            f"Versione catalogo agent non supportata: {path}"
        )
    records = catalog.get("agents")
    if not isinstance(records, list):
        raise ChatAgentCatalogError(f"Catalogo agent non valido: {path}")
    ids: set[str] = set()
    names: set[str] = set()
    normalized_records = []
    for raw in records:
        if not isinstance(raw, dict):
            raise ChatAgentCatalogError(f"Record agent non valido in {path}")
        unknown = sorted(set(raw) - KNOWN_RECORD_FIELDS)
        if unknown:
            raise ChatAgentCatalogError(
                f"Campi non consentiti nel catalogo agent ({path}): {', '.join(unknown)}"
            )
        try:
            agent_id = validate_chat_agent_id(raw.get("id"))
            name = validate_chat_agent_name(raw.get("name"))
            description = validate_chat_agent_description(raw.get("description"))
            provider_id = validate_provider_id(raw.get("provider_id"))
            model_id = validate_model_id(raw.get("model_id"))
        except ChatAgentValidationError as exc:
            raise ChatAgentCatalogError(
                f"Record agent non valido in {path}: {exc.message}"
            ) from exc
        knowledge_base_ids = raw.get("knowledge_base_ids")
        if not isinstance(knowledge_base_ids, list) or not knowledge_base_ids:
            raise ChatAgentCatalogError(
                f"Record agent non valido in {path}: knowledge_base_ids mancanti"
            )
        try:
            normalized_knowledge_base_ids = validate_knowledge_base_ids(
                knowledge_base_ids,
                limit=10_000,
            )
        except ChatAgentValidationError as exc:
            raise ChatAgentCatalogError(
                f"Record agent non valido in {path}: {exc.message}"
            ) from exc
        try:
            prompt_ref = validate_prompt_ref(raw.get("prompt_ref"))
        except ChatAgentValidationError as exc:
            raise ChatAgentCatalogError(
                f"Record agent non valido in {path}: {exc.message}"
            ) from exc
        if agent_id in ids or name.casefold() in names:
            raise ChatAgentCatalogError(
                f"Agent duplicato nel catalogo: {path}"
            )
        ids.add(agent_id)
        names.add(name.casefold())
        normalized_records.append(
            {
                "id": agent_id,
                "name": name,
                "description": description,
                "provider_id": provider_id,
                "model_id": model_id,
                "knowledge_base_ids": normalized_knowledge_base_ids,
                "prompt_ref": prompt_ref,
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
            }
        )
    return {
        "schema_version": CHAT_AGENT_SCHEMA_VERSION,
        "agents": normalized_records,
    }


def _find_record(records: list[dict], agent_id: str) -> dict | None:
    return next(
        (record for record in records if record.get("id") == agent_id),
        None,
    )


def _validate_timestamp(value, *, path: Path, field: str) -> str:
    timestamp = str(value or "").strip()
    if not timestamp:
        raise ChatAgentCatalogError(
            f"Timestamp {field} mancante nel catalogo: {path}"
        )
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ChatAgentCatalogError(
            f"Timestamp {field} non valido nel catalogo: {path}"
        ) from exc
    if parsed.tzinfo is None:
        raise ChatAgentCatalogError(
            f"Timestamp {field} senza timezone nel catalogo: {path}"
        )
    return timestamp


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
