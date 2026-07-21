import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from flask import current_app, request

from utils.settings_store import SettingsStore


@dataclass(frozen=True)
class WorkspaceContext:
    user_id: str
    workspace_id: str
    settings_file: str
    file_index: str
    upload_folder: str
    chroma_collection: str
    secrets_file: str
    secret_key: str

    def as_config(self) -> dict:
        return {
            "USER_ID": self.user_id,
            "WORKSPACE_ID": self.workspace_id,
            "SETTINGS_FILE": self.settings_file,
            "FILE_INDEX": self.file_index,
            "UPLOAD_FOLDER": self.upload_folder,
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
    file_index = workspace_data / "files.json"
    return WorkspaceContext(
        user_id=user["id"],
        workspace_id=workspace_id,
        settings_file=str(settings_file),
        file_index=str(file_index),
        upload_folder=str(workspace_upload),
        chroma_collection=collection_for_workspace(workspace_id),
        secrets_file=app.config.get("SECRETS_FILE", "app/data/secrets.json"),
        secret_key=app.config.get("RAG_SECRET_KEY") or app.config.get("SECRET_KEY", ""),
    )


def workspace_from_request(app=None) -> WorkspaceContext:
    from utils.auth import current_user

    user = current_user()
    if not user and getattr(request, "api_key", None):
        user = {"id": request.api_key.get("user_id")}
    if not user:
        raise RuntimeError("A user or API key is required for workspace operations")
    return workspace_for_user(user, app=app)


def safe_workspace_id(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value or "")).strip("-._")
    return safe[:80] or "workspace"


def collection_for_workspace(workspace_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", safe_workspace_id(workspace_id))
    return f"documents_{safe}"


def remove_workspace_files(user_id: str, app=None) -> None:
    app = app or current_app
    workspace_id = safe_workspace_id(user_id)
    from utils.conversation_memory import get_conversation_store
    from utils.index_lock import index_write_lock
    from utils.job_store import get_job_store
    from utils.prompt_store import PromptStore
    from utils.rag_engine import clear_cache
    from utils.secret_store import SecretStore

    with index_write_lock():
        job_store = get_job_store()
        if job_store.active_jobs_count(workspace_id):
            raise RuntimeError("Impossibile eliminare l'utente mentre ha job attivi")

        _delete_chroma_collection(collection_for_workspace(workspace_id))
        get_conversation_store().clear_by_prefix(f"{workspace_id}:")
        PromptStore(app.config.get("PROMPTS_DIR", "app/data")).delete_user_prompts(user_id)
        SecretStore(
            app.config.get("SECRETS_FILE"),
            key=app.config.get("RAG_SECRET_KEY") or app.config.get("SECRET_KEY"),
        ).delete_owner(workspace_id)
        job_store.clear_by_workspace(workspace_id)

        for root_key in ("WORKSPACE_DATA_DIR", "WORKSPACE_UPLOAD_DIR"):
            root = Path(app.config[root_key])
            path = root / workspace_id
            if path.exists():
                shutil.rmtree(path)

        clear_cache()


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
