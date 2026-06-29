from werkzeug.security import generate_password_hash

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
