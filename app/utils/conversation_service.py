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
no-op and the routes fall back to the legacy in-memory only behaviour.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from utils.conversation_history_store import (
    ConversationHistoryStore,
    ConversationNotFoundError,
    ContinuityError,
    QuotaExceededError,
    TurnConflictError,
    TurnInProgressError,
)
from utils.conversation_memory import (
    ConversationMemoryStore,
    ConversationTurn,
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
        result = {"error": self.message, "status": self.code}
        result.update(self.payload)
        return result


TURN_REPLAY_LIMIT_MESSAGES = 20


@dataclass(frozen=True)
class BeginTurnOutcome:
    """Result of :meth:`ConversationService.begin_turn`.

    ``status`` is one of:

    * ``disabled``  – history is off; proceed with legacy in-memory flow.
    * ``new``       – a reservation was created; generation should proceed.
    * ``ready``     – a staged result is available for replay (``result``).
    * ``complete``  – the turn already finished (``messages`` for replay).
    * ``generating``– another worker holds the lease (``retry_after``).
    * ``conflict``  – same turn_id, different fingerprint.
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
    expected_parent_turn_id: Optional[str] = None


@dataclass(frozen=True)
class CompleteTurnOutcome:
    status: str
    replayed: bool = False
    messages: Optional[list[dict]] = None
    title: Optional[str] = None
    message_count: Optional[int] = None
    payload_bytes: Optional[int] = None
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
                status="error",
                error=str(exc),
                expected_parent_turn_id=exc.expected_parent_turn_id,
            )
        except QuotaExceededError as exc:
            return BeginTurnOutcome(status="error", error=str(exc))

        status = result["status"]
        if status == "new":
            return BeginTurnOutcome(
                status="new",
                lease_token=result.get("lease_token"),
                lease_expires_at=result.get("lease_expires_at"),
                conversation=result.get("conversation"),
            )
        if status == "ready":
            pending = self.pending_store.get(request.scope_key, request.turn_id)
            if pending is not None:
                return BeginTurnOutcome(
                    status="ready",
                    result=pending.result,
                    result_digest=pending.result_digest,
                    conversation=result.get("conversation"),
                )
            return BeginTurnOutcome(
                status="generating",
                retry_after=5,
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
        self.pending_store.put(
            scope_key,
            turn_id,
            lease_token=lease_token,
            result_digest=result_digest,
            result=result,
        )
        try:
            self.history_store.mark_turn_ready(
                scope_key, turn_id, lease_token, result_digest
            )
        except Exception as exc:
            log.warning("mark_turn_ready failed for %s: %s", turn_id, exc)
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
            self.pending_store.delete(scope_key, turn_id)
            return CompleteTurnOutcome(status="conflict", error=str(exc))

        if result.get("replayed"):
            messages = result.get("messages")
            self.pending_store.delete(scope_key, turn_id)
            return CompleteTurnOutcome(
                status="complete", replayed=True, messages=messages
            )

        self.pending_store.delete(scope_key, turn_id)

        if warm_conversation_id:
            try:
                self.memory_store.append_turn_once(
                    warm_conversation_id,
                    user=user_content,
                    assistant=assistant_content,
                    knowledge_base_ids=warm_knowledge_base_ids
                    or selected_knowledge_base_ids,
                )
            except Exception as exc:
                log.warning("warm memory append failed for %s: %s", turn_id, exc)

        return CompleteTurnOutcome(
            status="complete",
            replayed=False,
            title=result.get("title"),
            message_count=result.get("message_count"),
            payload_bytes=result.get("payload_bytes"),
        )

    def fail_turn(
        self,
        scope_key: str,
        turn_id: str,
        lease_token: Optional[str],
    ) -> bool:
        if not self.enabled:
            return False
        self.pending_store.delete(scope_key, turn_id)
        if not lease_token:
            return False
        try:
            return self.history_store.fail_turn(scope_key, turn_id, lease_token)
        except Exception as exc:
            log.warning("fail_turn failed for %s: %s", turn_id, exc)
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
            messages, _ = self.history_store.list_messages(
                conv["id"], limit=TURN_REPLAY_LIMIT_MESSAGES
            )
        except Exception as exc:
            log.warning("hydrate: list_messages failed for %s: %s", conv["id"], exc)
            return False

        turns = _messages_to_turns(messages)
        selected_kb_ids = [
            kb["knowledge_base_id"]
            for kb in conv.get("knowledge_base_ids", [])
            if kb.get("is_selected")
        ]
        try:
            return self.memory_store.hydrate_if_absent(
                warm_conversation_id,
                summary=conv.get("summary", ""),
                turns=turns,
                knowledge_base_ids=selected_kb_ids,
                version=conv.get("summary_version"),
            )
        except Exception as exc:
            log.warning("hydrate: hydrate_if_absent failed: %s", exc)
            return False

    # ------------------------------------------------------------------
    # Pass-through helpers
    # ------------------------------------------------------------------

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

        result: Optional[dict] = None
        if title is not None:
            try:
                result = self.history_store.rename(history_id, title)
            except ConversationNotFoundError:
                return None
        if archived is not None:
            try:
                if archived:
                    result = self.history_store.archive(history_id)
                else:
                    result = self.history_store.unarchive(history_id)
            except ConversationNotFoundError:
                return None
        return result

    def delete_conversation(self, history_id: str) -> bool:
        """Hard delete a conversation."""
        if not self.enabled:
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
    response_language: Optional[str] = None,
    client_context: Optional[dict] = None,
    extra: Optional[dict] = None,
) -> str:
    """Stable SHA-256 fingerprint of the request parameters.

    Used as the idempotency key for a turn: two requests with the same
    ``turn_id`` and the same fingerprint replay the same result, while a
    different fingerprint raises :class:`TurnConflictError`.
    """

    payload = {
        "query": str(query or ""),
        "model": str(model or ""),
        "provider": str(provider or ""),
        "temperature": None if temperature is None else round(float(temperature), 6),
        "k": k,
        "knowledge_base_ids": sorted(str(kb) for kb in (knowledge_base_ids or [])),
        "system_prompt_id": str(system_prompt_id or ""),
        "response_language": str(response_language or ""),
        "client_context": client_context or {},
        "extra": extra or {},
    }
    canonical = repr(sorted(payload.items(), key=lambda item: item[0]))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


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
