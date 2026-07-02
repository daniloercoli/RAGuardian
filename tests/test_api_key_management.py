import concurrent.futures
import json
import tempfile
from pathlib import Path

import pytest

from app.utils.api_key_logger import ApiKeyLogger
from app.utils.user_store import UserStore


def _make_user(path: Path, user_id: str, *, role: str = "user") -> Path:
    existing = []
    if path.exists():
        with path.open("r") as f:
            existing = json.load(f)

    existing.append({
        "id": user_id,
        "email": f"{user_id}@example.com",
        "display_name": user_id,
        "password_hash": "dummy",
        "role": role,
        "enabled": True,
        "api_keys": [],
    })

    with path.open("w") as f:
        json.dump(existing, f)


def test_create_and_retrieve_api_key():
    with tempfile.TemporaryDirectory() as tmp:
        store_path = Path(tmp) / "users.json"
        _make_user(store_path, "user-1")

        store = UserStore(store_path)
        created = store.create_api_key(
            user_id="user-1",
            name="prod",
            scopes=["query"],
            description="Production key",
        )

        assert created["name"] == "prod"
        assert created["description"] == "Production key"
        assert created["masked_key"] != created["key"]
        assert created["masked_key"].startswith(created["key"][:8])
        assert created["masked_key"].endswith(created["key"][-4:])

        keys = store.get_api_keys("user-1")
        assert len(keys) == 1
        assert keys[0]["name"] == "prod"
        assert keys[0]["description"] == "Production key"
        assert "key" not in keys[0]


def test_bootstrap_admin_if_empty_creates_only_one_admin_concurrently(tmp_path):
    store_path = tmp_path / "users.json"
    store = UserStore(store_path)

    def bootstrap(email: str):
        return store.bootstrap_admin_if_empty(email=email, password="admin")

    emails = ["first@example.local", "second@example.local"]
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(bootstrap, emails))

    users = UserStore(store_path).list()
    assert len(users) == 1
    assert users[0]["role"] == "admin"
    assert sum(result is not None for result in results) == 1


def test_raw_api_key_is_hidden_from_public_user_views():
    with tempfile.TemporaryDirectory() as tmp:
        store_path = Path(tmp) / "users.json"
        _make_user(store_path, "user-1")

        store = UserStore(store_path)
        store.create_api_key(
            user_id="user-1",
            name="secret",
            scopes=["query"],
            api_key_value="raw-secret-value",
        )

        listed_user = store.list()[0]
        listed_key = listed_user["api_keys"][0]
        assert listed_key["masked_key"] == "raw-secr...alue"
        assert "key" not in listed_key

        hidden_key = store.get_api_key("user-1", "secret")
        revealed_key = store.get_api_key("user-1", "secret", include_raw=True)
        assert "key" not in hidden_key
        assert revealed_key["key"] == "raw-secret-value"


def test_duplicate_key_name_fails():
    with tempfile.TemporaryDirectory() as tmp:
        store_path = Path(tmp) / "users.json"
        _make_user(store_path, "user-1")

        store = UserStore(store_path)
        store.create_api_key(user_id="user-1", name="key", scopes=["query"])

        try:
            store.create_api_key(user_id="user-1", name="key", scopes=["query"])
            assert False, "Should have raised ValueError"
        except ValueError:
            pass


def test_rename_to_duplicate_key_name_fails():
    with tempfile.TemporaryDirectory() as tmp:
        store_path = Path(tmp) / "users.json"
        _make_user(store_path, "user-1")

        store = UserStore(store_path)
        store.create_api_key(user_id="user-1", name="first", scopes=["query"])
        store.create_api_key(user_id="user-1", name="second", scopes=["query"])

        with pytest.raises(ValueError):
            store.update_api_key_name(user_id="user-1", key_name="first", new_name="second")


def test_toggle_api_key_enabled():
    with tempfile.TemporaryDirectory() as tmp:
        store_path = Path(tmp) / "users.json"
        _make_user(store_path, "user-1")

        store = UserStore(store_path)
        store.create_api_key(user_id="user-1", name="test", scopes=["ingest"])

        result = store.toggle_api_key_enabled(user_id="user-1", key_name="test", enabled=False)
        assert result is not None
        assert result["enabled"] is False


def test_delete_api_key():
    with tempfile.TemporaryDirectory() as tmp:
        store_path = Path(tmp) / "users.json"
        _make_user(store_path, "user-1")

        store = UserStore(store_path)
        store.create_api_key(user_id="user-1", name="ephemeral", scopes=["query"])
        assert len(store.get_api_keys("user-1")) == 1

        store.delete_api_key(user_id="user-1", key_name="ephemeral")
        assert len(store.get_api_keys("user-1")) == 0


def test_update_usage_counts():
    with tempfile.TemporaryDirectory() as tmp:
        store_path = Path(tmp) / "users.json"
        _make_user(store_path, "user-1")

        store = UserStore(store_path)
        store.create_api_key(user_id="user-1", name="metered", scopes=["query"])

        for _ in range(3):
            store.update_api_key_usage(user_id="user-1", key_name="metered")

        keys = store.get_api_keys("user-1")
        assert keys[0]["usage_count"] == 3
        assert keys[0]["last_used"] is not None


def test_get_api_keys_returns_empty_for_unknown_user():
    with tempfile.TemporaryDirectory() as tmp:
        store_path = Path(tmp) / "users.json"
        store = UserStore(store_path)
        assert store.get_api_keys("nonexistent") == []


def test_create_api_key_requires_existing_user():
    with tempfile.TemporaryDirectory() as tmp:
        store_path = Path(tmp) / "users.json"
        store = UserStore(store_path)

        try:
            store.create_api_key(user_id="nonexistent", name="key", scopes=["query"])
            assert False, "Should have raised ValueError"
        except ValueError:
            pass


def test_masked_key_format():
    with tempfile.TemporaryDirectory() as tmp:
        store_path = Path(tmp) / "users.json"
        _make_user(store_path, "user-1")

        store = UserStore(store_path)
        key = store.create_api_key(user_id="user-1", name="masked-test", scopes=["query"])
        raw = key["key"]
        masked = key["masked_key"]

        assert len(raw) > 8
        assert masked.startswith(raw[:8])
        assert masked.endswith(raw[-4:])
        assert "..." in masked


def test_toggle_nonexistent_key_returns_none():
    with tempfile.TemporaryDirectory() as tmp:
        store_path = Path(tmp) / "users.json"
        _make_user(store_path, "user-1")

        store = UserStore(store_path)
        result = store.toggle_api_key_enabled(
            user_id="user-1", key_name="nonexistent", enabled=True
        )
        assert result is None


def test_delete_nonexistent_key_returns_false():
    with tempfile.TemporaryDirectory() as tmp:
        store_path = Path(tmp) / "users.json"
        _make_user(store_path, "user-1")

        store = UserStore(store_path)
        result = store.delete_api_key(user_id="user-1", key_name="nonexistent")
        assert result is False


def test_api_key_logger_keeps_all_concurrent_entries(tmp_path):
    usage_file = tmp_path / "api_keys_usage.json"

    def write_entry(index: int) -> None:
        ApiKeyLogger(str(usage_file)).log(
            user_id="user-1",
            key_name=f"key-{index}",
            endpoint="/api/v1/query",
            method="POST",
            status_code=200,
            scopes_used=["query"],
            duration_ms=12,
            request_id=f"req-{index}",
            ip_address="127.0.0.1",
            workspace_id="workspace-user-1",
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
        list(executor.map(write_entry, range(200)))

    data = json.loads(usage_file.read_text())
    assert data["logging_enabled"] is True
    assert len(data["log_entries"]) == 200
    assert {entry["key_name"] for entry in data["log_entries"]} == {
        f"key-{index}" for index in range(200)
    }


def test_api_key_logger_recent_entries_returns_latest_first(tmp_path):
    usage_file = tmp_path / "api_keys_usage.json"
    logger = ApiKeyLogger(str(usage_file))

    for index in range(25):
        logger.log(
            user_id="user-1",
            key_name=f"key-{index:02d}",
            endpoint="/api/v1/query",
            method="POST",
            status_code=200,
        )

    recent = logger.recent_entries(20)

    assert len(recent) == 20
    assert recent[0]["key_name"] == "key-24"
    assert recent[-1]["key_name"] == "key-05"
    assert "key-04" not in {entry["key_name"] for entry in recent}
