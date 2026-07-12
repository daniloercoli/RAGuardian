"""Dedicated polling process for periodic data source sync."""

from __future__ import annotations

import argparse
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Iterable

from utils.data_ingestion.jobs import start_data_source_sync_job
from utils.settings_store import SettingsStore
from utils.user_store import UserStore
from utils.workspace import workspace_for_user


log = logging.getLogger(__name__)


def poll_due_data_sources(app=None, *, now: datetime | None = None) -> dict:
    app = app or runtime_app_from_env()
    now = now or datetime.now(timezone.utc)
    summary = {
        "workspaces": 0,
        "sources_checked": 0,
        "jobs_started": 0,
        "jobs_conflicted": 0,
        "errors": [],
        "jobs": [],
    }

    for config in workspace_configs(app):
        summary["workspaces"] += 1
        settings = SettingsStore(config["SETTINGS_FILE"]).load()
        sources = settings.get("data_sources", [])
        summary["sources_checked"] += len(sources)
        for source in due_data_sources(sources, now=now):
            source_id = source.get("id", "")
            try:
                payload, status_code = start_data_source_sync_job(config, source_id, trigger="poller")
            except Exception as exc:
                log.error("Unable to start data source sync for %s: %s", source_id, exc)
                summary["errors"].append(
                    {
                        "workspace_id": config.get("WORKSPACE_ID", ""),
                        "data_source_id": source_id,
                        "error": str(exc),
                    }
                )
                continue

            if status_code == 202:
                summary["jobs_started"] += 1
                summary["jobs"].append(
                    {
                        "workspace_id": config.get("WORKSPACE_ID", ""),
                        "data_source_id": source_id,
                        "job_id": payload.get("job_id") or payload.get("id"),
                    }
                )
            elif status_code == 409:
                summary["jobs_conflicted"] += 1
            else:
                summary["errors"].append(
                    {
                        "workspace_id": config.get("WORKSPACE_ID", ""),
                        "data_source_id": source_id,
                        "error": payload.get("error", f"Unexpected status {status_code}"),
                    }
                )
    return summary


def due_data_sources(sources: Iterable[dict], *, now: datetime | None = None) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    due = []
    for source in sources:
        if not source.get("enabled", True) or not source.get("sync_enabled"):
            continue
        interval_seconds = _sync_interval_seconds(source)
        if interval_seconds <= 0:
            continue
        next_sync_at = _parse_datetime(source.get("next_sync_at"))
        if next_sync_at:
            if next_sync_at <= now:
                due.append(source)
            continue
        last_sync = _parse_datetime(source.get("last_sync"))
        if not last_sync or last_sync + timedelta(seconds=interval_seconds) <= now:
            due.append(source)
    return due


def workspace_configs(app) -> Iterable[dict]:
    users = UserStore(app.config["USERS_FILE"]).list()
    for user in users:
        if not user.get("enabled", True):
            continue
        yield workspace_for_user(user, app=app).as_config()


def run_forever(app=None, *, interval_seconds: int | None = None) -> None:
    app = app or runtime_app_from_env()
    interval_seconds = interval_seconds or _env_int("RAG_DATA_SOURCE_POLLER_INTERVAL_SECONDS", 60, minimum=5)
    log.info("Data source poller started with %ss interval", interval_seconds)
    while True:
        started = time.time()
        summary = poll_due_data_sources(app)
        log.info(
            "Data source poller tick: workspaces=%s checked=%s started=%s conflicts=%s errors=%s",
            summary["workspaces"],
            summary["sources_checked"],
            summary["jobs_started"],
            summary["jobs_conflicted"],
            len(summary["errors"]),
        )
        elapsed = time.time() - started
        time.sleep(max(1, interval_seconds - elapsed))


def runtime_app_from_env():
    from config import Config

    config = {
        "UPLOAD_FOLDER": Config.paths.upload_folder,
        "SETTINGS_FILE": Config.paths.settings_file,
        "FILE_INDEX": Config.paths.file_index,
        "USERS_FILE": os.getenv("RAG_USERS_FILE", "app/data/users.json"),
        "SECRETS_FILE": os.getenv("RAG_SECRETS_FILE", "app/data/secrets.json"),
        "WORKSPACE_DATA_DIR": os.getenv("RAG_WORKSPACE_DATA_DIR", "app/data/workspaces"),
        "WORKSPACE_UPLOAD_DIR": os.getenv("RAG_WORKSPACE_UPLOAD_DIR", "app/uploads/workspaces"),
        "SECRET_KEY": (
            os.getenv("RAG_SECRET_KEY")
            or Config.api_keys.flask_secret_key
            or os.getenv("FLASK_SECRET_KEY")
            or "dev-secret-key"
        ),
        "VECTOR_STORE_BACKEND": Config.vector_store.backend,
    }
    for key in ("UPLOAD_FOLDER", "WORKSPACE_DATA_DIR", "WORKSPACE_UPLOAD_DIR"):
        os.makedirs(config[key], exist_ok=True)
    return SimpleNamespace(config=config)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Poll configured data sources and enqueue due sync jobs.")
    parser.add_argument("--once", action="store_true", help="Run a single polling tick and exit.")
    parser.add_argument(
        "--interval-seconds",
        type=int,
        default=_env_int("RAG_DATA_SOURCE_POLLER_INTERVAL_SECONDS", 60, minimum=5),
        help="Polling loop interval in seconds.",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper())
    app = runtime_app_from_env()
    if args.once:
        summary = poll_due_data_sources(app)
        log.info("Data source poller once summary: %s", summary)
        return 0 if not summary["errors"] else 1
    run_forever(app, interval_seconds=args.interval_seconds)
    return 0


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _sync_interval_seconds(source: dict) -> int:
    try:
        return int(source.get("sync_interval_seconds") or 0)
    except (TypeError, ValueError):
        return 0


def _env_int(name: str, default: int, *, minimum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        return default
    return max(minimum, value)


if __name__ == "__main__":
    raise SystemExit(main())
