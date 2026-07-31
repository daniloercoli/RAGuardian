from __future__ import annotations

import logging
import os
import shutil
import threading
import time
import uuid
from pathlib import Path

from flask import jsonify, render_template, request

from utils.auth import (
    current_user,
    require_api_any_scope,
    require_api_scope,
    require_login,
)
from utils.job_store import RUNNING_JOB_STATUSES, get_job_store, queue_name
from utils.index_lock import (
    assert_distributed_locks_healthy,
    lifecycle_read_lock,
    lifecycle_write_lock,
)
from utils.knowledge_base_store import (
    DEFAULT_KNOWLEDGE_BASE_ID,
    KnowledgeBaseValidationError,
    validate_knowledge_base_description,
    validate_knowledge_base_id,
    validate_knowledge_base_name,
)
from utils.settings_store import SettingsStore
from utils.state_backend import configured_queue_backend, redis_connection
from utils.workspace import (
    _delete_chroma_collection,
    collection_for_knowledge_base,
    knowledge_base_context,
    knowledge_base_store,
    workspace_from_request,
)


_inline_delete_workers: set[str] = set()
_inline_delete_workers_lock = threading.Lock()
log = logging.getLogger(__name__)


def register_knowledge_base_routes(app) -> None:
    @app.route("/knowledge-bases", methods=["GET"])
    @require_login
    def knowledge_bases_page():
        return render_template("knowledge_bases.html")

    @app.route("/api/knowledge-bases", methods=["GET"])
    @require_login
    def knowledge_bases_list():
        return jsonify(_list_response(app, public=False))

    @app.route("/api/knowledge-bases", methods=["POST"])
    @require_login
    def knowledge_bases_create():
        return _create_response(app, public=False)

    @app.route("/api/knowledge-bases/<knowledge_base_id>", methods=["GET"])
    @require_login
    def knowledge_bases_get(knowledge_base_id):
        return jsonify(_get_payload(app, knowledge_base_id, public=False))

    @app.route("/api/knowledge-bases/<knowledge_base_id>", methods=["PATCH"])
    @require_login
    def knowledge_bases_update(knowledge_base_id):
        return _update_response(app, knowledge_base_id, public=False)

    @app.route("/api/knowledge-bases/<knowledge_base_id>", methods=["DELETE"])
    @require_login
    def knowledge_bases_delete(knowledge_base_id):
        return _delete_response(app, knowledge_base_id, public=False)

    @app.route("/api/knowledge-bases/jobs/<job_id>", methods=["GET"])
    @require_login
    def knowledge_bases_job(job_id):
        job = get_job_store().get(job_id)
        workspace = workspace_from_request(app)
        if (
            not job
            or job.get("type") != "delete_knowledge_base"
            or job.get("workspace_id") != workspace.workspace_id
        ):
            raise _not_found()
        return jsonify(job)

    @app.route("/api/v1/knowledge-bases", methods=["GET"])
    @require_api_any_scope("query", "ingest", "kb_manage")
    def api_knowledge_bases_list():
        return jsonify(_list_response(app, public=True))

    @app.route("/api/v1/knowledge-bases", methods=["POST"])
    @require_api_scope("kb_manage")
    def api_knowledge_bases_create():
        return _create_response(app, public=True)

    @app.route("/api/v1/knowledge-bases/<knowledge_base_id>", methods=["GET"])
    @require_api_any_scope("query", "ingest", "kb_manage")
    def api_knowledge_bases_get(knowledge_base_id):
        return jsonify(_get_payload(app, knowledge_base_id, public=True))

    @app.route("/api/v1/knowledge-bases/<knowledge_base_id>", methods=["PATCH"])
    @require_api_scope("kb_manage")
    def api_knowledge_bases_update(knowledge_base_id):
        return _update_response(app, knowledge_base_id, public=True)

    @app.route("/api/v1/knowledge-bases/<knowledge_base_id>", methods=["DELETE"])
    @require_api_scope("kb_manage")
    def api_knowledge_bases_delete(knowledge_base_id):
        return _delete_response(app, knowledge_base_id, public=True)


def _list_payload(app, *, public: bool) -> list[dict]:
    with lifecycle_read_lock():
        workspace = workspace_from_request(app)
        store = knowledge_base_store(workspace, app=app)
        records = store.list()
        if public:
            allowed = _api_key_allowed_ids()
            records = [
                record for record in records if record["id"] in allowed
            ]
        payload = []
        for record in records:
            knowledge_base_id = record["id"]
            collection_name = collection_for_knowledge_base(
                workspace.workspace_id,
                knowledge_base_id,
            )
            with lifecycle_read_lock(scope=collection_name):
                current = store.get(knowledge_base_id)
                if current is None:
                    continue
                payload.append(_with_stats(workspace, current))
        return payload


def _list_response(app, *, public: bool) -> dict:
    return {
        "knowledge_bases": _list_payload(app, public=public),
        "limits": {
            "max_query_knowledge_bases": int(
                app.config.get("MAX_QUERY_KNOWLEDGE_BASES", 5)
            ),
        },
    }


def _get_payload(app, knowledge_base_id: str, *, public: bool) -> dict:
    knowledge_base_id = validate_knowledge_base_id(knowledge_base_id)
    if public:
        _ensure_api_key_allowed(knowledge_base_id)
    with lifecycle_read_lock():
        workspace = workspace_from_request(app)
        collection_name = collection_for_knowledge_base(
            workspace.workspace_id,
            knowledge_base_id,
        )
        with lifecycle_read_lock(scope=collection_name):
            record = knowledge_base_store(
                workspace,
                app=app,
            ).get(knowledge_base_id)
            if record is None:
                raise _not_found()
            request._rag_knowledge_base_id = knowledge_base_id
            return _with_stats(workspace, record)


def _create_response(app, *, public: bool):
    data = _json_object()
    _reject_unknown_fields(data, {"name", "description"})
    with lifecycle_read_lock():
        workspace = workspace_from_request(app)
        store = knowledge_base_store(workspace, app=app)
        record = store.create(
            name=data.get("name"),
            description=data.get("description", ""),
        )
        if public:
            collection_name = collection_for_knowledge_base(
                workspace.workspace_id,
                record["id"],
            )
            with lifecycle_read_lock(scope=collection_name):
                try:
                    _add_created_knowledge_base_to_api_key(
                        app,
                        record["id"],
                        validate=lambda: _ensure_created_knowledge_base_active(
                            store,
                            record["id"],
                        ),
                    )
                except Exception:
                    try:
                        _rollback_created_knowledge_base_if_active(
                            store,
                            record["id"],
                        )
                    except Exception as rollback_error:
                        raise RuntimeError(
                            "Grant API key non completato e rollback della "
                            "knowledge base fallito"
                        ) from rollback_error
                    raise
        request._rag_knowledge_base_id = record["id"]
        return jsonify(_with_stats(workspace, record)), 201


def _update_response(app, knowledge_base_id: str, *, public: bool):
    data = _json_object()
    _reject_unknown_fields(data, {"name", "description"})
    if not data:
        raise KnowledgeBaseValidationError("Nessun campo da aggiornare")
    if "name" in data:
        validate_knowledge_base_name(data["name"])
    if "description" in data:
        validate_knowledge_base_description(data["description"])
    knowledge_base_id = validate_knowledge_base_id(knowledge_base_id)
    if public:
        _ensure_api_key_allowed(knowledge_base_id)
    workspace = workspace_from_request(app)
    collection_name = collection_for_knowledge_base(
        workspace.workspace_id,
        knowledge_base_id,
    )
    with lifecycle_write_lock(scope=collection_name, publish=True):
        assert_distributed_locks_healthy()
        if "name" in data:
            from utils.rag_engine import clear_cache_for_collection

            clear_cache_for_collection(collection_name)
            assert_distributed_locks_healthy()
        record = knowledge_base_store(workspace, app=app).update(
            knowledge_base_id,
            name=data.get("name") if "name" in data else None,
            description=(
                data.get("description")
                if "description" in data
                else None
            ),
        )
        assert_distributed_locks_healthy()
        request._rag_knowledge_base_id = knowledge_base_id
        return jsonify(_with_stats(workspace, record))


def _delete_response(app, knowledge_base_id: str, *, public: bool):
    knowledge_base_id = validate_knowledge_base_id(knowledge_base_id)
    requester_api_key_id = ""
    api_key_allowed = True
    if public:
        requester_api_key_id = str(
            (getattr(request, "api_key", None) or {}).get("api_key_id") or ""
        )
        api_key_allowed = knowledge_base_id in _api_key_allowed_ids()
        if (
            knowledge_base_id == DEFAULT_KNOWLEDGE_BASE_ID
            and not api_key_allowed
        ):
            raise _not_found()
    if knowledge_base_id == DEFAULT_KNOWLEDGE_BASE_ID:
        raise KnowledgeBaseValidationError(
            "La knowledge base default non può essere eliminata",
            code="default_knowledge_base",
            status_code=409,
        )
    with lifecycle_read_lock():
        admission = _admit_delete_job(
            app,
            knowledge_base_id,
            public=public,
            api_key_allowed=api_key_allowed,
            requester_api_key_id=requester_api_key_id,
        )

    job_id = admission["job_id"]
    job_store = admission["job_store"]
    payload = admission["payload"]
    queued = admission["queued"]
    runtime = admission["runtime"]
    record = admission["record"]
    store = admission["store"]
    try:
        if queued:
            _enqueue_delete_knowledge_base_job(job_id, runtime)
        else:
            _start_inline_delete_worker(job_id, runtime)
    except Exception as exc:
        if record is not None and record.get("status") == "deleting":
            _mark_delete_failed(
                store,
                knowledge_base_id,
                f"{type(exc).__name__}: worker non avviato",
            )
        job_store.append_error(
            job_id,
            knowledge_base_id,
            "Eliminazione non avviata",
        )
        job_store.finish(job_id, "failed", "Eliminazione knowledge base non avviata")
        raise
    current = job_store.get(job_id) or payload
    return jsonify({"job_id": job_id, **current}), 202


def _admit_delete_job(
    app,
    knowledge_base_id: str,
    *,
    public: bool,
    api_key_allowed: bool,
    requester_api_key_id: str,
) -> dict:
    """Authorize and persist a deletion job under the global read gate."""
    workspace = workspace_from_request(app)
    request._rag_knowledge_base_id = knowledge_base_id
    store = knowledge_base_store(workspace, app=app)
    record = store.get(knowledge_base_id)
    job_store = get_job_store()
    active_delete_job = None
    if record is None or (public and not api_key_allowed):
        active_delete_job = _active_delete_job_for_target(
            job_store,
            workspace.workspace_id,
            knowledge_base_id,
        )
    if public and not api_key_allowed:
        requester_owns_tombstone = bool(
            record
            and record.get("status") in {"deleting", "delete_failed"}
            and requester_api_key_id
            and store.delete_requester_api_key_id(knowledge_base_id)
            == requester_api_key_id
        )
        requester_owns_job = bool(
            active_delete_job
            and str(active_delete_job.get("requester_api_key_id") or "")
            == requester_api_key_id
        )
        if not requester_owns_tombstone and not requester_owns_job:
            raise _not_found()
    if record is None and active_delete_job is None:
        raise _not_found()
    context = (
        knowledge_base_context(
            workspace,
            knowledge_base_id,
            allow_inactive=True,
        )
        if record is not None
        else None
    )
    queued = configured_queue_backend() == "redis"
    job_id = uuid.uuid4().hex
    job = {
        "id": job_id,
        "type": "delete_knowledge_base",
        "status": "queued" if queued else "running",
        "message": (
            "Eliminazione knowledge base in coda"
            if queued
            else "Eliminazione knowledge base avviata"
        ),
        "processed": 0,
        "total": 7,
        "current_file": "",
        "errors": [],
        "result": None,
        "user_id": workspace.user_id,
        "workspace_id": workspace.workspace_id,
        "knowledge_base_id": knowledge_base_id,
        "started_at": time.time(),
        "finished_at": None,
    }
    if requester_api_key_id:
        job["requester_api_key_id"] = requester_api_key_id
    create_delete_job = getattr(job_store, "create_delete_job", None)
    if create_delete_job is None:
        create_delete_job = job_store.create_job
    payload, status_code = create_delete_job(job)
    if status_code >= 400 and status_code != 409:
        raise RuntimeError("Job di eliminazione non creato")
    if status_code == 409:
        raise KnowledgeBaseValidationError(
            payload.get("error") or "La knowledge base ha job attivi",
            code=payload.get("status")
            or "knowledge_base_has_active_jobs",
            status_code=409,
        )
    admitted_job_id = str(
        payload.get("id") or payload.get("job_id") or ""
    )
    if admitted_job_id:
        job_id = admitted_job_id

    runtime = _delete_runtime_config(
        app,
        workspace,
        knowledge_base_id,
        context=context,
    )
    runtime["DELETE_REQUESTER_API_KEY_ID"] = requester_api_key_id
    return {
        "job_id": job_id,
        "job_store": job_store,
        "payload": payload,
        "queued": queued,
        "runtime": runtime,
        "record": record,
        "store": store,
    }


def _start_inline_delete_worker(job_id: str, config: dict) -> bool:
    """Start one local worker, allowing a persisted job to recover after restart."""
    with _inline_delete_workers_lock:
        if job_id in _inline_delete_workers:
            return False
        _inline_delete_workers.add(job_id)

    def run() -> None:
        try:
            _delete_knowledge_base_job(job_id, config)
        finally:
            with _inline_delete_workers_lock:
                _inline_delete_workers.discard(job_id)

    try:
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
    except BaseException:
        with _inline_delete_workers_lock:
            _inline_delete_workers.discard(job_id)
        raise
    return True


def _enqueue_delete_knowledge_base_job(job_id: str, config: dict) -> None:
    from rq import Queue
    from rq.exceptions import DuplicateJobError

    connection = redis_connection()
    queue = Queue(queue_name(), connection=connection)
    rq_job_id = f"delete-kb-{job_id}"
    try:
        queue.enqueue(
            _delete_knowledge_base_job,
            job_id,
            config,
            job_id=rq_job_id,
            unique=True,
            job_timeout="2h",
            result_ttl=3600,
            failure_ttl=86400,
        )
    except DuplicateJobError:
        _requeue_terminal_rq_delete_job(
            queue,
            connection,
            rq_job_id,
        )


def _requeue_terminal_rq_delete_job(queue, connection, rq_job_id: str) -> None:
    """Accept an active duplicate or safely requeue a terminal RQ job."""
    from rq.exceptions import InvalidJobOperation, NoSuchJobError
    from rq.job import Job

    active_statuses = {"queued", "started", "deferred", "scheduled"}

    def fetch_job():
        try:
            return Job.fetch(rq_job_id, connection=connection)
        except NoSuchJobError as exc:
            raise RuntimeError(
                "Job RQ di eliminazione non più disponibile"
            ) from exc

    rq_job = fetch_job()
    status = _rq_job_status(rq_job)
    if status in active_statuses:
        return

    if status in {"failed", "stopped"}:
        registry = rq_job.failed_job_registry
    elif status == "canceled":
        registry = queue.canceled_job_registry
    elif status == "finished":
        registry = rq_job.finished_job_registry
    else:
        raise RuntimeError(f"Stato job RQ di eliminazione non valido: {status}")

    try:
        registry.requeue(rq_job)
    except InvalidJobOperation as exc:
        # A concurrent reconciliation may already have moved the same job.
        refreshed_status = _rq_job_status(fetch_job())
        if refreshed_status in active_statuses:
            return
        raise RuntimeError(
            "Job RQ di eliminazione terminale non riaccodato"
        ) from exc


def _rq_job_status(job) -> str:
    status = job.get_status(refresh=True)
    return str(getattr(status, "value", status) or "").lower()


def _active_delete_job_for_target(
    job_store,
    workspace_id: str,
    knowledge_base_id: str,
) -> dict | None:
    finder = getattr(job_store, "knowledge_base_deletion_job_id", None)
    if finder is None:
        return None
    job_id = finder(workspace_id, knowledge_base_id)
    job = job_store.get(job_id) if job_id else None
    if (
        not job
        or job.get("type") != "delete_knowledge_base"
        or job.get("status") not in RUNNING_JOB_STATUSES
        or job.get("workspace_id") != workspace_id
        or (job.get("knowledge_base_id") or DEFAULT_KNOWLEDGE_BASE_ID)
        != knowledge_base_id
    ):
        return None
    return job


def _delete_runtime_config(
    app,
    workspace,
    knowledge_base_id: str,
    *,
    context=None,
) -> dict:
    if context is not None:
        runtime = context.as_config()
    else:
        workspace_data = Path(workspace.data_folder)
        workspace_upload = Path(workspace.workspace_upload_folder)
        runtime = {
            "USER_ID": workspace.user_id,
            "WORKSPACE_ID": workspace.workspace_id,
            "KNOWLEDGE_BASE_ID": knowledge_base_id,
            "KNOWLEDGE_BASE_NAME": "",
            "SETTINGS_FILE": workspace.settings_file,
            "FILE_INDEX": str(
                workspace_data
                / "knowledge_bases"
                / knowledge_base_id
                / "files.json"
            ),
            "UPLOAD_FOLDER": str(
                workspace_upload
                / "__knowledge_bases__"
                / knowledge_base_id
            ),
            "WORKSPACE_UPLOAD_FOLDER": workspace.workspace_upload_folder,
            "CHROMA_COLLECTION": collection_for_knowledge_base(
                workspace.workspace_id,
                knowledge_base_id,
            ),
            "SECRETS_FILE": workspace.secrets_file,
            "SECRET_KEY": workspace.secret_key,
        }
    runtime["KNOWLEDGE_BASES_FILE"] = workspace.knowledge_bases_file
    runtime["USERS_FILE"] = app.config["USERS_FILE"]
    return runtime


def _delete_knowledge_base_job(job_id: str, config: dict) -> None:
    """Run the complete destructive lifecycle under the shared write gate."""
    try:
        from utils.index_lock import (
            DistributedLockLeaseLostError,
            index_write_lock,
            lifecycle_write_lock,
        )

        with lifecycle_write_lock(scope=config["CHROMA_COLLECTION"]):
            with index_write_lock():
                _run_delete_knowledge_base_job(job_id, config)
    except DistributedLockLeaseLostError:
        log.critical(
            "Eliminazione knowledge base %s interrotta dopo la perdita della "
            "lease; nessuna ulteriore scrittura live eseguita. Rilanciare "
            "l'eliminazione per il recovery.",
            config["KNOWLEDGE_BASE_ID"],
        )
    except Exception as exc:
        job_store = get_job_store()
        knowledge_base_id = config["KNOWLEDGE_BASE_ID"]
        job_store.append_error(
            job_id,
            knowledge_base_id,
            f"{type(exc).__name__}: eliminazione non completata",
        )
        job_store.finish(
            job_id,
            "failed",
            "Eliminazione knowledge base non completata",
        )


def _run_delete_knowledge_base_job(job_id: str, config: dict) -> None:
    from utils.index_lock import (
        DistributedLockLeaseLostError,
        assert_distributed_locks_healthy,
    )

    job_store = get_job_store()
    knowledge_base_id = config["KNOWLEDGE_BASE_ID"]
    workspace_id = config["WORKSPACE_ID"]
    result = {
        "collections_deleted": 0,
        "chunks_deleted": 0,
        "files_deleted": 0,
        "data_sources_deleted": 0,
        "secrets_deleted": 0,
        "api_keys_updated": 0,
        "api_keys_disabled": 0,
    }

    def update_job(**patch) -> None:
        assert_distributed_locks_healthy()
        job_store.update(job_id, **patch)
        assert_distributed_locks_healthy()

    def append_job_error(message: str) -> None:
        assert_distributed_locks_healthy()
        job_store.append_error(job_id, knowledge_base_id, message)
        assert_distributed_locks_healthy()

    def finish_job(status: str, message: str) -> None:
        assert_distributed_locks_healthy()
        job_store.finish(job_id, status, message)
        assert_distributed_locks_healthy()

    try:
        assert_distributed_locks_healthy()
        job = job_store.get(job_id)
        if not job or job.get("status") not in RUNNING_JOB_STATUSES:
            return
        if (
            job_store.knowledge_base_deletion_job_id(
                workspace_id,
                knowledge_base_id,
            )
            != job_id
        ):
            return
        update_job(
            status="running",
            message="Eliminazione knowledge base avviata",
        )

        workspace = workspace_from_config(config)
        store = knowledge_base_store(workspace)
        record = store.get(knowledge_base_id)
        processed = max(0, int(job.get("processed") or 0))
        if record is None and processed < 5:
            raise RuntimeError(
                "Knowledge base assente prima del cleanup finale"
            )
        if record is not None:
            assert_distributed_locks_healthy()
            record = store.begin_delete(
                knowledge_base_id,
                requester_api_key_id=str(
                    config.get("DELETE_REQUESTER_API_KEY_ID") or ""
                ),
            )
            assert_distributed_locks_healthy()
        stored_result = job.get("result")
        if isinstance(stored_result, dict):
            result.update(stored_result)
        from utils.secret_store import SecretStore

        secret_store = SecretStore(
            config.get("SECRETS_FILE"),
            key=config.get("SECRET_KEY"),
        )
        secret_owner = f"{workspace_id}:{knowledge_base_id}"

        if record is None:
            # Compatibility with jobs created before catalog removal became
            # the last checkpoint: resume only idempotent final cleanup.
            assert_distributed_locks_healthy()
            result["secrets_deleted"] += secret_store.delete_owner(secret_owner)
            assert_distributed_locks_healthy()
            checkpoint = {"result": result}
            if processed < 5:
                checkpoint["processed"] = 5
            update_job(**checkpoint)
        else:
            entries = _file_entries(config["FILE_INDEX"])
            result["files_deleted"] = len(entries)
            result["chunks_deleted"] = sum(
                max(0, int(entry.get("chunks") or 0)) for entry in entries
            )

            settings_store = SettingsStore(config["SETTINGS_FILE"])
            removed_sources = []
            removed_secret_refs: set[str] = set()

            def remove_data_sources(settings: dict) -> None:
                kept_sources = []
                for source in settings.get("data_sources", []):
                    source_kb = (
                        source.get("knowledge_base_id")
                        or DEFAULT_KNOWLEDGE_BASE_ID
                    )
                    if source_kb == knowledge_base_id:
                        removed_sources.append(source)
                        for descriptor in (source.get("secrets") or {}).values():
                            if (
                                isinstance(descriptor, dict)
                                and descriptor.get("ref")
                            ):
                                removed_secret_refs.add(str(descriptor["ref"]))
                    else:
                        kept_sources.append(source)
                settings["data_sources"] = kept_sources

            assert_distributed_locks_healthy()
            settings_store.mutate(remove_data_sources)
            assert_distributed_locks_healthy()
            result["data_sources_deleted"] = len(removed_sources)
            for ref in removed_secret_refs:
                assert_distributed_locks_healthy()
                result["secrets_deleted"] += int(
                    secret_store.delete_secret(ref)
                )
                assert_distributed_locks_healthy()
            assert_distributed_locks_healthy()
            result["secrets_deleted"] += secret_store.delete_owner(secret_owner)
            assert_distributed_locks_healthy()
            update_job(
                processed=max(processed, 2),
                result=result,
            )

            assert_distributed_locks_healthy()
            result["collections_deleted"] = int(
                _delete_chroma_collection(config["CHROMA_COLLECTION"])
            )
            assert_distributed_locks_healthy()
            update_job(processed=max(processed, 3))

            data_folder = Path(config["FILE_INDEX"]).parent
            upload_folder = Path(config["UPLOAD_FOLDER"])
            assert_distributed_locks_healthy()
            if data_folder.exists():
                shutil.rmtree(data_folder)
                assert_distributed_locks_healthy()
            assert_distributed_locks_healthy()
            if upload_folder.exists():
                shutil.rmtree(upload_folder)
                assert_distributed_locks_healthy()
            update_job(processed=max(processed, 4))

            from utils.rag_engine import clear_cache_for_collection

            assert_distributed_locks_healthy()
            clear_cache_for_collection(config["CHROMA_COLLECTION"])
            assert_distributed_locks_healthy()
            from utils.conversation_memory import get_conversation_store

            assert_distributed_locks_healthy()
            get_conversation_store().clear_by_knowledge_base(
                workspace_id,
                knowledge_base_id,
            )
            assert_distributed_locks_healthy()
            job_store.clear_by_knowledge_base(
                workspace_id,
                knowledge_base_id,
                exclude_job_id=job_id,
            )
            assert_distributed_locks_healthy()
            update_job(processed=max(processed, 5))

        from utils.user_store import UserStore

        user_store = UserStore(config["USERS_FILE"])

        def remove_catalog() -> None:
            assert_distributed_locks_healthy()
            if record is not None and not store.remove(knowledge_base_id):
                if store.get(knowledge_base_id) is not None:
                    raise RuntimeError(
                        "Knowledge base non trovata durante il cleanup finale"
                    )
            assert_distributed_locks_healthy()

        assert_distributed_locks_healthy()
        key_result = user_store.remove_knowledge_base_from_api_keys(
            user_id=config["USER_ID"],
            knowledge_base_id=knowledge_base_id,
            finalize=remove_catalog,
            lease_check=assert_distributed_locks_healthy,
        )
        assert_distributed_locks_healthy()
        result["api_keys_updated"] = key_result["updated"]
        result["api_keys_disabled"] = key_result["disabled"]
        update_job(
            processed=max(processed, 6),
            result=result,
        )
        update_job(
            processed=max(processed, 7),
            result=result,
        )
        finish_job("completed", "Knowledge base eliminata")
    except DistributedLockLeaseLostError:
        log.critical(
            "Lease persa durante l'eliminazione della knowledge base %s; "
            "rollback e aggiornamenti di stato interrotti.",
            knowledge_base_id,
        )
        raise
    except Exception as exc:
        try:
            workspace = workspace_from_config(config)
            assert_distributed_locks_healthy()
            _mark_delete_failed(
                knowledge_base_store(workspace),
                knowledge_base_id,
                f"{type(exc).__name__}: eliminazione non completata",
            )
            assert_distributed_locks_healthy()
        except DistributedLockLeaseLostError:
            raise
        except Exception:
            pass
        append_job_error(
            f"{type(exc).__name__}: eliminazione non completata",
        )
        update_job(result=result)
        finish_job("failed", "Eliminazione knowledge base non completata")


def _mark_delete_failed(store, knowledge_base_id: str, message: str) -> None:
    if store.get(knowledge_base_id) is None:
        return
    store.set_status(
        knowledge_base_id,
        "delete_failed",
        delete_error=message,
    )


def workspace_from_config(config: dict):
    from utils.workspace import WorkspaceContext

    data_folder = str(Path(config["SETTINGS_FILE"]).parent)
    return WorkspaceContext(
        user_id=config["USER_ID"],
        workspace_id=config["WORKSPACE_ID"],
        settings_file=config["SETTINGS_FILE"],
        data_folder=data_folder,
        workspace_upload_folder=config["WORKSPACE_UPLOAD_FOLDER"],
        knowledge_bases_file=str(Path(data_folder) / "knowledge_bases.json"),
        secrets_file=config["SECRETS_FILE"],
        secret_key=config["SECRET_KEY"],
    )


def _with_stats(workspace, record: dict) -> dict:
    context = knowledge_base_context(
        workspace,
        record["id"],
        allow_inactive=True,
    )
    entries = _file_entries(context.file_index)
    settings = SettingsStore(workspace.settings_file).load()
    data_sources = sum(
        1
        for source in settings.get("data_sources", [])
        if (source.get("knowledge_base_id") or DEFAULT_KNOWLEDGE_BASE_ID)
        == record["id"]
    )
    return {
        **record,
        "stats": {
            "tracked_files": len(entries),
            "indexed_files": sum(
                entry.get("status") == "indexed" for entry in entries
            ),
            "chunks": sum(max(0, int(entry.get("chunks") or 0)) for entry in entries),
            "data_sources": data_sources,
        },
    }


def _file_entries(path: str) -> list[dict]:
    if not os.path.isfile(path):
        return []
    from utils.file_index import FileIndex

    return FileIndex(path).list()


def _ensure_api_key_allowed(knowledge_base_id: str) -> None:
    allowed = _api_key_allowed_ids()
    if knowledge_base_id not in allowed:
        raise _not_found()


def _add_created_knowledge_base_to_api_key(
    app,
    knowledge_base_id: str,
    *,
    validate=None,
) -> None:
    key = getattr(request, "api_key", None) or {}
    key_name = key.get("_user_key_name")
    if not key_name:
        return
    from utils.user_store import UserStore

    updated = UserStore(app.config["USERS_FILE"]).add_knowledge_base_to_api_key(
        user_id=key["user_id"],
        key_name=key_name,
        knowledge_base_id=knowledge_base_id,
        key_id=str(key.get("api_key_id") or ""),
        required_scope="kb_manage",
        validate=validate,
    )
    if updated is None:
        raise RuntimeError("API key richiedente non più disponibile")


def _ensure_created_knowledge_base_active(store, knowledge_base_id: str) -> None:
    record = store.get(knowledge_base_id)
    if record is None or record.get("status") != "active":
        raise RuntimeError("Knowledge base creata non più disponibile")


def _rollback_created_knowledge_base_if_active(
    store,
    knowledge_base_id: str,
) -> None:
    record = store.get(knowledge_base_id)
    if record is None or record.get("status") != "active":
        return
    if not store.remove(knowledge_base_id):
        current = store.get(knowledge_base_id)
        if current is not None and current.get("status") == "active":
            raise RuntimeError("Rollback knowledge base non completato")


def _api_key_allowed_ids() -> set[str]:
    key = getattr(request, "api_key", None) or {}
    values = (
        key.get("knowledge_base_ids")
        if "knowledge_base_ids" in key
        else [DEFAULT_KNOWLEDGE_BASE_ID]
    )
    return set(values or [])


def _json_object() -> dict:
    if not request.is_json:
        raise KnowledgeBaseValidationError(
            "Content-Type deve essere application/json"
        )
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise KnowledgeBaseValidationError("Body JSON non valido")
    return data


def _reject_unknown_fields(data: dict, allowed: set[str]) -> None:
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise KnowledgeBaseValidationError(
            f"Campi non consentiti: {', '.join(unknown)}"
        )


def _not_found() -> KnowledgeBaseValidationError:
    return KnowledgeBaseValidationError(
        "Knowledge base non trovata",
        code="knowledge_base_not_found",
        status_code=404,
    )
