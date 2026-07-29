import importlib
import concurrent.futures
import json
from email.message import EmailMessage
from pathlib import Path
from datetime import datetime, timezone

import pytest

from app import create_app
from app.utils.file_index import FileIndex
from app.utils.settings_store import SettingsStore


@pytest.fixture
def flask_app(tmp_path, monkeypatch):
    monkeypatch.setenv("RAG_ADMIN_PASSWORD_HASH", "")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", "")
    monkeypatch.setenv("RAG_ADMIN_PASSWORD", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD", "")
    app = create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "test-secret",
            "SETTINGS_FILE": str(tmp_path / "settings.json"),
            "FILE_INDEX": str(tmp_path / "files.json"),
            "UPLOAD_FOLDER": str(tmp_path / "uploads"),
            "USERS_FILE": str(tmp_path / "users.json"),
            "SECRETS_FILE": str(tmp_path / "secrets.json"),
            "WORKSPACE_DATA_DIR": str(tmp_path / "workspaces"),
            "WORKSPACE_UPLOAD_DIR": str(tmp_path / "workspace_uploads"),
            "MAX_UPLOAD_SIZE_MB": 5,
            "RATE_LIMIT_REQUESTS": 1000,
            "RATE_LIMIT_WINDOW": 60,
        }
    )
    return app


@pytest.fixture
def client(flask_app):
    return flask_app.test_client()


def _workspace_settings(flask_app):
    from utils.user_store import UserStore
    from utils.workspace import workspace_for_user

    user = UserStore(flask_app.config["USERS_FILE"]).list()[0]
    return SettingsStore(workspace_for_user(user, app=flask_app).settings_file)


def test_ingestion_registry_exposes_only_allowlisted_plugins():
    from utils.data_ingestion.registry import available_plugins, get_ingester
    from utils.validators import ValidationError

    plugins = available_plugins()

    assert plugins == [
        {"id": "email_imap", "display_name": "Email IMAP"},
        {"id": "folder_watch", "display_name": "Cartella Locale"},
        {"id": "microsoft_drive", "display_name": "Microsoft Drive"},
    ]
    assert get_ingester("email_imap").display_name == "Email IMAP"
    assert get_ingester("microsoft_drive").display_name == "Microsoft Drive"
    assert get_ingester("folder_watch").display_name == "Cartella Locale"
    with pytest.raises(ValidationError):
        get_ingester("not-installed")


def test_data_source_normalizes_periodic_sync_fields():
    from utils.settings_store import normalize_data_source

    source = normalize_data_source(
        {
            "id": "Legal Mailbox",
            "name": "Legal Mailbox",
            "plugin": "email_imap",
            "sync_enabled": "on",
            "sync_interval_minutes": "30",
            "next_sync_at": "2026-06-24T10:00:00+00:00",
            "last_sync_status": "queued",
        }
    )

    assert source["id"] == "legal-mailbox"
    assert source["sync_enabled"] is True
    assert source["sync_interval_seconds"] == 1800
    assert source["next_sync_at"] == "2026-06-24T10:00:00+00:00"
    assert source["last_sync_status"] == "queued"


def test_poller_finds_due_data_sources():
    from utils.data_ingestion.poller import due_data_sources

    now = datetime(2026, 6, 24, 12, 0, tzinfo=timezone.utc)
    sources = [
        {
            "id": "due-now",
            "enabled": True,
            "sync_enabled": True,
            "sync_interval_seconds": 900,
            "next_sync_at": "2026-06-24T11:59:00+00:00",
        },
        {
            "id": "future",
            "enabled": True,
            "sync_enabled": True,
            "sync_interval_seconds": 900,
            "next_sync_at": "2026-06-24T12:10:00+00:00",
        },
        {
            "id": "manual",
            "enabled": True,
            "sync_enabled": False,
            "sync_interval_seconds": 900,
        },
    ]

    assert [source["id"] for source in due_data_sources(sources, now=now)] == ["due-now"]


def test_poller_trigger_marks_source_queued_without_import_error(monkeypatch, tmp_path):
    from utils.data_ingestion import jobs

    settings_file = tmp_path / "settings.json"
    store = SettingsStore(str(settings_file))
    store.save(
        {
            "data_sources": [
                {
                    "id": "legal-mailbox",
                    "name": "Legal Mailbox",
                    "plugin": "email_imap",
                    "enabled": True,
                    "sync_enabled": True,
                    "sync_interval_seconds": 900,
                    "next_sync_at": "2026-06-24T11:59:00+00:00",
                    "last_sync_status": "completed",
                }
            ]
        }
    )
    config = {
        "SETTINGS_FILE": str(settings_file),
        "FILE_INDEX": str(tmp_path / "files.json"),
        "UPLOAD_FOLDER": str(tmp_path / "uploads"),
        "SECRETS_FILE": str(tmp_path / "secrets.json"),
        "SECRET_KEY": "test-secret",
    }
    ran = []

    def fake_run(job_id, job_config, data_source_id):
        ran.append((job_id, job_config, data_source_id))

    class ImmediateThread:
        def __init__(self, *, target, args, daemon):
            self.target = target
            self.args = args
            self.daemon = daemon

        def start(self):
            self.target(*self.args)

    monkeypatch.setattr(jobs, "run_data_source_sync_job", fake_run)
    monkeypatch.setattr(jobs.threading, "Thread", ImmediateThread)

    payload, status_code = jobs.start_data_source_sync_job(config, "legal-mailbox", trigger="poller")

    assert status_code == 202
    assert payload["job_id"]
    assert ran and ran[0][2] == "legal-mailbox"
    source = store.load()["data_sources"][0]
    assert source["last_sync_status"] == "queued"
    assert source["next_sync_at"]
    assert source["next_sync_at"] != "2026-06-24T11:59:00+00:00"


def test_ingestion_storage_materializes_inside_upload_external(tmp_path):
    from utils.data_ingestion.base import IngestionItem
    from utils.data_ingestion.storage import IngestionStorage, stable_filename

    upload_folder = tmp_path / "uploads"
    item = IngestionItem(
        content="Hello from email",
        filename="Quarterly Contract.md",
        extension="md",
        remote_id="<message-123>",
    )

    stored = IngestionStorage(str(upload_folder)).materialize_item("Legal Mailbox", item)

    path = Path(stored["file_path"])
    assert path.read_text(encoding="utf-8") == "Hello from email"
    assert path.is_relative_to(upload_folder.resolve())
    assert "/external/legal-mailbox/" in str(path)
    assert stored["filename"] == stable_filename("Legal Mailbox", "<message-123>", "Quarterly Contract.md", "md")


def test_ingestion_storage_materializes_binary_drive_file(tmp_path):
    from utils.data_ingestion.base import IngestionItem
    from utils.data_ingestion.storage import IngestionStorage

    stored = IngestionStorage(str(tmp_path / "uploads")).materialize_item(
        "contracts-drive",
        IngestionItem(
            content="",
            content_bytes=b"%PDF-1.4",
            filename="Contract.pdf",
            extension="pdf",
            remote_id="drive-item-1",
        ),
    )

    assert Path(stored["file_path"]).read_bytes() == b"%PDF-1.4"
    assert stored["extension"] == "pdf"


def test_ingestion_storage_stages_items_and_attachments_without_overwrite(
    tmp_path,
):
    from utils.data_ingestion.base import IngestionAttachment, IngestionItem
    from utils.data_ingestion.storage import IngestionStorage

    storage = IngestionStorage(str(tmp_path / "uploads"))
    original_item = IngestionItem(
        content="old item",
        filename="Contract.md",
        extension="md",
        remote_id="item-1",
    )
    replacement_item = IngestionItem(
        content="new item",
        filename="Contract.md",
        extension="md",
        remote_id="item-1",
    )
    stored_item = storage.materialize_item("source", original_item)
    staged_item = storage.materialize_item(
        "source",
        replacement_item,
        staged=True,
    )

    assert Path(stored_item["file_path"]).read_text(encoding="utf-8") == "old item"
    assert Path(staged_item["staged_file_path"]).read_text(
        encoding="utf-8"
    ) == "new item"

    parent = IngestionItem(
        content="parent",
        filename="Parent.md",
        extension="md",
        remote_id="parent-1",
    )
    original_attachment = IngestionAttachment(
        filename="Appendix.txt",
        extension="txt",
        content=b"old attachment",
        remote_id="attachment-1",
    )
    replacement_attachment = IngestionAttachment(
        filename="Appendix.txt",
        extension="txt",
        content=b"new attachment",
        remote_id="attachment-1",
    )
    stored_attachment = storage.materialize_attachment(
        "source",
        parent,
        original_attachment,
    )
    staged_attachment = storage.materialize_attachment(
        "source",
        parent,
        replacement_attachment,
        staged=True,
    )

    assert Path(stored_attachment["file_path"]).read_bytes() == b"old attachment"
    assert Path(staged_attachment["staged_file_path"]).read_bytes() == (
        b"new attachment"
    )


def test_staged_document_indexing_restores_file_vectors_and_file_index(
    tmp_path,
    monkeypatch,
):
    import utils.chroma_manager as chroma_manager
    import utils.document_indexer as document_indexer
    import utils.rag_engine as rag_engine

    upload_folder = tmp_path / "uploads"
    upload_folder.mkdir()
    final_path = upload_folder / "external.txt"
    staged_path = upload_folder / ".external.txt.staged"
    final_path.write_text("old content", encoding="utf-8")
    staged_path.write_text("new content", encoding="utf-8")
    file_index = FileIndex(str(tmp_path / "files.json"))
    file_index.record(
        "external.txt",
        str(final_path),
        2,
        status="indexed",
        metadata={"remote_id": "remote-1"},
    )
    previous_entry = file_index.get("external.txt")
    vector_snapshot = {
        "ids": ["old-1", "old-2"],
        "documents": ["old one", "old two"],
        "metadatas": [
            {"source": str(final_path)},
            {"source": str(final_path)},
        ],
        "embeddings": [[1.0], [2.0]],
    }
    restored = []

    monkeypatch.setattr(
        chroma_manager,
        "snapshot_documents_by_source",
        lambda source, collection_name=None: vector_snapshot,
    )
    monkeypatch.setattr(
        chroma_manager,
        "restore_documents_by_source",
        lambda source, snapshot, collection_name=None: restored.append(
            (source, snapshot, collection_name)
        ),
    )
    monkeypatch.setattr(
        rag_engine,
        "clear_cache_for_collection",
        lambda _collection: None,
    )

    def fail_index(config, filename, file_path, extension, **_kwargs):
        assert Path(file_path).read_text(encoding="utf-8") == "new content"
        FileIndex(config["FILE_INDEX"]).record(
            filename,
            file_path,
            1,
            status="indexed",
            metadata={"remote_id": "remote-1", "version": "new"},
        )
        raise RuntimeError("embedding unavailable")

    monkeypatch.setattr(
        document_indexer,
        "index_saved_document",
        fail_index,
    )
    config = {
        "UPLOAD_FOLDER": str(upload_folder),
        "FILE_INDEX": str(tmp_path / "files.json"),
        "CHROMA_COLLECTION": "documents-test",
    }

    with pytest.raises(RuntimeError, match="embedding unavailable"):
        document_indexer.index_staged_document(
            config,
            "external.txt",
            str(final_path),
            str(staged_path),
            "txt",
        )

    assert final_path.read_text(encoding="utf-8") == "old content"
    assert not staged_path.exists()
    assert file_index.get("external.txt") == previous_entry
    assert restored == [
        (str(final_path), vector_snapshot, "documents-test")
    ]
    assert not list(upload_folder.glob("*.rollback-ingestion"))


def test_staged_document_indexing_does_not_rollback_after_lease_loss(
    tmp_path,
    monkeypatch,
):
    import utils.chroma_manager as chroma_manager
    import utils.document_indexer as document_indexer
    import utils.index_lock as locks

    upload_folder = tmp_path / "uploads"
    upload_folder.mkdir()
    final_path = upload_folder / "external.txt"
    staged_path = upload_folder / ".external.txt.staged"
    final_path.write_text("old content", encoding="utf-8")
    staged_path.write_text("new content", encoding="utf-8")
    file_index = FileIndex(str(tmp_path / "files.json"))
    file_index.record(
        "external.txt",
        str(final_path),
        2,
        status="indexed",
        metadata={"version": "old"},
    )
    restored = []

    monkeypatch.setattr(
        chroma_manager,
        "snapshot_documents_by_source",
        lambda source, collection_name=None: {"ids": ["old"]},
    )
    monkeypatch.setattr(
        chroma_manager,
        "restore_documents_by_source",
        lambda *args, **kwargs: restored.append((args, kwargs)),
    )

    def lose_lease_after_concurrent_write(
        config,
        filename,
        file_path,
        extension,
        **_kwargs,
    ):
        Path(file_path).write_text("concurrent winner", encoding="utf-8")
        FileIndex(config["FILE_INDEX"]).record(
            filename,
            file_path,
            1,
            status="indexed",
            metadata={"version": "concurrent"},
        )
        raise locks.DistributedLockLeaseLostError("simulated lease loss")

    monkeypatch.setattr(
        document_indexer,
        "index_saved_document",
        lose_lease_after_concurrent_write,
    )
    config = {
        "UPLOAD_FOLDER": str(upload_folder),
        "FILE_INDEX": str(tmp_path / "files.json"),
        "CHROMA_COLLECTION": "documents-test",
    }

    with pytest.raises(
        locks.DistributedLockLeaseLostError,
        match="simulated lease loss",
    ):
        document_indexer.index_staged_document(
            config,
            "external.txt",
            str(final_path),
            str(staged_path),
            "txt",
        )

    assert final_path.read_text(encoding="utf-8") == "concurrent winner"
    assert file_index.get("external.txt")["version"] == "concurrent"
    assert restored == []
    backups = list(upload_folder.glob(".*.rollback-ingestion"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "old content"
    assert not staged_path.exists()


def test_staged_document_indexing_commits_and_removes_temporary_files(
    tmp_path,
    monkeypatch,
):
    import utils.chroma_manager as chroma_manager
    import utils.document_indexer as document_indexer

    upload_folder = tmp_path / "uploads"
    upload_folder.mkdir()
    final_path = upload_folder / "external.txt"
    staged_path = upload_folder / ".external.txt.staged"
    final_path.write_text("old content", encoding="utf-8")
    staged_path.write_text("new content", encoding="utf-8")
    file_index = FileIndex(str(tmp_path / "files.json"))
    file_index.record(
        "external.txt",
        str(final_path),
        2,
        status="indexed",
    )
    restored = []
    monkeypatch.setattr(
        chroma_manager,
        "snapshot_documents_by_source",
        lambda source, collection_name=None: {"ids": ["old"]},
    )
    monkeypatch.setattr(
        chroma_manager,
        "restore_documents_by_source",
        lambda *args, **kwargs: restored.append((args, kwargs)),
    )

    def complete_index(config, filename, file_path, extension, **_kwargs):
        FileIndex(config["FILE_INDEX"]).record(
            filename,
            file_path,
            1,
            status="indexed",
            metadata={"version": "new"},
        )
        return {"status": "indexed", "chunks": 1}

    monkeypatch.setattr(
        document_indexer,
        "index_saved_document",
        complete_index,
    )
    config = {
        "UPLOAD_FOLDER": str(upload_folder),
        "FILE_INDEX": str(tmp_path / "files.json"),
        "CHROMA_COLLECTION": "documents-test",
    }

    result = document_indexer.index_staged_document(
        config,
        "external.txt",
        str(final_path),
        str(staged_path),
        "txt",
    )

    assert result == {"status": "indexed", "chunks": 1}
    assert final_path.read_text(encoding="utf-8") == "new content"
    assert file_index.get("external.txt")["version"] == "new"
    assert restored == []
    assert not staged_path.exists()
    assert not list(upload_folder.glob("*.rollback-ingestion"))


def test_email_parser_extracts_html_body_and_supported_attachments():
    from utils.data_ingestion.email_ingester import parse_email_message

    message = EmailMessage()
    message["Subject"] = "Contract Update"
    message["From"] = "Legal <legal@example.com>"
    message["To"] = "ops@example.com"
    message["Message-ID"] = "<msg-1@example.com>"
    message["Date"] = "Tue, 23 Jun 2026 10:00:00 +0000"
    message.set_content("Plain body")
    message.add_alternative("<html><body><p>HTML body</p></body></html>", subtype="html")
    message.add_attachment(b"Attachment text", maintype="text", subtype="plain", filename="notes.txt")
    message.add_attachment(b"ignored", maintype="image", subtype="png", filename="image.png")

    item = parse_email_message(message.as_bytes(), "42", {"include_body": True, "include_attachments": True})

    assert item is not None
    assert item.remote_id == "42"
    assert item.metadata["subject"] == "Contract Update"
    assert item.metadata["sender"] == "Legal <legal@example.com>"
    assert "Plain body" in item.content
    assert len(item.attachments) == 1
    assert item.attachments[0].filename == "notes.txt"
    assert item.attachments[0].metadata["source_type"] == "email_attachment"


def test_microsoft_drive_ingester_lists_and_downloads_supported_files(monkeypatch):
    import utils.data_ingestion.drive_ingester as drive_ingester

    class FakeGraphClient:
        def __init__(self, base_url, token):
            self.base_url = base_url
            self.token = token

        def walk_children(self, *, drive_id, folder_path, item_id, recursive):
            assert drive_id == "drive-1"
            assert folder_path == "Contracts"
            assert item_id == ""
            assert recursive is True
            return iter(
                [
                    {
                        "id": "item-1",
                        "name": "Contract.pdf",
                        "size": 8,
                        "eTag": "etag-1",
                        "lastModifiedDateTime": "2026-06-23T10:00:00Z",
                        "webUrl": "https://example.com/contract",
                        "file": {"mimeType": "application/pdf"},
                        "parentReference": {"driveId": "drive-1", "id": "parent-1"},
                    },
                    {
                        "id": "item-2",
                        "name": "Image.png",
                        "size": 8,
                        "eTag": "etag-2",
                        "file": {"mimeType": "image/png"},
                    },
                    {
                        "id": "item-3",
                        "name": "Already.md",
                        "size": 8,
                        "eTag": "same",
                        "file": {"mimeType": "text/markdown"},
                    },
                ]
            )

        def download_item(self, drive_id, item_id):
            assert item_id == "item-1"
            return b"%PDF-1.4"

    monkeypatch.setenv("RAG_SOURCE_MS_GRAPH_TOKEN", "token")
    monkeypatch.setattr(drive_ingester, "_GraphClient", FakeGraphClient)

    result = drive_ingester.MicrosoftDriveIngester().sync(
        None,
        {
            "token_env": "RAG_SOURCE_MS_GRAPH_TOKEN",
            "drive_id": "drive-1",
            "folder_path": "Contracts",
            "recursive": True,
        },
        {"items": {"item-3": "same"}},
    )

    assert len(result.items) == 1
    item = result.items[0]
    assert item.filename == "Contract.pdf"
    assert item.content_bytes == b"%PDF-1.4"
    assert item.metadata["source_type"] == "microsoft_drive"
    assert item.metadata["drive_id"] == "drive-1"
    assert item.metadata["mime_type"] == "application/pdf"
    assert result.cursor["items"]["item-1"] == "etag-1"
    assert result.cursor["items"]["item-3"] == "same"


def test_sync_data_source_materializes_and_indexes_with_metadata(tmp_path, monkeypatch):
    from utils.data_ingestion.base import IngestionItem, SyncResult
    import utils.data_ingestion.service as service

    settings_file = tmp_path / "settings.json"
    file_index = tmp_path / "files.json"
    upload_folder = tmp_path / "uploads"
    SettingsStore(str(settings_file)).update(
        {
            "data_sources": [
                {
                    "id": "legal-mailbox",
                    "name": "Legal Mailbox",
                    "plugin": "fake",
                    "enabled": True,
                    "config": {"host": "example"},
                    "secrets_env": {"password_env": "RAG_SOURCE_PASSWORD"},
                }
            ]
        }
    )

    class FakeIngester:
        plugin_id = "fake"
        display_name = "Fake"

        def validate_config(self, config):
            return config

        def sync(self, context, source_config, cursor=None):
            return SyncResult(
                items=[
                    IngestionItem(
                        content="External text",
                        filename="Contract.md",
                        extension="md",
                        remote_id="remote-1",
                        updated_at="2026-06-23T10:00:00+00:00",
                        metadata={"subject": "Contract", "sender": "legal@example.com"},
                    )
                ],
                cursor={"last_uid": 99},
            )

    indexed = []

    def fake_index(
        config,
        filename,
        file_path,
        staged_file_path,
        extension,
        **kwargs,
    ):
        Path(staged_file_path).replace(file_path)
        indexed.append(
            {
                "filename": filename,
                "file_path": file_path,
                "extension": extension,
                "metadata": kwargs.get("extra_metadata", {}),
            }
        )
        FileIndex(config["FILE_INDEX"]).record(
            filename,
            file_path,
            1,
            metadata=kwargs.get("extra_metadata", {}),
        )
        return {"status": "indexed", "chunks": 1}

    monkeypatch.setattr(service, "get_ingester", lambda plugin_id: FakeIngester())
    monkeypatch.setattr(service, "index_staged_document", fake_index)

    result = service.sync_data_source(
        {
            "SETTINGS_FILE": str(settings_file),
            "FILE_INDEX": str(file_index),
            "UPLOAD_FOLDER": str(upload_folder),
            "SECRETS_FILE": str(tmp_path / "secrets.json"),
            "SECRET_KEY": "test-secret",
        },
        "legal-mailbox",
    )

    assert result["status"] == "completed"
    assert result["indexed"] == 1
    assert indexed[0]["extension"] == "md"
    assert Path(indexed[0]["file_path"]).read_text(encoding="utf-8") == "External text"
    assert indexed[0]["metadata"]["data_source_id"] == "legal-mailbox"
    assert indexed[0]["metadata"]["ingestion_plugin"] == "fake"
    assert indexed[0]["metadata"]["remote_id"] == "remote-1"
    updated = SettingsStore(str(settings_file)).load()["data_sources"][0]
    assert updated["cursor"] == {"last_uid": 99}
    assert updated["last_error"] == ""


def test_concurrent_data_source_state_updates_preserve_other_knowledge_bases(
    tmp_path,
):
    import utils.data_ingestion.service as service

    store = SettingsStore(str(tmp_path / "settings.json"))
    secondary_id = "kb_11111111111111111111111111111111"
    store.update(
        {
            "data_sources": [
                {
                    "id": "default-mail",
                    "name": "Default Mail",
                    "plugin": "email_imap",
                    "knowledge_base_id": "default",
                },
                {
                    "id": "legal-mail",
                    "name": "Legal Mail",
                    "plugin": "email_imap",
                    "knowledge_base_id": secondary_id,
                },
            ]
        }
    )

    def update_source(arguments):
        source_id, knowledge_base_id, cursor = arguments
        for _ in range(12):
            service._update_data_source_state(
                store,
                source_id,
                knowledge_base_id=knowledge_base_id,
                cursor=cursor,
                last_sync_status="completed",
            )

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        list(
            executor.map(
                update_source,
                [
                    ("default-mail", "default", {"uid": 10}),
                    ("legal-mail", secondary_id, {"uid": 20}),
                ],
            )
        )

    sources = {
        source["id"]: source
        for source in store.load()["data_sources"]
    }
    assert sources["default-mail"]["cursor"] == {"uid": 10}
    assert sources["legal-mail"]["cursor"] == {"uid": 20}


def test_data_source_sync_job_updates_job_store(monkeypatch, tmp_path):
    app_module = importlib.import_module("app.app")
    service = importlib.import_module("utils.data_ingestion.service")
    job_store = importlib.import_module("utils.job_store").get_job_store()
    job_store.create_job(
        {
            "id": "sync-job",
            "type": "data_source_sync",
            "status": "running",
            "message": "",
            "processed": 0,
            "total": 0,
            "current_file": "",
            "errors": [],
            "result": None,
            "started_at": 0,
            "finished_at": None,
        }
    )

    def fake_sync(config, data_source_id, progress_callback=None):
        progress_callback({"processed": 1, "total": 1, "current_file": "message.md"})
        return {
            "status": "completed",
            "data_source_id": data_source_id,
            "items": 1,
            "indexed": 1,
            "duplicates": 0,
            "errors": [],
        }

    monkeypatch.setattr(service, "sync_data_source", fake_sync)

    app_module._run_data_source_sync_job(
        "sync-job",
        {
            "SETTINGS_FILE": str(tmp_path / "settings.json"),
            "FILE_INDEX": str(tmp_path / "files.json"),
            "UPLOAD_FOLDER": str(tmp_path / "uploads"),
            "SECRETS_FILE": str(tmp_path / "secrets.json"),
            "SECRET_KEY": "test-secret",
        },
        "legal-mailbox",
    )

    job = job_store.get("sync-job")
    assert job["status"] == "completed"
    assert job["result"]["indexed"] == 1


def test_late_data_source_worker_stops_before_writing_an_inactive_kb(
    flask_app,
    monkeypatch,
):
    app_module = importlib.import_module("app.app")
    service = importlib.import_module("utils.data_ingestion.service")
    from utils.job_store import get_job_store
    from utils.user_store import UserStore
    from utils.workspace import (
        knowledge_base_context,
        knowledge_base_store,
        workspace_for_user,
    )

    user = UserStore(flask_app.config["USERS_FILE"]).create_user(
        email="sync@example.local",
        password="admin",
        display_name="Sync",
    )
    workspace = workspace_for_user(user, app=flask_app)
    secondary = knowledge_base_store(workspace, app=flask_app).create(
        name="Late sync",
    )
    config = knowledge_base_context(workspace, secondary["id"]).as_config()
    job_store = get_job_store()
    job_store.create_job(
        {
            "id": "late-sync",
            "type": "data_source_sync",
            "status": "queued",
            "workspace_id": workspace.workspace_id,
            "knowledge_base_id": secondary["id"],
            "errors": [],
        }
    )
    knowledge_base_store(workspace, app=flask_app).set_status(
        secondary["id"],
        "deleting",
    )
    writes = []
    monkeypatch.setattr(
        service,
        "mark_data_source_sync_running",
        lambda *_args, **_kwargs: writes.append("mark"),
    )
    monkeypatch.setattr(
        service,
        "sync_data_source",
        lambda *_args, **_kwargs: writes.append("sync"),
    )

    app_module._run_data_source_sync_job(
        "late-sync",
        config,
        "mail",
    )

    assert writes == []
    assert job_store.get("late-sync")["status"] == "failed"


def test_admin_can_save_email_data_source(client, flask_app):
    client.post("/admin/login", data={"password": "admin"})

    response = client.post(
        "/admin/data-sources",
        data={
            "id": "legal-mailbox",
            "name": "Legal Mailbox",
            "plugin": "email_imap",
            "enabled": "on",
            "host": "imap.example.com",
            "port": "993",
            "use_ssl": "on",
            "username": "legal@example.com",
            "password_env": "RAG_SOURCE_LEGAL_PASSWORD",
            "folder": "INBOX",
            "include_body": "on",
            "include_attachments": "on",
            "max_messages": "10",
        },
    )

    assert response.status_code == 302
    settings = _workspace_settings(flask_app).load()
    source = settings["data_sources"][0]
    assert source["id"] == "legal-mailbox"
    assert source["plugin"] == "email_imap"
    assert source["config"]["host"] == "imap.example.com"
    assert source["secrets_env"] == {"password_env": "RAG_SOURCE_LEGAL_PASSWORD"}


def test_plugin_change_deletes_all_previous_secret_refs(
    client,
    flask_app,
):
    from utils.secret_store import SecretStore
    from utils.user_store import UserStore
    from utils.workspace import workspace_for_user

    client.post("/admin/login", data={"password": "admin"})
    client.post(
        "/admin/data-sources",
        data={
            "id": "switch-source",
            "name": "Switch Source",
            "plugin": "email_imap",
            "enabled": "on",
            "host": "imap.example.com",
            "port": "993",
            "use_ssl": "on",
            "username": "legal@example.com",
            "password": "old-password",
            "folder": "INBOX",
            "include_body": "on",
            "include_attachments": "on",
            "max_messages": "10",
        },
    )
    settings_store = _workspace_settings(flask_app)
    previous = settings_store.load()["data_sources"][0]
    old_ref = previous["secrets"]["password"]["ref"]
    secret_store = SecretStore(
        flask_app.config["SECRETS_FILE"],
        key=flask_app.config["SECRET_KEY"],
    )
    assert secret_store.get_secret(old_ref) == "old-password"

    response = client.post(
        "/admin/data-sources",
        data={
            "id": "switch-source",
            "name": "Switch Source",
            "plugin": "microsoft_drive",
            "enabled": "on",
            "token": "new-token",
            "drive_id": "drive-1",
            "folder_path": "Contracts",
            "recursive": "on",
            "include_extensions": "pdf,txt,md",
            "max_files": "20",
            "max_file_size_mb": "15",
        },
    )

    source = settings_store.load()["data_sources"][0]
    new_ref = source["secrets"]["token"]["ref"]
    user = UserStore(flask_app.config["USERS_FILE"]).list()[0]
    workspace = workspace_for_user(user, app=flask_app)

    assert response.status_code == 302
    assert source["plugin"] == "microsoft_drive"
    assert new_ref.startswith(f"{workspace.workspace_id}:")
    assert secret_store.get_secret(new_ref) == "new-token"
    assert secret_store.get_secret(old_ref) == ""


def test_plugin_change_settings_failure_preserves_old_secret_only(
    client,
    flask_app,
    monkeypatch,
):
    from utils.secret_store import SecretStore

    client.post("/admin/login", data={"password": "admin"})
    client.post(
        "/admin/data-sources",
        data={
            "id": "switch-source",
            "name": "Switch Source",
            "plugin": "email_imap",
            "enabled": "on",
            "host": "imap.example.com",
            "port": "993",
            "use_ssl": "on",
            "username": "legal@example.com",
            "password": "old-password",
            "folder": "INBOX",
            "include_body": "on",
            "include_attachments": "on",
            "max_messages": "10",
        },
    )
    settings_store = _workspace_settings(flask_app)
    previous = settings_store.load()["data_sources"][0]
    old_ref = previous["secrets"]["password"]["ref"]
    secret_store = SecretStore(
        flask_app.config["SECRETS_FILE"],
        key=flask_app.config["SECRET_KEY"],
    )
    runtime_settings = importlib.import_module("utils.settings_store")
    original_write = runtime_settings.SettingsStore._write_unlocked

    def fail_workspace_write(store, settings):
        if Path(store.path) == Path(settings_store.path):
            raise OSError("simulated settings failure")
        return original_write(store, settings)

    monkeypatch.setattr(
        runtime_settings.SettingsStore,
        "_write_unlocked",
        fail_workspace_write,
    )

    response = client.post(
        "/admin/data-sources",
        data={
            "id": "switch-source",
            "name": "Switch Source",
            "plugin": "microsoft_drive",
            "enabled": "on",
            "token": "new-token",
            "drive_id": "drive-1",
            "folder_path": "Contracts",
            "recursive": "on",
            "include_extensions": "pdf,txt,md",
            "max_files": "20",
            "max_file_size_mb": "15",
        },
    )

    stored_source = settings_store.load()["data_sources"][0]
    persisted_secrets = json.loads(
        Path(flask_app.config["SECRETS_FILE"]).read_text(encoding="utf-8")
    )

    assert response.status_code == 302
    assert stored_source["plugin"] == "email_imap"
    assert stored_source["secrets"]["password"]["ref"] == old_ref
    assert secret_store.get_secret(old_ref) == "old-password"
    assert set(persisted_secrets) == {old_ref}


def test_admin_can_save_microsoft_drive_data_source(client, flask_app):
    client.post("/admin/login", data={"password": "admin"})

    response = client.post(
        "/admin/data-sources",
        data={
            "id": "contracts-drive",
            "name": "Contracts Drive",
            "plugin": "microsoft_drive",
            "enabled": "on",
            "token_env": "RAG_SOURCE_MS_GRAPH_TOKEN",
            "drive_id": "drive-1",
            "folder_path": "Shared Documents/Contracts",
            "item_id": "",
            "recursive": "on",
            "include_extensions": "pdf,txt,md",
            "max_files": "20",
            "max_file_size_mb": "15",
        },
    )

    assert response.status_code == 302
    settings = _workspace_settings(flask_app).load()
    source = settings["data_sources"][0]
    assert source["id"] == "contracts-drive"
    assert source["plugin"] == "microsoft_drive"
    assert source["config"]["drive_id"] == "drive-1"
    assert source["config"]["folder_path"] == "Shared Documents/Contracts"
    assert source["secrets_env"] == {"token_env": "RAG_SOURCE_MS_GRAPH_TOKEN"}


def _folder_sync_context(tmp_path):
    from utils.data_ingestion.base import SyncContext

    return SyncContext(
        upload_folder=str(tmp_path / "uploads"),
        settings_file=str(tmp_path / "settings.json"),
        file_index=str(tmp_path / "files.json"),
    )


def test_folder_ingester_validates_and_scans_files(tmp_path):
    from utils.data_ingestion.folder_ingester import FolderIngester

    test_folder = tmp_path / "test-docs"
    test_folder.mkdir()
    (test_folder / "doc1.pdf").write_bytes(b"%PDF-1.4")
    (test_folder / "doc2.txt").write_text("Hello text", encoding="utf-8")
    (test_folder / "data.csv").write_text("a,b\n1,2", encoding="utf-8")
    (test_folder / "ignore.tmp").write_text("tmp", encoding="utf-8")
    (test_folder / "ignore.docx").write_bytes(b"docx")

    ingester = FolderIngester()
    config = ingester.validate_config(
        {
            "folder_path": str(test_folder),
            "recursive": False,
            "include_extensions": "pdf,txt,csv,docx",
            "exclude_patterns": "*.tmp",
            "max_files": 10,
        }
    )

    assert config["folder_path"] == str(test_folder.resolve())
    assert config["include_extensions"] == {"pdf", "txt", "csv"}

    result = ingester.sync(_folder_sync_context(tmp_path), config)

    assert result.errors == []
    filenames = {item.filename for item in result.items}
    assert filenames == {"doc1.pdf", "doc2.txt", "data.csv"}

    pdf_item = next(item for item in result.items if item.filename == "doc1.pdf")
    txt_item = next(item for item in result.items if item.filename == "doc2.txt")
    csv_item = next(item for item in result.items if item.filename == "data.csv")
    assert pdf_item.content_bytes == b"%PDF-1.4"
    assert txt_item.content == "Hello text"
    assert csv_item.content == "a,b\n1,2"
    assert result.cursor.get("last_sync")


def test_folder_ingester_recursive_scan(tmp_path):
    from utils.data_ingestion.folder_ingester import FolderIngester

    test_folder = tmp_path / "nested"
    test_folder.mkdir()
    (test_folder / "root.txt").write_text("root", encoding="utf-8")
    subfolder = test_folder / "sub"
    subfolder.mkdir()
    (subfolder / "nested.pdf").write_bytes(b"%PDF-1.4")

    result = FolderIngester().sync(
        _folder_sync_context(tmp_path),
        {
            "folder_path": str(test_folder),
            "recursive": True,
            "include_extensions": "txt,pdf",
            "max_files": 50,
        },
    )

    assert result.errors == []
    assert {item.filename for item in result.items} == {"root.txt", "nested.pdf"}


def test_folder_ingester_rejects_invalid_config(tmp_path):
    from utils.data_ingestion.folder_ingester import FolderIngester
    from utils.validators import ValidationError

    ingester = FolderIngester()

    with pytest.raises(ValidationError):
        ingester.validate_config({"folder_path": ""})

    with pytest.raises(ValidationError):
        ingester.validate_config({"folder_path": "relative/path"})

    with pytest.raises(ValidationError):
        ingester.validate_config({"folder_path": str(tmp_path / "nonexistent")})

    file_path = tmp_path / "file.txt"
    file_path.write_text("test", encoding="utf-8")
    with pytest.raises(ValidationError):
        ingester.validate_config({"folder_path": str(file_path)})

    with pytest.raises(ValidationError):
        ingester.validate_config({"folder_path": str(tmp_path), "include_extensions": "docx"})


def test_admin_can_save_folder_watch_data_source(client, flask_app, tmp_path):
    folder = tmp_path / "local-documents"
    folder.mkdir()
    client.post("/admin/login", data={"password": "admin"})

    response = client.post(
        "/admin/data-sources",
        data={
            "id": "local-documents",
            "name": "Local Documents",
            "plugin": "folder_watch",
            "enabled": "on",
            "folder_path": str(folder),
            "include_extensions": "pdf,txt,md,csv",
            "exclude_patterns": "*.tmp,*~",
            "max_files": "100",
            "recursive": "on",
        },
    )

    assert response.status_code == 302
    settings = _workspace_settings(flask_app).load()
    source = settings["data_sources"][0]
    assert source["id"] == "local-documents"
    assert source["plugin"] == "folder_watch"
    assert source["config"]["folder_path"] == str(folder)
    assert source["config"]["recursive"] is True
    assert source["config"]["include_extensions"] == "pdf,txt,md,csv"
    assert source["config"]["exclude_patterns"] == "*.tmp,*~"
    assert source["secrets_env"] == {}


def test_admin_data_sources_disables_manual_sync_for_disabled_source(client, tmp_path):
    folder = tmp_path / "local-documents"
    folder.mkdir()
    client.post("/admin/login", data={"password": "admin"})
    client.post(
        "/admin/data-sources",
        data={
            "id": "local-documents",
            "name": "Local Documents",
            "plugin": "folder_watch",
            "folder_path": str(folder),
            "include_extensions": "pdf,txt,md,csv",
            "exclude_patterns": "",
            "max_files": "100",
        },
    )

    response = client.get("/admin/data-sources")

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "Sync disabled" in html
    assert 'data-sync-disabled="true"' in html
    assert 'disabled title="Enable this data source before syncing"' in html

    sync_response = client.post("/admin/data-sources/local-documents/sync")

    assert sync_response.status_code == 400
    assert sync_response.get_json()["error"] == "Data source disabilitata"


def test_admin_toggle_data_source_rejects_invalid_json(client):
    client.post("/admin/login", data={"password": "admin"})

    response = client.post(
        "/admin/data-sources/local-documents/toggle",
        data="not-json",
        content_type="application/json",
    )

    assert response.status_code == 400
    assert response.get_json()["field"] == "enabled"


def test_folder_ingester_materializes_binary_and_text_files(tmp_path):
    from utils.data_ingestion.folder_ingester import FolderIngester
    from utils.data_ingestion.storage import IngestionStorage

    folder = tmp_path / "test-folder"
    folder.mkdir()
    pdf_bytes = b"%PDF-1.4\nbinary-ish"
    (folder / "doc1.pdf").write_bytes(pdf_bytes)
    (folder / "doc2.txt").write_text("Hello world", encoding="utf-8")

    result = FolderIngester().sync(
        _folder_sync_context(tmp_path),
        {
            "folder_path": str(folder),
            "recursive": False,
            "include_extensions": "pdf,txt",
            "max_files": 10,
        },
    )

    storage = IngestionStorage(str(tmp_path / "uploads"))
    pdf_item = next(item for item in result.items if item.filename == "doc1.pdf")
    txt_item = next(item for item in result.items if item.filename == "doc2.txt")
    stored_pdf = storage.materialize_item("local-documents", pdf_item)
    stored_txt = storage.materialize_item("local-documents", txt_item)

    assert Path(stored_pdf["file_path"]).read_bytes() == pdf_bytes
    assert Path(stored_txt["file_path"]).read_text(encoding="utf-8") == "Hello world"
    assert txt_item.metadata["source_type"] == "folder"
    assert txt_item.metadata["ingestion_plugin"] == "folder_watch"
