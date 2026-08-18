"""Persistent conversation history backed by a per-workspace SQLite database.

This module implements Phase 0/1 of the conversation-history roadmap:
fresh-install schema validation, the schema for
conversations / messages / turn reservations, and the
:class:`ConversationHistoryStore` that coordinates atomic turn completion,
idempotent retries, summary compare-and-swap, pagination and quota.

The store is intentionally independent from the warm
:class:`ConversationMemoryStore`. The application layer
(:class:`ConversationService`) decides when to hydrate the warm memory from
the durable history.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

from utils.conversation_artifacts import (
    ConversationArtifactDeletionPlan,
    ConversationArtifactReferences,
    exclusive_references,
    references_from_history_metadata,
)

log = logging.getLogger(__name__)


SCHEMA_VERSION = 1

TITLE_MAX_LEN = 80
TITLE_RENAME_MAX_LEN = 120
_CLIENT_CONVERSATION_ID_MAX_LEN = 80
_TURN_ID_MAX_LEN = 80
_REQUEST_FINGERPRINT_LEN = 64
_LEASE_TOKEN_LEN = 32
_SCOPE_KINDS = ("default", "kb", "multi")
_CONVERSATION_STATUSES = ("active", "archived")
_EXPECTED_TABLES = {
    "conversations",
    "conversation_knowledge_bases",
    "turn_requests",
    "messages",
    "conversation_artifact_cleanup_outbox",
}


def _now() -> float:
    return time.time()


def _new_id() -> str:
    return str(uuid.uuid4())


def _new_lease_token() -> str:
    return secrets.token_hex(_LEASE_TOKEN_LEN // 2)


def _utf8_bytes(value: str) -> int:
    return len((value or "").encode("utf-8"))


def _json_bytes(value: Any) -> int:
    try:
        return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError):
        return 0


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _validate_json_field(value: Any, *, max_bytes: int, default: Any) -> Any:
    if value is None:
        return default
    try:
        encoded = json.dumps(value, ensure_ascii=False)
        json.loads(encoded)
    except (TypeError, ValueError) as exc:
        raise ConversationHistoryError("invalid JSON field") from exc
    if len(encoded.encode("utf-8")) > max_bytes:
        raise ConversationHistoryError("JSON field exceeds size limit")
    return value


class ConversationHistoryError(RuntimeError):
    """Base error for conversation-history store failures."""

    def __init__(self, message: str, *, code: str = "history_error"):
        super().__init__(message)
        self.code = code


class TurnConflictError(ConversationHistoryError):
    """Raised when a turn_id collides with a different request fingerprint."""

    def __init__(self, message: str = "turn_id_conflict"):
        super().__init__(message, code="turn_id_conflict")


class TurnInProgressError(ConversationHistoryError):
    """Raised when a concurrent generation holds the turn lease."""

    def __init__(self, message: str = "turn_in_progress", retry_after: int = 5):
        super().__init__(message, code="turn_in_progress")
        self.retry_after = retry_after


class ContinuityError(ConversationHistoryError):
    """Raised when parent_turn_id does not match the last complete turn.

    ``expected_parent_turn_id`` is the turn_id the store expected the
    caller to reference, or ``None`` when no complete turn exists yet.
    """

    def __init__(
        self,
        message: str = "continuity_error",
        *,
        expected_parent_turn_id: Optional[str] = None,
    ):
        super().__init__(message, code="continuity_error")
        self.expected_parent_turn_id = expected_parent_turn_id


class QuotaExceededError(ConversationHistoryError):
    """Raised when a workspace or conversation quota would be exceeded."""

    def __init__(self, message: str = "quota_exceeded"):
        super().__init__(message, code="quota_exceeded")


class ConversationNotFoundError(ConversationHistoryError):
    def __init__(self, message: str = "conversation_not_found"):
        super().__init__(message, code="conversation_not_found")


class ConversationArchivedError(ConversationHistoryError):
    def __init__(self, message: str = "conversation_archived"):
        super().__init__(message, code="conversation_archived")


class IncompatibleConversationSchemaError(ConversationHistoryError):
    def __init__(self):
        super().__init__(
            "Unsupported conversation database schema; reset the local database",
            code="incompatible_conversation_schema",
        )


# ---------------------------------------------------------------------------
# Connection + fresh schema initialization
# ---------------------------------------------------------------------------

_SCHEMA_SQL = """
CREATE TABLE conversations (
  id                       TEXT PRIMARY KEY,
  client_conversation_id   TEXT NOT NULL,
  scope_key                TEXT NOT NULL UNIQUE,
  scope_kind               TEXT NOT NULL
                             CHECK (scope_kind IN ('default', 'kb', 'multi')),
  title                    TEXT NOT NULL DEFAULT '',
  status                   TEXT NOT NULL DEFAULT 'active'
                             CHECK (status IN ('active', 'archived')),
  agent_id                 TEXT NOT NULL DEFAULT '',
  agent_name               TEXT NOT NULL DEFAULT '',
  provider_id              TEXT NOT NULL DEFAULT '',
  model_id                 TEXT NOT NULL DEFAULT '',
  prompt_ref               TEXT NOT NULL DEFAULT '{}',
  response_language        TEXT NOT NULL DEFAULT 'auto',
  summary                  TEXT NOT NULL DEFAULT '',
  summary_version          INTEGER NOT NULL DEFAULT 0,
  summary_through_sequence INTEGER NOT NULL DEFAULT 0,
  message_count            INTEGER NOT NULL DEFAULT 0,
  payload_bytes            INTEGER NOT NULL DEFAULT 0,
  last_turn_id             TEXT,
  created_at               REAL NOT NULL,
  updated_at               REAL NOT NULL,
  archived_at              REAL
);

CREATE INDEX idx_conversations_updated
  ON conversations(updated_at DESC);
CREATE INDEX idx_conversations_status_updated
  ON conversations(status, updated_at DESC);

CREATE TABLE conversation_knowledge_bases (
  conversation_id          TEXT NOT NULL,
  knowledge_base_id        TEXT NOT NULL,
  is_selected              INTEGER NOT NULL DEFAULT 1
                             CHECK (is_selected IN (0, 1)),
  first_used_at            REAL NOT NULL,
  last_used_at             REAL NOT NULL,
  PRIMARY KEY (conversation_id, knowledge_base_id),
  FOREIGN KEY (conversation_id)
    REFERENCES conversations(id) ON DELETE CASCADE
);
CREATE INDEX idx_conversation_kb
  ON conversation_knowledge_bases(knowledge_base_id, conversation_id);

CREATE TABLE turn_requests (
  conversation_id          TEXT NOT NULL,
  turn_id                  TEXT NOT NULL,
  parent_turn_id           TEXT,
  request_fingerprint      TEXT NOT NULL,
  status                   TEXT NOT NULL
                             CHECK (status IN
                                ('generating', 'ready', 'complete', 'failed')),
  result_digest            TEXT,
  lease_token              TEXT,
  lease_expires_at         REAL,
  created_at               REAL NOT NULL,
  updated_at               REAL NOT NULL,
  PRIMARY KEY (conversation_id, turn_id),
  FOREIGN KEY (conversation_id)
    REFERENCES conversations(id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX idx_turn_requests_linear_parent
  ON turn_requests(conversation_id, COALESCE(parent_turn_id, ''))
  WHERE status IN ('generating', 'ready', 'complete');

CREATE TABLE messages (
  id                       INTEGER PRIMARY KEY AUTOINCREMENT,
  conversation_id          TEXT NOT NULL,
  turn_id                  TEXT NOT NULL,
  role                     TEXT NOT NULL
                             CHECK (role IN ('user', 'assistant')),
  message_type             TEXT NOT NULL DEFAULT 'text',
  content                  TEXT NOT NULL,
  sequence                 INTEGER NOT NULL,
  sources                  TEXT NOT NULL DEFAULT '[]',
  metadata                 TEXT NOT NULL DEFAULT '{}',
  payload_bytes            INTEGER NOT NULL DEFAULT 0,
  created_at               REAL NOT NULL,
  FOREIGN KEY (conversation_id)
    REFERENCES conversations(id) ON DELETE CASCADE,
  UNIQUE (conversation_id, sequence),
  UNIQUE (conversation_id, turn_id, role)
);
CREATE INDEX idx_messages_conversation_sequence
  ON messages(conversation_id, sequence);

CREATE TABLE conversation_artifact_cleanup_outbox (
  cleanup_key              TEXT PRIMARY KEY,
  references_json          TEXT NOT NULL,
  created_at               REAL NOT NULL
);
"""


def _apply_pragmas(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA secure_delete=ON")


def get_history_connection(db_path: str | Path) -> sqlite3.Connection:
    """Open a connection to a per-workspace conversations database."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    _apply_pragmas(conn)
    for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
        try:
            if candidate.exists():
                os.chmod(candidate, 0o600)
        except OSError:
            pass
    try:
        _ensure_schema_with_connection(conn)
    except Exception:
        conn.close()
        raise
    return conn


def _ensure_schema_with_connection(conn: sqlite3.Connection) -> None:
    current = int(conn.execute("PRAGMA user_version").fetchone()[0])
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    if current == SCHEMA_VERSION:
        if tables != _EXPECTED_TABLES:
            raise IncompatibleConversationSchemaError()
        return
    if current != 0 or tables:
        raise IncompatibleConversationSchemaError()
    conn.executescript(_SCHEMA_SQL)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()


def ensure_schema(db_path: str | Path) -> None:
    """Create the current schema or validate an existing current database."""
    conn = get_history_connection(db_path)
    conn.close()


# ---------------------------------------------------------------------------
# Row serialization
# ---------------------------------------------------------------------------

def _conversation_row(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "client_conversation_id": row["client_conversation_id"],
        "scope_key": row["scope_key"],
        "scope_kind": row["scope_kind"],
        "title": row["title"],
        "status": row["status"],
        "agent_id": row["agent_id"],
        "agent_name": row["agent_name"],
        "provider_id": row["provider_id"],
        "model_id": row["model_id"],
        "prompt_ref": json.loads(row["prompt_ref"] or "{}"),
        "response_language": row["response_language"],
        "summary": row["summary"],
        "summary_version": row["summary_version"],
        "summary_through_sequence": row["summary_through_sequence"],
        "message_count": row["message_count"],
        "payload_bytes": row["payload_bytes"],
        "last_turn_id": row["last_turn_id"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "archived_at": row["archived_at"],
    }


def _turn_row(row: sqlite3.Row) -> dict:
    return {
        "conversation_id": row["conversation_id"],
        "turn_id": row["turn_id"],
        "parent_turn_id": row["parent_turn_id"],
        "request_fingerprint": row["request_fingerprint"],
        "status": row["status"],
        "result_digest": row["result_digest"],
        "lease_token": row["lease_token"],
        "lease_expires_at": row["lease_expires_at"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _message_row(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "conversation_id": row["conversation_id"],
        "turn_id": row["turn_id"],
        "role": row["role"],
        "message_type": row["message_type"],
        "content": row["content"],
        "sequence": row["sequence"],
        "sources": json.loads(row["sources"] or "[]"),
        "metadata": json.loads(row["metadata"] or "{}"),
        "payload_bytes": row["payload_bytes"],
        "created_at": row["created_at"],
    }


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

class ConversationHistoryStore:
    """Durable, per-workspace conversation history backed by SQLite.

    Each workspace gets its own SQLite database at:
    ``{workspace_data_dir}/{workspace_id}/conversations.db``

    Each method opens a short connection, performs its work, and closes.
    Writes that mutate turn state use ``BEGIN IMMEDIATE`` so that concurrent
    retries are serialized at the database level.
    """

    def __init__(
        self,
        workspace_id: str,
        *,
        workspace_data_dir: Optional[str | Path] = None,
        max_conversations: int = 200,
        max_pending_turns: int = 100,
        max_messages_per_conversation: int = 2000,
        max_conversation_bytes: int = 33_554_432,
        max_message_chars: int = 50_000,
        max_sources_bytes_per_turn: int = 262_144,
        max_metadata_bytes_per_turn: int = 65_536,
        max_history_bytes: int = 268_435_456,
        retention_days: int = 90,
        lease_seconds: int = 900,
        incomplete_turn_retention_days: int = 7,
    ):
        self.workspace_id = workspace_id
        self.workspace_data_dir = Path(workspace_data_dir or "app/data/workspaces")
        self.path = self.workspace_data_dir / workspace_id / "conversations.db"
        self.max_conversations = max_conversations
        self.max_pending_turns = max_pending_turns
        self.max_messages_per_conversation = max_messages_per_conversation
        self.max_conversation_bytes = max_conversation_bytes
        self.max_message_chars = max_message_chars
        self.max_sources_bytes_per_turn = max_sources_bytes_per_turn
        self.max_metadata_bytes_per_turn = max_metadata_bytes_per_turn
        self.max_history_bytes = max_history_bytes
        self.retention_days = retention_days
        self.lease_seconds = lease_seconds
        self.incomplete_turn_retention_days = incomplete_turn_retention_days
        self.path.parent.mkdir(parents=True, exist_ok=True)
        ensure_schema(self.path)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = get_history_connection(self.path)
        try:
            yield conn
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Turn lifecycle
    # ------------------------------------------------------------------

    def begin_turn(
        self,
        *,
        client_conversation_id: str,
        scope_key: str,
        scope_kind: str,
        turn_id: str,
        parent_turn_id: Optional[str],
        request_fingerprint: str,
        selected_knowledge_base_ids: Optional[list[str]] = None,
        agent_id: str = "",
        agent_name: str = "",
        provider_id: str = "",
        model_id: str = "",
        prompt_ref: Optional[dict] = None,
        response_language: str = "auto",
    ) -> dict:
        """Begin or resume a turn.

        Returns a dict with ``status`` describing the outcome:

        * ``new``        - a reservation was created;
        * ``generating`` - an existing reservation is still active;
        * ``ready``      - a staged result is available for replay;
        * ``complete``   - the turn already finished (replay payload);
        * ``conflict``   - same turn_id with a different fingerprint.
        """
        if scope_kind not in _SCOPE_KINDS:
            raise ConversationHistoryError(f"invalid scope_kind: {scope_kind}")
        if not turn_id or len(turn_id) > _TURN_ID_MAX_LEN:
            raise ConversationHistoryError("invalid turn_id")
        if not request_fingerprint or len(request_fingerprint) > _REQUEST_FINGERPRINT_LEN:
            raise ConversationHistoryError("invalid request_fingerprint")

        now = _now()
        lease_token = _new_lease_token()
        lease_expires = now + self.lease_seconds

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                self._cleanup_expired_locked(conn, now)
                self._enforce_conversation_quota_for_new_scope(conn, scope_key)
                conversation = self._get_or_create_conversation(
                    conn,
                    client_conversation_id=client_conversation_id,
                    scope_key=scope_key,
                    scope_kind=scope_kind,
                    agent_id=agent_id,
                    agent_name=agent_name,
                    provider_id=provider_id,
                    model_id=model_id,
                    prompt_ref=prompt_ref or {},
                    response_language=response_language,
                    now=now,
                )
                conversation_id = conversation["id"]

                # Record KB ownership as soon as the reservation exists, not
                # only after the answer commits.  This lets a destructive KB
                # delete find and purge ready/generating stubs (and their
                # staged payloads) after a worker crash.  New associations are
                # deliberately not marked selected until ``complete_turn`` so
                # a failed attempt does not change the last committed UI
                # selection.
                self._associate_reservation_knowledge_bases(
                    conn,
                    conversation_id,
                    selected_knowledge_base_ids or [],
                    now,
                )

                existing = conn.execute(
                    "SELECT * FROM turn_requests WHERE conversation_id = ? AND turn_id = ?",
                    (conversation_id, turn_id),
                ).fetchone()

                if existing is not None:
                    result = self._handle_existing_turn(
                        conn, existing, request_fingerprint, now
                    )
                    result["conversation"] = conversation
                    conn.commit()
                    return result

                self._assert_linear_parent(conn, conversation_id, parent_turn_id)
                self._enforce_pending_quota(conn)

                try:
                    conn.execute(
                        """
                        INSERT INTO turn_requests
                            (conversation_id, turn_id, parent_turn_id,
                             request_fingerprint, status, lease_token,
                             lease_expires_at, created_at, updated_at)
                        VALUES (?, ?, ?, ?, 'generating', ?, ?, ?, ?)
                        """,
                        (
                            conversation_id,
                            turn_id,
                            parent_turn_id,
                            request_fingerprint,
                            lease_token,
                            lease_expires,
                            now,
                            now,
                        ),
                    )
                except sqlite3.IntegrityError as exc:
                    raise TurnInProgressError(
                        "a concurrent turn already claims this parent"
                    ) from exc
                conn.commit()
                return {
                    "status": "new",
                    "lease_token": lease_token,
                    "lease_expires_at": lease_expires,
                    "conversation": conversation,
                }
            except Exception:
                conn.rollback()
                raise

    def _handle_existing_turn(
        self,
        conn: sqlite3.Connection,
        existing: sqlite3.Row,
        request_fingerprint: str,
        now: float,
    ) -> dict:
        if existing["request_fingerprint"] != request_fingerprint:
            raise TurnConflictError()

        status = existing["status"]

        if status == "complete":
            messages = self._messages_for_turn(conn, existing["conversation_id"], existing["turn_id"])
            return {
                "status": "complete",
                "replayed": True,
                "messages": messages,
            }

        if status == "ready":
            return {
                "status": "ready",
                "replayed": True,
                "result_digest": existing["result_digest"],
                "lease_token": existing["lease_token"],
                "lease_expires_at": existing["lease_expires_at"],
            }

        if status == "failed":
            # A failed reservation releases its parent slot. Another turn may
            # have completed from that parent in the meantime, so retrying the
            # old row must re-check continuity before changing it back to an
            # indexed (``generating``) status.
            self._assert_linear_parent(
                conn,
                existing["conversation_id"],
                existing["parent_turn_id"],
            )
            lease_token = _new_lease_token()
            lease_expires = now + self.lease_seconds
            conn.execute(
                """
                UPDATE turn_requests
                   SET status = 'generating', lease_token = ?,
                       lease_expires_at = ?, updated_at = ?
                 WHERE conversation_id = ? AND turn_id = ?
                """,
                (
                    lease_token,
                    lease_expires,
                    now,
                    existing["conversation_id"],
                    existing["turn_id"],
                ),
            )
            return {
                "status": "new",
                "lease_token": lease_token,
                "lease_expires_at": lease_expires,
            }

        # generating
        if existing["lease_expires_at"] and existing["lease_expires_at"] < now:
            lease_token = _new_lease_token()
            lease_expires = now + self.lease_seconds
            conn.execute(
                """
                UPDATE turn_requests
                   SET lease_token = ?, lease_expires_at = ?, updated_at = ?
                 WHERE conversation_id = ? AND turn_id = ?
                """,
                (
                    lease_token,
                    lease_expires,
                    now,
                    existing["conversation_id"],
                    existing["turn_id"],
                ),
            )
            return {
                "status": "new",
                "lease_token": lease_token,
                "lease_expires_at": lease_expires,
            }

        raise TurnInProgressError()

    def _assert_linear_parent(
        self,
        conn: sqlite3.Connection,
        conversation_id: str,
        parent_turn_id: Optional[str],
    ) -> None:
        conversation = conn.execute(
            "SELECT last_turn_id FROM conversations WHERE id = ?",
            (conversation_id,),
        ).fetchone()
        expected_parent_turn_id = (
            conversation["last_turn_id"] if conversation is not None else None
        )

        if parent_turn_id is None:
            if expected_parent_turn_id is not None:
                raise ContinuityError(
                    "parent_turn_id required: a complete turn exists",
                    expected_parent_turn_id=expected_parent_turn_id,
                )
            return

        if expected_parent_turn_id is None:
            raise ContinuityError(
                "parent_turn_id does not match any complete turn",
                expected_parent_turn_id=None,
            )
        if expected_parent_turn_id != parent_turn_id:
            raise ContinuityError(
                "parent_turn_id does not match last complete turn",
                expected_parent_turn_id=expected_parent_turn_id,
            )

    def _enforce_pending_quota(self, conn: sqlite3.Connection) -> None:
        count = conn.execute(
            """
            SELECT COUNT(*) FROM turn_requests
             WHERE status != 'complete'
            """
        ).fetchone()[0]
        if self.max_pending_turns > 0 and count >= self.max_pending_turns:
            raise QuotaExceededError("pending turn quota exceeded")

    def _enforce_conversation_quota_for_new_scope(
        self,
        conn: sqlite3.Connection,
        scope_key: str,
    ) -> None:
        """Reject a new scope when the completed-conversation quota is full.

        Empty reservation stubs do not count as conversations. The same check
        is repeated at first completion so concurrent stubs cannot race past
        the quota.
        """

        if self.max_conversations <= 0:
            return
        existing = conn.execute(
            "SELECT 1 FROM conversations WHERE scope_key = ?", (scope_key,)
        ).fetchone()
        if existing is not None:
            return
        count = conn.execute(
            "SELECT COUNT(*) FROM conversations WHERE message_count > 0"
        ).fetchone()[0]
        if count >= self.max_conversations:
            raise QuotaExceededError("conversation quota exceeded")

    def _enforce_conversation_quota_on_first_commit(
        self,
        conn: sqlite3.Connection,
        conversation_id: str,
        current_count: int,
    ) -> None:
        if self.max_conversations <= 0 or current_count > 0:
            return
        count = conn.execute(
            """
            SELECT COUNT(*) FROM conversations
             WHERE message_count > 0 AND id != ?
            """,
            (conversation_id,),
        ).fetchone()[0]
        if count >= self.max_conversations:
            raise QuotaExceededError("conversation quota exceeded")

    def recover_ready_turn(
        self,
        scope_key: str,
        turn_id: str,
        *,
        request_fingerprint: str,
        expected_lease_token: Optional[str],
    ) -> dict:
        """Atomically replace a ``ready`` turn whose staged payload was lost.

        Only the worker that observed the current lease may recover it. A race
        with a successful completion returns the completed messages instead;
        a race with another recovery is surfaced as ``TurnInProgressError``.
        """

        now = _now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._get_turn_for_update(conn, scope_key, turn_id)
                if row is None:
                    raise ConversationNotFoundError("turn not found")
                if row["request_fingerprint"] != request_fingerprint:
                    raise TurnConflictError()
                if row["status"] == "complete":
                    messages = self._messages_for_turn(
                        conn, row["conversation_id"], turn_id
                    )
                    conn.rollback()
                    return {
                        "status": "complete",
                        "replayed": True,
                        "messages": messages,
                    }
                if row["status"] != "ready":
                    raise TurnInProgressError()
                if row["lease_token"] != expected_lease_token:
                    raise TurnInProgressError()

                lease_token = _new_lease_token()
                lease_expires_at = now + self.lease_seconds
                conn.execute(
                    """
                    UPDATE turn_requests
                       SET status = 'generating', result_digest = NULL,
                           lease_token = ?, lease_expires_at = ?, updated_at = ?
                     WHERE conversation_id = ? AND turn_id = ?
                    """,
                    (
                        lease_token,
                        lease_expires_at,
                        now,
                        row["conversation_id"],
                        turn_id,
                    ),
                )
                conversation_row = conn.execute(
                    "SELECT * FROM conversations WHERE id = ?",
                    (row["conversation_id"],),
                ).fetchone()
                conn.commit()
                return {
                    "status": "new",
                    "lease_token": lease_token,
                    "lease_expires_at": lease_expires_at,
                    "conversation": (
                        _conversation_row(conversation_row)
                        if conversation_row is not None
                        else None
                    ),
                }
            except Exception:
                conn.rollback()
                raise

    def mark_turn_ready(
        self,
        scope_key: str,
        turn_id: str,
        lease_token: str,
        result_digest: str,
    ) -> bool:
        now = _now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._get_turn_for_update(conn, scope_key, turn_id)
                if row is None:
                    conn.rollback()
                    return False
                if row["lease_token"] != lease_token:
                    conn.rollback()
                    return False
                if row["status"] not in ("generating", "ready"):
                    conn.rollback()
                    return False
                conn.execute(
                    """
                    UPDATE turn_requests
                       SET status = 'ready', result_digest = ?, updated_at = ?
                     WHERE conversation_id = ? AND turn_id = ?
                    """,
                    (result_digest, now, row["conversation_id"], turn_id),
                )
                conn.commit()
                return True
            except Exception:
                conn.rollback()
                raise

    def complete_turn(
        self,
        *,
        scope_key: str,
        turn_id: str,
        lease_token: str,
        request_fingerprint: str,
        user_content: str,
        assistant_content: str,
        message_type: str = "text",
        selected_knowledge_base_ids: Optional[list[str]] = None,
        agent_id: str = "",
        agent_name: str = "",
        provider_id: str = "",
        model_id: str = "",
        prompt_ref: Optional[dict] = None,
        response_language: str = "auto",
        sources: Optional[list[dict]] = None,
        metadata: Optional[dict] = None,
    ) -> dict:
        now = _now()
        self._validate_message_content(user_content, assistant_content)
        safe_sources = _validate_json_field(
            sources or [], max_bytes=self.max_sources_bytes_per_turn, default=[]
        )
        safe_metadata = _validate_json_field(
            metadata or {}, max_bytes=self.max_metadata_bytes_per_turn, default={}
        )
        safe_prompt_ref = _validate_json_field(
            prompt_ref or {}, max_bytes=65_536, default={}
        )

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._get_turn_for_update(conn, scope_key, turn_id)
                if row is None:
                    raise ConversationNotFoundError("turn not found")
                if row["status"] == "complete":
                    messages = self._messages_for_turn(
                        conn, row["conversation_id"], turn_id
                    )
                    conn.rollback()
                    return {"replayed": True, "messages": messages}
                if row["lease_token"] != lease_token:
                    raise TurnConflictError("lease token mismatch")
                if row["request_fingerprint"] != request_fingerprint:
                    raise TurnConflictError("fingerprint mismatch on complete")

                conversation_id = row["conversation_id"]
                conv = conn.execute(
                    "SELECT * FROM conversations WHERE id = ?",
                    (conversation_id,),
                ).fetchone()
                if conv is None:
                    raise ConversationNotFoundError("conversation not found")

                self._enforce_conversation_quota_on_first_commit(
                    conn,
                    conversation_id,
                    conv["message_count"],
                )
                self._enforce_message_quota(conn, conversation_id, conv["message_count"])
                self._enforce_conversation_bytes(
                    conn, conversation_id, conv["payload_bytes"],
                    user_content, assistant_content, safe_sources, safe_metadata,
                )
                self._enforce_history_bytes(
                    conn,
                    _utf8_bytes(user_content)
                    + _utf8_bytes(assistant_content)
                    + _json_bytes(safe_sources)
                    + _json_bytes(safe_metadata),
                )

                next_sequence = conn.execute(
                    "SELECT COALESCE(MAX(sequence), 0) + 1 FROM messages WHERE conversation_id = ?",
                    (conversation_id,),
                ).fetchone()[0]

                user_bytes = _utf8_bytes(user_content)
                assistant_bytes = _utf8_bytes(assistant_content)
                sources_json = json.dumps(safe_sources, ensure_ascii=False)
                metadata_json = json.dumps(safe_metadata, ensure_ascii=False)
                sources_bytes = _utf8_bytes(sources_json)
                metadata_bytes = _utf8_bytes(metadata_json)
                turn_bytes = user_bytes + assistant_bytes + sources_bytes + metadata_bytes

                conn.execute(
                    """
                    INSERT INTO messages
                        (conversation_id, turn_id, role, message_type,
                         content, sequence, sources, metadata, payload_bytes,
                         created_at)
                    VALUES (?, ?, 'user', 'text', ?, ?, '[]', '{}', ?, ?)
                    """,
                    (
                        conversation_id, turn_id, user_content,
                        next_sequence, user_bytes, now,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO messages
                        (conversation_id, turn_id, role, message_type,
                         content, sequence, sources, metadata, payload_bytes,
                         created_at)
                    VALUES (?, ?, 'assistant', ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        conversation_id, turn_id, message_type, assistant_content,
                        next_sequence + 1, sources_json, metadata_json,
                        assistant_bytes + sources_bytes + metadata_bytes, now,
                    ),
                )

                self._upsert_knowledge_bases(
                    conn, conversation_id, selected_knowledge_base_ids or [], now
                )

                title = self._derive_title(conv["title"], user_content)
                new_message_count = conv["message_count"] + 2
                new_payload_bytes = conv["payload_bytes"] + turn_bytes

                conn.execute(
                    """
                    UPDATE turn_requests
                       SET status = 'complete', lease_token = NULL,
                           lease_expires_at = NULL, updated_at = ?
                     WHERE conversation_id = ? AND turn_id = ?
                    """,
                    (now, conversation_id, turn_id),
                )
                conn.execute(
                    """
                    UPDATE conversations
                       SET title = ?, agent_id = ?, agent_name = ?,
                           provider_id = ?, model_id = ?, prompt_ref = ?,
                           response_language = ?, message_count = ?,
                           payload_bytes = ?, last_turn_id = ?, updated_at = ?
                     WHERE id = ?
                    """,
                    (
                        title, agent_id, agent_name, provider_id, model_id,
                        json.dumps(safe_prompt_ref, ensure_ascii=False),
                        response_language, new_message_count, new_payload_bytes,
                        turn_id, now, conversation_id,
                    ),
                )
                messages = self._messages_for_turn(conn, conversation_id, turn_id)
                conn.commit()

                return {
                    "user_sequence": next_sequence,
                    "assistant_sequence": next_sequence + 1,
                    "message_count": new_message_count,
                    "payload_bytes": new_payload_bytes,
                    "title": title,
                    "messages": messages,
                }
            except Exception:
                conn.rollback()
                raise

    def renew_turn_lease(
        self,
        scope_key: str,
        turn_id: str,
        lease_token: str,
    ) -> bool:
        now = _now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._get_turn_for_update(conn, scope_key, turn_id)
                if row is None or row["lease_token"] != lease_token:
                    conn.rollback()
                    return False
                if row["status"] not in ("generating", "ready"):
                    conn.rollback()
                    return False
                conn.execute(
                    """
                    UPDATE turn_requests
                       SET lease_expires_at = ?, updated_at = ?
                     WHERE conversation_id = ? AND turn_id = ?
                    """,
                    (now + self.lease_seconds, now, row["conversation_id"], turn_id),
                )
                conn.commit()
                return True
            except Exception:
                conn.rollback()
                raise

    def fail_turn(
        self,
        scope_key: str,
        turn_id: str,
        lease_token: str,
    ) -> bool:
        now = _now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._get_turn_for_update(conn, scope_key, turn_id)
                if row is None or row["lease_token"] != lease_token:
                    conn.rollback()
                    return False
                if row["status"] == "complete":
                    conn.rollback()
                    return False
                conn.execute(
                    """
                    UPDATE turn_requests
                       SET status = 'failed', lease_token = NULL,
                           lease_expires_at = NULL, updated_at = ?
                     WHERE conversation_id = ? AND turn_id = ?
                    """,
                    (now, row["conversation_id"], turn_id),
                )
                conn.commit()
                return True
            except Exception:
                conn.rollback()
                raise

    def get_turn(self, scope_key: str, turn_id: str) -> Optional[dict]:
        with self._connect() as conn:
            row = self._get_turn_for_update(conn, scope_key, turn_id)
            return _turn_row(row) if row else None

    def reset_continuity(self, history_id: str) -> dict:
        """Clean up expired leases and return the current parent turn id.

        Marks any ``generating`` turns whose lease has expired as
        ``failed`` so a new turn can be started. Returns the ``turn_id``
        of the last ``complete`` turn (the parent the next turn should
        reference), or ``None`` when no complete turn exists.
        """
        now = _now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT id, last_turn_id FROM conversations WHERE id = ?",
                    (history_id,),
                ).fetchone()
                if row is None:
                    raise ConversationNotFoundError()

                expired = conn.execute(
                    """
                    UPDATE turn_requests
                       SET status = 'failed', lease_token = NULL,
                           lease_expires_at = NULL, updated_at = ?
                     WHERE conversation_id = ?
                       AND status = 'generating'
                       AND lease_expires_at IS NOT NULL
                       AND lease_expires_at < ?
                    """,
                    (now, history_id, now),
                )
                cleaned = expired.rowcount

                conn.commit()
                return {
                    "parent_turn_id": row["last_turn_id"],
                    "cleaned_up_leases": cleaned,
                }
            except Exception:
                conn.rollback()
                raise

    def cleanup_expired(self, *, now: Optional[float] = None) -> dict:
        """Apply active-history and incomplete-turn retention policies.

        Archived conversations are never removed by time retention. Expired
        generating leases first become failed; old ``ready``/``failed`` rows
        and empty reservation stubs are then removed after the configured
        incomplete-turn retention window.

        Returned scope/turn identifiers let :class:`ConversationService`
        remove corresponding warm and staged state without coupling this
        durable store to either backend.
        """

        effective_now = _now() if now is None else float(now)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                result = self._cleanup_expired_locked(conn, effective_now)
                conn.commit()
                return result
            except Exception:
                conn.rollback()
                raise

    # ------------------------------------------------------------------
    # Read API
    # ------------------------------------------------------------------

    def get(self, history_id: str) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM conversations WHERE id = ?", (history_id,)
            ).fetchone()
            if row is None:
                return None
            result = _conversation_row(row)
            result["knowledge_base_ids"] = self._load_knowledge_bases(conn, history_id)
            result["has_incomplete_turn"] = self._has_incomplete_turn(conn, history_id)
            return result

    def get_by_scope_key(self, scope_key: str) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM conversations WHERE scope_key = ?", (scope_key,)
            ).fetchone()
            if row is None:
                return None
            result = _conversation_row(row)
            result["knowledge_base_ids"] = self._load_knowledge_bases(conn, row["id"])
            return result

    def list_messages(
        self,
        history_id: str,
        *,
        before_sequence: Optional[int] = None,
        limit: int = 50,
    ) -> tuple[list[dict], Optional[int]]:
        limit = max(1, min(limit, 200))
        with self._connect() as conn:
            exists = conn.execute(
                "SELECT 1 FROM conversations WHERE id = ?", (history_id,)
            ).fetchone()
            if exists is None:
                raise ConversationNotFoundError(history_id)
            if before_sequence is not None:
                rows = conn.execute(
                    """
                    SELECT * FROM messages
                     WHERE conversation_id = ? AND sequence < ?
                     ORDER BY sequence DESC LIMIT ?
                    """,
                    (history_id, before_sequence, limit + 1),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM messages
                     WHERE conversation_id = ?
                     ORDER BY sequence DESC LIMIT ?
                    """,
                    (history_id, limit + 1),
                ).fetchall()
            has_more = len(rows) > limit
            if has_more:
                rows = rows[:limit]
            messages = [_message_row(r) for r in rows]
            messages.reverse()
            next_cursor = None
            if has_more and messages:
                next_cursor = messages[0]["sequence"]
            return messages, next_cursor

    def list_messages_after_sequence(
        self,
        history_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 200,
    ) -> tuple[list[dict], Optional[int]]:
        """Read an ascending durable tail without materializing the transcript."""

        limit = max(1, min(limit, 200))
        with self._connect() as conn:
            exists = conn.execute(
                "SELECT 1 FROM conversations WHERE id = ?", (history_id,)
            ).fetchone()
            if exists is None:
                raise ConversationNotFoundError(history_id)
            rows = conn.execute(
                """
                SELECT * FROM messages
                 WHERE conversation_id = ? AND sequence > ?
                 ORDER BY sequence ASC LIMIT ?
                """,
                (history_id, max(0, int(after_sequence)), limit + 1),
            ).fetchall()
            has_more = len(rows) > limit
            if has_more:
                rows = rows[:limit]
            messages = [_message_row(row) for row in rows]
            next_after = None
            if has_more and messages:
                next_after = int(messages[-1]["sequence"])
            return messages, next_after

    def list(
        self,
        *,
        page: int = 1,
        per_page: int = 25,
        status: Optional[str] = None,
    ) -> tuple[list[dict], dict]:
        per_page = max(1, min(per_page, 100))
        page = max(1, page)
        if status and status not in _CONVERSATION_STATUSES:
            status = None
        with self._connect() as conn:
            where = "WHERE message_count > 0"
            params: list[Any] = []
            if status:
                where += " AND status = ?"
                params.append(status)
            total = conn.execute(
                f"SELECT COUNT(*) FROM conversations {where}", params
            ).fetchone()[0]
            offset = (page - 1) * per_page
            rows = conn.execute(
                f"""
                SELECT * FROM conversations {where}
                 ORDER BY updated_at DESC LIMIT ? OFFSET ?
                """,
                (*params, per_page, offset),
            ).fetchall()
            items = []
            for row in rows:
                conv = _conversation_row(row)
                conv["has_incomplete_turn"] = self._has_incomplete_turn(conn, row["id"])
                items.append(conv)
            page_count = max(1, (total + per_page - 1) // per_page)
            return items, {
                "page": page,
                "per_page": per_page,
                "total": total,
                "page_count": page_count,
                "has_prev": page > 1,
                "has_next": page < page_count,
            }

    # ------------------------------------------------------------------
    # Mutations
    # ------------------------------------------------------------------

    def rename(self, history_id: str, title: str) -> dict:
        return self.update_conversation(history_id, title=title)

    def archive(self, history_id: str) -> dict:
        return self.update_conversation(history_id, archived=True)

    def unarchive(self, history_id: str) -> dict:
        return self.update_conversation(history_id, archived=False)

    def update_conversation(
        self,
        history_id: str,
        *,
        title: Optional[str] = None,
        archived: Optional[bool] = None,
    ) -> dict:
        """Atomically rename and/or change archive status."""

        if title is not None:
            title = str(title).strip()
            if not title:
                raise ConversationHistoryError("title cannot be empty")
            if len(title) > TITLE_RENAME_MAX_LEN:
                raise ConversationHistoryError(
                    f"title exceeds {TITLE_RENAME_MAX_LEN} characters"
                )
        if archived is not None and not isinstance(archived, bool):
            raise ConversationHistoryError("archived must be boolean")
        now = _now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT * FROM conversations WHERE id = ?", (history_id,)
                ).fetchone()
                if row is None:
                    raise ConversationNotFoundError()
                if archived is True:
                    incomplete = conn.execute(
                        """
                        SELECT 1 FROM turn_requests
                         WHERE conversation_id = ?
                           AND status IN ('generating', 'ready')
                         LIMIT 1
                        """,
                        (history_id,),
                    ).fetchone()
                    if incomplete is not None:
                        raise TurnInProgressError(
                            "cannot archive a conversation with an active turn"
                        )
                next_title = title if title is not None else row["title"]
                next_status = row["status"]
                archived_at = row["archived_at"]
                if archived is True:
                    next_status = "archived"
                    archived_at = now
                elif archived is False:
                    next_status = "active"
                    archived_at = None
                conn.execute(
                    """
                    UPDATE conversations
                       SET title = ?, status = ?, archived_at = ?, updated_at = ?
                     WHERE id = ?
                    """,
                    (next_title, next_status, archived_at, now, history_id),
                )
                conn.commit()
                result = _conversation_row(row)
                result["title"] = next_title
                result["status"] = next_status
                result["archived_at"] = archived_at
                result["updated_at"] = now
                return result
            except Exception:
                conn.rollback()
                raise

    def update_summary(
        self,
        scope_key: str,
        summary: str,
        *,
        expected_version: int,
        through_sequence: int,
    ) -> bool:
        now = _now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT * FROM conversations WHERE scope_key = ?", (scope_key,)
                ).fetchone()
                if row is None:
                    conn.rollback()
                    return False
                if row["summary_version"] != expected_version:
                    conn.rollback()
                    return False
                if through_sequence <= row["summary_through_sequence"]:
                    conn.rollback()
                    return False
                conn.execute(
                    """
                    UPDATE conversations
                       SET summary = ?, summary_version = ?,
                           summary_through_sequence = ?, updated_at = ?
                     WHERE id = ?
                    """,
                    (
                        summary,
                        expected_version + 1,
                        through_sequence,
                        now,
                        row["id"],
                    ),
                )
                conn.commit()
                return True
            except Exception:
                conn.rollback()
                raise

    def delete(self, history_id: str) -> bool:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT id FROM conversations WHERE id = ?", (history_id,)
                ).fetchone()
                if row is None:
                    conn.rollback()
                    return False
                conn.execute(
                    "DELETE FROM conversations WHERE id = ?", (history_id,)
                )
                conn.commit()
                return True
            except Exception:
                conn.rollback()
                raise

    def delete_with_artifact_cleanup(
        self,
        history_id: str,
    ) -> tuple[bool, ConversationArtifactDeletionPlan]:
        """Atomically delete one conversation and persist its cleanup plan."""

        cleanup_key = f"conversation:{history_id}"
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute(
                    "SELECT id FROM conversations WHERE id = ?", (history_id,)
                ).fetchone()
                if row is None:
                    plan = self._artifact_cleanup_outbox_locked(
                        conn, cleanup_key, (history_id,)
                    )
                    conn.commit()
                    return False, plan
                plan = self._artifact_cleanup_plan_locked(conn, (history_id,))
                if plan.safe:
                    self._stage_artifact_cleanup_locked(
                        conn, cleanup_key, plan.exclusive
                    )
                conn.execute(
                    "DELETE FROM conversations WHERE id = ?", (history_id,)
                )
                conn.commit()
                return True, plan
            except Exception:
                conn.rollback()
                raise

    def complete_artifact_cleanup(self, cleanup_key: str) -> bool:
        if not cleanup_key:
            return False
        with self._connect() as conn:
            result = conn.execute(
                "DELETE FROM conversation_artifact_cleanup_outbox "
                "WHERE cleanup_key = ?",
                (cleanup_key,),
            )
            conn.commit()
            return bool(result.rowcount)

    def artifact_cleanup_plan(
        self,
        history_ids: list[str] | tuple[str, ...] | set[str],
    ) -> ConversationArtifactDeletionPlan:
        """Snapshot artifact ownership before deleting conversations.

        The returned ``exclusive`` references occur in the target set but in
        no retained durable conversation.  Invalid/corrupt metadata makes the
        plan unsafe and therefore empty: leaking an artifact is preferable to
        deleting a file whose ownership cannot be proven.
        """

        requested_ids = sorted(
            {
                str(history_id)
                for history_id in history_ids
                if str(history_id or "")
            }
        )
        if not requested_ids:
            return ConversationArtifactDeletionPlan(
                conversation_ids=(),
                target=ConversationArtifactReferences(),
                retained=ConversationArtifactReferences(),
                exclusive=ConversationArtifactReferences(),
            )

        placeholders = ",".join("?" for _ in requested_ids)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT id FROM conversations WHERE id IN ({placeholders})",
                requested_ids,
            ).fetchall()
            existing_ids = tuple(sorted(row["id"] for row in rows))
            return self._artifact_cleanup_plan_locked(conn, existing_ids)

    def artifact_cleanup_plan_by_knowledge_base(
        self,
        knowledge_base_id: str,
    ) -> ConversationArtifactDeletionPlan:
        """Snapshot exclusive artifacts for the KB hard-delete cascade."""

        if not knowledge_base_id:
            return ConversationArtifactDeletionPlan(
                conversation_ids=(),
                target=ConversationArtifactReferences(),
                retained=ConversationArtifactReferences(),
                exclusive=ConversationArtifactReferences(),
            )
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT conversation_id
                  FROM conversation_knowledge_bases
                 WHERE knowledge_base_id = ?
                 ORDER BY conversation_id ASC
                """,
                (knowledge_base_id,),
            ).fetchall()
            conversation_ids = tuple(row["conversation_id"] for row in rows)
            return self._artifact_cleanup_plan_locked(conn, conversation_ids)

    def _artifact_cleanup_plan_locked(
        self,
        conn: sqlite3.Connection,
        conversation_ids: tuple[str, ...],
    ) -> ConversationArtifactDeletionPlan:
        target_ids = set(conversation_ids)
        target = ConversationArtifactReferences()
        retained = ConversationArtifactReferences()
        safe = True

        if target_ids:
            rows = conn.execute(
                "SELECT conversation_id, metadata FROM messages"
            ).fetchall()
            for row in rows:
                try:
                    metadata = json.loads(row["metadata"] or "{}")
                except (TypeError, ValueError, json.JSONDecodeError):
                    safe = False
                    continue
                references = references_from_history_metadata(metadata)
                if row["conversation_id"] in target_ids:
                    target = target.union(references)
                else:
                    retained = retained.union(references)

        exclusive = ConversationArtifactReferences()
        if safe:
            exclusive = exclusive_references(target, retained)
        return ConversationArtifactDeletionPlan(
            conversation_ids=conversation_ids,
            target=target,
            retained=retained,
            exclusive=exclusive,
            safe=safe,
        )

    def delete_by_knowledge_base(self, knowledge_base_id: str) -> int:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                rows = conn.execute(
                    """
                    SELECT conversation_id FROM conversation_knowledge_bases
                     WHERE knowledge_base_id = ?
                    """,
                    (knowledge_base_id,),
                ).fetchall()
                count = 0
                for row in rows:
                    conn.execute(
                        "DELETE FROM conversations WHERE id = ?",
                        (row["conversation_id"],),
                    )
                    count += 1
                conn.commit()
                return count
            except Exception:
                conn.rollback()
                raise

    def delete_by_knowledge_base_with_artifact_cleanup(
        self,
        knowledge_base_id: str,
    ) -> tuple[int, ConversationArtifactDeletionPlan]:
        """Cascade a KB and checkpoint exclusive artifacts in one commit."""

        cleanup_key = f"knowledge-base:{knowledge_base_id}"
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                rows = conn.execute(
                    """
                    SELECT DISTINCT conversation_id
                      FROM conversation_knowledge_bases
                     WHERE knowledge_base_id = ?
                     ORDER BY conversation_id ASC
                    """,
                    (knowledge_base_id,),
                ).fetchall()
                conversation_ids = tuple(row["conversation_id"] for row in rows)
                if not conversation_ids:
                    plan = self._artifact_cleanup_outbox_locked(
                        conn, cleanup_key, ()
                    )
                    conn.commit()
                    return 0, plan
                plan = self._artifact_cleanup_plan_locked(conn, conversation_ids)
                if plan.safe:
                    self._stage_artifact_cleanup_locked(
                        conn, cleanup_key, plan.exclusive
                    )
                for conversation_id in conversation_ids:
                    conn.execute(
                        "DELETE FROM conversations WHERE id = ?",
                        (conversation_id,),
                    )
                conn.commit()
                return len(conversation_ids), plan
            except Exception:
                conn.rollback()
                raise

    def _stage_artifact_cleanup_locked(
        self,
        conn: sqlite3.Connection,
        cleanup_key: str,
        references: ConversationArtifactReferences,
    ) -> None:
        payload = {
            "attachment_ids": sorted(references.attachment_ids),
            "image_names": sorted(references.image_names),
            "run_ids": sorted(references.run_ids),
        }
        conn.execute(
            """
            INSERT INTO conversation_artifact_cleanup_outbox
                   (cleanup_key, references_json, created_at)
            VALUES (?, ?, ?)
            ON CONFLICT(cleanup_key) DO UPDATE SET
                references_json = excluded.references_json,
                created_at = excluded.created_at
            """,
            (cleanup_key, _canonical_json(payload), _now()),
        )

    def _artifact_cleanup_outbox_locked(
        self,
        conn: sqlite3.Connection,
        cleanup_key: str,
        conversation_ids: tuple[str, ...],
    ) -> ConversationArtifactDeletionPlan:
        row = conn.execute(
            "SELECT references_json FROM conversation_artifact_cleanup_outbox "
            "WHERE cleanup_key = ?",
            (cleanup_key,),
        ).fetchone()
        if row is None:
            return ConversationArtifactDeletionPlan(
                conversation_ids=conversation_ids,
                target=ConversationArtifactReferences(),
                retained=ConversationArtifactReferences(),
                exclusive=ConversationArtifactReferences(),
                safe=False,
            )
        try:
            payload = json.loads(row["references_json"] or "{}")
            references = ConversationArtifactReferences(
                attachment_ids=frozenset(payload.get("attachment_ids") or ()),
                image_names=frozenset(payload.get("image_names") or ()),
                run_ids=frozenset(payload.get("run_ids") or ()),
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            return ConversationArtifactDeletionPlan(
                conversation_ids=conversation_ids,
                target=ConversationArtifactReferences(),
                retained=ConversationArtifactReferences(),
                exclusive=ConversationArtifactReferences(),
                safe=False,
            )
        return ConversationArtifactDeletionPlan(
            conversation_ids=conversation_ids,
            target=references,
            retained=ConversationArtifactReferences(),
            exclusive=references,
            safe=True,
        )

    def scope_keys_by_knowledge_base(self, knowledge_base_id: str) -> list[str]:
        """Return conversation scope keys that reference a knowledge base.

        Callers that own volatile per-scope state can take this snapshot before
        :meth:`delete_by_knowledge_base` and clear those caches after the
        durable cascade succeeds.
        """
        if not knowledge_base_id:
            return []
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT c.scope_key
                  FROM conversations AS c
                  JOIN conversation_knowledge_bases AS ckb
                    ON ckb.conversation_id = c.id
                 WHERE ckb.knowledge_base_id = ?
                 ORDER BY c.scope_key ASC
                """,
                (knowledge_base_id,),
            ).fetchall()
            return [row["scope_key"] for row in rows]

    def count_by_knowledge_base(self, knowledge_base_id: str) -> int:
        with self._connect() as conn:
            return conn.execute(
                """
                SELECT COUNT(DISTINCT conversation_id)
                  FROM conversation_knowledge_bases
                 WHERE knowledge_base_id = ?
                """,
                (knowledge_base_id,),
            ).fetchone()[0]

    def quota_status(self) -> dict:
        self.cleanup_expired()
        with self._connect() as conn:
            total_conversations = conn.execute(
                "SELECT COUNT(*) FROM conversations WHERE message_count > 0"
            ).fetchone()[0]
            total_bytes = conn.execute(
                "SELECT COALESCE(SUM(payload_bytes), 0) FROM conversations"
            ).fetchone()[0]
            pending = conn.execute(
                "SELECT COUNT(*) FROM turn_requests WHERE status IN ('generating','ready')"
            ).fetchone()[0]
            return {
                "conversations": total_conversations,
                "max_conversations": self.max_conversations,
                "bytes": total_bytes,
                "max_bytes": self.max_history_bytes,
                "pending_turns": pending,
                "max_pending_turns": self.max_pending_turns,
            }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _cleanup_expired_locked(
        self,
        conn: sqlite3.Connection,
        now: float,
    ) -> dict:
        deleted_scope_keys: list[str] = []
        deleted_turns: list[dict] = []
        conversations_deleted = 0
        turns_deleted = 0

        # An expired lease no longer represents active work. Keep the failed
        # row until incomplete retention elapses so retries retain fingerprint
        # conflict semantics during that window.
        expired = conn.execute(
            """
            UPDATE turn_requests
               SET status = 'failed', lease_token = NULL,
                   lease_expires_at = NULL
             WHERE status = 'generating'
               AND lease_expires_at IS NOT NULL
               AND lease_expires_at < ?
            """,
            (now,),
        )
        expired_leases_failed = max(0, expired.rowcount)

        if self.retention_days > 0:
            cutoff = now - (self.retention_days * 86_400)
            rows = conn.execute(
                """
                SELECT id, scope_key FROM conversations AS c
                 WHERE c.status = 'active'
                   AND c.message_count > 0
                   AND c.updated_at < ?
                   AND NOT EXISTS (
                       SELECT 1 FROM turn_requests AS t
                        WHERE t.conversation_id = c.id
                          AND t.status IN ('generating', 'ready')
                   )
                """,
                (cutoff,),
            ).fetchall()
            for row in rows:
                conn.execute("DELETE FROM conversations WHERE id = ?", (row["id"],))
                deleted_scope_keys.append(row["scope_key"])
            conversations_deleted += len(rows)

        if self.incomplete_turn_retention_days > 0:
            cutoff = now - (self.incomplete_turn_retention_days * 86_400)
            rows = conn.execute(
                """
                SELECT t.conversation_id, t.turn_id, c.scope_key
                  FROM turn_requests AS t
                  JOIN conversations AS c ON c.id = t.conversation_id
                 WHERE t.status IN ('ready', 'failed')
                   AND t.updated_at < ?
                """,
                (cutoff,),
            ).fetchall()
            for row in rows:
                conn.execute(
                    "DELETE FROM turn_requests WHERE conversation_id = ? AND turn_id = ?",
                    (row["conversation_id"], row["turn_id"]),
                )
                deleted_turns.append(
                    {"scope_key": row["scope_key"], "turn_id": row["turn_id"]}
                )
            turns_deleted += len(rows)

            stub_rows = conn.execute(
                """
                SELECT c.id, c.scope_key FROM conversations AS c
                 WHERE c.message_count = 0
                   AND c.updated_at < ?
                   AND NOT EXISTS (
                       SELECT 1 FROM turn_requests AS t
                        WHERE t.conversation_id = c.id
                   )
                """,
                (cutoff,),
            ).fetchall()
            for row in stub_rows:
                conn.execute("DELETE FROM conversations WHERE id = ?", (row["id"],))
                deleted_scope_keys.append(row["scope_key"])
            conversations_deleted += len(stub_rows)

        return {
            "conversations_deleted": conversations_deleted,
            "turns_deleted": turns_deleted,
            "expired_leases_failed": expired_leases_failed,
            "deleted_scope_keys": deleted_scope_keys,
            "deleted_turns": deleted_turns,
        }

    def _get_turn_for_update(
        self,
        conn: sqlite3.Connection,
        scope_key: str,
        turn_id: str,
    ) -> Optional[sqlite3.Row]:
        conv = conn.execute(
            "SELECT id FROM conversations WHERE scope_key = ?", (scope_key,)
        ).fetchone()
        if conv is None:
            return None
        return conn.execute(
            """
            SELECT * FROM turn_requests
             WHERE conversation_id = ? AND turn_id = ?
            """,
            (conv["id"], turn_id),
        ).fetchone()

    def _get_or_create_conversation(
        self,
        conn: sqlite3.Connection,
        *,
        client_conversation_id: str,
        scope_key: str,
        scope_kind: str,
        agent_id: str,
        agent_name: str,
        provider_id: str,
        model_id: str,
        prompt_ref: dict,
        response_language: str,
        now: float,
    ) -> dict:
        row = conn.execute(
            "SELECT * FROM conversations WHERE scope_key = ?", (scope_key,)
        ).fetchone()
        if row is not None:
            if row["status"] == "archived":
                raise ConversationArchivedError()
            return _conversation_row(row)
        conversation_id = _new_id()
        conn.execute(
            """
            INSERT INTO conversations
                (id, client_conversation_id, scope_key, scope_kind, title,
                 status, agent_id, agent_name, provider_id, model_id,
                 prompt_ref, response_language, summary, summary_version,
                 summary_through_sequence, message_count, payload_bytes,
                 last_turn_id, created_at, updated_at, archived_at)
            VALUES (?, ?, ?, ?, '', 'active', ?, ?, ?, ?, ?, ?, '', 0, 0, 0, 0,
                    NULL, ?, ?, NULL)
            """,
            (
                conversation_id, client_conversation_id, scope_key, scope_kind,
                agent_id, agent_name, provider_id, model_id,
                json.dumps(prompt_ref, ensure_ascii=False), response_language,
                now, now,
            ),
        )
        return {
            "id": conversation_id,
            "client_conversation_id": client_conversation_id,
            "scope_key": scope_key,
            "scope_kind": scope_kind,
            "title": "",
            "status": "active",
            "agent_id": agent_id,
            "agent_name": agent_name,
            "provider_id": provider_id,
            "model_id": model_id,
            "prompt_ref": prompt_ref,
            "response_language": response_language,
            "summary": "",
            "summary_version": 0,
            "summary_through_sequence": 0,
            "message_count": 0,
            "payload_bytes": 0,
            "last_turn_id": None,
            "created_at": now,
            "updated_at": now,
            "archived_at": None,
        }

    def _messages_for_turn(
        self,
        conn: sqlite3.Connection,
        conversation_id: str,
        turn_id: str,
    ) -> list[dict]:
        rows = conn.execute(
            """
            SELECT * FROM messages
             WHERE conversation_id = ? AND turn_id = ?
             ORDER BY sequence
            """,
            (conversation_id, turn_id),
        ).fetchall()
        return [_message_row(r) for r in rows]

    def _upsert_knowledge_bases(
        self,
        conn: sqlite3.Connection,
        conversation_id: str,
        knowledge_base_ids: list[str],
        now: float,
    ) -> None:
        existing = {
            r["knowledge_base_id"]: r
            for r in conn.execute(
                "SELECT * FROM conversation_knowledge_bases WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchall()
        }
        for kb_id in knowledge_base_ids:
            if not kb_id:
                continue
            if kb_id in existing:
                conn.execute(
                    """
                    UPDATE conversation_knowledge_bases
                       SET is_selected = 1, last_used_at = ?
                     WHERE conversation_id = ? AND knowledge_base_id = ?
                    """,
                    (now, conversation_id, kb_id),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO conversation_knowledge_bases
                        (conversation_id, knowledge_base_id, is_selected,
                         first_used_at, last_used_at)
                    VALUES (?, ?, 1, ?, ?)
                    """,
                    (conversation_id, kb_id, now, now),
                )
        current_ids = set(knowledge_base_ids)
        for kb_id, _row in existing.items():
            if kb_id not in current_ids:
                conn.execute(
                    """
                    UPDATE conversation_knowledge_bases
                       SET is_selected = 0
                     WHERE conversation_id = ? AND knowledge_base_id = ?
                    """,
                    (conversation_id, kb_id),
                )

    def _associate_reservation_knowledge_bases(
        self,
        conn: sqlite3.Connection,
        conversation_id: str,
        knowledge_base_ids: list[str],
        now: float,
    ) -> None:
        """Persist KB ownership for an incomplete turn without selecting it."""

        for kb_id in dict.fromkeys(knowledge_base_ids):
            if not kb_id:
                continue
            conn.execute(
                """
                INSERT OR IGNORE INTO conversation_knowledge_bases
                    (conversation_id, knowledge_base_id, is_selected,
                     first_used_at, last_used_at)
                VALUES (?, ?, 0, ?, ?)
                """,
                (conversation_id, kb_id, now, now),
            )

    def _load_knowledge_bases(
        self,
        conn: sqlite3.Connection,
        conversation_id: str,
    ) -> list[dict]:
        rows = conn.execute(
            """
            SELECT knowledge_base_id, is_selected, first_used_at, last_used_at
              FROM conversation_knowledge_bases
             WHERE conversation_id = ?
            """,
            (conversation_id,),
        ).fetchall()
        return [
            {
                "knowledge_base_id": r["knowledge_base_id"],
                "is_selected": bool(r["is_selected"]),
                "first_used_at": r["first_used_at"],
                "last_used_at": r["last_used_at"],
            }
            for r in rows
        ]

    def _has_incomplete_turn(
        self,
        conn: sqlite3.Connection,
        conversation_id: str,
    ) -> bool:
        row = conn.execute(
            """
            SELECT 1 FROM turn_requests
             WHERE conversation_id = ? AND status IN ('generating','ready','failed')
             LIMIT 1
            """,
            (conversation_id,),
        ).fetchone()
        return row is not None

    def _validate_message_content(self, user_content: str, assistant_content: str) -> None:
        if not isinstance(user_content, str) or not isinstance(assistant_content, str):
            raise ConversationHistoryError("message content must be strings")
        if len(user_content) > self.max_message_chars:
            raise ConversationHistoryError("user message exceeds size limit")
        if len(assistant_content) > self.max_message_chars:
            raise ConversationHistoryError("assistant message exceeds size limit")

    def _enforce_message_quota(
        self,
        conn: sqlite3.Connection,
        conversation_id: str,
        current_count: int,
    ) -> None:
        if current_count + 2 > self.max_messages_per_conversation:
            raise QuotaExceededError("conversation message quota exceeded")

    def _enforce_conversation_bytes(
        self,
        conn: sqlite3.Connection,
        conversation_id: str,
        current_bytes: int,
        user_content: str,
        assistant_content: str,
        sources: list,
        metadata: dict,
    ) -> None:
        turn_bytes = (
            _utf8_bytes(user_content)
            + _utf8_bytes(assistant_content)
            + _json_bytes(sources)
            + _json_bytes(metadata)
        )
        if current_bytes + turn_bytes > self.max_conversation_bytes:
            raise QuotaExceededError("conversation byte quota exceeded")

    def _enforce_history_bytes(
        self, conn: sqlite3.Connection, turn_bytes: int = 0
    ) -> None:
        total = conn.execute(
            "SELECT COALESCE(SUM(payload_bytes), 0) FROM conversations"
        ).fetchone()[0]
        if total + turn_bytes > self.max_history_bytes:
            raise QuotaExceededError("workspace history byte quota exceeded")

    def _derive_title(self, current_title: str, user_content: str) -> str:
        if current_title:
            return current_title
        normalized = " ".join(user_content.split())[:TITLE_MAX_LEN]
        return normalized
