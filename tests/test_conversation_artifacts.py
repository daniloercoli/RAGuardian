import importlib
import sqlite3

import pytest

# Importing the package adds ``app/`` to sys.path, matching the production
# module namespace used internally by the service (``utils.*``).
import app as _app_package  # noqa: F401

from utils.conversation_artifacts import (
    ArtifactCleanupError,
    ConversationArtifactReferences,
    cleanup_workspace_artifacts,
    exclusive_references,
    references_from_history_metadata,
    references_from_response_payload,
)
from utils.conversation_history_store import (
    ConversationHistoryError,
    ConversationHistoryStore,
)
from utils.conversation_memory import ConversationMemoryStore
from utils.conversation_service import ConversationService
from utils.pending_turn_store import PendingTurnResultStore


def _refs(*, attachments=(), images=(), runs=()):
    return ConversationArtifactReferences(
        attachment_ids=frozenset(attachments),
        image_names=frozenset(images),
        run_ids=frozenset(runs),
    )


def _history_store(tmp_path):
    return ConversationHistoryStore(
        "artifact-test-workspace",
        workspace_data_dir=tmp_path / "workspaces",
    )


def _commit_turn(
    store,
    *,
    scope_key,
    turn_id,
    fingerprint,
    metadata,
    knowledge_base_ids=(),
):
    began = store.begin_turn(
        client_conversation_id=scope_key.rsplit(":", 1)[-1],
        scope_key=scope_key,
        scope_kind="kb" if knowledge_base_ids else "default",
        turn_id=turn_id,
        parent_turn_id=None,
        request_fingerprint=fingerprint,
        selected_knowledge_base_ids=list(knowledge_base_ids),
    )
    store.complete_turn(
        scope_key=scope_key,
        turn_id=turn_id,
        lease_token=began["lease_token"],
        request_fingerprint=fingerprint,
        user_content="question",
        assistant_content="answer",
        message_type="code_interpreter",
        selected_knowledge_base_ids=list(knowledge_base_ids),
        metadata=metadata,
    )
    return began["conversation"]["id"]


def test_history_metadata_extracts_only_valid_server_response_references():
    attachment_id = "a" * 32
    run_id = "b" * 12
    image_name = f"{run_id}_chart.png"
    metadata = {
        "path": "/outside/ignore-me.csv",
        "attachments": [{"id": "c" * 32}],
        "response_payload": {
            "attachments": [
                {"id": attachment_id, "path": "/outside/secret.csv"},
                {"file_id": "NOT-A-VALID-ID"},
            ],
            "result": {
                "images": [
                    f"/code_pics/{image_name}",
                    f"https://example.test/code_pics/{run_id}_remote.png",
                    "/code_pics/../../outside.png",
                    f"/code_pics/{run_id}_query.png?download=1",
                ],
                "host_path": "/outside/ignore-me",
            },
        },
    }

    references = references_from_history_metadata(metadata)

    assert references == _refs(
        attachments=[attachment_id],
        images=[image_name],
        runs=[run_id],
    )


def test_response_payload_accepts_explicit_run_id_without_generated_images():
    run_id = "d" * 12

    references = references_from_response_payload(
        {"result": {"run_id": run_id, "images": []}}
    )

    assert references == _refs(runs=[run_id])


def test_history_metadata_persists_only_safe_code_run_id():
    app_module = importlib.import_module("app.app")

    safe = app_module._history_metadata_for_result(
        {
            "type": "code_interpreter",
            "result": {"success": True, "run_id": "abc123def456"},
        }
    )
    unsafe = app_module._history_metadata_for_result(
        {
            "type": "code_interpreter",
            "result": {"success": True, "run_id": "../../outside"},
        }
    )

    assert safe["response_payload"]["result"]["run_id"] == "abc123def456"
    assert "run_id" not in unsafe["response_payload"]["result"]


def test_exclusive_references_protects_shared_upload_and_entire_shared_run():
    shared_attachment = "1" * 32
    private_attachment = "2" * 32
    shared_run = "3" * 12
    private_run = "4" * 12
    shared_image = f"{shared_run}_shared.png"
    second_image_from_shared_run = f"{shared_run}_private-looking.png"
    private_image = f"{private_run}_private.png"
    target = _refs(
        attachments=[shared_attachment, private_attachment],
        images=[shared_image, second_image_from_shared_run, private_image],
        runs=[shared_run, private_run],
    )
    retained = _refs(
        attachments=[shared_attachment],
        images=[shared_image],
        runs=[shared_run],
    )

    exclusive = exclusive_references(target, retained)

    assert exclusive == _refs(
        attachments=[private_attachment],
        images=[private_image],
        runs=[private_run],
    )


def test_cleanup_removes_only_selected_workspace_artifacts(tmp_path):
    workspace_root = tmp_path / "workspace-upload"
    upload_root = workspace_root / "chat_files" / "uploads"
    pics_root = workspace_root / "chat_files" / "pics"
    runs_root = workspace_root / "chat_files" / "code_runs"
    upload_root.mkdir(parents=True)
    pics_root.mkdir(parents=True)
    runs_root.mkdir(parents=True)

    target_attachment = "5" * 32
    retained_attachment = "6" * 32
    (upload_root / f"{target_attachment}_data.csv").write_text("target")
    (upload_root / f"{retained_attachment}_data.csv").write_text("retained")
    # Similar text without the required separator is not owned by the ID.
    near_match = upload_root / f"{target_attachment}suffix.csv"
    near_match.write_text("near-match")

    target_run = "7" * 12
    retained_run = "8" * 12
    target_image = pics_root / f"{target_run}_chart.png"
    second_target_image = pics_root / f"{target_run}_extra.png"
    retained_image = pics_root / f"{retained_run}_chart.png"
    target_image.write_text("target image")
    second_target_image.write_text("same run")
    retained_image.write_text("retained image")
    target_run_dir = runs_root / target_run
    retained_run_dir = runs_root / retained_run
    target_run_dir.mkdir()
    retained_run_dir.mkdir()
    (target_run_dir / "output").mkdir()
    (target_run_dir / "output" / "result.json").write_text("{}")
    (retained_run_dir / "result.json").write_text("{}")

    result = cleanup_workspace_artifacts(
        workspace_root,
        _refs(
            attachments=[target_attachment],
            images=[target_image.name],
            runs=[target_run],
        ),
    )

    assert result.ok is True
    assert result.deleted_files == 3
    assert result.deleted_run_directories == 1
    assert not (upload_root / f"{target_attachment}_data.csv").exists()
    assert not target_image.exists()
    # Selecting a run removes all public images from that run, including ones
    # omitted from the persisted response payload.
    assert not second_target_image.exists()
    assert not target_run_dir.exists()
    assert (upload_root / f"{retained_attachment}_data.csv").exists()
    assert near_match.exists()
    assert retained_image.exists()
    assert retained_run_dir.exists()


def test_cleanup_unlinks_matching_symlinks_without_following_them(tmp_path):
    workspace_root = tmp_path / "workspace-upload"
    upload_root = workspace_root / "chat_files" / "uploads"
    runs_root = workspace_root / "chat_files" / "code_runs"
    upload_root.mkdir(parents=True)
    runs_root.mkdir(parents=True)
    outside_file = tmp_path / "outside.csv"
    outside_file.write_text("must survive")
    outside_dir = tmp_path / "outside-run"
    outside_dir.mkdir()
    (outside_dir / "must-survive.txt").write_text("keep")
    attachment_id = "9" * 32
    run_id = "a" * 12
    upload_link = upload_root / f"{attachment_id}_outside.csv"
    run_link = runs_root / run_id
    upload_link.symlink_to(outside_file)
    run_link.symlink_to(outside_dir, target_is_directory=True)

    result = cleanup_workspace_artifacts(
        workspace_root,
        _refs(attachments=[attachment_id], runs=[run_id]),
    )

    assert result.ok is True
    assert result.deleted_files == 2
    assert result.deleted_run_directories == 0
    assert not upload_link.exists()
    assert not run_link.exists()
    assert outside_file.read_text() == "must survive"
    assert (outside_dir / "must-survive.txt").read_text() == "keep"


def test_cleanup_rejects_symlinked_workspace_root(tmp_path):
    outside_root = tmp_path / "outside-workspace"
    upload_root = outside_root / "chat_files" / "uploads"
    upload_root.mkdir(parents=True)
    attachment_id = "f" * 32
    outside_file = upload_root / f"{attachment_id}_private.csv"
    outside_file.write_text("keep")
    workspace_link = tmp_path / "workspace-link"
    workspace_link.symlink_to(outside_root, target_is_directory=True)

    with pytest.raises(ArtifactCleanupError):
        cleanup_workspace_artifacts(
            workspace_link,
            _refs(attachments=[attachment_id]),
        )

    assert outside_file.read_text() == "keep"


def test_cleanup_is_idempotent_and_strict_mode_reports_unsafe_entries(tmp_path):
    workspace_root = tmp_path / "workspace-upload"
    upload_root = workspace_root / "chat_files" / "uploads"
    upload_root.mkdir(parents=True)
    attachment_id = "b" * 32
    unexpected_directory = upload_root / f"{attachment_id}_not-a-file"
    unexpected_directory.mkdir()
    references = _refs(attachments=[attachment_id])

    non_strict = cleanup_workspace_artifacts(
        workspace_root,
        references,
        strict=False,
    )

    assert non_strict.ok is False
    assert non_strict.deleted_files == 0
    assert unexpected_directory.exists()
    with pytest.raises(ArtifactCleanupError) as raised:
        cleanup_workspace_artifacts(workspace_root, references)
    assert raised.value.result.errors

    unexpected_directory.rmdir()
    first_retry = cleanup_workspace_artifacts(workspace_root, references)
    second_retry = cleanup_workspace_artifacts(workspace_root, references)
    assert first_retry.ok is True
    assert second_retry == first_retry


def test_store_plan_excludes_references_still_owned_by_retained_conversation(
    tmp_path,
):
    store = _history_store(tmp_path)
    shared_attachment = "c" * 32
    private_attachment = "d" * 32
    shared_run = "e" * 12
    private_run = "f" * 12
    target_id = _commit_turn(
        store,
        scope_key="artifact-test-workspace:target",
        turn_id="target-turn",
        fingerprint="1" * 64,
        metadata={
            "response_payload": {
                "attachments": [
                    {"id": shared_attachment},
                    {"id": private_attachment},
                ],
                "result": {
                    "images": [
                        f"/code_pics/{shared_run}_shared.png",
                        f"/code_pics/{private_run}_private.png",
                    ]
                },
            }
        },
    )
    retained_id = _commit_turn(
        store,
        scope_key="artifact-test-workspace:retained",
        turn_id="retained-turn",
        fingerprint="2" * 64,
        metadata={
            "response_payload": {
                "attachments": [{"id": shared_attachment}],
                "result": {
                    "images": [f"/code_pics/{shared_run}_shared.png"]
                },
            }
        },
    )

    plan = store.artifact_cleanup_plan([target_id])

    assert plan.safe is True
    assert plan.conversation_ids == (target_id,)
    assert plan.target.attachment_ids == {
        shared_attachment,
        private_attachment,
    }
    assert plan.retained.attachment_ids == {shared_attachment}
    assert plan.exclusive.attachment_ids == {private_attachment}
    assert plan.exclusive.run_ids == {private_run}
    assert plan.exclusive.image_names == {f"{private_run}_private.png"}
    assert retained_id not in plan.conversation_ids


def test_store_kb_plan_covers_all_target_conversations_and_keeps_other_kb_refs(
    tmp_path,
):
    store = _history_store(tmp_path)
    target_kb = "kb_target"
    other_kb = "kb_other"
    shared_attachment = "1" * 32
    first_attachment = "2" * 32
    second_attachment = "3" * 32
    first_id = _commit_turn(
        store,
        scope_key="artifact-test-workspace:kb:target:first",
        turn_id="first-turn",
        fingerprint="3" * 64,
        knowledge_base_ids=[target_kb],
        metadata={
            "response_payload": {
                "attachments": [
                    {"id": shared_attachment},
                    {"id": first_attachment},
                ]
            }
        },
    )
    second_id = _commit_turn(
        store,
        scope_key="artifact-test-workspace:kb:target:second",
        turn_id="second-turn",
        fingerprint="4" * 64,
        knowledge_base_ids=[target_kb, other_kb],
        metadata={
            "response_payload": {
                "attachments": [{"id": second_attachment}]
            }
        },
    )
    retained_id = _commit_turn(
        store,
        scope_key="artifact-test-workspace:kb:other",
        turn_id="other-turn",
        fingerprint="5" * 64,
        knowledge_base_ids=[other_kb],
        metadata={
            "response_payload": {
                "attachments": [{"id": shared_attachment}]
            }
        },
    )

    plan = store.artifact_cleanup_plan_by_knowledge_base(target_kb)

    assert plan.safe is True
    assert set(plan.conversation_ids) == {first_id, second_id}
    assert retained_id not in plan.conversation_ids
    assert plan.exclusive.attachment_ids == {
        first_attachment,
        second_attachment,
    }
    assert shared_attachment in plan.target.attachment_ids
    assert shared_attachment in plan.retained.attachment_ids


def test_store_plan_fails_closed_when_any_ownership_metadata_is_corrupt(tmp_path):
    store = _history_store(tmp_path)
    attachment_id = "4" * 32
    target_id = _commit_turn(
        store,
        scope_key="artifact-test-workspace:unsafe-target",
        turn_id="unsafe-target-turn",
        fingerprint="6" * 64,
        metadata={
            "response_payload": {"attachments": [{"id": attachment_id}]}
        },
    )
    retained_id = _commit_turn(
        store,
        scope_key="artifact-test-workspace:unsafe-retained",
        turn_id="unsafe-retained-turn",
        fingerprint="7" * 64,
        metadata={},
    )
    with sqlite3.connect(store.path) as conn:
        conn.execute(
            "UPDATE messages SET metadata = ? "
            "WHERE conversation_id = ? AND role = 'assistant'",
            ("{not-json", retained_id),
        )

    plan = store.artifact_cleanup_plan([target_id])

    assert plan.safe is False
    assert plan.target.attachment_ids == {attachment_id}
    assert plan.exclusive.empty is True


def test_service_deletes_shared_artifacts_only_after_last_owner(tmp_path):
    store = _history_store(tmp_path)
    service = ConversationService(
        store,
        pending_store=PendingTurnResultStore(),
        memory_store=ConversationMemoryStore(),
    )
    workspace_root = tmp_path / "workspace-upload"
    upload_root = workspace_root / "chat_files" / "uploads"
    pics_root = workspace_root / "chat_files" / "pics"
    runs_root = workspace_root / "chat_files" / "code_runs"
    upload_root.mkdir(parents=True)
    pics_root.mkdir(parents=True)
    runs_root.mkdir(parents=True)
    shared_attachment = "5" * 32
    shared_run = "6" * 12
    upload_file = upload_root / f"{shared_attachment}_shared.csv"
    image_file = pics_root / f"{shared_run}_shared.png"
    run_dir = runs_root / shared_run
    upload_file.write_text("shared")
    image_file.write_text("shared")
    run_dir.mkdir()
    metadata = {
        "response_payload": {
            "attachments": [{"id": shared_attachment}],
            "result": {"images": [f"/code_pics/{image_file.name}"]},
        }
    }
    first_id = _commit_turn(
        store,
        scope_key="artifact-test-workspace:first-owner",
        turn_id="first-owner-turn",
        fingerprint="8" * 64,
        metadata=metadata,
    )
    second_id = _commit_turn(
        store,
        scope_key="artifact-test-workspace:second-owner",
        turn_id="second-owner-turn",
        fingerprint="9" * 64,
        metadata=metadata,
    )

    assert service.delete_conversation(
        first_id,
        workspace_upload_folder=str(workspace_root),
    ) is True
    assert upload_file.exists()
    assert image_file.exists()
    assert run_dir.exists()
    assert store.get(first_id) is None
    assert store.get(second_id) is not None

    assert service.delete_conversation(
        second_id,
        workspace_upload_folder=str(workspace_root),
    ) is True
    assert not upload_file.exists()
    assert not image_file.exists()
    assert not run_dir.exists()
    assert store.get(second_id) is None


def test_service_checkpoints_cleanup_when_artifact_cleanup_fails(tmp_path):
    store = _history_store(tmp_path)
    service = ConversationService(
        store,
        pending_store=PendingTurnResultStore(),
        memory_store=ConversationMemoryStore(),
    )
    workspace_root = tmp_path / "workspace-upload"
    upload_root = workspace_root / "chat_files" / "uploads"
    upload_root.mkdir(parents=True)
    attachment_id = "7" * 32
    # A directory where an owned upload file is expected is treated as an
    # unsafe filesystem state and must block the durable delete.
    (upload_root / f"{attachment_id}_unexpected-directory").mkdir()
    history_id = _commit_turn(
        store,
        scope_key="artifact-test-workspace:cleanup-failure",
        turn_id="cleanup-failure-turn",
        fingerprint="a" * 64,
        metadata={
            "response_payload": {"attachments": [{"id": attachment_id}]}
        },
    )

    with pytest.raises(ConversationHistoryError) as raised:
        service.delete_conversation(
            history_id,
            workspace_upload_folder=str(workspace_root),
        )

    assert raised.value.code == "artifact_cleanup_failed"
    assert store.get(history_id) is None

    (upload_root / f"{attachment_id}_unexpected-directory").rmdir()
    assert service.delete_conversation(
        history_id,
        workspace_upload_folder=str(workspace_root),
    ) is True
