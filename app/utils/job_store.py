import os
import sys
import threading
import time
import hashlib
from copy import deepcopy
from typing import Any, Optional

from utils.state_backend import (
    configured_queue_backend,
    configured_state_backend,
    redis_connection,
    redis_scan_delete,
    state_key_prefix,
)


sys.modules.setdefault("utils.job_store", sys.modules[__name__])
sys.modules.setdefault("app.utils.job_store", sys.modules[__name__])


RUNNING_JOB_STATUSES = {"queued", "running"}


def job_ttl_seconds() -> int:
    return _env_int("RAG_JOB_TTL_SECONDS", 86400, minimum=60)


def lock_ttl_seconds() -> int:
    return _env_int("RAG_LOCK_TTL_SECONDS", 21600, minimum=60)


def queue_name() -> str:
    value = os.getenv("RAG_QUEUE_NAME", "rag-default").strip()
    return value or "rag-default"


def get_job_store():
    if configured_state_backend() == "redis" or configured_queue_backend() == "redis":
        try:
            return RedisJobStore()
        except Exception:
            if configured_queue_backend() == "redis":
                raise
    return MemoryJobStore.instance()


class MemoryJobStore:
    _instance = None
    _instance_lock = threading.Lock()

    @classmethod
    def instance(cls):
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    @classmethod
    def reset(cls):
        with cls._instance_lock:
            cls._instance = cls()

    def __init__(self):
        self._lock = threading.RLock()
        self._jobs: dict[str, dict] = {}
        self._active_rebuild_job_id: Optional[str] = None
        self._active_data_source_sync_job_ids: dict[str, str] = {}

    def create_rebuild_job(self, job: dict) -> tuple[dict, int]:
        with self._lock:
            active_id = self._active_rebuild_job_id
            active = self._jobs.get(active_id) if active_id else None
            if active and active.get("status") in RUNNING_JOB_STATUSES:
                return {"error": "Ricostruzione indice gia' in corso", "status": "conflict", "job_id": active_id}, 409

            stored = deepcopy(job)
            self._jobs[stored["id"]] = stored
            self._active_rebuild_job_id = stored["id"]
            return self.get(stored["id"]) or stored, 202

    def create_job(self, job: dict) -> tuple[dict, int]:
        with self._lock:
            stored = deepcopy(job)
            self._jobs[stored["id"]] = stored
            return self.get(stored["id"]) or stored, 202

    def create_data_source_sync_job(self, job: dict) -> tuple[dict, int]:
        with self._lock:
            job_id = str(job["id"])
            sync_key = _data_source_sync_key(job)
            active_id = self._active_data_source_sync_job_ids.get(sync_key)
            active = self._jobs.get(active_id) if active_id else None
            if active and active.get("status") in RUNNING_JOB_STATUSES:
                return {
                    "error": "Sync data source gia' in corso",
                    "status": "conflict",
                    "job_id": active_id,
                    "data_source_id": job.get("data_source_id", ""),
                }, 409

            stored = deepcopy(job)
            self._jobs[job_id] = stored
            self._active_data_source_sync_job_ids[sync_key] = job_id
            return self.get(job_id) or stored, 202

    def get(self, job_id: str) -> dict | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return _public_job(job) if job else None

    def update(self, job_id: str, **patch) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job:
                job.update(patch)

    def append_error(self, job_id: str, filename: str, message: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is not None:
                job.setdefault("errors", []).append({"filename": filename, "error": message})

    def finish(self, job_id: str, status: str, message: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job:
                job.update({"status": status, "message": message, "current_file": "", "finished_at": time.time()})
            if self._active_rebuild_job_id == job_id:
                self._active_rebuild_job_id = None
            if job and job.get("type") == "data_source_sync":
                sync_key = _data_source_sync_key(job)
                if self._active_data_source_sync_job_ids.get(sync_key) == job_id:
                    self._active_data_source_sync_job_ids.pop(sync_key, None)

    def active_jobs_count(self, workspace_id: str | None = None) -> int:
        with self._lock:
            return sum(
                1
                for job in self._jobs.values()
                if job.get("status") in RUNNING_JOB_STATUSES
                and (workspace_id is None or job.get("workspace_id") == workspace_id)
            )

    def clear_by_workspace(self, workspace_id: str) -> int:
        if not workspace_id:
            return 0
        with self._lock:
            job_ids = [
                job_id
                for job_id, job in self._jobs.items()
                if job.get("workspace_id") == workspace_id
            ]
            for job_id in job_ids:
                self._jobs.pop(job_id, None)
            if self._active_rebuild_job_id in job_ids:
                self._active_rebuild_job_id = None
            for sync_key, job_id in list(self._active_data_source_sync_job_ids.items()):
                if job_id in job_ids:
                    self._active_data_source_sync_job_ids.pop(sync_key, None)
            return len(job_ids)

    def clear(self) -> None:
        with self._lock:
            self._jobs.clear()
            self._active_rebuild_job_id = None
            self._active_data_source_sync_job_ids.clear()


class RedisJobStore:
    def __init__(self, redis_client=None):
        self.redis = redis_client or redis_connection()
        self.prefix = state_key_prefix()
        self.ttl = job_ttl_seconds()
        self.lock_ttl = lock_ttl_seconds()

    def create_rebuild_job(self, job: dict) -> tuple[dict, int]:
        job_id = str(job["id"])
        active_key = self._active_rebuild_key()
        if not self.redis.set(active_key, job_id.encode("utf-8"), nx=True, ex=self.lock_ttl):
            active_id = _decode(self.redis.get(active_key))
            active = self.get(active_id) if active_id else None
            if active and active.get("status") in RUNNING_JOB_STATUSES:
                return {"error": "Ricostruzione indice gia' in corso", "status": "conflict", "job_id": active_id}, 409
            self.redis.delete(active_key)
            if not self.redis.set(active_key, job_id.encode("utf-8"), nx=True, ex=self.lock_ttl):
                active_id = _decode(self.redis.get(active_key))
                return {"error": "Ricostruzione indice gia' in corso", "status": "conflict", "job_id": active_id}, 409

        self._save(job)
        return self.get(job_id) or deepcopy(job), 202

    def create_job(self, job: dict) -> tuple[dict, int]:
        self._save(job)
        return self.get(str(job["id"])) or deepcopy(job), 202

    def create_data_source_sync_job(self, job: dict) -> tuple[dict, int]:
        job_id = str(job["id"])
        active_key = self._active_data_source_sync_key(job)
        if not self.redis.set(active_key, job_id.encode("utf-8"), nx=True, ex=self.lock_ttl):
            active_id = _decode(self.redis.get(active_key))
            active = self.get(active_id) if active_id else None
            if active and active.get("status") in RUNNING_JOB_STATUSES:
                return {
                    "error": "Sync data source gia' in corso",
                    "status": "conflict",
                    "job_id": active_id,
                    "data_source_id": job.get("data_source_id", ""),
                }, 409
            self.redis.delete(active_key)
            if not self.redis.set(active_key, job_id.encode("utf-8"), nx=True, ex=self.lock_ttl):
                active_id = _decode(self.redis.get(active_key))
                return {
                    "error": "Sync data source gia' in corso",
                    "status": "conflict",
                    "job_id": active_id,
                    "data_source_id": job.get("data_source_id", ""),
                }, 409

        self._save(job)
        return self.get(job_id) or deepcopy(job), 202

    def get(self, job_id: str) -> dict | None:
        if not job_id:
            return None
        raw = self.redis.get(self._job_key(job_id))
        if not raw:
            return None
        import json

        return _public_job(json.loads(raw))

    def update(self, job_id: str, **patch) -> None:
        job = self.get(job_id)
        if not job:
            return
        job.update(patch)
        self._save(job)

    def append_error(self, job_id: str, filename: str, message: str) -> None:
        job = self.get(job_id)
        if not job:
            return
        job.setdefault("errors", []).append({"filename": filename, "error": message})
        self._save(job)

    def finish(self, job_id: str, status: str, message: str) -> None:
        job = self.get(job_id)
        if job:
            job.update({"status": status, "message": message, "current_file": "", "finished_at": time.time()})
            self._save(job)
        active_id = _decode(self.redis.get(self._active_rebuild_key()))
        if active_id == job_id:
            self.redis.delete(self._active_rebuild_key())
        if job and job.get("type") == "data_source_sync":
            active_key = self._active_data_source_sync_key(job)
            active_id = _decode(self.redis.get(active_key))
            if active_id == job_id:
                self.redis.delete(active_key)

    def active_jobs_count(self, workspace_id: str | None = None) -> int:
        count = 0
        for key in self.redis.scan_iter(match=f"{self.prefix}:job:*", count=200):
            raw = self.redis.get(key)
            if not raw:
                continue
            import json

            try:
                job = json.loads(raw)
                if (
                    job.get("status") in RUNNING_JOB_STATUSES
                    and (workspace_id is None or job.get("workspace_id") == workspace_id)
                ):
                    count += 1
            except (TypeError, ValueError):
                continue
        return count

    def clear_by_workspace(self, workspace_id: str) -> int:
        if not workspace_id:
            return 0
        deleted = 0
        for key in list(self.redis.scan_iter(match=f"{self.prefix}:job:*", count=200)):
            raw = self.redis.get(key)
            if not raw:
                continue
            import json

            try:
                job = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if job.get("workspace_id") != workspace_id:
                continue
            job_id = str(job.get("id") or "")
            deleted += int(self.redis.delete(key) or 0)
            if _decode(self.redis.get(self._active_rebuild_key())) == job_id:
                self.redis.delete(self._active_rebuild_key())
            if job.get("type") == "data_source_sync":
                active_key = self._active_data_source_sync_key(job)
                if _decode(self.redis.get(active_key)) == job_id:
                    self.redis.delete(active_key)
        return deleted

    def clear(self) -> None:
        # scan glob * matches all sub-keys: job:<id>, job:active:rebuild, job:active:data-source-sync:*
        redis_scan_delete(self.redis, f"{self.prefix}:job:*")
        # Best-effort cleanup in case scan missed anything
        self.redis.delete(self._active_rebuild_key())

    def _save(self, job: dict) -> None:
        import json

        payload = json.dumps(job, ensure_ascii=False)
        self.redis.setex(self._job_key(str(job["id"])), self.ttl, payload.encode("utf-8"))

    def _job_key(self, job_id: str) -> str:
        return f"{self.prefix}:job:{job_id}"

    def _active_rebuild_key(self) -> str:
        return f"{self.prefix}:job:active:rebuild"

    def _active_data_source_sync_key(self, job: dict) -> str:
        return f"{self.prefix}:job:active:data-source-sync:{_data_source_sync_key(job)}"


def _public_job(job: dict | None) -> dict | None:
    if not job:
        return None
    public = deepcopy(job)
    public["errors"] = list(public.get("errors", []))
    return public


def _decode(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return str(value)


def _data_source_sync_key(job: dict) -> str:
    raw = f"{job.get('workspace_id') or ''}:{job.get('data_source_id') or ''}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _env_int(name: str, default: int, *, minimum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        return default
    return max(minimum, value)
