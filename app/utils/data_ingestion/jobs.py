"""Background job helpers for data source sync."""

from __future__ import annotations

import logging
import threading
import time
import uuid

from utils.job_store import get_job_store, queue_name
from utils.settings_store import SettingsStore
from utils.state_backend import configured_queue_backend, redis_connection
from utils.validators import ValidationError, validate_string


log = logging.getLogger(__name__)


def start_data_source_sync_job(
    config: dict,
    data_source_id: str,
    *,
    trigger: str = "manual",
) -> tuple[dict, int]:
    settings = SettingsStore(config["SETTINGS_FILE"]).load()
    data_source_id = validate_string(data_source_id, "data_source_id", min_length=1, max_length=120)
    source = next(
        (source for source in settings.get("data_sources", []) if source.get("id") == data_source_id),
        None,
    )
    if source is None:
        raise ValidationError("Data source non trovata", "data_source_id", code="not_found")
    if not source.get("enabled", True):
        raise ValidationError("Data source disabilitata", "data_source_id")

    queued = configured_queue_backend() == "redis"
    job_id = uuid.uuid4().hex
    job = {
        "id": job_id,
        "type": "data_source_sync",
        "status": "queued" if queued else "running",
        "message": "Sync data source in coda" if queued else "Sync data source avviata",
        "processed": 0,
        "total": 0,
        "current_file": "",
        "data_source_id": data_source_id,
        "trigger": trigger,
        "user_id": config.get("USER_ID"),
        "workspace_id": config.get("WORKSPACE_ID"),
        "errors": [],
        "result": None,
        "started_at": time.time(),
        "finished_at": None,
    }
    payload, status_code = get_job_store().create_data_source_sync_job(job)
    if status_code >= 400:
        return payload, status_code

    try:
        if queued:
            enqueue_data_source_sync_job(job_id, config, data_source_id)
            if trigger == "poller":
                _mark_sync_queued(config, data_source_id)
        else:
            target = run_data_source_sync_job
            args = (job_id, config, data_source_id)
            if trigger == "poller":
                target = _run_polled_data_source_sync_job
                args = (job_id, config, data_source_id)
            thread = threading.Thread(
                target=target,
                args=args,
                daemon=True,
            )
            thread.start()
    except Exception as exc:
        finish_job(job_id, "failed", f"Errore accodamento data source: {exc}")
        _mark_sync_failed(config, data_source_id, str(exc))
        raise

    return {"job_id": job_id, **(get_job_store().get(job_id) or payload)}, 202


def enqueue_data_source_sync_job(job_id: str, config: dict, data_source_id: str) -> None:
    from rq import Queue

    queue = Queue(queue_name(), connection=redis_connection())
    queue.enqueue(
        run_data_source_sync_job,
        job_id,
        config,
        data_source_id,
        job_timeout="2h",
        result_ttl=3600,
        failure_ttl=86400,
    )


def run_data_source_sync_job(job_id: str, config: dict, data_source_id: str) -> None:
    from utils.data_ingestion.service import mark_data_source_sync_running, sync_data_source
    from utils.index_lock import index_write_lock

    update_job(
        job_id,
        status="running",
        message=f"Sync data source {data_source_id}",
        current_file="",
    )
    mark_data_source_sync_running(config, data_source_id)

    def progress(patch: dict) -> None:
        update_job(
            job_id,
            processed=patch.get("processed", 0),
            total=patch.get("total", 0),
            current_file=patch.get("current_file", ""),
        )

    try:
        with index_write_lock():
            result = sync_data_source(config, data_source_id, progress_callback=progress)
        for error in result.get("errors", []):
            append_job_error(job_id, error.get("remote_id", data_source_id), error.get("error", "Errore sync"))
        update_job(
            job_id,
            result=result,
            processed=result.get("processed", result.get("items", 0)),
            total=result.get("total", result.get("items", 0)),
            current_file="",
        )
        if result.get("errors"):
            finish_job(job_id, "completed_with_errors", result.get("status", "Sync completata con errori"))
        else:
            finish_job(job_id, "completed", "Sync data source completata")
    except ValidationError as exc:
        append_job_error(job_id, data_source_id, exc.message)
        update_job(job_id, result=exc.to_dict())
        _mark_sync_failed(config, data_source_id, exc.message)
        finish_job(job_id, "failed", exc.message)
    except Exception as exc:
        log.error("Errore job data source %s: %s", job_id, exc)
        append_job_error(job_id, data_source_id, str(exc))
        update_job(job_id, result={"error": str(exc), "status": "server_error"})
        _mark_sync_failed(config, data_source_id, str(exc))
        finish_job(job_id, "failed", str(exc))


def _run_polled_data_source_sync_job(job_id: str, config: dict, data_source_id: str) -> None:
    try:
        _mark_sync_queued(config, data_source_id)
    except Exception as exc:
        log.error("Errore stato queued data source %s: %s", data_source_id, exc)
        append_job_error(job_id, data_source_id, str(exc))
        update_job(job_id, result={"error": str(exc), "status": "server_error"})
        _mark_sync_failed(config, data_source_id, str(exc))
        finish_job(job_id, "failed", str(exc))
        return
    run_data_source_sync_job(job_id, config, data_source_id)


def get_job(job_id: str) -> dict | None:
    return get_job_store().get(job_id)


def update_job(job_id: str, **patch) -> None:
    get_job_store().update(job_id, **patch)


def append_job_error(job_id: str, filename: str, message: str) -> None:
    get_job_store().append_error(job_id, filename, message)


def finish_job(job_id: str, status: str, message: str) -> None:
    get_job_store().finish(job_id, status, message)


def _mark_sync_queued(config: dict, data_source_id: str) -> None:
    from utils.data_ingestion.service import mark_data_source_sync_queued

    mark_data_source_sync_queued(config, data_source_id)


def _mark_sync_failed(config: dict, data_source_id: str, message: str) -> None:
    try:
        from utils.data_ingestion.service import mark_data_source_sync_failed

        mark_data_source_sync_failed(config, data_source_id, message)
    except Exception as exc:
        log.warning("Unable to update data source sync failure state: %s", exc)
