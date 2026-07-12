from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from utils.file_lock import ProcessSafeFileLock

SUPPORTED_VARIABLES = {"UTENTE", "NOME_UTENTE", "DATA_ODOIERNO", "ORA"}


class PromptStore:
    """JSON-backed store for user-curated and admin-shared system prompts."""

    _locks: dict[str, ProcessSafeFileLock] = {}
    _locks_guard = threading.Lock()

    def __init__(
        self,
        data_dir: Optional[str] = None,
        shared_path: Optional[str] = None,
        user_dir: Optional[str] = None,
    ):
        data_dir = data_dir or "app/data"
        Path(data_dir).mkdir(parents=True, exist_ok=True)

        self.data_dir = data_dir
        self.shared_path = Path(
            shared_path
            if shared_path
            else os.path.join(data_dir, "shared_prompts.json")
        )
        self.user_dir = Path(
            user_dir if user_dir else os.path.join(data_dir, "user_prompts")
        )
        self.user_dir.mkdir(parents=True, exist_ok=True)

        with self._locks_guard:
            lock_key = str(Path(self.data_dir).resolve())
            self._lock = self._locks.setdefault(
                lock_key,
                ProcessSafeFileLock(Path(self.data_dir) / ".prompts.lock"),
            )

    # ---------------------------------------------------------------
    # Shared (admin) prompts
    # ---------------------------------------------------------------

    def list_shared(self) -> list[dict]:
        with self._lock:
            all_prompts = self._read(self.shared_path)
            return [p for p in all_prompts if p.get("is_active", True)]

    def toggle_shared(self, prompt_id: str) -> Optional[dict]:
        with self._lock:
            all_prompts = self._read(self.shared_path)
            found = None
            for p in all_prompts:
                if p.get("id") == prompt_id:
                    found = p
                    p["is_active"] = not p.get("is_active", True)
                    p["updated_at"] = _now()
                    break
            if not found:
                return None
            self._write(self.shared_path, all_prompts)
        return self._redact(found)

    def all_shared(self) -> list[dict]:
        """Return all shared prompts (active + inactive)."""
        with self._lock:
            return self._read(self.shared_path)

    def create_shared(self, name: str, content: str, created_by: str) -> dict:
        self._validate_prompt(name, content)
        prompt = {
            "id": uuid.uuid4().hex,
            "name": name,
            "content": content,
            "created_by": created_by,
            "created_at": _now(),
            "updated_at": _now(),
            "is_active": True,
        }
        with self._lock:
            prompts = self._read(self.shared_path)
            prompts.append(prompt)
            self._write(self.shared_path, prompts)
        return self._redact(prompt)

    def update_shared(
        self, prompt_id: str, name: Optional[str] = None, content: Optional[str] = None
    ) -> Optional[dict]:
        self._validate_prompt(name, content, require_any=False)
        with self._lock:
            prompts = self._read(self.shared_path)
            updated: Optional[dict] = None
            for i, p in enumerate(prompts):
                if p["id"] != prompt_id:
                    continue
                if name is not None:
                    p["name"] = name
                if content is not None:
                    p["content"] = content
                p["updated_at"] = _now()
                prompts[i] = p
                updated = self._redact(p)
                break
            if updated:
                self._write(self.shared_path, prompts)
            return updated

    def activate_shared(self, prompt_id: str) -> Optional[dict]:
        with self._lock:
            return self._toggle_active_unlocked(self.shared_path, prompt_id, True)

    def deactivate_shared(self, prompt_id: str) -> Optional[dict]:
        with self._lock:
            return self._toggle_active_unlocked(self.shared_path, prompt_id, False)

    def delete_shared(self, prompt_id: str) -> bool:
        with self._lock:
            prompts = self._read(self.shared_path)
            new_prompts = [p for p in prompts if p["id"] != prompt_id]
            if len(new_prompts) == len(prompts):
                return False
            self._write(self.shared_path, new_prompts)
        return True

    def get_shared(self, prompt_id: str) -> Optional[dict]:
        for p in self.list_shared():
            if p["id"] == prompt_id:
                return self._redact(p)
        return None

    # ---------------------------------------------------------------
    # User (personal) prompts
    # ---------------------------------------------------------------

    def _user_file(self, user_id: str) -> Path:
        return self.user_dir / f"{user_id}.json"

    def list_user_prompts(self, user_id: str) -> list[dict]:
        with self._lock:
            prompts = self._read(self._user_file(user_id))
            return [p for p in prompts if p.get("is_active", True)]

    def create_user_prompt(
        self, user_id: str, name: str, content: str
    ) -> dict:
        self._validate_prompt(name, content)
        prompt = {
            "id": uuid.uuid4().hex,
            "name": name,
            "content": content,
            "created_at": _now(),
            "updated_at": _now(),
            "is_active": True,
        }
        with self._lock:
            prompts = self._read(self._user_file(user_id))
            prompts.append(prompt)
            self._write(self._user_file(user_id), prompts)
        return self._redact(prompt)

    def update_user_prompt(
        self,
        user_id: str,
        prompt_id: str,
        name: Optional[str] = None,
        content: Optional[str] = None,
    ) -> Optional[dict]:
        self._validate_prompt(name, content, require_any=False)
        with self._lock:
            prompts = self._read(self._user_file(user_id))
            updated: Optional[dict] = None
            for i, p in enumerate(prompts):
                if p["id"] != prompt_id:
                    continue
                if name is not None:
                    p["name"] = name
                if content is not None:
                    p["content"] = content
                p["updated_at"] = _now()
                prompts[i] = p
                updated = self._redact(p)
                break
            if updated:
                self._write(self._user_file(user_id), prompts)
            return updated

    def activate_user_prompt(self, user_id: str, prompt_id: str) -> Optional[dict]:
        with self._lock:
            return self._toggle_active_unlocked(
                self._user_file(user_id), prompt_id, True
            )

    def deactivate_user_prompt(
        self, user_id: str, prompt_id: str
    ) -> Optional[dict]:
        with self._lock:
            return self._toggle_active_unlocked(
                self._user_file(user_id), prompt_id, False
            )

    def delete_user_prompt(self, user_id: str, prompt_id: str) -> bool:
        with self._lock:
            prompts = self._read(self._user_file(user_id))
            new_prompts = [p for p in prompts if p["id"] != prompt_id]
            if len(new_prompts) == len(prompts):
                return False
            self._write(self._user_file(user_id), new_prompts)
        return True

    def get_user_prompt(self, user_id: str, prompt_id: str) -> Optional[dict]:
        for p in self.list_user_prompts(user_id):
            if p["id"] == prompt_id:
                return self._redact(p)
        return None

    # ---------------------------------------------------------------
    # Unified look-up (searches personal then shared)
    # ---------------------------------------------------------------

    def resolve(self, user_id: str, prompt_id: str) -> Optional[dict]:
        """Return prompt from personal storage, fall back to shared."""
        if not user_id or not prompt_id:
            return None

        result = self.get_user_prompt(user_id, prompt_id)
        if result:
            return result
        return self.get_shared(prompt_id)

    def list_for_dropdown(self, user_id: str) -> list[dict]:
        """List personal + shared prompts for the chat dropdown selector."""
        items: list[dict] = []
        for p in self.list_shared():
            items.append({**self._redact(p), "scope": "shared"})
        for p in self.list_user_prompts(user_id):
            items.append({**self._redact(p), "scope": "personal"})
        return items

    # ---------------------------------------------------------------
    # Template variable resolution
    # ---------------------------------------------------------------

    @staticmethod
    def resolve_template(content: str, user: Optional[dict] = None) -> str:
        """Replace {{VARIABLE}} placeholders with runtime values."""
        if not content:
            return content

        def _replace(match: re.Match) -> str:
            var = match.group(1).upper()
            if var == "UTENTE" and user:
                return user.get("email", "")
            if var == "NOME_UTENTE" and user:
                return user.get("display_name") or user.get("email", "")
            if var == "DATA_ODOIERNO":
                return datetime.now(timezone.utc).strftime("%Y-%m-%d")
            if var == "ORA":
                return datetime.now(timezone.utc).strftime("%H:%M UTC")
            return match.group(0)

        return re.sub(r"\{\{([A-Za-z_]+)\}\}", _replace, content)

    # ---------------------------------------------------------------
    # Internal helpers
    # ---------------------------------------------------------------

    def _toggle_active_unlocked(
        self, path: Path, prompt_id: str, active: bool
    ) -> Optional[dict]:
        prompts = self._read(path)
        updated: Optional[dict] = None
        for i, p in enumerate(prompts):
            if p["id"] == prompt_id:
                p["is_active"] = active
                p["updated_at"] = _now()
                prompts[i] = p
                updated = self._redact(p)
                break
        if updated:
            self._write(path, prompts)
        return updated

    @staticmethod
    def _read(path: Path) -> list[dict]:
        if not path.exists():
            return []
        try:
            with path.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            return []
        prompts = data.get("prompts") if isinstance(data, dict) else data
        return prompts if isinstance(prompts, list) else []

    def _write(self, path: Path, prompts: list[dict]) -> None:
        fd, tmp = tempfile.mkstemp(
            prefix=".prompts.",
            suffix=".json",
            dir=str(path.parent),
            text=True,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"prompts": prompts}, f, indent=2)
                f.write("\n")
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    @staticmethod
    def _validate_prompt(
        name: Optional[str], content: Optional[str], require_any: bool = True
    ) -> None:
        if require_any and not name and not content:
            raise ValueError("name or content is required")
        if name and len(name) > 500:
            raise ValueError("name too long (max 500)")
        if content is not None and len(content) > 100_000:
            raise ValueError("content too long (max 100000)")

    @staticmethod
    def _redact(p: dict) -> dict:
        return {k: v for k, v in p.items() if k != "password"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
