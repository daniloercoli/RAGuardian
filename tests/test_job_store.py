from app.utils.job_store import MemoryJobStore, RedisJobStore
from app.utils.index_lock import index_write_lock


def _job(job_id="job-1", status="running"):
    return {
        "id": job_id,
        "status": status,
        "message": "started",
        "processed": 0,
        "total": 1,
        "current_file": "",
        "errors": [],
        "profile": {},
        "started_at": 1.0,
        "finished_at": None,
    }


def test_memory_job_store_tracks_rebuild_lifecycle():
    store = MemoryJobStore()

    payload, status = store.create_rebuild_job(_job())
    conflict, conflict_status = store.create_rebuild_job(_job("job-2"))

    assert status == 202
    assert payload["id"] == "job-1"
    assert conflict_status == 409
    assert conflict["job_id"] == "job-1"
    assert store.active_jobs_count() == 1

    store.update("job-1", processed=1)
    store.append_error("job-1", "demo.pdf", "boom")
    store.finish("job-1", "completed_with_errors", "done")

    finished = store.get("job-1")
    assert finished["processed"] == 1
    assert finished["errors"] == [{"filename": "demo.pdf", "error": "boom"}]
    assert finished["status"] == "completed_with_errors"
    assert store.active_jobs_count() == 0


def test_memory_job_store_allows_generic_jobs_without_rebuild_conflict():
    store = MemoryJobStore()

    first, first_status = store.create_job({**_job("job-1"), "type": "file_upload"})
    second, second_status = store.create_job({**_job("job-2"), "type": "audio_upload"})

    assert first_status == 202
    assert second_status == 202
    assert first["id"] == "job-1"
    assert second["id"] == "job-2"
    assert store.active_jobs_count() == 2


def test_memory_job_store_rejects_concurrent_data_source_sync():
    store = MemoryJobStore()
    first_job = {
        **_job("sync-1"),
        "type": "data_source_sync",
        "workspace_id": "workspace-a",
        "data_source_id": "legal-mailbox",
    }
    second_job = {**first_job, "id": "sync-2"}

    _payload, status = store.create_data_source_sync_job(first_job)
    conflict, conflict_status = store.create_data_source_sync_job(second_job)

    assert status == 202
    assert conflict_status == 409
    assert conflict["job_id"] == "sync-1"

    store.finish("sync-1", "completed", "done")
    payload, status = store.create_data_source_sync_job(second_job)

    assert status == 202
    assert payload["id"] == "sync-2"


def test_redis_job_store_uses_shared_active_lock():
    redis = FakeRedis()
    first = RedisJobStore(redis)
    second = RedisJobStore(redis)

    _payload, status = first.create_rebuild_job(_job("job-1"))
    conflict, conflict_status = second.create_rebuild_job(_job("job-2"))

    assert status == 202
    assert conflict_status == 409
    assert conflict["job_id"] == "job-1"

    first.finish("job-1", "completed", "done")
    payload, status = second.create_rebuild_job(_job("job-2"))

    assert status == 202
    assert payload["id"] == "job-2"


def test_redis_job_store_rejects_concurrent_data_source_sync():
    redis = FakeRedis()
    first = RedisJobStore(redis)
    second = RedisJobStore(redis)
    first_job = {
        **_job("sync-1"),
        "type": "data_source_sync",
        "workspace_id": "workspace-a",
        "data_source_id": "legal-mailbox",
    }
    second_job = {**first_job, "id": "sync-2"}

    _payload, status = first.create_data_source_sync_job(first_job)
    conflict, conflict_status = second.create_data_source_sync_job(second_job)

    assert status == 202
    assert conflict_status == 409
    assert conflict["job_id"] == "sync-1"

    first.finish("sync-1", "completed", "done")
    payload, status = second.create_data_source_sync_job(second_job)

    assert status == 202
    assert payload["id"] == "sync-2"


def test_index_write_lock_uses_memory_fallback(monkeypatch):
    monkeypatch.setattr("app.utils.index_lock.configured_state_backend", lambda: "memory")
    monkeypatch.setattr("app.utils.index_lock.configured_queue_backend", lambda: "inline")

    with index_write_lock():
        assert True


class FakeRedis:
    def __init__(self):
        self.data = {}

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.data:
            return False
        self.data[key] = value
        return True

    def setex(self, key, _ttl, value):
        self.data[key] = value

    def get(self, key):
        return self.data.get(key)

    def delete(self, *keys):
        deleted = 0
        for key in keys:
            if key in self.data:
                deleted += 1
                del self.data[key]
        return deleted

    def scan_iter(self, match=None, count=None):
        prefix = (match or "").rstrip("*")
        for key in list(self.data):
            if not match or key.startswith(prefix):
                yield key
