"""Safe lifecycle helpers for Code Interpreter conversation artifacts.

Conversation messages store only public identifiers for chat uploads and
generated images.  This module turns those identifiers into a conservative
deletion plan and applies it below one workspace upload root.  It deliberately
does not accept host paths from message metadata.
"""

from __future__ import annotations

import logging
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


log = logging.getLogger(__name__)

ATTACHMENT_ID_RE = re.compile(r"^[0-9a-f]{32}$")
RUN_ID_RE = re.compile(r"^[0-9a-f]{12}$")
IMAGE_NAME_RE = re.compile(r"^(?P<run_id>[0-9a-f]{12})_[A-Za-z0-9._-]+\.png$")


@dataclass(frozen=True)
class ConversationArtifactReferences:
    """Validated public identifiers owned by one or more conversations."""

    attachment_ids: frozenset[str] = frozenset()
    image_names: frozenset[str] = frozenset()
    run_ids: frozenset[str] = frozenset()

    def union(
        self,
        other: "ConversationArtifactReferences",
    ) -> "ConversationArtifactReferences":
        return ConversationArtifactReferences(
            attachment_ids=self.attachment_ids | other.attachment_ids,
            image_names=self.image_names | other.image_names,
            run_ids=self.run_ids | other.run_ids,
        )

    @property
    def empty(self) -> bool:
        return not (self.attachment_ids or self.image_names or self.run_ids)


@dataclass(frozen=True)
class ConversationArtifactDeletionPlan:
    """References that are exclusive to a set of conversations."""

    conversation_ids: tuple[str, ...]
    target: ConversationArtifactReferences
    retained: ConversationArtifactReferences
    exclusive: ConversationArtifactReferences
    safe: bool = True


@dataclass(frozen=True)
class ArtifactCleanupResult:
    deleted_files: int = 0
    deleted_run_directories: int = 0
    errors: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.errors


class ArtifactCleanupError(RuntimeError):
    """Raised when a selective artifact cleanup cannot finish safely."""

    def __init__(self, result: ArtifactCleanupResult):
        self.result = result
        super().__init__("; ".join(result.errors) or "artifact cleanup failed")


def references_from_history_metadata(metadata: object) -> ConversationArtifactReferences:
    """Extract artifact identifiers from one persisted message metadata value.

    Only the server-generated ``response_payload`` shape is inspected.  Any
    client-controlled path-like value outside that structure is ignored.
    """

    if not isinstance(metadata, dict):
        return ConversationArtifactReferences()
    payload = metadata.get("response_payload")
    if not isinstance(payload, dict):
        return ConversationArtifactReferences()
    return references_from_response_payload(payload)


def references_from_response_payload(payload: object) -> ConversationArtifactReferences:
    """Extract validated identifiers from a Code Interpreter response."""

    if not isinstance(payload, dict):
        return ConversationArtifactReferences()

    attachment_ids: set[str] = set()
    for attachment in payload.get("attachments") or ():
        if not isinstance(attachment, dict):
            continue
        attachment_id = str(
            attachment.get("id") or attachment.get("file_id") or ""
        ).strip().lower()
        if ATTACHMENT_ID_RE.fullmatch(attachment_id):
            attachment_ids.add(attachment_id)

    image_names: set[str] = set()
    run_ids: set[str] = set()
    interpreter_result = payload.get("result")
    if isinstance(interpreter_result, dict):
        run_id = str(interpreter_result.get("run_id") or "").strip().lower()
        if RUN_ID_RE.fullmatch(run_id):
            run_ids.add(run_id)
        for image in interpreter_result.get("images") or ():
            image_name = _public_image_name(image)
            if image_name is None:
                continue
            image_names.add(image_name)
            match = IMAGE_NAME_RE.fullmatch(image_name)
            if match is not None:
                # Older persisted responses predate the explicit run_id.  A
                # generated image name still lets us identify its run safely.
                run_ids.add(match.group("run_id"))

    return ConversationArtifactReferences(
        attachment_ids=frozenset(attachment_ids),
        image_names=frozenset(image_names),
        run_ids=frozenset(run_ids),
    )


def exclusive_references(
    target: ConversationArtifactReferences,
    retained: ConversationArtifactReferences,
) -> ConversationArtifactReferences:
    """Return target references that no retained conversation still uses.

    Sharing any image from a run protects the whole run.  This conservative
    rule avoids removing unlisted output files belonging to that same run.
    """

    exclusive_run_ids = target.run_ids - retained.run_ids
    exclusive_images = set()
    for image_name in target.image_names - retained.image_names:
        run_id = _run_id_for_image(image_name)
        if run_id not in retained.run_ids:
            exclusive_images.add(image_name)
    return ConversationArtifactReferences(
        attachment_ids=target.attachment_ids - retained.attachment_ids,
        image_names=frozenset(exclusive_images),
        run_ids=frozenset(exclusive_run_ids),
    )


def cleanup_workspace_artifacts(
    workspace_upload_folder: str | Path,
    references: ConversationArtifactReferences,
    *,
    strict: bool = True,
) -> ArtifactCleanupResult:
    """Remove validated artifacts below ``workspace_upload_folder`` only.

    Missing entries are treated as already cleaned, making retries
    idempotent.  In strict mode every filesystem error is reported after all
    independent targets have been attempted.
    """

    workspace_root = Path(workspace_upload_folder)
    chat_root = workspace_root / "chat_files"
    upload_root = chat_root / "uploads"
    pics_root = chat_root / "pics"
    runs_root = chat_root / "code_runs"

    deleted_files = 0
    deleted_run_directories = 0
    errors: list[str] = []

    if not _artifact_ancestors_are_safe(workspace_root, chat_root, errors):
        result = ArtifactCleanupResult(errors=tuple(errors))
        if strict:
            raise ArtifactCleanupError(result)
        return result

    deleted_files += _cleanup_upload_artifacts(
        upload_root,
        references,
        errors,
    )
    deleted_files += _cleanup_image_artifacts(
        pics_root,
        references,
        errors,
    )
    run_files, deleted_run_directories = _cleanup_run_artifacts(
        runs_root,
        references,
        errors,
    )
    deleted_files += run_files

    result = ArtifactCleanupResult(
        deleted_files=deleted_files,
        deleted_run_directories=deleted_run_directories,
        errors=tuple(errors),
    )
    if strict and not result.ok:
        raise ArtifactCleanupError(result)
    return result


def _artifact_ancestors_are_safe(
    workspace_root: Path,
    chat_root: Path,
    errors: list[str],
) -> bool:
    """Reject ancestor symlinks before traversing artifact directories."""

    roots = (
        (workspace_root, "workspace upload root"),
        (chat_root, "chat_files"),
    )
    for root, label in roots:
        try:
            if root.is_symlink():
                errors.append(f"unsafe symlinked artifact directory: {label}")
        except OSError as exc:
            errors.append(f"{label}: {exc}")
    return not errors


def _cleanup_upload_artifacts(
    upload_root: Path,
    references: ConversationArtifactReferences,
    errors: list[str],
) -> int:
    deleted = 0
    candidates = _directory_entries(upload_root, "uploads", errors)
    for attachment_id in sorted(references.attachment_ids):
        if not ATTACHMENT_ID_RE.fullmatch(attachment_id):
            continue
        prefix = f"{attachment_id}_"
        for candidate in candidates:
            if candidate.name.startswith(prefix):
                deleted += _unlink_file_like(candidate, errors)
    return deleted


def _cleanup_image_artifacts(
    pics_root: Path,
    references: ConversationArtifactReferences,
    errors: list[str],
) -> int:
    deleted = 0
    image_names = {
        image_name
        for image_name in references.image_names
        if IMAGE_NAME_RE.fullmatch(image_name)
    }
    for candidate in _directory_entries(pics_root, "pics", errors):
        candidate_run_id = _run_id_for_image(candidate.name)
        is_named_image = candidate.name in image_names
        belongs_to_run = candidate_run_id in references.run_ids
        if is_named_image or belongs_to_run:
            deleted += _unlink_file_like(candidate, errors)
    return deleted


def _cleanup_run_artifacts(
    runs_root: Path,
    references: ConversationArtifactReferences,
    errors: list[str],
) -> tuple[int, int]:
    deleted_files = 0
    deleted_directories = 0
    if not _artifact_directory_is_safe(runs_root, "code_runs", errors):
        return deleted_files, deleted_directories

    for run_id in sorted(references.run_ids):
        if not RUN_ID_RE.fullmatch(run_id):
            continue
        run_path = runs_root / run_id
        try:
            if run_path.is_symlink() or run_path.is_file():
                run_path.unlink()
                deleted_files += 1
            elif run_path.is_dir():
                shutil.rmtree(run_path)
                deleted_directories += 1
        except OSError as exc:
            errors.append(f"code_runs/{run_id}: {exc}")
    return deleted_files, deleted_directories


def _public_image_name(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
        return None
    prefix = "/code_pics/"
    if not parsed.path.startswith(prefix):
        return None
    image_name = parsed.path[len(prefix):]
    if "/" in image_name or not IMAGE_NAME_RE.fullmatch(image_name):
        return None
    return image_name


def _run_id_for_image(image_name: str) -> str | None:
    match = IMAGE_NAME_RE.fullmatch(str(image_name or ""))
    return match.group("run_id") if match is not None else None


def _unlink_file_like(path: Path, errors: list[str]) -> int:
    """Unlink a regular file or symlink without following it."""

    try:
        if path.is_symlink() or path.is_file():
            path.unlink()
            return 1
        if path.exists():
            errors.append(f"unexpected non-file artifact: {path.name}")
    except OSError as exc:
        errors.append(f"{path.name}: {exc}")
    return 0


def _directory_entries(
    path: Path,
    label: str,
    errors: list[str],
) -> tuple[Path, ...]:
    if not _artifact_directory_is_safe(path, label, errors):
        return ()
    if not path.exists():
        return ()
    try:
        return tuple(path.iterdir())
    except OSError as exc:
        errors.append(f"{label}: {exc}")
        return ()


def _artifact_directory_is_safe(
    path: Path,
    label: str,
    errors: list[str],
) -> bool:
    """Reject symlinked/non-directory artifact roots before traversal."""

    try:
        for candidate in (path.parent, path):
            if candidate.is_symlink():
                errors.append(f"unsafe symlinked artifact directory: {label}")
                return False
        if path.exists() and not path.is_dir():
            errors.append(f"unexpected non-directory artifact root: {label}")
            return False
    except OSError as exc:
        errors.append(f"{label}: {exc}")
        return False
    return True
