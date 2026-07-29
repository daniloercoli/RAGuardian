import json
import threading

import scripts.migrate_knowledge_bases as migration_module
from scripts.migrate_knowledge_bases import migrate
from utils.settings_store import SettingsStore


def _write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_migration_dry_run_apply_and_idempotency(tmp_path):
    workspace_root = tmp_path / "workspaces"
    workspace = workspace_root / "alice"
    settings_file = workspace / "settings.json"
    users_file = tmp_path / "users.json"
    _write_json(
        settings_file,
        {
            "data_sources": [
                {"id": "mail", "plugin": "email_imap"},
                {
                    "id": "drive",
                    "plugin": "microsoft_drive",
                    "knowledge_base_id": "default",
                },
            ]
        },
    )
    _write_json(
        users_file,
        {
            "users": [
                {
                    "id": "alice",
                    "api_keys": [{"name": "legacy", "scopes": ["query"]}],
                }
            ]
        },
    )

    dry_run = migrate(
        workspace_root=workspace_root,
        users_file=users_file,
        apply=False,
        max_additional=20,
    )
    assert dry_run["catalogs_created"] == 1
    assert dry_run["data_sources_updated"] == 1
    assert dry_run["api_keys_updated"] == 1
    assert not (workspace / "knowledge_bases.json").exists()
    assert "knowledge_base_id" not in json.loads(
        settings_file.read_text(encoding="utf-8")
    )["data_sources"][0]

    applied = migrate(
        workspace_root=workspace_root,
        users_file=users_file,
        apply=True,
        max_additional=20,
    )
    assert applied["errors"] == []
    catalog = json.loads(
        (workspace / "knowledge_bases.json").read_text(encoding="utf-8")
    )
    assert [item["id"] for item in catalog["knowledge_bases"]] == ["default"]
    assert json.loads(settings_file.read_text(encoding="utf-8"))["data_sources"][0][
        "knowledge_base_id"
    ] == "default"
    assert json.loads(users_file.read_text(encoding="utf-8"))["users"][0][
        "api_keys"
    ][0]["knowledge_base_ids"] == ["default"]

    repeated = migrate(
        workspace_root=workspace_root,
        users_file=users_file,
        apply=True,
        max_additional=20,
    )
    assert repeated["catalogs_created"] == 0
    assert repeated["data_sources_updated"] == 0
    assert repeated["api_keys_updated"] == 0
    assert repeated["changes"] == []


def test_migration_reports_corrupt_catalog_without_repairing_it(tmp_path):
    workspace_root = tmp_path / "workspaces"
    workspace = workspace_root / "broken"
    workspace.mkdir(parents=True)
    catalog = workspace / "knowledge_bases.json"
    catalog.write_text("{broken", encoding="utf-8")
    settings = workspace / "settings.json"
    _write_json(settings, {"data_sources": [{"id": "mail"}]})

    report = migrate(
        workspace_root=workspace_root,
        users_file=tmp_path / "missing-users.json",
        apply=True,
        max_additional=20,
    )

    assert report["errors"][0]["error"] == "invalid_knowledge_base_catalog"
    assert catalog.read_text(encoding="utf-8") == "{broken"
    assert "knowledge_base_id" not in json.loads(
        settings.read_text(encoding="utf-8")
    )["data_sources"][0]


def test_settings_migration_serializes_concurrent_store_updates(
    tmp_path,
    monkeypatch,
):
    workspace_root = tmp_path / "workspaces"
    settings_file = workspace_root / "alice" / "settings.json"
    _write_json(
        settings_file,
        {"data_sources": [{"id": "mail", "plugin": "email_imap"}]},
    )
    migration_writing = threading.Event()
    release_migration = threading.Event()
    concurrent_done = threading.Event()
    original_write = migration_module._write_json_atomic

    def paused_write(path, payload):
        migration_writing.set()
        assert release_migration.wait(timeout=2)
        original_write(path, payload)

    monkeypatch.setattr(migration_module, "_write_json_atomic", paused_write)
    migration_thread = threading.Thread(
        target=migrate,
        kwargs={
            "workspace_root": workspace_root,
            "users_file": tmp_path / "missing-users.json",
            "apply": True,
            "max_additional": 20,
        },
    )
    migration_thread.start()
    assert migration_writing.wait(timeout=2)

    def update_settings():
        SettingsStore(str(settings_file)).update(
            {"concurrent_update": {"preserved": True}}
        )
        concurrent_done.set()

    update_thread = threading.Thread(target=update_settings)
    update_thread.start()
    assert not concurrent_done.wait(timeout=0.1)

    release_migration.set()
    migration_thread.join(timeout=2)
    update_thread.join(timeout=2)

    saved = json.loads(settings_file.read_text(encoding="utf-8"))
    assert saved["data_sources"][0]["knowledge_base_id"] == "default"
    assert saved["concurrent_update"] == {"preserved": True}
