import pytest
from app.utils.user_store import UserStore


@pytest.fixture
def user_store(tmp_path):
    users_file = tmp_path / "users.json"
    store = UserStore(users_file)
    return store


def test_api_key_creation_and_retrieval(user_store):
    user = user_store.create_user(email="user1@example.com", password="password123", display_name="user1")
    user_id = user["id"]
    key = user_store.create_api_key(
        user_id=user_id,
        name="test-key",
        scopes=["query"],
        description="Test API key"
    )
    assert key["name"] == "test-key"
    assert key["scopes"] == ["query"]
    assert key["description"] == "Test API key"
    assert key["enabled"] is True
    assert len(key["masked_key"]) > 0

    keys = user_store.get_api_keys(user_id)
    assert len(keys) == 1
    assert keys[0]["name"] == "test-key"
    assert keys[0]["description"] == "Test API key"
    assert keys[0]["user_id"] == user_id


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
