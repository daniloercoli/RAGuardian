"""Coordination layer for durable conversation history and warm memory.

:class:`ConversationService` is the single entry point used by the query /
streaming routes to:

1. Begin a turn (reservation + lease) in the durable
   :class:`ConversationHistoryStore`, replaying a staged or completed result
   when a client retries the same ``turn_id``.
2. Stage the generated result in the :class:`PendingTurnResultStore` so a
   retry that lands before the durable commit can replay without regenerating.
3. Complete the turn atomically in the history store and reconcile the warm
   :class:`ConversationMemoryStore` via idempotent append.
4. Hydrate the warm memory from durable history when a worker starts cold.

When history is disabled (``CONVERSATION_HISTORY_ENABLED=0``) the service is a
no-op and the routes use the volatile in-memory-only behaviour.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Optional

from utils.conversation_history_store import (
    ConversationArchivedError,
    ConversationHistoryError,
    ConversationHistoryStore,
    ConversationNotFoundError,
    ContinuityError,
    QuotaExceededError,
    TurnConflictError,
    TurnInProgressError,
)
from utils.conversation_artifacts import (
    ArtifactCleanupError,
    cleanup_workspace_artifacts,
)
from utils.conversation_memory import (
    ConversationMemoryStore,
    ConversationSummaryJob,
    ConversationTurn,
    fallback_summary,
    get_conversation_store,
)
from utils.pending_turn_store import (
    PendingTurnResultStore,
    get_pending_turn_store,
)

log = logging.getLogger(__name__)

_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


class ConversationTurnError(Exception):
    """Raised when a turn cannot begin (conflict, generating, quota, etc.).

    Carries an HTTP ``status_code`` and optional ``Retry-After`` hint so the
    route handlers can translate it into a proper response.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str = "turn_error",
        status_code: int = 409,
        retry_after: Optional[int] = None,
        payload: Optional[dict] = None,
    ):
        self.message = message
        self.code = code
        self.status_code = status_code
        self.retry_after = retry_after
        self.payload = payload or {}
        super().__init__(self.message)

    def to_dict(self) -> dict:
        # ``status`` is retained for existing clients while ``code`` gives
        # newer clients an explicit machine-readable error field.
        result = {
            "error": self.message,
            "status": self.code,
            "code": self.code,
        }
        result.update(self.payload)
        return result


HYDRATION_LIMIT_MESSAGES = 200


@dataclass(frozen=True)
class BeginTurnOutcome:
    """Result of :meth:`ConversationService.begin_turn`.

    ``status`` is one of:

    * ``disabled``  – history is off; proceed with volatile in-memory flow.
    * ``new``       – a reservation was created; generation should proceed.
    * ``ready``     – a staged result is available for replay (``result``).
    * ``complete``  – the turn already finished (``messages`` for replay).
    * ``generating``– another worker holds the lease (``retry_after``).
    * ``conflict``  – same turn_id, different fingerprint.
    * ``volatile_result_lost`` – a durable ready marker survived after its
      short-lived replay payload expired; regeneration must be explicit.
    """

    status: str
    lease_token: Optional[str] = None
    lease_expires_at: Optional[float] = None
    conversation: Optional[dict] = None
    result: Any = None
    result_digest: Optional[str] = None
    messages: Optional[list[dict]] = None
    retry_after: int = 5
    error: Optional[str] = None
    code: Optional[str] = None
    expected_parent_turn_id: Optional[str] = None


@dataclass(frozen=True)
class CompleteTurnOutcome:
    status: str
    replayed: bool = False
    messages: Optional[list[dict]] = None
    user_sequence: Optional[int] = None
    assistant_sequence: Optional[int] = None
    title: Optional[str] = None
    message_count: Optional[int] = None
    payload_bytes: Optional[int] = None
    summary_job: Optional[ConversationSummaryJob] = None
    error: Optional[str] = None


@dataclass(frozen=True)
class TurnRequest:
    """Inputs needed to begin a turn reservation."""

    scope_key: str
    scope_kind: str
    client_conversation_id: str
    turn_id: str
    request_fingerprint: str
    parent_turn_id: Optional[str] = None
    selected_knowledge_base_ids: Optional[list[str]] = None
    agent_id: str = ""
    agent_name: str = ""
    provider_id: str = ""
    model_id: str = ""
    prompt_ref: Optional[dict] = None
    response_language: str = "auto"
    workspace_id: str = ""
    recover_lost_result: bool = False


class ConversationService:
    """Coordinates durable history, pending results and warm memory."""

    def __init__(
        self,
        history_store: Optional[ConversationHistoryStore] = None,
        *,
        workspace_id: Optional[str] = None,
        app: Optional[Any] = None,
        pending_store: Optional[PendingTurnResultStore] = None,
        memory_store: Optional[ConversationMemoryStore] = None,
        enabled: bool = True,
    ):
        self._history_store = history_store
        self._workspace_id = workspace_id
        self._app = app
        self.pending_store = pending_store or get_pending_turn_store()
        self.memory_store = memory_store or get_conversation_store()
        self.enabled = bool(enabled and history_store is not None)

    @property
    def history_store(self) -> Optional[ConversationHistoryStore]:
        """Lazy-load history store for workspace if not provided."""
        if self._history_store is None and self._workspace_id:
            self._history_store = get_history_store_for_workspace(
                self._workspace_id, self._app
            )
        return self._history_store

    # ------------------------------------------------------------------
    # Turn lifecycle
    # ------------------------------------------------------------------

    def begin_turn(self, request: TurnRequest) -> BeginTurnOutcome:
        if not self.enabled:
            return BeginTurnOutcome(status="disabled")

        self.cleanup_expired()
        try:
            result = self.history_store.begin_turn(
                client_conversation_id=request.client_conversation_id,
                scope_key=request.scope_key,
                scope_kind=request.scope_kind,
                turn_id=request.turn_id,
                parent_turn_id=request.parent_turn_id,
                request_fingerprint=request.request_fingerprint,
                selected_knowledge_base_ids=request.selected_knowledge_base_ids,
                agent_id=request.agent_id,
                agent_name=request.agent_name,
                provider_id=request.provider_id,
                model_id=request.model_id,
                prompt_ref=request.prompt_ref,
                response_language=request.response_language,
            )
        except TurnConflictError:
            return BeginTurnOutcome(status="conflict", error="turn_id_conflict")
        except TurnInProgressError as exc:
            return BeginTurnOutcome(
                status="generating", retry_after=exc.retry_after
            )
        except ContinuityError as exc:
            return BeginTurnOutcome(
                status="continuity_error",
                error=str(exc),
                expected_parent_turn_id=exc.expected_parent_turn_id,
            )
        except ConversationArchivedError as exc:
            return BeginTurnOutcome(
                status="archived",
                error=str(exc),
                code=exc.code,
            )
        except QuotaExceededError as exc:
            return BeginTurnOutcome(
                status="error",
                error=str(exc),
                code=exc.code,
            )

        status = result["status"]
        if status == "new":
            return BeginTurnOutcome(
                status="new",
                lease_token=result.get("lease_token"),
                lease_expires_at=result.get("lease_expires_at"),
                conversation=result.get("conversation"),
            )
        if status == "ready":
            lease_token = result.get("lease_token")
            result_digest = result.get("result_digest")
            pending = self.pending_store.get(
                request.scope_key,
                request.turn_id,
                lease_token=lease_token,
            )
            if (
                pending is not None
                and pending.lease_token == lease_token
                and pending.result_digest == result_digest
            ):
                return BeginTurnOutcome(
                    status="ready",
                    lease_token=lease_token,
                    lease_expires_at=result.get("lease_expires_at"),
                    result=pending.result,
                    result_digest=pending.result_digest,
                    conversation=result.get("conversation"),
                )
            # The durable digest survived but its volatile payload did not.
            # Do not silently regenerate under the same idempotency key: that
            # could produce a different answer for a turn the server already
            # marked ready.  A future explicit recovery action may invalidate
            # the ready marker after the user chooses to regenerate.
            if pending is not None:
                self.pending_store.delete_if_lease(
                    request.scope_key,
                    request.turn_id,
                    pending.lease_token,
                )
            if request.recover_lost_result:
                try:
                    recovered = self.history_store.recover_ready_turn(
                        request.scope_key,
                        request.turn_id,
                        request_fingerprint=request.request_fingerprint,
                        expected_lease_token=lease_token,
                    )
                except TurnInProgressError as exc:
                    return BeginTurnOutcome(
                        status="generating",
                        retry_after=exc.retry_after,
                        conversation=result.get("conversation"),
                    )
                if recovered.get("status") == "complete":
                    return BeginTurnOutcome(
                        status="complete",
                        messages=recovered.get("messages"),
                        conversation=result.get("conversation"),
                    )
                return BeginTurnOutcome(
                    status="new",
                    lease_token=recovered.get("lease_token"),
                    lease_expires_at=recovered.get("lease_expires_at"),
                    conversation=(
                        recovered.get("conversation")
                        or result.get("conversation")
                    ),
                )
            return BeginTurnOutcome(
                status="volatile_result_lost",
                error="Il risultato temporaneo del turno non è più disponibile",
                code="volatile_result_lost",
                conversation=result.get("conversation"),
            )
        if status == "complete":
            return BeginTurnOutcome(
                status="complete",
                messages=result.get("messages"),
                conversation=result.get("conversation"),
            )
        return BeginTurnOutcome(status=status, conversation=result.get("conversation"))

    def stage_result(
        self,
        scope_key: str,
        turn_id: str,
        *,
        lease_token: str,
        result: Any,
        result_digest: str,
    ) -> bool:
        if not self.enabled or not scope_key or not turn_id or not lease_token:
            return False
        stored = self.pending_store.put(
            scope_key,
            turn_id,
            lease_token=lease_token,
            result_digest=result_digest,
            result=result,
        )
        if not stored:
            return False
        try:
            marked = self.history_store.mark_turn_ready(
                scope_key, turn_id, lease_token, result_digest
            )
        except Exception as exc:
            self.pending_store.delete_if_lease(scope_key, turn_id, lease_token)
            log.warning("mark_turn_ready failed for %s: %s", turn_id, exc)
            return False
        if not marked:
            self.pending_store.delete_if_lease(scope_key, turn_id, lease_token)
            return False
        return True

    def complete_turn(
        self,
        scope_key: str,
        turn_id: str,
        *,
        lease_token: str,
        request_fingerprint: str,
        user_content: str,
        assistant_content: str,
        message_type: str = "text",
        request: Optional[TurnRequest] = None,
        selected_knowledge_base_ids: Optional[list[str]] = None,
        agent_id: str = "",
        agent_name: str = "",
        provider_id: str = "",
        model_id: str = "",
        prompt_ref: Optional[dict] = None,
        response_language: str = "auto",
        sources: Optional[list[dict]] = None,
        metadata: Optional[dict] = None,
        warm_conversation_id: Optional[str] = None,
        warm_knowledge_base_ids: Optional[list[str]] = None,
    ) -> CompleteTurnOutcome:
        if not self.enabled:
            return CompleteTurnOutcome(status="disabled")

        if request is not None:
            selected_knowledge_base_ids = (
                selected_knowledge_base_ids
                if selected_knowledge_base_ids is not None
                else request.selected_knowledge_base_ids
            )
            agent_id = agent_id or request.agent_id
            agent_name = agent_name or request.agent_name
            provider_id = provider_id or request.provider_id
            model_id = model_id or request.model_id
            prompt_ref = prompt_ref if prompt_ref is not None else request.prompt_ref
            response_language = response_language or request.response_language

        try:
            result = self.history_store.complete_turn(
                scope_key=scope_key,
                turn_id=turn_id,
                lease_token=lease_token,
                request_fingerprint=request_fingerprint,
                user_content=user_content,
                assistant_content=assistant_content,
                message_type=message_type,
                selected_knowledge_base_ids=selected_knowledge_base_ids,
                agent_id=agent_id,
                agent_name=agent_name,
                provider_id=provider_id,
                model_id=model_id,
                prompt_ref=prompt_ref,
                response_language=response_language,
                sources=sources,
                metadata=metadata,
            )
        except TurnConflictError as exc:
            self.pending_store.delete_if_lease(scope_key, turn_id, lease_token)
            return CompleteTurnOutcome(status="conflict", error=str(exc))

        if result.get("replayed"):
            messages = result.get("messages")
            self.pending_store.delete_if_lease(scope_key, turn_id, lease_token)
            if warm_conversation_id:
                # A worker may have committed durably and crashed before its
                # warm-memory append. Rebuild from the authoritative history
                # on replay instead of guessing whether the last user text is
                # a duplicate (identical consecutive questions are valid).
                try:
                    self.rehydrate_for_prompt(scope_key, warm_conversation_id)
                except Exception as exc:
                    log.warning(
                        "warm memory replay reconciliation failed for %s: %s",
                        turn_id,
                        exc,
                    )
            return CompleteTurnOutcome(
                status="complete", replayed=True, messages=messages
            )

        self.pending_store.delete_if_lease(scope_key, turn_id, lease_token)

        summary_job = None
        if warm_conversation_id:
            try:
                summary_job = self.memory_store.append_turn(
                    warm_conversation_id,
                    user=user_content,
                    assistant=assistant_content,
                    knowledge_base_ids=warm_knowledge_base_ids
                    or selected_knowledge_base_ids,
                    assistant_sequence=result.get("assistant_sequence"),
                )
            except Exception as exc:
                log.warning("warm memory append failed for %s: %s", turn_id, exc)
                self.rehydrate_for_prompt(scope_key, warm_conversation_id)

        return CompleteTurnOutcome(
            status="complete",
            replayed=False,
            messages=result.get("messages"),
            user_sequence=result.get("user_sequence"),
            assistant_sequence=result.get("assistant_sequence"),
            title=result.get("title"),
            message_count=result.get("message_count"),
            payload_bytes=result.get("payload_bytes"),
            summary_job=summary_job,
        )

    def fail_turn(
        self,
        scope_key: str,
        turn_id: str,
        lease_token: Optional[str],
    ) -> bool:
        if not self.enabled:
            return False
        if not lease_token:
            return False
        self.pending_store.delete_if_lease(scope_key, turn_id, lease_token)
        try:
            return self.history_store.fail_turn(scope_key, turn_id, lease_token)
        except Exception as exc:
            log.warning("fail_turn failed for %s: %s", turn_id, exc)
            return False

    def renew_turn_lease(
        self,
        scope_key: str,
        turn_id: str,
        lease_token: Optional[str],
    ) -> bool:
        """Extend an owned generating/ready lease.

        Streaming integrations can call this periodically while provider work
        is active. Ownership loss is reported as ``False`` rather than hidden.
        """

        if not self.enabled or not lease_token:
            return False
        try:
            return self.history_store.renew_turn_lease(
                scope_key, turn_id, lease_token
            )
        except Exception as exc:
            log.warning("renew_turn_lease failed for %s: %s", turn_id, exc)
            return False

    # ------------------------------------------------------------------
    # Hydration
    # ------------------------------------------------------------------

    def hydrate_for_prompt(
        self,
        scope_key: str,
        warm_conversation_id: str,
    ) -> bool:
        """Populate warm memory from durable history for prompt context."""

        for _attempt in range(3):
            hydrated = self._hydrate_for_prompt(
                scope_key,
                warm_conversation_id,
                replace=False,
            )
            if hydrated is not None:
                return hydrated
        return False

    def sync_for_prompt(
        self,
        scope_key: str,
        warm_conversation_id: str,
    ) -> bool:
        """Catch warm memory up to every committed assistant sequence.

        ``hydrate_if_absent`` is sufficient after TTL, but not in the small
        window between a SQLite commit and another worker's warm append.  The
        coverage check also detects out-of-order states such as sequences 2,6
        with 4 missing and rebuilds those states from SQLite.
        """

        if not self.enabled or not warm_conversation_id:
            return False
        try:
            conversation = self.history_store.get_by_scope_key(scope_key)
        except Exception as exc:
            log.warning("sync: get_by_scope_key failed for %s: %s", scope_key, exc)
            return False
        if not conversation:
            return False
        target_sequence = int(conversation.get("message_count") or 0)
        durable_kb_ids = {
            str(item.get("knowledge_base_id"))
            for item in conversation.get("knowledge_base_ids", [])
            if isinstance(item, dict) and item.get("knowledge_base_id")
        }
        try:
            sequence_is_current = self.memory_store.durable_state_is_current(
                warm_conversation_id,
                target_sequence,
            )
            warm_kb_ids = self.memory_store.knowledge_base_ids(
                warm_conversation_id
            )
            if sequence_is_current and durable_kb_ids.issubset(warm_kb_ids):
                return False
        except Exception as exc:
            log.warning("sync: warm coverage check failed for %s: %s", scope_key, exc)
        return self.rehydrate_for_prompt(scope_key, warm_conversation_id)

    def rehydrate_for_prompt(
        self,
        scope_key: str,
        warm_conversation_id: str,
    ) -> bool:
        """Atomically refresh warm state from an authoritative DB snapshot."""

        # If a completion commits between the DB snapshot and the atomic
        # replace, the memory store rejects the older snapshot. Re-read so the
        # next attempt includes that just-committed sequence.
        for _attempt in range(3):
            if self._hydrate_for_prompt(
                scope_key,
                warm_conversation_id,
                replace=True,
            ) is True:
                return True
        return False

    def _hydrate_for_prompt(
        self,
        scope_key: str,
        warm_conversation_id: str,
        *,
        replace: bool,
    ) -> Optional[bool]:

        if not self.enabled or not warm_conversation_id:
            return False
        try:
            conv = self.history_store.get_by_scope_key(scope_key)
        except Exception as exc:
            log.warning("hydrate: get_by_scope_key failed for %s: %s", scope_key, exc)
            return False
        if not conv:
            return False
        try:
            compacted = self._compacted_hydration_tail(
                scope_key,
                conv,
            )
        except Exception as exc:
            log.warning("hydrate: list_messages failed for %s: %s", conv["id"], exc)
            return False
        if compacted is None:
            # A concurrent summarizer advanced the durable CAS. Let the caller
            # re-read the authoritative summary and tail.
            return None
        summary, summary_version, summary_through_sequence, turns = compacted

        durable_kb_ids = [
            kb["knowledge_base_id"]
            for kb in conv.get("knowledge_base_ids", [])
            if kb.get("knowledge_base_id")
        ]
        try:
            if replace:
                return self.memory_store.replace_from_durable(
                    warm_conversation_id,
                    summary=summary,
                    turns=turns,
                    knowledge_base_ids=durable_kb_ids,
                    version=summary_version,
                    through_sequence=summary_through_sequence,
                )
            return self.memory_store.hydrate_if_absent(
                warm_conversation_id,
                summary=summary,
                turns=turns,
                knowledge_base_ids=durable_kb_ids,
                version=summary_version,
                through_sequence=summary_through_sequence,
            )
        except Exception as exc:
            log.warning("hydrate: hydrate_if_absent failed: %s", exc)
            return False

    def _compacted_hydration_tail(
        self,
        scope_key: str,
        conversation: dict,
    ) -> Optional[tuple[str, int, int, list[ConversationTurn]]]:
        """Stream the durable tail, summarize old turns and retain only N.

        This is the cold-start fallback for histories whose asynchronous
        summarizer did not run.  It never constructs the full transcript in
        RAM/Redis and advances the durable summary cursor with CAS.
        """

        history_id = conversation["id"]
        summary = str(conversation.get("summary") or "")
        summary_version = int(conversation.get("summary_version") or 0)
        original_through = int(
            conversation.get("summary_through_sequence") or 0
        )
        through_sequence = original_through
        after_sequence = original_through
        recent_turns: list[ConversationTurn] = []
        pending_user: Optional[dict] = None
        keep = max(1, int(self.memory_store.recent_turns_to_keep))

        while True:
            page, next_after = self.history_store.list_messages_after_sequence(
                history_id,
                after_sequence=after_sequence,
                limit=HYDRATION_LIMIT_MESSAGES,
            )
            for message in page:
                if message.get("role") == "user":
                    pending_user = message
                    continue
                if message.get("role") != "assistant" or pending_user is None:
                    continue
                turn = ConversationTurn(
                    user=str(pending_user.get("content") or ""),
                    assistant=str(message.get("content") or ""),
                    assistant_sequence=int(message.get("sequence") or 0),
                )
                pending_user = None
                recent_turns.append(turn)
                if len(recent_turns) > keep:
                    evicted = recent_turns.pop(0)
                    summary = fallback_summary(
                        ConversationSummaryJob(
                            conversation_id=scope_key,
                            previous_summary=summary,
                            turns_to_summarize=[evicted],
                            recent_turns=list(recent_turns),
                            version=summary_version,
                        )
                    )
                    through_sequence = int(evicted.assistant_sequence or 0)
            if next_after is None:
                break
            if int(next_after) <= after_sequence:
                raise RuntimeError("conversation message cursor did not advance")
            after_sequence = int(next_after)

        if through_sequence > original_through:
            if not self.history_store.update_summary(
                scope_key,
                summary,
                expected_version=summary_version,
                through_sequence=through_sequence,
            ):
                return None
            summary_version += 1
        return summary, summary_version, through_sequence, recent_turns

    # ------------------------------------------------------------------
    # Pass-through helpers
    # ------------------------------------------------------------------

    def cleanup_expired(self, *, now: Optional[float] = None) -> dict:
        """Run durable retention and reconcile removed volatile state."""

        empty = {
            "conversations_deleted": 0,
            "turns_deleted": 0,
            "expired_leases_failed": 0,
            "deleted_scope_keys": [],
            "deleted_turns": [],
        }
        if not self.enabled:
            return empty
        try:
            result = self.history_store.cleanup_expired(now=now)
        except Exception as exc:
            log.warning("conversation retention cleanup failed: %s", exc)
            return empty
        for scope_key in result.get("deleted_scope_keys", []):
            try:
                self.memory_store.clear(scope_key)
                self.pending_store.clear_scope(scope_key)
            except Exception as exc:
                log.warning("volatile conversation cleanup failed for %s: %s", scope_key, exc)
        for item in result.get("deleted_turns", []):
            try:
                self.pending_store.delete(item["scope_key"], item["turn_id"])
            except Exception as exc:
                log.warning("pending turn cleanup failed: %s", exc)
        return result

    def update_summary(
        self,
        scope_key: str,
        summary: str,
        *,
        expected_version: int,
        through_sequence: int,
    ) -> bool:
        """Persist a summary produced by the warm-memory summarizer.

        This is the application integration hook: callers should pass the
        durable conversation's current ``summary_version`` and the last message
        sequence represented by the summary. The store performs CAS and cursor
        monotonicity checks.
        """

        if not self.enabled or not scope_key:
            return False
        return self.history_store.update_summary(
            scope_key,
            summary,
            expected_version=expected_version,
            through_sequence=through_sequence,
        )

    def get_conversation(self, history_id_or_scope_key: str) -> Optional[dict]:
        """Get a conversation by history_id (UUID) or scope_key.

        A value is treated as a UUID only when it matches the canonical
        8-4-4-4-12 hex format; anything else falls back to a scope_key
        lookup. This avoids a wasted DB hit on every scope_key query.
        """
        if not self.enabled:
            return None
        key = str(history_id_or_scope_key or "")
        if _UUID_RE.match(key):
            result = self.history_store.get(key)
            if result:
                return result
        return self.history_store.get_by_scope_key(key)

    def list_messages(
        self,
        history_id: str,
        *,
        before_sequence: Optional[int] = None,
        limit: int = 50,
    ) -> tuple[list[dict], Optional[int]]:
        """List messages with cursor pagination."""
        if not self.enabled:
            return [], None
        return self.history_store.list_messages(
            history_id,
            before_sequence=before_sequence,
            limit=limit
        )

    def list_conversations(
        self,
        *,
        page: int = 1,
        per_page: int = 20,
        status: Optional[str] = None,
    ) -> tuple[list[dict], dict]:
        """List conversations with pagination."""
        if not self.enabled:
            return [], {"page": page, "per_page": per_page, "total": 0}
        self.cleanup_expired()
        return self.history_store.list(page=page, per_page=per_page, status=status)

    def get_conversation_by_id(self, history_id: str) -> Optional[dict]:
        """Get a conversation by its UUID history_id."""
        if not self.enabled:
            return None
        return self.history_store.get(history_id)

    def update_conversation(
        self,
        history_id: str,
        *,
        title: Optional[str] = None,
        archived: Optional[bool] = None,
    ) -> Optional[dict]:
        """Rename and/or archive/unarchive a conversation.

        When both ``title`` and ``archived`` are provided, the rename is
        applied first and the archive/unarchive on the resulting row.
        Returns ``None`` when the conversation does not exist.
        """
        if not self.enabled:
            return None

        if title is None and archived is None:
            return self.history_store.get(history_id)

        try:
            return self.history_store.update_conversation(
                history_id,
                title=title,
                archived=archived,
            )
        except ConversationNotFoundError:
            return None

    def delete_conversation(
        self,
        history_id: str,
        *,
        workspace_upload_folder: Optional[str] = None,
    ) -> bool:
        """Hard delete a conversation."""
        if not self.enabled:
            return False
        from utils.index_lock import lifecycle_write_lock

        # Queries hold the matching global lifecycle read gate from turn
        # reservation through durable completion.  Taking the writer here
        # prevents a late CI result from creating a new artifact or message
        # while ownership is being snapshotted and deleted.
        with lifecycle_write_lock():
            conversation = self.history_store.get(history_id)
            scope_key = (conversation or {}).get("scope_key") or ""
            if conversation and scope_key:
                # Volatile state must disappear before the durable row that
                # tells us its scope. Backend failures abort before the
                # transactional durable delete.
                self.memory_store.clear(scope_key)
                self.pending_store.clear_scope(scope_key)
            if workspace_upload_folder:
                deleted, cleanup_plan = (
                    self.history_store.delete_with_artifact_cleanup(history_id)
                )
                if cleanup_plan.safe:
                    try:
                        cleanup_workspace_artifacts(
                            workspace_upload_folder,
                            cleanup_plan.exclusive,
                            strict=True,
                        )
                    except ArtifactCleanupError as exc:
                        # SQLite already owns a durable outbox item. A retry
                        # can therefore finish cleanup even though the
                        # conversation row has been removed atomically.
                        raise ConversationHistoryError(
                            "conversation_artifact_cleanup_failed",
                            code="artifact_cleanup_failed",
                        ) from exc
                    self.history_store.complete_artifact_cleanup(
                        f"conversation:{history_id}"
                    )
                else:
                    if not deleted:
                        return False
                    log.warning(
                        "Skipping unsafe artifact cleanup for conversation %s",
                        history_id,
                    )
                return deleted or cleanup_plan.safe
            if not conversation:
                return False
            return self.history_store.delete(history_id)

    def reset_continuity(self, history_id: str) -> dict:
        """Clean up expired leases and return the current parent turn id."""
        if not self.enabled:
            return {"parent_turn_id": None, "cleaned_up_leases": 0}
        return self.history_store.reset_continuity(history_id)


def _messages_to_turns(messages: list[dict]) -> list[ConversationTurn]:
    """Pair user/assistant messages into :class:`ConversationTurn` objects."""

    turns: list[ConversationTurn] = []
    i = 0
    while i + 1 < len(messages):
        current = messages[i]
        following = messages[i + 1]
        if current.get("role") == "user" and following.get("role") == "assistant":
            turns.append(
                ConversationTurn(
                    user=current.get("content", ""),
                    assistant=following.get("content", ""),
                    assistant_sequence=following.get("sequence"),
                )
            )
            i += 2
        else:
            i += 1
    return turns


# ---------------------------------------------------------------------------
# Fingerprint helpers
# ---------------------------------------------------------------------------

def compute_request_fingerprint(
    *,
    query: str,
    model: Optional[str] = None,
    provider: Optional[str] = None,
    temperature: Optional[float] = None,
    k: Optional[int] = None,
    knowledge_base_ids: Optional[list[str]] = None,
    system_prompt_id: Optional[str] = None,
    system_prompt_scope: Optional[str] = None,
    agent_id: Optional[str] = None,
    response_language: Optional[str] = None,
    client_context: Optional[dict] = None,
    use_code_interpreter: bool = False,
    attached_files: Optional[list[dict]] = None,
    extra: Optional[dict] = None,
) -> str:
    """Stable SHA-256 fingerprint of the request parameters.

    Used as the idempotency key for a turn: two requests with the same
    ``turn_id`` and the same fingerprint replay the same result, while a
    different fingerprint raises :class:`TurnConflictError`.
    """

    safe_attachments = []
    for attachment in attached_files or []:
        if not isinstance(attachment, dict):
            continue
        safe_attachments.append(
            {
                "id": str(
                    attachment.get("id") or attachment.get("file_id") or ""
                ),
                "name": str(attachment.get("name") or ""),
                "type": str(attachment.get("type") or ""),
                "digest": str(
                    attachment.get("digest") or attachment.get("sha256") or ""
                ),
            }
        )

    payload = {
        "query": str(query or ""),
        "model": str(model or ""),
        "provider": str(provider or ""),
        "temperature": None if temperature is None else round(float(temperature), 6),
        "k": k,
        "knowledge_base_ids": sorted(str(kb) for kb in (knowledge_base_ids or [])),
        "system_prompt_id": str(system_prompt_id or ""),
        "system_prompt_scope": str(system_prompt_scope or ""),
        "agent_id": str(agent_id or ""),
        "response_language": str(response_language or ""),
        "client_context": client_context or {},
        "use_code_interpreter": bool(use_code_interpreter),
        "attached_files": safe_attachments,
        "extra": extra or {},
    }
    try:
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError):
        # Validation normally guarantees JSON-safe values. Keep the helper
        # deterministic for internal callers that pass custom scalar objects.
        canonical = json.dumps(
            _canonical_fingerprint_value(payload),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _canonical_fingerprint_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {
            str(key): _canonical_fingerprint_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_canonical_fingerprint_value(item) for item in value]
    return str(value)


def derive_scope_kind(scope_key: str) -> str:
    """Infer the scope_kind from a scoped conversation id."""

    if ":multi-chat:" in scope_key:
        return "multi"
    if ":kb:" in scope_key:
        return "kb"
    return "default"


def extract_workspace_id_from_scope(scope_key: str) -> str:
    """Extract workspace_id from a scoped conversation key."""
    parts = scope_key.split(":")
    if not parts:
        raise ConversationTurnError("invalid scope_key", status_code=400)
    return parts[0]


def compute_result_digest(result: Any) -> str:
    """Stable digest of a result payload for turn-ready staging."""

    try:
        import json

        canonical = json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        canonical = str(result)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Per-workspace factory
# ---------------------------------------------------------------------------

import threading  # noqa: E402

import os  # noqa: E402

from collections import OrderedDict  # noqa: E402


# Singleton cache for per-workspace services with LRU eviction.
_service_cache: OrderedDict[str, ConversationService] = OrderedDict()
_SERVICE_CACHE_MAX_SIZE = 100
_service_cache_lock = threading.Lock()


def _config_value(app, key: str, default=None):
    if app is not None:
        cfg = getattr(app, "config", None)
        if isinstance(cfg, dict) and key in cfg:
            return cfg[key]
    return os.getenv(f"RAG_{key}", default)


def get_history_store_for_workspace(
    workspace_id: str,
    app=None,
) -> Optional[ConversationHistoryStore]:
    """Build a per-workspace history store.

    Returns ``None`` when history is disabled.
    """
    enabled = _config_value(app, "CONVERSATION_HISTORY_ENABLED", "0")
    if not _truthy(enabled):
        return None

    data_dir = _config_value(app, "WORKSPACE_DATA_DIR", "app/data/workspaces")

    return ConversationHistoryStore(
        workspace_id=workspace_id,
        workspace_data_dir=data_dir,
        max_conversations=int(_config_value(app, "MAX_CONVERSATIONS_PER_WORKSPACE", "200") or 200),
        max_pending_turns=int(_config_value(app, "MAX_PENDING_HISTORY_TURNS_PER_WORKSPACE", "100") or 100),
        max_messages_per_conversation=int(_config_value(app, "MAX_CONVERSATION_MESSAGES", "2000") or 2000),
        max_conversation_bytes=int(_config_value(app, "MAX_CONVERSATION_BYTES", "33554432") or 33554432),
        max_message_chars=int(_config_value(app, "MAX_CONVERSATION_HISTORY_MESSAGE_CHARS", "50000") or 50000),
        max_sources_bytes_per_turn=int(_config_value(app, "MAX_CONVERSATION_SOURCES_BYTES_PER_TURN", "262144") or 262144),
        max_metadata_bytes_per_turn=int(_config_value(app, "MAX_CONVERSATION_METADATA_BYTES_PER_TURN", "65536") or 65536),
        max_history_bytes=int(_config_value(app, "MAX_CONVERSATION_HISTORY_BYTES", "268435456") or 268435456),
        retention_days=int(_config_value(app, "CONVERSATION_HISTORY_RETENTION_DAYS", "90") or 90),
        lease_seconds=int(_config_value(app, "CONVERSATION_TURN_LEASE_SECONDS", "900") or 900),
        incomplete_turn_retention_days=int(_config_value(app, "INCOMPLETE_HISTORY_TURN_RETENTION_DAYS", "7") or 7),
    )


def _truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def get_conversation_service_for_workspace(
    workspace_id: str,
    app=None,
) -> ConversationService:
    """Get or create a per-workspace conversation service.

    Services are cached by ``workspace_id`` with LRU eviction when the
    cache exceeds ``_SERVICE_CACHE_MAX_SIZE`` entries. The cache is
    guarded by a lock so the service is built at most once per workspace
    even under concurrent requests.
    """
    if not workspace_id:
        raise ConversationTurnError(
            "workspace_id is required", status_code=400
        )

    with _service_cache_lock:
        cached = _service_cache.get(workspace_id)
        if cached is not None:
            _service_cache.move_to_end(workspace_id)
            return cached

        if len(_service_cache) >= _SERVICE_CACHE_MAX_SIZE:
            _service_cache.popitem(last=False)

        history = get_history_store_for_workspace(workspace_id, app)
        enabled = _truthy(_config_value(app, "CONVERSATION_HISTORY_ENABLED", "0"))
        service = ConversationService(
            history_store=history,
            workspace_id=workspace_id,
            app=app,
            enabled=enabled and history is not None,
        )
        _service_cache[workspace_id] = service
        return service


def reset_conversation_service() -> None:
    """Drop cached services (used by tests)."""
    with _service_cache_lock:
        _service_cache.clear()


def configure_conversation_service(service: Optional[ConversationService]) -> None:
    """Inject a service instance for a specific workspace (used by tests)."""
    with _service_cache_lock:
        if service and service._workspace_id:
            _service_cache[service._workspace_id] = service
        else:
            _service_cache.clear()
