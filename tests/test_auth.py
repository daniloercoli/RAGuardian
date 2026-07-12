from werkzeug.security import generate_password_hash
import pytest

from app import create_app
from app.utils.auth import check_admin_password


def test_admin_password_hash_env_authenticates(monkeypatch):
    monkeypatch.delenv("RAG_ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("ADMIN_PASSWORD_HASH", raising=False)
    monkeypatch.setenv("RAG_ADMIN_PASSWORD_HASH", generate_password_hash("strong-pass"))

    assert check_admin_password("strong-pass") is True
    assert check_admin_password("wrong-pass") is False


def test_admin_password_hash_env_takes_precedence_over_plaintext(monkeypatch):
    monkeypatch.setenv("RAG_ADMIN_PASSWORD_HASH", generate_password_hash("hashed-pass"))
    monkeypatch.setenv("RAG_ADMIN_PASSWORD", "plain-pass")
    monkeypatch.delenv("ADMIN_PASSWORD_HASH", raising=False)
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)

    assert check_admin_password("hashed-pass") is True
    assert check_admin_password("plain-pass") is False


def test_external_bind_rejects_insecure_default_credentials(monkeypatch, tmp_path):
    monkeypatch.setenv("GUNICORN_HOST", "0.0.0.0")
    monkeypatch.delenv("RAG_SECRET_KEY", raising=False)
    monkeypatch.delenv("RAG_ADMIN_PASSWORD_HASH", raising=False)
    monkeypatch.delenv("ADMIN_PASSWORD_HASH", raising=False)
    monkeypatch.delenv("RAG_ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)

    with pytest.raises(RuntimeError, match="Unsafe production configuration"):
        create_app(
            {
                "TESTING": False,
                "SECRET_KEY": "dev-secret",
                "SETTINGS_FILE": str(tmp_path / "settings.json"),
                "FILE_INDEX": str(tmp_path / "files.json"),
                "UPLOAD_FOLDER": str(tmp_path / "uploads"),
                "WORKSPACE_DATA_DIR": str(tmp_path / "workspaces"),
                "WORKSPACE_UPLOAD_DIR": str(tmp_path / "workspace-uploads"),
            }
        )
