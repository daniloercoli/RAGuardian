import os
import re
import shutil
import hashlib
from dataclasses import dataclass
from pathlib import Path

from flask import current_app, request

from utils.knowledge_base_store import (
    DEFAULT_KNOWLEDGE_BASE_ID,
    KnowledgeBaseStore,
    KnowledgeBaseValidationError,
    validate_knowledge_base_id,
)
from utils.settings_store import SettingsStore


@dataclass(frozen=True)
class WorkspaceContext:
    user_id: str
    workspace_id: str
    settings_file: str
    data_folder: str
    workspace_upload_folder: str
    knowledge_bases_file: str
    secrets_file: str
    secret_key: str

    @property
    def file_index(self) -> str:
        return str(Path(self.data_folder) / "files.json")

    @property
    def upload_folder(self) -> str:
        return self.workspace_upload_folder

    @property
    def chroma_collection(self) -> str:
        return collection_for_workspace(self.workspace_id)

    def as_config(self) -> dict:
        """Backward-compatible default-KB runtime config."""
        default = knowledge_base_context(self, DEFAULT_KNOWLEDGE_BASE_ID, create_dirs=True)
        return default.as_config()


@dataclass(frozen=True)
class KnowledgeBaseContext:
    user_id: str
    workspace_id: str
    knowledge_base_id: str
    knowledge_base_name: str
    settings_file: str
    file_index: str
    upload_folder: str
    workspace_upload_folder: str
    chroma_collection: str
    secrets_file: str
    secret_key: str

    def as_config(self) -> dict:
        return {
            "USER_ID": self.user_id,
            "WORKSPACE_ID": self.workspace_id,
            "KNOWLEDGE_BASE_ID": self.knowledge_base_id,
            "KNOWLEDGE_BASE_NAME": self.knowledge_base_name,
            "SETTINGS_FILE": self.settings_file,
            "FILE_INDEX": self.file_index,
            "UPLOAD_FOLDER": self.upload_folder,
            "WORKSPACE_UPLOAD_FOLDER": self.workspace_upload_folder,
            "CHROMA_COLLECTION": self.chroma_collection,
            "SECRETS_FILE": self.secrets_file,
            "SECRET_KEY": self.secret_key,
        }


def workspace_for_user(user: dict, app=None) -> WorkspaceContext:
    if not user or not user.get("id"):
        raise RuntimeError("A logged-in user is required")
    app = app or current_app
    workspace_id = safe_workspace_id(user["id"])
    data_root = Path(app.config.get("WORKSPACE_DATA_DIR", "app/data/workspaces"))
    upload_root = Path(app.config.get("WORKSPACE_UPLOAD_DIR", "app/uploads/workspaces"))
    workspace_data = data_root / workspace_id
    workspace_upload = upload_root / workspace_id
    workspace_data.mkdir(parents=True, exist_ok=True)
    workspace_upload.mkdir(parents=True, exist_ok=True)
    settings_file = workspace_data / "settings.json"
    if not settings_file.exists():
        global_settings = SettingsStore(app.config.get("SETTINGS_FILE")).load()
        SettingsStore(str(settings_file)).save({**global_settings, "auth": {"api_keys": []}, "data_sources": []})
    context = WorkspaceContext(
        user_id=user["id"],
        workspace_id=workspace_id,
        settings_file=str(settings_file),
        data_folder=str(workspace_data),
        workspace_upload_folder=str(workspace_upload),
        knowledge_bases_file=str(workspace_data / "knowledge_bases.json"),
        secrets_file=app.config.get("SECRETS_FILE", "app/data/secrets.json"),
        secret_key=app.config.get("RAG_SECRET_KEY") or app.config.get("SECRET_KEY", ""),
    )
    knowledge_base_store(context, app=app).ensure_default()
    return context


def workspace_from_request(app=None) -> WorkspaceContext:
    from utils.auth import current_user
    from utils.index_lock import lifecycle_read_lock
    from utils.user_store import UserStore

    app = app or current_app
    with lifecycle_read_lock():
        api_key = getattr(request, "api_key", None)
        if api_key:
            user = UserStore(app.config["USERS_FILE"]).get(
                str(api_key.get("user_id") or "")
            )
        else:
            user = current_user()
        if not user or not user.get("enabled", True):
            raise RuntimeError(
                "A valid user or API key owner is required for workspace operations"
            )
        return workspace_for_user(user, app=app)


def safe_workspace_id(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value or "")).strip("-._")
    return safe[:80] or "workspace"


def collection_for_workspace(workspace_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", safe_workspace_id(workspace_id))
    return f"documents_{safe}"


def collection_for_knowledge_base(workspace_id: str, knowledge_base_id: str) -> str:
    knowledge_base_id = validate_knowledge_base_id(knowledge_base_id)
    if knowledge_base_id == DEFAULT_KNOWLEDGE_BASE_ID:
        return collection_for_workspace(workspace_id)
    digest = hashlib.sha256(f"{workspace_id}:{knowledge_base_id}".encode("utf-8")).hexdigest()
    return f"kb_{digest[:40]}"


def knowledge_base_store(workspace: WorkspaceContext, app=None) -> KnowledgeBaseStore:
    if app is None:
        from flask import has_app_context

        max_additional = (
            int(current_app.config.get("MAX_KNOWLEDGE_BASES", 20))
            if has_app_context()
            else int(os.getenv("RAG_MAX_KNOWLEDGE_BASES", "20"))
        )
    else:
        max_additional = int(app.config.get("MAX_KNOWLEDGE_BASES", 20))
    return KnowledgeBaseStore(
        workspace.knowledge_bases_file,
        max_additional=max_additional,
    )


def knowledge_base_context(
    workspace: WorkspaceContext,
    knowledge_base_id: str | None = None,
    *,
    api_key: dict | None = None,
    allow_inactive: bool = False,
    create_dirs: bool = False,
) -> KnowledgeBaseContext:
    knowledge_base_id = validate_knowledge_base_id(knowledge_base_id)
    allowed_ids = None
    if api_key is not None:
        values = (
            api_key.get("knowledge_base_ids")
            if "knowledge_base_ids" in api_key
            else [DEFAULT_KNOWLEDGE_BASE_ID]
        )
        allowed_ids = set(values or [])
        if knowledge_base_id not in allowed_ids:
            raise KnowledgeBaseValidationError(
                "Knowledge base non trovata",
                code="knowledge_base_not_found",
                status_code=404,
            )
    record = knowledge_base_store(workspace).get(knowledge_base_id)
    if record is None:
        raise KnowledgeBaseValidationError(
            "Knowledge base non trovata",
            code="knowledge_base_not_found",
            status_code=404,
        )
    if not allow_inactive and record["status"] != "active":
        code = (
            "knowledge_base_deleting"
            if record["status"] == "deleting"
            else "knowledge_base_delete_failed"
        )
        raise KnowledgeBaseValidationError(
            "Knowledge base non disponibile",
            code=code,
            status_code=409,
        )

    workspace_data = Path(workspace.data_folder)
    workspace_upload = Path(workspace.workspace_upload_folder)
    if knowledge_base_id == DEFAULT_KNOWLEDGE_BASE_ID:
        file_index = workspace_data / "files.json"
        upload_folder = workspace_upload
    else:
        file_index = workspace_data / "knowledge_bases" / knowledge_base_id / "files.json"
        upload_folder = workspace_upload / "__knowledge_bases__" / knowledge_base_id
    if create_dirs:
        file_index.parent.mkdir(parents=True, exist_ok=True)
        upload_folder.mkdir(parents=True, exist_ok=True)
    return KnowledgeBaseContext(
        user_id=workspace.user_id,
        workspace_id=workspace.workspace_id,
        knowledge_base_id=knowledge_base_id,
        knowledge_base_name=record["name"],
        settings_file=workspace.settings_file,
        file_index=str(file_index),
        upload_folder=str(upload_folder),
        workspace_upload_folder=workspace.workspace_upload_folder,
        chroma_collection=collection_for_knowledge_base(
            workspace.workspace_id,
            knowledge_base_id,
        ),
        secrets_file=workspace.secrets_file,
        secret_key=workspace.secret_key,
    )


def knowledge_base_from_request(
    knowledge_base_id: str | None = None,
    *,
    app=None,
    allow_inactive: bool = False,
    create_dirs: bool = False,
) -> KnowledgeBaseContext:
    app = app or current_app
    workspace = workspace_from_request(app)
    return knowledge_base_context(
        workspace,
        knowledge_base_id,
        api_key=getattr(request, "api_key", None),
        allow_inactive=allow_inactive,
        create_dirs=create_dirs,
    )


def remove_workspace_files(user_id: str, app=None) -> None:
    app = app or current_app
    workspace_id = safe_workspace_id(user_id)
    from utils.conversation_memory import get_conversation_store
    from utils.index_lock import (
        assert_distributed_locks_healthy,
        index_write_lock,
        lifecycle_write_lock,
    )
    from utils.job_store import get_job_store
    from utils.prompt_store import PromptStore
    from utils.rag_engine import clear_cache_for_collection
    from utils.secret_store import SecretStore
    from utils.user_store import UserDeletionPreflightError

    with lifecycle_write_lock():
        with index_write_lock():
            job_store = get_job_store()
            if job_store.active_jobs_count(workspace_id):
                raise UserDeletionPreflightError(
                    "Impossibile eliminare l'utente mentre ha job attivi"
                )

            workspace = workspace_for_user({"id": user_id}, app=app)
            for record in knowledge_base_store(workspace, app=app).list():
                assert_distributed_locks_healthy()
                collection_name = collection_for_knowledge_base(
                    workspace_id,
                    record["id"],
                )
                _delete_chroma_collection(collection_name)
                clear_cache_for_collection(collection_name)
            assert_distributed_locks_healthy()
            get_conversation_store().clear_by_prefix(f"{workspace_id}:")
            PromptStore(
                app.config.get("PROMPTS_DIR", "app/data")
            ).delete_user_prompts(user_id)
            SecretStore(
                app.config.get("SECRETS_FILE"),
                key=app.config.get("RAG_SECRET_KEY")
                or app.config.get("SECRET_KEY"),
            ).delete_owner(workspace_id)
            assert_distributed_locks_healthy()
            job_store.clear_by_workspace(workspace_id)

            for root_key in (
                "WORKSPACE_DATA_DIR",
                "WORKSPACE_UPLOAD_DIR",
            ):
                root = Path(app.config[root_key])
                path = root / workspace_id
                if path.exists():
                    assert_distributed_locks_healthy()
                    shutil.rmtree(path)


def _delete_chroma_collection(collection_name: str) -> bool:
    from utils.chroma_manager import _get_chroma_client

    client = _get_chroma_client()
    collection_names = {
        item if isinstance(item, str) else item.name
        for item in client.list_collections()
    }
    if collection_name not in collection_names:
        return False
    client.delete_collection(collection_name)
    return True
