#!/usr/bin/env python3
"""Idempotently bootstrap multi-knowledge-base metadata without moving data."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
APP_ROOT = REPOSITORY_ROOT / "app"
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from utils.knowledge_base_store import KnowledgeBaseCatalogError, KnowledgeBaseStore
from utils.settings_store import SettingsStore
from utils.user_store import UserStore
from utils.workspace import safe_workspace_id


def migrate(
    *,
    workspace_root: Path,
    users_file: Path,
    apply: bool,
    max_additional: int,
) -> dict:
    report = {
        "mode": "apply" if apply else "dry-run",
        "workspaces_checked": 0,
        "catalogs_created": 0,
        "data_sources_updated": 0,
        "api_keys_updated": 0,
        "errors": [],
        "changes": [],
    }
    valid_workspaces: set[str] = set()
    if not workspace_root.exists():
        return report

    for workspace_dir in sorted(path for path in workspace_root.iterdir() if path.is_dir()):
        report["workspaces_checked"] += 1
        workspace_id = workspace_dir.name
        catalog_path = workspace_dir / "knowledge_bases.json"
        store = KnowledgeBaseStore(catalog_path, max_additional=max_additional)
        try:
            if not catalog_path.exists():
                report["catalogs_created"] += 1
                report["changes"].append(
                    {"workspace_id": workspace_id, "change": "create_default_catalog"}
                )
                if apply:
                    store.ensure_default()
            else:
                store.list()
            valid_workspaces.add(workspace_id)
        except KnowledgeBaseCatalogError as exc:
            report["errors"].append(
                {
                    "workspace_id": workspace_id,
                    "error": "invalid_knowledge_base_catalog",
                    "detail": str(exc),
                }
            )
            continue

        settings_path = workspace_dir / "settings.json"
        if settings_path.exists():
            try:
                settings_store = SettingsStore(str(settings_path))
                with settings_store.transaction():
                    settings = _read_json(settings_path)
                    sources = (
                        settings.get("data_sources", [])
                        if isinstance(settings, dict)
                        else []
                    )
                    changed = 0
                    for source in sources:
                        if (
                            isinstance(source, dict)
                            and "knowledge_base_id" not in source
                        ):
                            source["knowledge_base_id"] = "default"
                            changed += 1
                    if changed:
                        report["data_sources_updated"] += changed
                        report["changes"].append(
                            {
                                "workspace_id": workspace_id,
                                "change": "default_data_sources",
                                "count": changed,
                            }
                        )
                        if apply:
                            _write_json_atomic(settings_path, settings)
            except (OSError, ValueError) as exc:
                report["errors"].append(
                    {
                        "workspace_id": workspace_id,
                        "error": "invalid_settings",
                        "detail": str(exc),
                    }
                )

    if users_file.exists():
        try:
            users_document = _read_json(users_file)
        except ValueError as exc:
            report["errors"].append(
                {
                    "error": "invalid_users_file",
                    "detail": str(exc),
                }
            )
            return report
        users = (
            users_document.get("users")
            if isinstance(users_document, dict)
            else users_document
        )
        users = users if isinstance(users, list) else []
        eligible_user_ids: set[str] = set()
        changed_keys = 0
        for user in users:
            if not isinstance(user, dict):
                continue
            workspace_id = safe_workspace_id(user.get("id"))
            if workspace_id not in valid_workspaces:
                continue
            eligible_user_ids.add(str(user.get("id") or ""))
            for key in user.get("api_keys") or []:
                if isinstance(key, dict) and "knowledge_base_ids" not in key:
                    changed_keys += 1
        if apply and changed_keys:
            changed_keys = UserStore(
                str(users_file)
            ).ensure_api_key_knowledge_base_ids(
                user_ids=eligible_user_ids,
            )
        if changed_keys:
            report["api_keys_updated"] = changed_keys
            report["changes"].append(
                {"change": "default_api_key_allowlists", "count": changed_keys}
            )
    return report


def _read_json(path: Path):
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (json.JSONDecodeError, OSError) as exc:
        raise ValueError(f"JSON non valido: {path}") from exc


def _write_json_atomic(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.stem}.migration.",
        suffix=".json",
        dir=str(path.parent),
        text=True,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temporary_name, path)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--workspace-root",
        default=os.getenv("RAG_WORKSPACE_DATA_DIR", "app/data/workspaces"),
    )
    parser.add_argument(
        "--users-file",
        default=os.getenv("RAG_USERS_FILE", "app/data/users.json"),
    )
    parser.add_argument(
        "--max-additional",
        type=int,
        default=int(os.getenv("RAG_MAX_KNOWLEDGE_BASES", "20")),
    )
    arguments = parser.parse_args(argv)
    report = migrate(
        workspace_root=Path(arguments.workspace_root),
        users_file=Path(arguments.users_file),
        apply=arguments.apply,
        max_additional=arguments.max_additional,
    )
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 1 if report["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
