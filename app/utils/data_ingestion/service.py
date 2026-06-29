"""Data source sync orchestration."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Callable

from utils.data_ingestion.base import IngestionItem, SyncContext
from utils.data_ingestion.registry import available_plugins, get_ingester
from utils.data_ingestion.storage import IngestionStorage
from utils.document_indexer import index_saved_document, normalize_metadata_values
from utils.file_index import FileIndex
from utils.secret_store import SecretStore
from utils.settings_store import SettingsStore
from utils.validators import ValidationError


def data_source_summaries(settings: dict, file_index_path: str) -> list[dict]:
    entries = FileIndex(file_index_path).list()
    counts: dict[str, int] = {}
    for entry in entries:
        data_source_id = entry.get("data_source_id")
        if data_source_id:
            counts[data_source_id] = counts.get(data_source_id, 0) + 1
    plugin_names = {plugin["id"]: plugin["display_name"] for plugin in available_plugins()}
    summaries = []
    for source in settings.get("data_sources", []):
        summaries.append(
            {
                **source,
                "plugin_name": plugin_names.get(source.get("plugin"), source.get("plugin", "")),
                "indexed_count": counts.get(source.get("id", ""), 0),
            }
        )
    return summaries


def sync_data_source(
    config: dict,
    data_source_id: str,
    *,
    progress_callback: Callable[[dict], None] | None = None,
) -> dict:
    store = SettingsStore(config["SETTINGS_FILE"])
    settings = store.load()
    source = _find_data_source(settings, data_source_id)
    if not source.get("enabled", True):
        raise ValidationError("Data source disabilitata", "data_source_id")

    ingester = get_ingester(source.get("plugin"))
    source_config = {
        **source.get("config", {}),
        **source.get("secrets_env", {}),
        **_resolved_user_secrets(config, source),
    }
    source_config = ingester.validate_config(source_config)
    context = SyncContext(
        upload_folder=config["UPLOAD_FOLDER"],
        settings_file=config["SETTINGS_FILE"],
        file_index=config["FILE_INDEX"],
    )
    result = ingester.sync(context, source_config, source.get("cursor", {}))

    storage = IngestionStorage(config["UPLOAD_FOLDER"])
    indexed = 0
    duplicates = 0
    errors = list(result.errors)
    total = len(result.items) + sum(len(item.attachments) for item in result.items)
    processed = 0

    def report(current_file: str = "") -> None:
        if progress_callback:
            progress_callback({"processed": processed, "total": total, "current_file": current_file})

    for item in result.items:
        item_metadata = _item_metadata(source, ingester.plugin_id, item)
        try:
            stored = storage.materialize_item(source["id"], item)
            outcome = index_saved_document(
                config,
                stored["filename"],
                stored["file_path"],
                stored["extension"],
                extra_metadata=item_metadata,
            )
            if outcome.get("status") == "duplicate":
                duplicates += 1
            else:
                indexed += 1
        except Exception as exc:
            errors.append({"remote_id": item.remote_id, "error": str(exc)})
        processed += 1
        report(item.filename)

        for attachment in item.attachments:
            attachment_metadata = normalize_metadata_values(
                {
                    **item_metadata,
                    **attachment.metadata,
                    "ingestion_plugin": ingester.plugin_id,
                    "data_source_id": source["id"],
                }
            )
            try:
                stored_attachment = storage.materialize_attachment(source["id"], item, attachment)
                if not stored_attachment:
                    processed += 1
                    report(attachment.filename)
                    continue
                outcome = index_saved_document(
                    config,
                    stored_attachment["filename"],
                    stored_attachment["file_path"],
                    stored_attachment["extension"],
                    extra_metadata=attachment_metadata,
                )
                if outcome.get("status") == "duplicate":
                    duplicates += 1
                else:
                    indexed += 1
            except Exception as exc:
                errors.append({"remote_id": attachment.remote_id, "error": str(exc)})
            processed += 1
            report(attachment.filename)

    final_status = "completed_with_errors" if errors else "completed"
    _update_data_source_state(
        store,
        source["id"],
        cursor=result.cursor or source.get("cursor", {}),
        last_error=f"{len(errors)} errore/i durante la sync" if errors else "",
        last_sync_status=final_status,
        touch_last_sync=True,
        schedule_next=True,
    )
    return {
        "status": final_status,
        "data_source_id": source["id"],
        "items": len(result.items),
        "processed": processed,
        "total": total,
        "indexed": indexed,
        "duplicates": duplicates,
        "errors": errors,
        "cursor": result.cursor,
    }


def mark_data_source_sync_queued(config: dict, data_source_id: str) -> None:
    _update_data_source_state(
        SettingsStore(config["SETTINGS_FILE"]),
        data_source_id,
        last_error="",
        last_sync_status="queued",
        schedule_next=True,
    )


def mark_data_source_sync_running(config: dict, data_source_id: str) -> None:
    _update_data_source_state(
        SettingsStore(config["SETTINGS_FILE"]),
        data_source_id,
        last_sync_status="running",
    )


def mark_data_source_sync_failed(config: dict, data_source_id: str, message: str) -> None:
    _update_data_source_state(
        SettingsStore(config["SETTINGS_FILE"]),
        data_source_id,
        last_error=message,
        last_sync_status="failed",
        touch_last_sync=True,
        schedule_next=True,
    )


def toggle_data_source_enabled(
    store: SettingsStore,
    data_source_id: str,
    enabled: bool,
) -> dict:
    """Enable or disable a data source."""
    settings = store.load()
    updated_sources = []
    matched: dict | None = None
    now = datetime.now(timezone.utc)
    for source in settings.get("data_sources", []):
        if source.get("id") == data_source_id:
            source = {**source, "enabled": bool(enabled)}
            if not enabled:
                source["last_sync_status"] = "disabled"
                source["next_sync_at"] = ""
            elif source.get("sync_enabled") and source.get("sync_interval_seconds", 0) > 0:
                source["next_sync_at"] = source.get("next_sync_at") or (
                    now + timedelta(seconds=int(source["sync_interval_seconds"]))
                ).isoformat(timespec="seconds")
            else:
                source["next_sync_at"] = ""
            matched = source
        updated_sources.append(source)
    if matched is None:
        raise ValidationError("Data source non trovata", "data_source_id", code="not_found")
    store.save({**settings, "data_sources": updated_sources})
    return matched


def _find_data_source(settings: dict, data_source_id: str) -> dict:
    for source in settings.get("data_sources", []):
        if source.get("id") == data_source_id:
            return source
    raise ValidationError("Data source non trovata", "data_source_id", code="not_found")


def _resolved_user_secrets(config: dict, source: dict) -> dict:
    resolved = {}
    store = SecretStore(config.get("SECRETS_FILE"), key=config.get("SECRET_KEY"))
    for name, descriptor in (source.get("secrets") or {}).items():
        if not isinstance(descriptor, dict) or descriptor.get("mode") != "user_secret":
            continue
        ref = descriptor.get("ref")
        if ref:
            resolved[name] = store.get_secret(ref)
    return resolved


def _item_metadata(source: dict, plugin_id: str, item: IngestionItem) -> dict:
    return normalize_metadata_values(
        {
            **item.metadata,
            "source_type": item.metadata.get("source_type", "external"),
            "ingestion_plugin": plugin_id,
            "data_source_id": source["id"],
            "remote_id": item.remote_id,
            "remote_updated_at": item.updated_at,
        }
    )


def _update_data_source_state(
    store: SettingsStore,
    data_source_id: str,
    *,
    cursor: dict | None = None,
    last_error: str | None = None,
    last_sync_status: str | None = None,
    next_sync_at: str | None = None,
    touch_last_sync: bool = False,
    schedule_next: bool = False,
) -> None:
    settings = store.load()
    updated_sources = []
    now = datetime.now(timezone.utc)
    for source in settings.get("data_sources", []):
        if source.get("id") == data_source_id:
            source = {
                **source,
            }
            if cursor is not None:
                source["cursor"] = cursor or {}
            if touch_last_sync:
                source["last_sync"] = now.isoformat(timespec="seconds")
            if last_error is not None:
                source["last_error"] = last_error
            if last_sync_status is not None:
                source["last_sync_status"] = last_sync_status
            if next_sync_at is not None:
                source["next_sync_at"] = next_sync_at
            elif schedule_next:
                source["next_sync_at"] = _next_sync_at(source, now)
        updated_sources.append(source)
    store.save({**settings, "data_sources": updated_sources})


def _next_sync_at(source: dict, now: datetime) -> str:
    if not source.get("sync_enabled"):
        return ""
    try:
        interval_seconds = int(source.get("sync_interval_seconds") or 0)
    except (TypeError, ValueError):
        interval_seconds = 0
    if interval_seconds <= 0:
        return ""
    return (now + timedelta(seconds=interval_seconds)).isoformat(timespec="seconds")
