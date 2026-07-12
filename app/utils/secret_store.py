import base64
import hashlib
import hmac
import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Optional

from utils.file_lock import ProcessSafeFileLock


class SecretStore:
    """Small encrypted JSON secret store for connector credentials."""

    _locks: dict[str, ProcessSafeFileLock] = {}
    _locks_guard = threading.Lock()

    def __init__(self, path: Optional[str] = None, key: Optional[str] = None):
        configured = path or os.getenv("RAG_SECRETS_FILE", "app/data/secrets.json")
        self.path = Path(configured)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.key = (key or os.getenv("RAG_SECRET_KEY") or os.getenv("FLASK_SECRET_KEY") or "dev-secret-key").encode("utf-8")
        with self._locks_guard:
            self._lock = self._locks.setdefault(
                str(self.path.resolve()),
                ProcessSafeFileLock(self.path.with_suffix(self.path.suffix + ".lock")),
            )

    def set_secret(self, owner_id: str, name: str, value: str) -> str:
        ref = secret_ref(owner_id, name)
        with self._lock:
            data = self._load_unlocked()
            data[ref] = self._encrypt(value)
            self._save_unlocked(data)
        return ref

    def get_secret(self, ref: str) -> str:
        with self._lock:
            payload = self._load_unlocked().get(ref)
        return self._decrypt(payload) if payload else ""

    def delete_owner(self, owner_id: str) -> int:
        prefix = f"{owner_id}:"
        with self._lock:
            data = self._load_unlocked()
            keys = [key for key in data if key.startswith(prefix)]
            for key in keys:
                data.pop(key, None)
            self._save_unlocked(data)
        return len(keys)

    def _encrypt(self, value: str) -> dict:
        raw = str(value or "").encode("utf-8")
        nonce = os.urandom(16)
        stream = _keystream(self.key, nonce, len(raw))
        ciphertext = bytes(byte ^ stream[index] for index, byte in enumerate(raw))
        tag = hmac.new(self.key, nonce + ciphertext, hashlib.sha256).digest()
        return {
            "nonce": base64.b64encode(nonce).decode("ascii"),
            "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
            "tag": base64.b64encode(tag).decode("ascii"),
        }

    def _decrypt(self, payload: dict) -> str:
        nonce = base64.b64decode(payload["nonce"])
        ciphertext = base64.b64decode(payload["ciphertext"])
        tag = base64.b64decode(payload["tag"])
        expected = hmac.new(self.key, nonce + ciphertext, hashlib.sha256).digest()
        if not hmac.compare_digest(tag, expected):
            raise ValueError("Secret authentication failed")
        stream = _keystream(self.key, nonce, len(ciphertext))
        raw = bytes(byte ^ stream[index] for index, byte in enumerate(ciphertext))
        return raw.decode("utf-8")

    def _load_unlocked(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            with self.path.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save_unlocked(self, data: dict) -> None:
        fd, tmp_name = tempfile.mkstemp(
            prefix=".secrets.",
            suffix=".json",
            dir=str(self.path.parent),
            text=True,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, sort_keys=True)
                f.write("\n")
            os.replace(tmp_name, self.path)
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)


def secret_ref(owner_id: str, name: str) -> str:
    clean_name = "".join(char if char.isalnum() or char in "._:-" else "-" for char in str(name or "secret"))
    return f"{owner_id}:{clean_name.strip('-') or 'secret'}"


def _keystream(key: bytes, nonce: bytes, length: int) -> bytes:
    chunks = []
    counter = 0
    while sum(len(chunk) for chunk in chunks) < length:
        counter_bytes = counter.to_bytes(8, "big")
        chunks.append(hmac.new(key, nonce + counter_bytes, hashlib.sha256).digest())
        counter += 1
    return b"".join(chunks)[:length]
