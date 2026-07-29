import os
import sys
import threading
import time
import hashlib
import json
import uuid
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from typing import Any

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


class JobTargetUnavailableError(RuntimeError):
    """Raised when a background job must stop before mutating its target KB."""


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


def ensure_job_target_active(job_id: str, config: dict) -> None:
    """Revalidate job ownership, delete gate, and catalog state before a write."""
    store = get_job_store()
    job = store.get(job_id)
    if not job or job.get("status") not in RUNNING_JOB_STATUSES:
        raise JobTargetUnavailableError("Job non più attivo")

    workspace_id = str(config.get("WORKSPACE_ID") or "")
    knowledge_base_id = str(config.get("KNOWLEDGE_BASE_ID") or "default")
    if (
        workspace_id
        and job.get("workspace_id")
        and job.get("workspace_id") != workspace_id
    ):
        raise JobTargetUnavailableError("Target workspace del job non valido")
    if job.get("knowledge_base_id") and (
        job.get("knowledge_base_id") or "default"
    ) != knowledge_base_id:
        raise JobTargetUnavailableError("Target knowledge base del job non valido")

    if workspace_id:
        delete_job_id = store.knowledge_base_deletion_job_id(
            workspace_id,
            knowledge_base_id,
        )
        if delete_job_id and delete_job_id != job_id:
            raise JobTargetUnavailableError("Knowledge base in eliminazione")
        ensure_knowledge_base_target_active(config)


def ensure_knowledge_base_target_active(config: dict) -> None:
    """Re-read the catalog so a frozen runtime config cannot resurrect a KB."""
    if not config.get("WORKSPACE_ID") or not config.get("SETTINGS_FILE"):
        return
    from utils.knowledge_base_store import KnowledgeBaseStore
    from utils.user_store import UserStore

    users_file = config.get("USERS_FILE")
    user_id = str(config.get("USER_ID") or "")
    if users_file and user_id:
        owner = UserStore(users_file).get(user_id)
        if owner is None or not owner.get("enabled", True):
            raise JobTargetUnavailableError("Workspace non più disponibile")

    catalog_path = config.get("KNOWLEDGE_BASES_FILE") or str(
        Path(config["SETTINGS_FILE"]).with_name("knowledge_bases.json")
    )
    KnowledgeBaseStore(catalog_path).require_active(
        str(config.get("KNOWLEDGE_BASE_ID") or "default")
    )


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
        self._active_rebuild_job_ids: dict[str, str] = {}
        self._active_data_source_sync_job_ids: dict[str, str] = {}
        self._active_delete_job_ids: dict[str, str] = {}

    def create_rebuild_job(self, job: dict) -> tuple[dict, int]:
        with self._lock:
            delete_conflict = self._delete_conflict_unlocked(job)
            if delete_conflict:
                return delete_conflict, 409
            rebuild_key = _rebuild_key(job)
            active_id = self._active_rebuild_job_ids.get(rebuild_key)
            if rebuild_key != "legacy":
                active_id = active_id or self._active_rebuild_job_ids.get("legacy")
            active = self._jobs.get(active_id) if active_id else None
            if active and active.get("status") in RUNNING_JOB_STATUSES:
                return {"error": "Ricostruzione indice gia' in corso", "status": "conflict", "job_id": active_id}, 409

            stored = deepcopy(job)
            stored.setdefault("type", "rebuild_index")
            self._jobs[stored["id"]] = stored
            self._active_rebuild_job_ids[rebuild_key] = stored["id"]
            return self.get(stored["id"]) or stored, 202

    def create_job(self, job: dict) -> tuple[dict, int]:
        if job.get("type") == "delete_knowledge_base":
            return self.create_delete_job(job)
        with self._lock:
            delete_conflict = self._delete_conflict_unlocked(job)
            if delete_conflict:
                return delete_conflict, 409
            stored = deepcopy(job)
            self._jobs[stored["id"]] = stored
            return self.get(stored["id"]) or stored, 202

    def create_data_source_sync_job(self, job: dict) -> tuple[dict, int]:
        with self._lock:
            delete_conflict = self._delete_conflict_unlocked(job)
            if delete_conflict:
                return delete_conflict, 409
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

    def create_delete_job(self, job: dict) -> tuple[dict, int]:
        """Atomically reserve one KB for deletion or return the existing delete."""
        with self._lock:
            target_key = _knowledge_base_key(job)
            if not target_key:
                raise ValueError("workspace_id richiesto per eliminare una knowledge base")
            active_delete = self._active_delete_unlocked(job)
            if active_delete:
                return self.get(active_delete) or deepcopy(self._jobs[active_delete]), 202

            legacy_rebuild_id = self._active_rebuild_job_ids.get("legacy")
            legacy_rebuild = (
                self._jobs.get(legacy_rebuild_id) if legacy_rebuild_id else None
            )
            if legacy_rebuild and legacy_rebuild.get("status") in RUNNING_JOB_STATUSES:
                return _active_jobs_conflict(legacy_rebuild_id), 409

            active = self._active_jobs_for_target_unlocked(job)
            if active:
                return _active_jobs_conflict(str(active[0].get("id") or "")), 409

            stored = deepcopy({**job, "type": "delete_knowledge_base"})
            job_id = str(stored["id"])
            self._jobs[job_id] = stored
            self._active_delete_job_ids[target_key] = job_id
            return self.get(job_id) or stored, 202

    def knowledge_base_deletion_job_id(
        self,
        workspace_id: str,
        knowledge_base_id: str,
    ) -> str:
        with self._lock:
            return self._active_delete_unlocked(
                {
                    "workspace_id": workspace_id,
                    "knowledge_base_id": knowledge_base_id,
                }
            )

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
            if job and job.get("type") in {"rebuild", "rebuild_index"}:
                rebuild_key = _rebuild_key(job)
                if self._active_rebuild_job_ids.get(rebuild_key) == job_id:
                    self._active_rebuild_job_ids.pop(rebuild_key, None)
            if job and job.get("type") == "data_source_sync":
                sync_key = _data_source_sync_key(job)
                if self._active_data_source_sync_job_ids.get(sync_key) == job_id:
                    self._active_data_source_sync_job_ids.pop(sync_key, None)
            if job and job.get("type") == "delete_knowledge_base":
                target_key = _knowledge_base_key(job)
                if self._active_delete_job_ids.get(target_key) == job_id:
                    self._active_delete_job_ids.pop(target_key, None)

    def active_jobs_count(
        self,
        workspace_id: str | None = None,
        knowledge_base_id: str | None = None,
    ) -> int:
        with self._lock:
            return sum(
                1
                for job in self._jobs.values()
                if job.get("status") in RUNNING_JOB_STATUSES
                and (workspace_id is None or job.get("workspace_id") == workspace_id)
                and (
                    knowledge_base_id is None
                    or (job.get("knowledge_base_id") or "default") == knowledge_base_id
                )
            )

    def clear_by_knowledge_base(
        self,
        workspace_id: str,
        knowledge_base_id: str,
        *,
        exclude_job_id: str | None = None,
    ) -> int:
        if not workspace_id or not knowledge_base_id:
            return 0
        with self._lock:
            job_ids = [
                job_id
                for job_id, job in self._jobs.items()
                if job.get("workspace_id") == workspace_id
                and (job.get("knowledge_base_id") or "default") == knowledge_base_id
                and job_id != exclude_job_id
            ]
            for job_id in job_ids:
                self._jobs.pop(job_id, None)
            self._clear_job_ids(job_ids)
            return len(job_ids)

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
            self._clear_job_ids(job_ids)
            return len(job_ids)

    def clear(self) -> None:
        with self._lock:
            self._jobs.clear()
            self._active_rebuild_job_ids.clear()
            self._active_data_source_sync_job_ids.clear()
            self._active_delete_job_ids.clear()

    def _clear_job_ids(self, job_ids: list[str]) -> None:
        for rebuild_key, job_id in list(self._active_rebuild_job_ids.items()):
            if job_id in job_ids:
                self._active_rebuild_job_ids.pop(rebuild_key, None)
        for sync_key, job_id in list(self._active_data_source_sync_job_ids.items()):
            if job_id in job_ids:
                self._active_data_source_sync_job_ids.pop(sync_key, None)
        for target_key, job_id in list(self._active_delete_job_ids.items()):
            if job_id in job_ids:
                self._active_delete_job_ids.pop(target_key, None)

    def _active_delete_unlocked(self, job: dict) -> str:
        target_key = _knowledge_base_key(job)
        active_id = self._active_delete_job_ids.get(target_key)
        active = self._jobs.get(active_id) if active_id else None
        if active and active.get("status") in RUNNING_JOB_STATUSES:
            return str(active_id)
        if active_id:
            self._active_delete_job_ids.pop(target_key, None)
        for candidate in self._jobs.values():
            if (
                candidate.get("type") == "delete_knowledge_base"
                and candidate.get("status") in RUNNING_JOB_STATUSES
                and _same_knowledge_base(candidate, job)
            ):
                candidate_id = str(candidate.get("id") or "")
                self._active_delete_job_ids[target_key] = candidate_id
                return candidate_id
        return ""

    def _delete_conflict_unlocked(self, job: dict) -> dict | None:
        active_id = self._active_delete_unlocked(job)
        return _delete_conflict(active_id) if active_id else None

    def _active_jobs_for_target_unlocked(self, job: dict) -> list[dict]:
        return [
            candidate
            for candidate in self._jobs.values()
            if candidate.get("status") in RUNNING_JOB_STATUSES
            and _same_knowledge_base(candidate, job)
        ]


class RedisJobStore:
    def __init__(self, redis_client=None):
        self.redis = redis_client or redis_connection()
        self.prefix = state_key_prefix()
        self.ttl = job_ttl_seconds()
        self.lock_ttl = lock_ttl_seconds()

    def create_rebuild_job(self, job: dict) -> tuple[dict, int]:
        job = {**job, "type": job.get("type") or "rebuild_index"}
        job_id = str(job["id"])
        with self._admission_guard():
            delete_conflict = self._delete_conflict(job)
            if delete_conflict:
                return delete_conflict, 409
            if _rebuild_key(job) != "legacy":
                legacy_id = self._legacy_active_rebuild_id()
                if legacy_id:
                    return _rebuild_conflict(legacy_id), 409
            else:
                legacy_id = _decode(
                    self.redis.get(f"{self.prefix}:job:active:rebuild")
                )
                if legacy_id:
                    return _rebuild_conflict(legacy_id), 409

            active_key = self._active_rebuild_key(job)
            if not self.redis.set(
                active_key,
                job_id.encode("utf-8"),
                nx=True,
                ex=self.lock_ttl,
            ):
                active_id = _decode(self.redis.get(active_key))
                active = self.get(active_id) if active_id else None
                if active and active.get("status") in RUNNING_JOB_STATUSES:
                    return _rebuild_conflict(active_id), 409
                self.redis.delete(active_key)
                if not self.redis.set(
                    active_key,
                    job_id.encode("utf-8"),
                    nx=True,
                    ex=self.lock_ttl,
                ):
                    active_id = _decode(self.redis.get(active_key))
                    return _rebuild_conflict(active_id), 409

            try:
                self._save(job)
            except Exception:
                if _decode(self.redis.get(active_key)) == job_id:
                    self.redis.delete(active_key)
                raise
            return self.get(job_id) or deepcopy(job), 202

    def create_job(self, job: dict) -> tuple[dict, int]:
        if job.get("type") == "delete_knowledge_base":
            return self.create_delete_job(job)
        with self._admission_guard():
            delete_conflict = self._delete_conflict(job)
            if delete_conflict:
                return delete_conflict, 409
            self._save(job)
            return self.get(str(job["id"])) or deepcopy(job), 202

    def create_data_source_sync_job(self, job: dict) -> tuple[dict, int]:
        job_id = str(job["id"])
        with self._admission_guard():
            delete_conflict = self._delete_conflict(job)
            if delete_conflict:
                return delete_conflict, 409
            active_key = self._active_data_source_sync_key(job)
            if not self.redis.set(
                active_key,
                job_id.encode("utf-8"),
                nx=True,
                ex=self.lock_ttl,
            ):
                active_id = _decode(self.redis.get(active_key))
                active = self.get(active_id) if active_id else None
                if active and active.get("status") in RUNNING_JOB_STATUSES:
                    return _sync_conflict(active_id, job), 409
                self.redis.delete(active_key)
                if not self.redis.set(
                    active_key,
                    job_id.encode("utf-8"),
                    nx=True,
                    ex=self.lock_ttl,
                ):
                    active_id = _decode(self.redis.get(active_key))
                    return _sync_conflict(active_id, job), 409

            try:
                self._save(job)
            except Exception:
                if _decode(self.redis.get(active_key)) == job_id:
                    self.redis.delete(active_key)
                raise
            return self.get(job_id) or deepcopy(job), 202

    def create_delete_job(self, job: dict) -> tuple[dict, int]:
        target_key = _knowledge_base_key(job)
        if not target_key:
            raise ValueError("workspace_id richiesto per eliminare una knowledge base")
        job = {**job, "type": "delete_knowledge_base"}
        job_id = str(job["id"])
        with self._admission_guard():
            active_delete = self._active_delete(job)
            if active_delete:
                existing = self.get(active_delete)
                if existing:
                    return existing, 202

            legacy_rebuild_id = self._legacy_active_rebuild_id()
            if legacy_rebuild_id:
                return _active_jobs_conflict(legacy_rebuild_id), 409
            scoped_rebuild_id = _decode(
                self.redis.get(self._active_rebuild_key(job))
            )
            if scoped_rebuild_id:
                return _active_jobs_conflict(scoped_rebuild_id), 409

            active = self._active_jobs_for_target(job)
            if active:
                active_job = active[0]
                if active_job.get("type") == "delete_knowledge_base":
                    existing_id = str(active_job.get("id") or "")
                    self.redis.set(
                        self._active_delete_key(job),
                        existing_id.encode("utf-8"),
                        ex=self.lock_ttl,
                    )
                    return self.get(existing_id) or active_job, 202
                return _active_jobs_conflict(
                    str(active_job.get("id") or "")
                ), 409

            active_key = self._active_delete_key(job)
            if not self.redis.set(
                active_key,
                job_id.encode("utf-8"),
                nx=True,
                ex=self.lock_ttl,
            ):
                active_id = _decode(self.redis.get(active_key))
                return _delete_conflict(active_id), 409
            try:
                self._save(job)
            except Exception:
                if _decode(self.redis.get(active_key)) == job_id:
                    self.redis.delete(active_key)
                raise
            return self.get(job_id) or deepcopy(job), 202

    def knowledge_base_deletion_job_id(
        self,
        workspace_id: str,
        knowledge_base_id: str,
    ) -> str:
        return self._active_delete(
            {
                "workspace_id": workspace_id,
                "knowledge_base_id": knowledge_base_id,
            }
        )

    def get(self, job_id: str) -> dict | None:
        if not job_id:
            return None
        raw = self.redis.get(self._job_key(job_id))
        if not raw:
            return None
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
        active_key = self._active_rebuild_key(job or {})
        active_id = _decode(self.redis.get(active_key))
        if active_id == job_id:
            self.redis.delete(active_key)
        for legacy_key in self._legacy_active_rebuild_keys():
            if _decode(self.redis.get(legacy_key)) == job_id:
                self.redis.delete(legacy_key)
        if job and job.get("type") == "data_source_sync":
            active_key = self._active_data_source_sync_key(job)
            active_id = _decode(self.redis.get(active_key))
            if active_id == job_id:
                self.redis.delete(active_key)
        if job and job.get("type") == "delete_knowledge_base":
            active_key = self._active_delete_key(job)
            if _decode(self.redis.get(active_key)) == job_id:
                self.redis.delete(active_key)

    def active_jobs_count(
        self,
        workspace_id: str | None = None,
        knowledge_base_id: str | None = None,
    ) -> int:
        return sum(
            1
            for job in self._iter_jobs()
            if job.get("status") in RUNNING_JOB_STATUSES
            and (workspace_id is None or job.get("workspace_id") == workspace_id)
            and (
                knowledge_base_id is None
                or (job.get("knowledge_base_id") or "default")
                == knowledge_base_id
            )
        )

    def clear_by_knowledge_base(
        self,
        workspace_id: str,
        knowledge_base_id: str,
        *,
        exclude_job_id: str | None = None,
    ) -> int:
        return self._clear_matching(
            lambda job: job.get("workspace_id") == workspace_id
            and (job.get("knowledge_base_id") or "default") == knowledge_base_id
            and str(job.get("id") or "") != str(exclude_job_id or "")
        )

    def clear_by_workspace(self, workspace_id: str) -> int:
        if not workspace_id:
            return 0
        return self._clear_matching(
            lambda job: job.get("workspace_id") == workspace_id
        )

    def _clear_matching(self, predicate) -> int:
        deleted = 0
        for key in list(self.redis.scan_iter(match=f"{self.prefix}:job:*", count=200)):
            raw = self.redis.get(key)
            if not raw:
                continue
            try:
                job = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if not predicate(job):
                continue
            job_id = str(job.get("id") or "")
            deleted += int(self.redis.delete(key) or 0)
            rebuild_key = self._active_rebuild_key(job)
            if _decode(self.redis.get(rebuild_key)) == job_id:
                self.redis.delete(rebuild_key)
            if job.get("type") == "data_source_sync":
                active_key = self._active_data_source_sync_key(job)
                if _decode(self.redis.get(active_key)) == job_id:
                    self.redis.delete(active_key)
            if job.get("type") == "delete_knowledge_base":
                active_key = self._active_delete_key(job)
                if _decode(self.redis.get(active_key)) == job_id:
                    self.redis.delete(active_key)
        return deleted

    def clear(self) -> None:
        # scan glob * matches jobs and all active/admission sub-keys.
        redis_scan_delete(self.redis, f"{self.prefix}:job:*")
        # Best-effort cleanup in case scan missed any scoped rebuild key.
        redis_scan_delete(self.redis, f"{self.prefix}:job:active:rebuild:*")

    def _save(self, job: dict) -> None:
        payload = json.dumps(job, ensure_ascii=False)
        self.redis.setex(self._job_key(str(job["id"])), self.ttl, payload.encode("utf-8"))

    def _job_key(self, job_id: str) -> str:
        return f"{self.prefix}:job:{job_id}"

    def _active_rebuild_key(self, job: dict) -> str:
        return f"{self.prefix}:job:active:rebuild:{_rebuild_key(job)}"

    def _active_data_source_sync_key(self, job: dict) -> str:
        return f"{self.prefix}:job:active:data-source-sync:{_data_source_sync_key(job)}"

    def _active_delete_key(self, job: dict) -> str:
        return f"{self.prefix}:job:active:delete:{_knowledge_base_key(job)}"

    def _legacy_active_rebuild_keys(self) -> tuple[str, str]:
        return (
            f"{self.prefix}:job:active:rebuild",
            f"{self.prefix}:job:active:rebuild:legacy",
        )

    def _legacy_active_rebuild_id(self) -> str:
        for key in self._legacy_active_rebuild_keys():
            active_id = _decode(self.redis.get(key))
            if active_id:
                return active_id
        return ""

    def _active_delete(self, job: dict) -> str:
        target_key = _knowledge_base_key(job)
        if not target_key:
            return ""
        active_key = self._active_delete_key(job)
        active_id = _decode(self.redis.get(active_key))
        active = self.get(active_id) if active_id else None
        if active and active.get("status") in RUNNING_JOB_STATUSES:
            return active_id
        if active_id:
            self.redis.delete(active_key)
        for candidate in self._active_jobs_for_target(job):
            if candidate.get("type") != "delete_knowledge_base":
                continue
            candidate_id = str(candidate.get("id") or "")
            self.redis.set(
                active_key,
                candidate_id.encode("utf-8"),
                ex=self.lock_ttl,
            )
            return candidate_id
        return ""

    def _delete_conflict(self, job: dict) -> dict | None:
        active_id = self._active_delete(job)
        return _delete_conflict(active_id) if active_id else None

    def _active_jobs_for_target(self, job: dict) -> list[dict]:
        return [
            candidate
            for candidate in self._iter_jobs()
            if candidate.get("status") in RUNNING_JOB_STATUSES
            and _same_knowledge_base(candidate, job)
        ]

    def _iter_jobs(self):
        for key in self.redis.scan_iter(
            match=f"{self.prefix}:job:*",
            count=200,
        ):
            raw = self.redis.get(key)
            if not raw:
                continue
            try:
                job = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if isinstance(job, dict) and job.get("id"):
                yield job

    @contextmanager
    def _admission_guard(self):
        key = f"{self.prefix}:job:admission"
        token = uuid.uuid4().hex.encode("utf-8")
        deadline = time.monotonic() + 2.0
        while not self.redis.set(key, token, nx=True, ex=10):
            if time.monotonic() >= deadline:
                raise RuntimeError("Timeout lock ammissione job")
            time.sleep(0.01)
        try:
            yield
        finally:
            self._release_admission_guard(key, token)

    def _release_admission_guard(self, key: str, token: bytes) -> None:
        if _decode(self.redis.get(key)) != _decode(token):
            return
        if hasattr(self.redis, "eval"):
            try:
                self.redis.eval(
                    "if redis.call('get', KEYS[1]) == ARGV[1] "
                    "then return redis.call('del', KEYS[1]) else return 0 end",
                    1,
                    key,
                    token,
                )
                return
            except Exception:
                pass
        if _decode(self.redis.get(key)) == _decode(token):
            self.redis.delete(key)


def _public_job(job: dict | None) -> dict | None:
    if not job:
        return None
    public = deepcopy(job)
    public["errors"] = list(public.get("errors", []))
    public["knowledge_base_id"] = public.get("knowledge_base_id") or "default"
    return public


def _decode(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return str(value)


def _data_source_sync_key(job: dict) -> str:
    raw = (
        f"{job.get('workspace_id') or ''}:"
        f"{job.get('knowledge_base_id') or 'default'}:"
        f"{job.get('data_source_id') or ''}"
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _knowledge_base_key(job: dict) -> str:
    workspace_id = str(job.get("workspace_id") or "")
    if not workspace_id:
        return ""
    raw = (
        f"{workspace_id}:"
        f"{job.get('knowledge_base_id') or 'default'}"
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _same_knowledge_base(first: dict, second: dict) -> bool:
    first_workspace = str(first.get("workspace_id") or "")
    second_workspace = str(second.get("workspace_id") or "")
    return bool(first_workspace) and first_workspace == second_workspace and (
        first.get("knowledge_base_id") or "default"
    ) == (second.get("knowledge_base_id") or "default")


def _delete_conflict(job_id: str) -> dict:
    return {
        "error": "Knowledge base in eliminazione",
        "status": "knowledge_base_deleting",
        "job_id": job_id,
    }


def _active_jobs_conflict(job_id: str) -> dict:
    return {
        "error": "La knowledge base ha job attivi",
        "status": "knowledge_base_has_active_jobs",
        "job_id": job_id,
    }


def _rebuild_conflict(job_id: str) -> dict:
    return {
        "error": "Ricostruzione indice gia' in corso",
        "status": "conflict",
        "job_id": job_id,
    }


def _sync_conflict(job_id: str, job: dict) -> dict:
    return {
        "error": "Sync data source gia' in corso",
        "status": "conflict",
        "job_id": job_id,
        "data_source_id": job.get("data_source_id", ""),
    }


def _rebuild_key(job: dict) -> str:
    if not job.get("workspace_id"):
        return "legacy"
    raw = (
        f"{job.get('workspace_id') or ''}:"
        f"{job.get('knowledge_base_id') or 'default'}"
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _env_int(name: str, default: int, *, minimum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        return default
    return max(minimum, value)
