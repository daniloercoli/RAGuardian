import json
import logging
import threading
import time
import sys
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Optional

from utils.state_backend import (
    configured_state_backend,
    redis_connection,
    redis_scan_delete,
    state_key_prefix,
)


CONVERSATION_SUMMARY_THRESHOLD_CHARS = 12000
CONVERSATION_PROMPT_MAX_CHARS = 8000
CONVERSATION_RETRIEVAL_MAX_CHARS = 3000
CONVERSATION_SUMMARY_MAX_CHARS = 3000
CONVERSATION_RECENT_TURNS_TO_KEEP = 4
CONVERSATION_MAX_STORED_MESSAGE_CHARS = 6000
CONVERSATION_TTL_SECONDS = 6 * 60 * 60
CONVERSATION_MAX_ITEMS = 100
log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ConversationTurn:
    user: str
    assistant: str


@dataclass(frozen=True)
class ConversationSummaryJob:
    conversation_id: str
    previous_summary: str
    turns_to_summarize: list[ConversationTurn]
    recent_turns: list[ConversationTurn]
    version: int


@dataclass
class ConversationState:
    summary: str = ""
    turns: list[ConversationTurn] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    version: int = 0


class ConversationMemoryStore:
    """Thread-safe in-process memory for short-lived chat conversations."""

    def __init__(
        self,
        *,
        summary_threshold_chars: int = CONVERSATION_SUMMARY_THRESHOLD_CHARS,
        prompt_max_chars: int = CONVERSATION_PROMPT_MAX_CHARS,
        retrieval_max_chars: int = CONVERSATION_RETRIEVAL_MAX_CHARS,
        summary_max_chars: int = CONVERSATION_SUMMARY_MAX_CHARS,
        recent_turns_to_keep: int = CONVERSATION_RECENT_TURNS_TO_KEEP,
        ttl_seconds: int = CONVERSATION_TTL_SECONDS,
        max_items: int = CONVERSATION_MAX_ITEMS,
    ):
        self.summary_threshold_chars = summary_threshold_chars
        self.prompt_max_chars = prompt_max_chars
        self.retrieval_max_chars = retrieval_max_chars
        self.summary_max_chars = summary_max_chars
        self.recent_turns_to_keep = max(1, recent_turns_to_keep)
        self.ttl_seconds = ttl_seconds
        self.max_items = max_items
        self._lock = threading.RLock()
        self._conversations: OrderedDict[str, ConversationState] = OrderedDict()

    def render_for_prompt(self, conversation_id: Optional[str], *, max_chars: Optional[int] = None) -> str:
        if not conversation_id:
            return ""

        with self._lock:
            state = self._get_state(conversation_id, create=False)
            if not state:
                return ""
            state.updated_at = time.time()
            self._conversations.move_to_end(conversation_id)
            return _trim_left(_render_state(state), max_chars or self.prompt_max_chars)

    def render_for_retrieval(self, conversation_id: Optional[str]) -> str:
        return self.render_for_prompt(conversation_id, max_chars=self.retrieval_max_chars)

    def append_turn(
        self,
        conversation_id: Optional[str],
        *,
        user: str,
        assistant: str,
    ) -> Optional[ConversationSummaryJob]:
        if not conversation_id:
            return None

        turn = ConversationTurn(
            user=_clamp_text(user, CONVERSATION_MAX_STORED_MESSAGE_CHARS),
            assistant=_clamp_text(assistant, CONVERSATION_MAX_STORED_MESSAGE_CHARS),
        )

        with self._lock:
            state = self._get_state(conversation_id, create=True)
            state.turns.append(turn)
            state.updated_at = time.time()
            state.version += 1
            self._conversations.move_to_end(conversation_id)
            self._evict_if_needed()

            if _state_size(state) <= self.summary_threshold_chars:
                return None

            split_index = self._summary_split_index(state.turns)
            if split_index <= 0:
                return None

            return ConversationSummaryJob(
                conversation_id=conversation_id,
                previous_summary=state.summary,
                turns_to_summarize=list(state.turns[:split_index]),
                recent_turns=list(state.turns[split_index:]),
                version=state.version,
            )

    def apply_summary(self, job: ConversationSummaryJob, summary: str) -> bool:
        with self._lock:
            state = self._get_state(job.conversation_id, create=False)
            if not state or state.version != job.version:
                return False

            state.summary = _clamp_text(summary.strip(), self.summary_max_chars)
            state.turns = list(job.recent_turns)
            state.updated_at = time.time()
            state.version += 1
            self._conversations.move_to_end(job.conversation_id)
            return True

    def clear(self, conversation_id: Optional[str]) -> bool:
        if not conversation_id:
            return False

        with self._lock:
            return self._conversations.pop(conversation_id, None) is not None

    def clear_all(self) -> None:
        with self._lock:
            self._conversations.clear()

    def clear_by_prefix(self, prefix: str) -> int:
        """Remove all conversations starting with given prefix. Returns count removed."""
        if not prefix:
            return 0
        with self._lock:
            to_remove = [cid for cid in self._conversations if cid.startswith(prefix)]
            for cid in to_remove:
                self._conversations.pop(cid, None)
            return len(to_remove)

    def _get_state(self, conversation_id: str, *, create: bool) -> Optional[ConversationState]:
        self._purge_expired()
        state = self._conversations.get(conversation_id)
        if state or not create:
            return state

        state = ConversationState()
        self._conversations[conversation_id] = state
        return state

    def _purge_expired(self) -> None:
        now = time.time()
        expired = [
            conversation_id
            for conversation_id, state in self._conversations.items()
            if now - state.updated_at > self.ttl_seconds
        ]
        for conversation_id in expired:
            self._conversations.pop(conversation_id, None)

    def _evict_if_needed(self) -> None:
        while len(self._conversations) > self.max_items:
            self._conversations.popitem(last=False)

    def _summary_split_index(self, turns: list[ConversationTurn]) -> int:
        if len(turns) <= self.recent_turns_to_keep:
            return max(0, len(turns) - 1)
        return len(turns) - self.recent_turns_to_keep


class RedisConversationMemoryStore(ConversationMemoryStore):
    """Redis-backed conversation memory for multi-worker deployments."""

    def __init__(self, *, redis_client=None, key_prefix: Optional[str] = None, **kwargs):
        super().__init__(**kwargs)
        self._redis = redis_client or redis_connection()
        self._key_prefix = key_prefix or f"{state_key_prefix()}:conversation"

    @property
    def backend(self) -> str:
        return "redis"

    def render_for_prompt(self, conversation_id: Optional[str], *, max_chars: Optional[int] = None) -> str:
        if not conversation_id:
            return ""

        with self._redis_lock(conversation_id):
            state = self._load_state(conversation_id)
            if not state:
                return ""
            state.updated_at = time.time()
            self._save_state(conversation_id, state)
            return _trim_left(_render_state(state), max_chars or self.prompt_max_chars)

    def render_for_retrieval(self, conversation_id: Optional[str]) -> str:
        return self.render_for_prompt(conversation_id, max_chars=self.retrieval_max_chars)

    def append_turn(
        self,
        conversation_id: Optional[str],
        *,
        user: str,
        assistant: str,
    ) -> Optional[ConversationSummaryJob]:
        if not conversation_id:
            return None

        turn = ConversationTurn(
            user=_clamp_text(user, CONVERSATION_MAX_STORED_MESSAGE_CHARS),
            assistant=_clamp_text(assistant, CONVERSATION_MAX_STORED_MESSAGE_CHARS),
        )

        with self._redis_lock(conversation_id):
            state = self._load_state(conversation_id) or ConversationState()
            state.turns.append(turn)
            state.updated_at = time.time()
            state.version += 1
            self._save_state(conversation_id, state)

            if _state_size(state) <= self.summary_threshold_chars:
                return None

            split_index = self._summary_split_index(state.turns)
            if split_index <= 0:
                return None

            return ConversationSummaryJob(
                conversation_id=conversation_id,
                previous_summary=state.summary,
                turns_to_summarize=list(state.turns[:split_index]),
                recent_turns=list(state.turns[split_index:]),
                version=state.version,
            )

    def apply_summary(self, job: ConversationSummaryJob, summary: str) -> bool:
        with self._redis_lock(job.conversation_id):
            state = self._load_state(job.conversation_id)
            if not state or state.version != job.version:
                return False

            state.summary = _clamp_text(summary.strip(), self.summary_max_chars)
            state.turns = list(job.recent_turns)
            state.updated_at = time.time()
            state.version += 1
            self._save_state(job.conversation_id, state)
            return True

    def clear(self, conversation_id: Optional[str]) -> bool:
        if not conversation_id:
            return False
        return bool(self._redis.delete(self._state_key(conversation_id)))

    def clear_all(self) -> None:
        redis_scan_delete(self._redis, f"{self._key_prefix}:*")
        redis_scan_delete(self._redis, f"{self._key_prefix}:lock:*")

    def clear_by_prefix(self, prefix: str) -> int:
        """Remove all conversations starting with given prefix. Returns count removed."""
        if not prefix:
            return 0
        count = redis_scan_delete(self._redis, f"{self._key_prefix}:{prefix}*")
        redis_scan_delete(self._redis, f"{self._key_prefix}:lock:{prefix}*")
        return count

    def _state_key(self, conversation_id: str) -> str:
        return f"{self._key_prefix}:{conversation_id}"

    def _lock_key(self, conversation_id: str) -> str:
        return f"{self._key_prefix}:lock:{conversation_id}"

    def _redis_lock(self, conversation_id: str):
        return self._redis.lock(
            self._lock_key(conversation_id),
            timeout=10,
            blocking_timeout=5,
        )

    def _load_state(self, conversation_id: str) -> Optional[ConversationState]:
        raw = self._redis.get(self._state_key(conversation_id))
        if not raw:
            return None
        try:
            payload = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
            state = ConversationState(
                summary=str(payload.get("summary") or ""),
                turns=[
                    ConversationTurn(
                        user=str(turn.get("user") or ""),
                        assistant=str(turn.get("assistant") or ""),
                    )
                    for turn in payload.get("turns", [])
                    if isinstance(turn, dict)
                ],
                created_at=float(payload.get("created_at") or time.time()),
                updated_at=float(payload.get("updated_at") or time.time()),
                version=int(payload.get("version") or 0),
            )
        except (TypeError, ValueError, json.JSONDecodeError, UnicodeDecodeError):
            self._redis.delete(self._state_key(conversation_id))
            return None
        if time.time() - state.updated_at > self.ttl_seconds:
            self._redis.delete(self._state_key(conversation_id))
            return None
        return state

    def _save_state(self, conversation_id: str, state: ConversationState) -> None:
        payload = {
            "schema_version": 1,
            "summary": state.summary,
            "turns": [
                {"user": turn.user, "assistant": turn.assistant}
                for turn in state.turns
            ],
            "created_at": state.created_at,
            "updated_at": state.updated_at,
            "version": state.version,
        }
        self._redis.setex(
            self._state_key(conversation_id),
            self.ttl_seconds,
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        )


def _build_default_store() -> ConversationMemoryStore:
    if configured_state_backend() == "redis":
        try:
            return RedisConversationMemoryStore()
        except Exception as exc:
            log.warning("Redis conversation store unavailable, falling back to memory: %s", exc)
    return ConversationMemoryStore()


_store = _build_default_store()

sys.modules.setdefault("utils.conversation_memory", sys.modules[__name__])
sys.modules.setdefault("app.utils.conversation_memory", sys.modules[__name__])


def get_conversation_store() -> ConversationMemoryStore:
    return _store


def reset_conversation_store() -> None:
    _store.clear_all()


def conversation_store_backend() -> str:
    return getattr(_store, "backend", "memory")


def format_turns(turns: list[ConversationTurn]) -> str:
    return "\n\n".join(
        f"Utente:\n{turn.user}\n\nAssistente:\n{turn.assistant}"
        for turn in turns
    )


def fallback_summary(job: ConversationSummaryJob) -> str:
    parts = []
    if job.previous_summary:
        parts.append(job.previous_summary)
    parts.append(format_turns(job.turns_to_summarize))
    return _clamp_text("\n\n".join(part for part in parts if part).strip(), CONVERSATION_SUMMARY_MAX_CHARS)


def _render_state(state: ConversationState) -> str:
    parts = []
    if state.summary:
        parts.append(f"Riassunto della conversazione precedente:\n{state.summary}")
    if state.turns:
        parts.append("Turni recenti:\n" + format_turns(state.turns))
    return "\n\n".join(parts)


def _state_size(state: ConversationState) -> int:
    return len(_render_state(state))


def _trim_left(value: str, max_chars: int) -> str:
    if len(value) <= max_chars:
        return value
    return "[...parte iniziale della conversazione omessa...]\n" + value[-max_chars:]


def _clamp_text(value: str, max_chars: int) -> str:
    value = (value or "").strip()
    if len(value) <= max_chars:
        return value

    marker = "\n[...contenuto abbreviato...]\n"
    head = max_chars // 2
    tail = max_chars - head - len(marker)
    if tail <= 0:
        return value[:max_chars]
    return value[:head].rstrip() + marker + value[-tail:].lstrip()
