"""Reusable document indexing helpers for uploads and external ingestion."""

from __future__ import annotations

import json
import logging
import os
import uuid
from copy import deepcopy
from typing import Any, Callable

from utils.file_index import FileIndex
from utils.settings_store import SettingsStore
from utils.validators import ValidationError


DOCUMENT_INDEX_EXTENSIONS = {"pdf", "txt", "md", "csv"}


INGESTION_METADATA_KEYS = {
    "source_type",
    "ingestion_plugin",
    "data_source_id",
    "remote_id",
    "remote_url",
    "remote_updated_at",
    "subject",
    "sender",
    "recipients",
    "thread_id",
    "message_id",
    "attachment_id",
    "attachment_filename",
    "drive_id",
    "item_id",
    "parent_id",
    "etag",
    "file_name",
    "mime_type",
    "size",
    "relative_path",
}

log = logging.getLogger(__name__)


def source_type_for_extension(extension: str) -> str:
    extension = extension.lower().lstrip(".")
    if extension == "pdf":
        return "pdf"
    if extension == "md":
        return "markdown"
    if extension == "csv":
        return "csv"
    if extension == "txt":
        return "text"
    return "document"


def index_saved_document(
    config: dict,
    filename: str,
    file_path: str,
    extension: str,
    *,
    ocr_documents_func: Callable | None = None,
    extra_metadata: dict | None = None,
) -> dict:
    """Parse, deduplicate, index, and record a saved document file."""

    from utils.chroma_manager import add_documents_to_chroma, delete_documents_by_source, find_document_by_id
    from utils.index_lock import assert_distributed_locks_healthy
    from utils.rag_engine import clear_cache_for_collection

    extension = extension.lower().lstrip(".")
    if extension not in DOCUMENT_INDEX_EXTENSIONS:
        raise ValidationError("Formato file non supportato", "file")

    parse_error = ""
    source_type = source_type_for_extension(extension)
    try:
        if extension == "pdf":
            from utils.pdf_processor import process_pdf

            documents = process_pdf(file_path, settings_path=config["SETTINGS_FILE"])
        else:
            from utils.text_processor import process_text_file

            documents = process_text_file(file_path, settings_path=config["SETTINGS_FILE"])
    except Exception as exc:
        documents = []
        parse_error = str(exc)

    collection_name = config.get("CHROMA_COLLECTION")
    index_extra = normalize_metadata_values({"source_type": source_type, **(extra_metadata or {})})
    ocr_error = ""
    if extension == "pdf" and ocr_documents_func:
        ocr_documents, ocr_extra, ocr_error = ocr_documents_func(
            file_path,
            parsed_documents=documents,
            parse_error=parse_error,
        )
        if ocr_extra:
            index_extra.update(normalize_metadata_values(ocr_extra))
        if ocr_documents:
            documents = ocr_documents

    apply_extra_metadata_to_documents(documents, index_extra)

    assert_distributed_locks_healthy()
    replaced_chunks = delete_documents_by_source(
        file_path,
        collection_name=collection_name,
    )
    if not documents:
        empty_error = ocr_error or parse_error or "Il file non contiene testo indicizzabile"
        assert_distributed_locks_healthy()
        FileIndex(config["FILE_INDEX"]).record(
            filename,
            file_path,
            0,
            status="empty",
            error=empty_error,
            metadata=index_record_metadata_for_config(config, extra=index_extra),
        )
        if replaced_chunks:
            clear_cache_for_collection(collection_name or "documents")
        raise ValidationError(empty_error, "file")

    document_id = str(documents[0].metadata.get("document_id") or "")
    source_id = str(documents[0].metadata.get("source_id") or "")
    duplicate = find_document_by_id(document_id, exclude_source=file_path, collection_name=collection_name) if document_id else None
    if duplicate:
        assert_distributed_locks_healthy()
        if replaced_chunks:
            clear_cache_for_collection(collection_name or "documents")
        duplicate_source = duplicate.get("source") or ""
        FileIndex(config["FILE_INDEX"]).record(
            filename,
            file_path,
            0,
            status="duplicate",
            error=f"Contenuto gia' indicizzato da {duplicate_source}",
            metadata=index_record_metadata_for_config(
                config,
                document_id=document_id,
                source_id=source_id,
                extra={
                    **index_extra,
                    "duplicate_of_source": duplicate_source,
                    "indexed_chunks": duplicate.get("chunks", 0),
                },
            ),
        )
        return {
            "message": f"{filename} gia' presente nella knowledge base; non reindicizzato",
            "filename": filename,
            "chunks": 0,
            "status": "duplicate",
            "source_type": index_extra.get("source_type", source_type),
            "document_id": document_id,
            "duplicate_of_source": duplicate_source,
        }

    assert_distributed_locks_healthy()
    add_documents_to_chroma(documents, collection_name=collection_name)
    clear_cache_for_collection(collection_name or "documents")
    assert_distributed_locks_healthy()
    FileIndex(config["FILE_INDEX"]).record(
        filename,
        file_path,
        len(documents),
        status="indexed",
        metadata=index_record_metadata_for_config(
            config,
            document_id=document_id,
            source_id=source_id,
            extra=index_extra,
        ),
    )
    return {
        "message": f"{filename} caricato e indicizzato",
        "filename": filename,
        "chunks": len(documents),
        "status": "indexed",
        "source_type": index_extra.get("source_type", source_type),
        "document_id": document_id,
        "ocr_used": bool(index_extra.get("ocr_used")),
    }


def index_staged_document(
    config: dict,
    filename: str,
    file_path: str,
    staged_file_path: str,
    extension: str,
    *,
    ocr_documents_func: Callable | None = None,
    extra_metadata: dict | None = None,
) -> dict:
    """Atomically replace one saved document and roll back its index on error."""

    from utils.chroma_manager import (
        restore_documents_by_source,
        snapshot_documents_by_source,
    )
    from utils.index_lock import (
        DistributedLockLeaseLostError,
        assert_distributed_locks_healthy,
    )
    from utils.rag_engine import clear_cache_for_collection

    upload_root = os.path.abspath(config["UPLOAD_FOLDER"])
    final_path = os.path.abspath(str(file_path))
    staged_path = os.path.abspath(str(staged_file_path))
    if (
        not final_path.startswith(upload_root + os.sep)
        or not staged_path.startswith(upload_root + os.sep)
        or os.path.dirname(final_path) != os.path.dirname(staged_path)
    ):
        raise ValidationError("Path staging data source non valido", "file")
    if not os.path.isfile(staged_path):
        raise ValidationError("File staging data source non trovato", "file")

    file_index = FileIndex(config["FILE_INDEX"])
    previous_entry = deepcopy(file_index.get(filename))
    had_existing_file = os.path.isfile(final_path)
    backup_path = (
        os.path.join(
            os.path.dirname(final_path),
            f".{os.path.basename(final_path)}.{uuid.uuid4().hex}.rollback-ingestion",
        )
        if had_existing_file
        else ""
    )
    collection_name = config.get("CHROMA_COLLECTION")
    vector_snapshot = None
    snapshot_captured = False
    backup_created = False
    replacement_committed = False

    try:
        assert_distributed_locks_healthy()
        vector_snapshot = snapshot_documents_by_source(
            final_path,
            collection_name=collection_name,
        )
        snapshot_captured = True
        if backup_path:
            os.replace(final_path, backup_path)
            backup_created = True
        os.replace(staged_path, final_path)
        replacement_committed = True
        result = index_saved_document(
            config,
            filename,
            final_path,
            extension,
            ocr_documents_func=ocr_documents_func,
            extra_metadata=extra_metadata,
        )
        assert_distributed_locks_healthy()
    except DistributedLockLeaseLostError:
        preserved_backup = (
            backup_path
            if backup_created and os.path.isfile(backup_path)
            else ""
        )
        log.critical(
            "Indicizzazione data source %s interrotta dopo la perdita della "
            "lease; nessun rollback live eseguito. Backup preservato: %s. "
            "Rilanciare la sincronizzazione per il recovery.",
            filename,
            preserved_backup or "nessuno",
        )
        raise
    except Exception as exc:
        if snapshot_captured:
            rollback_errors = []
            try:
                if replacement_committed and os.path.isfile(final_path):
                    os.remove(final_path)
                if backup_created:
                    if not os.path.isfile(backup_path):
                        raise FileNotFoundError(
                            f"Backup data source non trovato: {backup_path}"
                        )
                    os.replace(backup_path, final_path)
            except Exception as rollback_exc:
                rollback_errors.append(f"file: {rollback_exc}")
            try:
                restore_documents_by_source(
                    final_path,
                    vector_snapshot or {},
                    collection_name=collection_name,
                )
                clear_cache_for_collection(collection_name or "documents")
            except Exception as rollback_exc:
                rollback_errors.append(f"vettori: {rollback_exc}")
            try:
                file_index.restore_entry(filename, previous_entry)
            except Exception as rollback_exc:
                rollback_errors.append(f"file index: {rollback_exc}")
            if rollback_errors:
                raise RuntimeError(
                    f"{exc}; rollback data source fallito: "
                    + "; ".join(rollback_errors)
                ) from exc
        raise
    else:
        if backup_created and os.path.isfile(backup_path):
            try:
                os.remove(backup_path)
            except OSError as cleanup_error:
                log.warning(
                    "Impossibile rimuovere il backup data source %s: %s",
                    backup_path,
                    cleanup_error,
                )
        return result
    finally:
        if os.path.isfile(staged_path):
            try:
                os.remove(staged_path)
            except OSError as cleanup_error:
                log.warning(
                    "Impossibile rimuovere lo staging data source %s: %s",
                    staged_path,
                    cleanup_error,
                )


def apply_extra_metadata_to_documents(documents: list, extra_metadata: dict | None) -> None:
    if not documents or not extra_metadata:
        return
    normalized = normalize_metadata_values(extra_metadata)
    for document in documents:
        document.metadata = {**(document.metadata or {}), **normalized}


def ingestion_metadata_from_entry(entry: dict) -> dict:
    return normalize_metadata_values(
        {key: entry.get(key) for key in INGESTION_METADATA_KEYS if entry.get(key) not in (None, "")}
    )


def index_record_metadata_for_config(
    config: dict,
    document_id: str = "",
    source_id: str = "",
    extra: dict | None = None,
) -> dict:
    include_ocr = bool(extra and extra.get("ocr_used"))
    metadata = index_profile_metadata(
        current_index_profile_for_config(config, include_ocr=include_ocr),
        {"document_id": document_id, "source_id": source_id},
    )
    if extra:
        metadata.update(normalize_metadata_values(extra))
    return metadata


def index_profile_metadata(profile: dict, extra: dict | None = None) -> dict:
    metadata = {"index_profile": dict(profile)}
    if extra:
        metadata.update(normalize_metadata_values(extra))
    return metadata


def current_index_profile_for_config(config: dict, include_ocr: bool = False) -> dict:
    settings = SettingsStore(config["SETTINGS_FILE"]).load()
    rag = settings["rag"]
    profile = {
        "embedding_provider": rag["embedding_provider"],
        "embedding_model": rag["embedding_model"],
        "chunk_size": rag["chunk_size"],
        "chunk_overlap": rag["chunk_overlap"],
    }
    if include_ocr:
        ocr = settings.get("ocr", {})
        profile.update(
            {
                "ocr_enabled": bool(ocr.get("enabled")),
                "ocr_auto_on_empty_pdf": bool(ocr.get("auto_on_empty_pdf", True)),
                "ocr_provider": ocr.get("provider", ""),
                "ocr_model": ocr.get("default_model", ""),
                "ocr_mode": ocr.get("ocr_mode", ""),
                "ocr_output_format": ocr.get("output_format", ""),
            }
        )
    return profile


def normalize_metadata_values(metadata: dict | None) -> dict:
    normalized = {}
    for key, value in (metadata or {}).items():
        if value is None:
            normalized[key] = ""
        elif isinstance(value, (str, int, float, bool)):
            normalized[key] = value
        elif isinstance(value, (list, tuple, set)):
            normalized[key] = ", ".join(str(item) for item in value if item is not None)
        elif isinstance(value, dict):
            normalized[key] = json.dumps(value, ensure_ascii=False, sort_keys=True)
        else:
            normalized[key] = str(value)
    return normalized
