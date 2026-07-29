import concurrent.futures
import importlib
import threading

import pytest
from redis.exceptions import (
    AuthenticationError,
    ConnectionError as RedisConnectionError,
    ResponseError,
    TimeoutError as RedisTimeoutError,
)

from app.utils.job_store import MemoryJobStore, RedisJobStore
from app.utils.index_lock import (
    index_write_lock,
    lifecycle_read_lock,
    lifecycle_write_lock,
)


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


def test_memory_job_store_counts_and_clears_one_workspace():
    store = MemoryJobStore()
    store.create_job({**_job("job-a"), "workspace_id": "workspace-a"})
    store.create_job({**_job("job-b"), "workspace_id": "workspace-b"})
    store.finish("job-a", "completed", "done")

    assert store.active_jobs_count("workspace-a") == 0
    assert store.active_jobs_count("workspace-b") == 1
    assert store.clear_by_workspace("workspace-a") == 1
    assert store.get("job-a") is None
    assert store.get("job-b") is not None


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


def test_memory_job_locks_and_cleanup_are_scoped_by_knowledge_base():
    store = MemoryJobStore()
    default_rebuild = {
        **_job("rebuild-default"),
        "type": "rebuild_index",
        "workspace_id": "workspace-a",
        "knowledge_base_id": "default",
    }
    secondary_rebuild = {
        **default_rebuild,
        "id": "rebuild-secondary",
        "knowledge_base_id": "kb_11111111111111111111111111111111",
    }

    assert store.create_rebuild_job(default_rebuild)[1] == 202
    assert store.create_rebuild_job(secondary_rebuild)[1] == 202
    assert store.active_jobs_count("workspace-a", "default") == 1
    assert store.active_jobs_count(
        "workspace-a",
        "kb_11111111111111111111111111111111",
    ) == 1
    assert store.clear_by_knowledge_base("workspace-a", "default") == 1
    assert store.get("rebuild-default") is None
    assert store.get("rebuild-secondary") is not None

    default_sync = {
        **_job("sync-default"),
        "type": "data_source_sync",
        "workspace_id": "workspace-a",
        "knowledge_base_id": "default",
        "data_source_id": "mail",
    }
    secondary_sync = {
        **default_sync,
        "id": "sync-secondary",
        "knowledge_base_id": "kb_11111111111111111111111111111111",
    }
    assert store.create_data_source_sync_job(default_sync)[1] == 202
    assert store.create_data_source_sync_job(secondary_sync)[1] == 202


def test_memory_delete_admission_is_idempotent_and_gates_target_jobs():
    store = MemoryJobStore()
    target = {
        **_job("delete-a"),
        "type": "delete_knowledge_base",
        "workspace_id": "workspace-a",
        "knowledge_base_id": "kb_11111111111111111111111111111111",
    }
    barrier = threading.Barrier(2)

    def admit(job_id):
        barrier.wait()
        return store.create_delete_job({**target, "id": job_id})

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(admit, ["delete-a", "delete-b"]))

    assert [status for _payload, status in results] == [202, 202]
    admitted_ids = {payload["id"] for payload, _status in results}
    assert len(admitted_ids) == 1
    admitted_id = admitted_ids.pop()
    blocked, blocked_status = store.create_job(
        {
            **_job("late-upload"),
            "type": "file_upload",
            "workspace_id": "workspace-a",
            "knowledge_base_id": target["knowledge_base_id"],
        }
    )
    assert blocked_status == 409
    assert blocked["status"] == "knowledge_base_deleting"

    other, other_status = store.create_job(
        {
            **_job("other-upload"),
            "type": "file_upload",
            "workspace_id": "workspace-a",
            "knowledge_base_id": "default",
        }
    )
    assert other_status == 202
    assert other["id"] == "other-upload"

    store.finish(admitted_id, "failed", "retry")
    retried, retry_status = store.create_delete_job(
        {**target, "id": "delete-retry"}
    )
    assert retry_status == 202
    assert retried["id"] == "delete-retry"


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


def test_redis_rebuild_locks_are_scoped_by_knowledge_base_and_honor_legacy_lock():
    redis = FakeRedis()
    store = RedisJobStore(redis)
    default_job = {
        **_job("default"),
        "workspace_id": "workspace-a",
        "knowledge_base_id": "default",
    }
    secondary_job = {
        **default_job,
        "id": "secondary",
        "knowledge_base_id": "kb_11111111111111111111111111111111",
    }

    assert store.create_rebuild_job(default_job)[1] == 202
    assert store.create_rebuild_job(secondary_job)[1] == 202

    legacy_store = RedisJobStore(FakeRedis())
    assert legacy_store.create_rebuild_job(_job("legacy"))[1] == 202
    _payload, status = legacy_store.create_rebuild_job(default_job)
    assert status == 409


def test_redis_delete_gate_blocks_target_jobs_and_honors_pre_scope_rebuild_lock():
    redis = FakeRedis()
    store = RedisJobStore(redis)
    target = {
        **_job("delete-a"),
        "type": "delete_knowledge_base",
        "workspace_id": "workspace-a",
        "knowledge_base_id": "kb_11111111111111111111111111111111",
    }
    legacy_key = f"{store.prefix}:job:active:rebuild"
    redis.set(legacy_key, b"old-worker", ex=store.lock_ttl)

    conflict, conflict_status = store.create_delete_job(target)
    assert conflict_status == 409
    assert conflict["job_id"] == "old-worker"
    rebuild_conflict, rebuild_status = store.create_rebuild_job(
        {
            **_job("scoped-rebuild"),
            "workspace_id": target["workspace_id"],
            "knowledge_base_id": target["knowledge_base_id"],
        }
    )
    assert rebuild_status == 409
    assert rebuild_conflict["job_id"] == "old-worker"

    redis.delete(legacy_key)
    admitted, admitted_status = store.create_delete_job(target)
    duplicate, duplicate_status = store.create_delete_job(
        {**target, "id": "delete-b"}
    )
    assert admitted_status == 202
    assert duplicate_status == 202
    assert duplicate["id"] == admitted["id"] == "delete-a"

    blocked, blocked_status = store.create_data_source_sync_job(
        {
            **_job("late-sync"),
            "type": "data_source_sync",
            "workspace_id": target["workspace_id"],
            "knowledge_base_id": target["knowledge_base_id"],
            "data_source_id": "mail",
        }
    )
    assert blocked_status == 409
    assert blocked["status"] == "knowledge_base_deleting"


def test_redis_job_store_counts_and_clears_one_workspace():
    redis = FakeRedis()
    store = RedisJobStore(redis)
    store.create_job({**_job("job-a"), "workspace_id": "workspace-a"})
    store.create_job({**_job("job-b"), "workspace_id": "workspace-b"})
    store.finish("job-a", "completed", "done")

    assert store.active_jobs_count("workspace-a") == 0
    assert store.active_jobs_count("workspace-b") == 1
    assert store.clear_by_workspace("workspace-a") == 1
    assert store.get("job-a") is None
    assert store.get("job-b") is not None


def test_index_write_lock_uses_memory_fallback(monkeypatch):
    locks = importlib.import_module("utils.index_lock")
    monkeypatch.setattr(locks, "configured_state_backend", lambda: "memory")
    monkeypatch.setattr(locks, "configured_queue_backend", lambda: "inline")

    with index_write_lock():
        assert True


def test_lifecycle_lock_allows_readers_and_excludes_writer(monkeypatch, tmp_path):
    locks = importlib.import_module("utils.index_lock")
    monkeypatch.setattr(locks, "configured_state_backend", lambda: "memory")
    monkeypatch.setattr(locks, "configured_queue_backend", lambda: "inline")
    monkeypatch.setenv(
        "RAG_LIFECYCLE_LOCK_FILE",
        str(tmp_path / "lifecycle.lock"),
    )
    release_readers = threading.Event()
    first_reader_entered = threading.Event()
    second_reader_entered = threading.Event()
    writer_started = threading.Event()
    writer_entered = threading.Event()

    def reader(entered):
        with lifecycle_read_lock():
            entered.set()
            assert release_readers.wait(timeout=2)

    def writer():
        writer_started.set()
        with lifecycle_write_lock():
            writer_entered.set()

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        first = executor.submit(reader, first_reader_entered)
        assert first_reader_entered.wait(timeout=2)
        second = executor.submit(reader, second_reader_entered)
        assert second_reader_entered.wait(timeout=2)
        pending_writer = executor.submit(writer)
        assert writer_started.wait(timeout=2)
        assert not writer_entered.wait(timeout=0.1)

        release_readers.set()
        assert writer_entered.wait(timeout=2)
        first.result(timeout=2)
        second.result(timeout=2)
        pending_writer.result(timeout=2)


def test_scoped_lifecycle_writer_blocks_only_its_knowledge_base(
    monkeypatch,
    tmp_path,
):
    locks = importlib.import_module("utils.index_lock")
    monkeypatch.setattr(locks, "configured_state_backend", lambda: "memory")
    monkeypatch.setattr(locks, "configured_queue_backend", lambda: "inline")
    monkeypatch.setenv(
        "RAG_LIFECYCLE_LOCK_FILE",
        str(tmp_path / "lifecycle.lock"),
    )
    release_reader = threading.Event()
    reader_entered = threading.Event()
    other_writer_entered = threading.Event()
    same_writer_started = threading.Event()
    same_writer_entered = threading.Event()

    def reader():
        with locks.lifecycle_read_lock(scope="collection-a"):
            reader_entered.set()
            assert release_reader.wait(timeout=2)

    def other_writer():
        with locks.lifecycle_write_lock(scope="collection-b"):
            other_writer_entered.set()

    def same_writer():
        same_writer_started.set()
        with locks.lifecycle_write_lock(scope="collection-a"):
            same_writer_entered.set()

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        active_reader = executor.submit(reader)
        assert reader_entered.wait(timeout=2)

        unrelated_writer = executor.submit(other_writer)
        assert other_writer_entered.wait(timeout=2)
        unrelated_writer.result(timeout=2)

        blocked_writer = executor.submit(same_writer)
        assert same_writer_started.wait(timeout=2)
        assert not same_writer_entered.wait(timeout=0.1)

        release_reader.set()
        assert same_writer_entered.wait(timeout=2)
        active_reader.result(timeout=2)
        blocked_writer.result(timeout=2)


def test_global_lifecycle_writer_blocks_scoped_reader(monkeypatch, tmp_path):
    locks = importlib.import_module("utils.index_lock")
    monkeypatch.setattr(locks, "configured_state_backend", lambda: "memory")
    monkeypatch.setattr(locks, "configured_queue_backend", lambda: "inline")
    monkeypatch.setenv(
        "RAG_LIFECYCLE_LOCK_FILE",
        str(tmp_path / "lifecycle.lock"),
    )
    release_writer = threading.Event()
    writer_entered = threading.Event()
    reader_started = threading.Event()
    reader_entered = threading.Event()

    def writer():
        with locks.lifecycle_write_lock():
            writer_entered.set()
            assert release_writer.wait(timeout=2)

    def reader():
        reader_started.set()
        with locks.lifecycle_read_lock(scope="collection-a"):
            reader_entered.set()

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        active_writer = executor.submit(writer)
        assert writer_entered.wait(timeout=2)
        blocked_reader = executor.submit(reader)
        assert reader_started.wait(timeout=2)
        assert not reader_entered.wait(timeout=0.1)

        release_writer.set()
        assert reader_entered.wait(timeout=2)
        active_writer.result(timeout=2)
        blocked_reader.result(timeout=2)


def test_scoped_lifecycle_writer_does_not_publish_global_epoch(
    monkeypatch,
    tmp_path,
):
    locks = importlib.import_module("utils.index_lock")
    monkeypatch.setattr(locks, "configured_state_backend", lambda: "memory")
    monkeypatch.setattr(locks, "configured_queue_backend", lambda: "inline")
    monkeypatch.setenv(
        "RAG_LIFECYCLE_LOCK_FILE",
        str(tmp_path / "lifecycle.lock"),
    )

    with locks.lifecycle_read_lock(scope="collection-a"):
        pass
    with locks.lifecycle_write_lock(scope="collection-a"):
        pass

    assert locks._read_lifecycle_generation() == 0


def test_lifecycle_epoch_invalidation_is_serialized(monkeypatch):
    locks = importlib.import_module("utils.index_lock")
    identity = "test-generation"
    callback_entered = threading.Event()
    release_callback = threading.Event()
    callback_calls = []
    saved_invalidators = dict(locks._LIFECYCLE_INVALIDATORS)
    saved_generations = dict(locks._OBSERVED_LIFECYCLE_GENERATIONS)
    monkeypatch.setattr(
        locks,
        "_lifecycle_generation_identity",
        lambda: identity,
    )
    monkeypatch.setattr(locks, "_read_lifecycle_generation", lambda: 1)
    locks._LIFECYCLE_INVALIDATORS.clear()
    locks._OBSERVED_LIFECYCLE_GENERATIONS.clear()
    locks._OBSERVED_LIFECYCLE_GENERATIONS[identity] = 0

    def invalidate():
        callback_calls.append(threading.get_ident())
        callback_entered.set()
        assert release_callback.wait(timeout=2)

    locks._LIFECYCLE_INVALIDATORS["test"] = invalidate
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(locks.synchronize_lifecycle_generation)
            assert callback_entered.wait(timeout=2)
            second = executor.submit(locks.synchronize_lifecycle_generation)
            assert len(callback_calls) == 1
            release_callback.set()
            assert first.result(timeout=2) == 1
            assert second.result(timeout=2) == 1
        assert len(callback_calls) == 1
    finally:
        locks._LIFECYCLE_INVALIDATORS.clear()
        locks._LIFECYCLE_INVALIDATORS.update(saved_invalidators)
        locks._OBSERVED_LIFECYCLE_GENERATIONS.clear()
        locks._OBSERVED_LIFECYCLE_GENERATIONS.update(saved_generations)


def test_lifecycle_writer_publishes_epoch_for_other_processes(
    monkeypatch,
    tmp_path,
):
    import app.utils.index_lock as locks

    monkeypatch.setattr(locks, "configured_state_backend", lambda: "memory")
    monkeypatch.setattr(locks, "configured_queue_backend", lambda: "inline")
    monkeypatch.setenv(
        "RAG_LIFECYCLE_LOCK_FILE",
        str(tmp_path / "lifecycle.lock"),
    )
    saved_invalidators = dict(locks._LIFECYCLE_INVALIDATORS)
    saved_generations = dict(locks._OBSERVED_LIFECYCLE_GENERATIONS)
    invalidations = []
    locks._LIFECYCLE_INVALIDATORS.clear()
    locks._OBSERVED_LIFECYCLE_GENERATIONS.clear()
    locks.register_lifecycle_invalidator(
        "test",
        lambda: invalidations.append(True),
    )

    try:
        with locks.lifecycle_read_lock():
            pass
        with locks.lifecycle_write_lock():
            pass

        identity = locks._lifecycle_generation_identity()
        assert locks._read_lifecycle_generation() == 1
        locks._OBSERVED_LIFECYCLE_GENERATIONS[identity] = 0
        with locks.lifecycle_read_lock():
            pass
        assert invalidations == [True]
    finally:
        locks._LIFECYCLE_INVALIDATORS.clear()
        locks._LIFECYCLE_INVALIDATORS.update(saved_invalidators)
        locks._OBSERVED_LIFECYCLE_GENERATIONS.clear()
        locks._OBSERVED_LIFECYCLE_GENERATIONS.update(saved_generations)


class FakeRedis:
    def __init__(self):
        self.data = {}
        self.gate_locked = False

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.data:
            return False
        self.data[key] = value
        return True

    def setex(self, key, _ttl, value):
        self.data[key] = value

    def get(self, key):
        return self.data.get(key)

    def incr(self, key):
        value = int(self.data.get(key, 0)) + 1
        self.data[key] = value
        return value

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

    def expire(self, key, _ttl):
        return key in self.data

    def lock(self, *_args, **_kwargs):
        client = self

        class Lock:
            def acquire(self, blocking=True):
                assert blocking is True
                if client.gate_locked:
                    return False
                client.gate_locked = True
                return True

            def release(self):
                assert client.gate_locked
                client.gate_locked = False

            def extend(self, _ttl, replace_ttl=False):
                assert replace_ttl is True
                return client.gate_locked

        return Lock()


def test_redis_lifecycle_lock_registers_readers_and_holds_writers():
    from app.utils.index_lock import _RedisLifecycleLock

    redis = FakeRedis()

    with _RedisLifecycleLock(redis, shared=True):
        assert redis.gate_locked is False
        assert len(
            [
                key
                for key in redis.data
                if ":lock:lifecycle:reader:" in key
            ]
        ) == 1
    assert not [
        key
        for key in redis.data
        if ":lock:lifecycle:reader:" in key
    ]

    with _RedisLifecycleLock(redis, shared=False):
        assert redis.gate_locked is True
    assert redis.gate_locked is False


def test_redis_lifecycle_lease_is_renewed_until_stopped():
    from app.utils.index_lock import _RedisLeaseRenewer

    renewed = threading.Event()
    renewer = _RedisLeaseRenewer(
        lambda: renewed.set() or True,
        ttl=60,
        description="test",
    )
    renewer._interval = 0.01

    renewer.start()
    assert renewed.wait(timeout=1)
    assert renewer.stop() is None


@pytest.mark.parametrize(
    "error_type",
    [
        pytest.param(RedisConnectionError, id="connection"),
        pytest.param(RedisTimeoutError, id="timeout"),
    ],
)
def test_redis_lifecycle_lease_retries_a_transient_renewal_error(error_type):
    from app.utils.index_lock import _RedisLeaseRenewer

    recovered = threading.Event()
    attempts = []

    def renew():
        attempts.append(True)
        if len(attempts) == 1:
            raise error_type("temporary redis outage")
        recovered.set()
        return True

    renewer = _RedisLeaseRenewer(renew, ttl=1, description="test")
    renewer._interval = 0.01
    renewer._retry_interval = 0.01

    renewer.start()
    assert recovered.wait(timeout=1)
    assert renewer.stop() is None
    assert len(attempts) >= 2


def test_redis_lifecycle_lease_fails_immediately_when_lock_is_not_owned():
    from redis.exceptions import LockNotOwnedError

    from app.utils.index_lock import _RedisLeaseRenewer

    attempts = []

    def renew():
        attempts.append(True)
        raise LockNotOwnedError("token no longer owns the lock")

    renewer = _RedisLeaseRenewer(renew, ttl=60, description="test")
    renewer._interval = 0.01
    renewer._retry_interval = 0.01

    renewer.start()
    assert renewer._lost.wait(timeout=1)
    error = renewer.stop()

    assert isinstance(error, LockNotOwnedError)
    assert len(attempts) == 1


@pytest.mark.parametrize(
    "error_type",
    [
        pytest.param(AuthenticationError, id="authentication"),
        pytest.param(ResponseError, id="response"),
        pytest.param(ValueError, id="unexpected"),
    ],
)
def test_redis_lifecycle_lease_fails_immediately_for_permanent_error(error_type):
    from app.utils.index_lock import _RedisLeaseRenewer

    attempts = []

    def renew():
        attempts.append(True)
        raise error_type("permanent renewal failure")

    renewer = _RedisLeaseRenewer(renew, ttl=60, description="test")
    renewer._interval = 0.01
    renewer._retry_interval = 0.01

    renewer.start()
    assert renewer._lost.wait(timeout=1)
    error = renewer.stop()

    assert isinstance(error, error_type)
    assert len(attempts) == 1


@pytest.mark.parametrize(
    "error_type",
    [
        pytest.param(RedisConnectionError, id="connection"),
        pytest.param(RedisTimeoutError, id="timeout"),
    ],
)
def test_redis_lifecycle_lease_fails_after_retry_window_expires(error_type):
    from app.utils.index_lock import _RedisLeaseRenewer

    attempts = []

    def renew():
        attempts.append(True)
        raise error_type("redis remains unavailable")

    renewer = _RedisLeaseRenewer(renew, ttl=0.05, description="test")
    renewer._interval = 0.005
    renewer._retry_interval = 0.005

    renewer.start()
    assert renewer._lost.wait(timeout=1)
    error = renewer.stop()

    assert isinstance(error, RuntimeError)
    assert "before expiry" in str(error)
    assert len(attempts) > 1


def test_redis_lifecycle_reader_reports_a_missing_lease():
    from app.utils.index_lock import _RedisLifecycleLock

    redis = FakeRedis()
    lock = _RedisLifecycleLock(redis, shared=True)

    with pytest.raises(RuntimeError, match="lease was lost"):
        with lock:
            redis.delete(lock._reader_key)


def test_distributed_lock_health_checks_and_unregisters_index_renewer():
    from app.utils.index_lock import (
        _RenewingRedisLock,
        assert_distributed_locks_healthy,
    )

    redis = FakeRedis()
    lock = _RenewingRedisLock(
        redis.lock("index"),
        ttl=60,
        description="index writer",
    )

    with pytest.raises(RuntimeError, match="Redis index writer lock lease was lost"):
        with lock:
            assert_distributed_locks_healthy()
            lock._renewer._set_error(RuntimeError("lease expired"))
            assert_distributed_locks_healthy()

    assert_distributed_locks_healthy()


def test_distributed_lock_health_checks_lifecycle_renewer_per_thread():
    from app.utils.index_lock import (
        _RedisLifecycleLock,
        assert_distributed_locks_healthy,
    )

    redis = FakeRedis()
    lock = _RedisLifecycleLock(redis, shared=True)

    with pytest.raises(
        RuntimeError,
        match="Redis lifecycle reader lock lease was lost",
    ):
        with lock:
            lock._renewer._set_error(RuntimeError("reader lease expired"))
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                assert (
                    executor.submit(
                        assert_distributed_locks_healthy
                    ).result(timeout=1)
                    is None
                )
            assert_distributed_locks_healthy()

    assert_distributed_locks_healthy()
