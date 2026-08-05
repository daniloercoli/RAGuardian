"""Conversation history API routes.

Implements:
- GET /api/conversations - paginated list
- GET /api/conversations/<history_id> - conversation record
- GET /api/conversations/<history_id>/messages - cursor-based message list
- PATCH /api/conversations/<history_id> - rename/archive/unarchive
- DELETE /api/conversations/<history_id> - hard delete
- POST /api/conversations/<history_id>/reset-continuity - clean expired leases
"""

from __future__ import annotations

import logging
import re
from typing import Optional

from flask import jsonify, request
from werkzeug.exceptions import BadRequest, NotFound

from utils.auth import require_login
from utils.conversation_history_store import (
    ContinuityError,
    ConversationHistoryError,
    ConversationNotFoundError,
    QuotaExceededError,
    TITLE_RENAME_MAX_LEN,
    TurnConflictError,
    TurnInProgressError,
)
from utils.conversation_service import (
    get_conversation_service_for_workspace,
)
from utils.workspace import workspace_from_request


logger = logging.getLogger(__name__)

_HISTORY_ID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def _is_valid_history_id(value: str) -> bool:
    return bool(_HISTORY_ID_RE.match(str(value or "")))


def _parse_int(value: str, *, name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise BadRequest(f"{name} must be an integer")
    if parsed < minimum:
        raise BadRequest(f"{name} must be >= {minimum}")
    if parsed > maximum:
        return maximum
    return parsed if parsed != 0 or default != 0 else default


def _require_workspace():
    workspace = workspace_from_request()
    return workspace.workspace_id


def _generic_error(status: int, message: str = "Internal error"):
    return jsonify({"error": message}), status


def _map_store_error(exc: ConversationHistoryError):
    """Map a ConversationHistoryError to an appropriate HTTP response.

    - ConversationNotFoundError -> 404
    - TurnConflictError -> 409
    - TurnInProgressError -> 409 with Retry-After header
    - ContinuityError -> 409 with expected_parent_turn_id
    - QuotaExceededError -> 429
    - generic ConversationHistoryError -> 400
    """
    if isinstance(exc, ConversationNotFoundError):
        return jsonify({"error": "Conversation not found", "code": exc.code}), 404
    if isinstance(exc, TurnConflictError):
        return jsonify({"error": str(exc), "code": exc.code}), 409
    if isinstance(exc, TurnInProgressError):
        resp = jsonify(
            {"error": str(exc), "code": exc.code, "retry_after": exc.retry_after}
        )
        resp.headers["Retry-After"] = str(exc.retry_after)
        return resp, 409
    if isinstance(exc, ContinuityError):
        return (
            jsonify(
                {
                    "error": str(exc),
                    "code": exc.code,
                    "expected_parent_turn_id": exc.expected_parent_turn_id,
                }
            ),
            409,
        )
    if isinstance(exc, QuotaExceededError):
        return jsonify({"error": str(exc), "code": exc.code}), 429
    return jsonify({"error": str(exc), "code": exc.code}), 400


def register_conversation_routes(app) -> None:
    @app.errorhandler(BadRequest)
    def _conversations_bad_request(err):
        return jsonify({"error": err.description or "Bad request"}), 400

    @app.errorhandler(NotFound)
    def _conversations_not_found(err):
        return jsonify({"error": err.description or "Not found"}), 404

    @app.route("/api/conversations", methods=["GET"])
    @require_login
    def conversations_list():
        """List conversations for current workspace with pagination."""
        workspace_id = _require_workspace()
        page = _parse_int(
            request.args.get("page", "1"), name="page", default=1, minimum=1, maximum=10_000
        )
        per_page = _parse_int(
            request.args.get("per_page", "20"),
            name="per_page",
            default=20,
            minimum=1,
            maximum=100,
        )

        status_filter: Optional[str] = request.args.get("status")
        archived_param = request.args.get("archived")
        if status_filter not in {"active", "archived", None}:
            raise BadRequest("status must be 'active' or 'archived'")
        if archived_param is not None:
            if archived_param.lower() not in {"true", "false"}:
                raise BadRequest("archived must be 'true' or 'false'")
            if status_filter is None:
                status_filter = "archived" if archived_param.lower() == "true" else "active"

        service = get_conversation_service_for_workspace(workspace_id)
        if not service.enabled:
            return jsonify(
                {
                    "conversations": [],
                    "pagination": {
                        "page": page,
                        "per_page": per_page,
                        "total": 0,
                        "page_count": 0,
                        "has_prev": False,
                        "has_next": False,
                    },
                }
            )

        try:
            conversations, pagination = service.list_conversations(
                page=page, per_page=per_page, status=status_filter
            )
        except ConversationHistoryError as exc:
            return _map_store_error(exc)
        except Exception:
            logger.exception("conversations_list failed for workspace %s", workspace_id)
            return _generic_error(500)
        return jsonify({"conversations": conversations, "pagination": pagination})

    @app.route("/api/conversations/<history_id>", methods=["GET"])
    @require_login
    def conversations_get(history_id: str):
        """Get a specific conversation by history_id."""
        if not _is_valid_history_id(history_id):
            raise BadRequest("history_id must be a UUID")
        workspace_id = _require_workspace()
        service = get_conversation_service_for_workspace(workspace_id)
        if not service.enabled:
            raise NotFound("History disabled")

        try:
            conversation = service.get_conversation_by_id(history_id)
        except ConversationHistoryError as exc:
            return _map_store_error(exc)
        except Exception:
            logger.exception("conversations_get failed for %s", history_id)
            return _generic_error(500)
        if not conversation:
            raise NotFound("Conversation not found")
        return jsonify(conversation)

    @app.route("/api/conversations/<history_id>/messages", methods=["GET"])
    @require_login
    def conversations_get_messages(history_id: str):
        """Get messages for a conversation with cursor pagination."""
        if not _is_valid_history_id(history_id):
            raise BadRequest("history_id must be a UUID")
        workspace_id = _require_workspace()
        service = get_conversation_service_for_workspace(workspace_id)
        if not service.enabled:
            return jsonify({"messages": [], "next_cursor": None})

        before_sequence_arg = request.args.get("before_sequence")
        before_sequence: Optional[int] = None
        if before_sequence_arg:
            try:
                before_sequence = int(before_sequence_arg)
            except ValueError:
                raise BadRequest("before_sequence must be an integer")
            if before_sequence < 0:
                raise BadRequest("before_sequence must be >= 0")

        limit = _parse_int(
            request.args.get("limit", "50"), name="limit", default=50, minimum=1, maximum=200
        )

        try:
            messages, next_cursor = service.list_messages(
                history_id,
                before_sequence=before_sequence,
                limit=limit,
            )
        except ConversationHistoryError as exc:
            return _map_store_error(exc)
        except Exception:
            logger.exception("conversations_get_messages failed for %s", history_id)
            return _generic_error(500)
        return jsonify(
            {
                "messages": messages,
                "next_cursor": next_cursor,
                "limit": limit,
            }
        )

    @app.route("/api/conversations/<history_id>", methods=["PATCH"])
    @require_login
    def conversations_update(history_id: str):
        """Rename and/or archive/unarchive a conversation."""
        if not _is_valid_history_id(history_id):
            raise BadRequest("history_id must be a UUID")
        workspace_id = _require_workspace()
        service = get_conversation_service_for_workspace(workspace_id)
        if not service.enabled:
            raise NotFound("History disabled")

        data = request.get_json(silent=True) or {}
        title = data.get("title")
        archived = data.get("archived")

        if title is not None:
            title = str(title).strip()
            if not title:
                raise BadRequest("title cannot be empty")
            if len(title) > TITLE_RENAME_MAX_LEN:
                raise BadRequest(
                    f"title must be <= {TITLE_RENAME_MAX_LEN} characters"
                )

        if archived is not None:
            if not isinstance(archived, bool):
                raise BadRequest("archived must be a boolean")

        if title is None and archived is None:
            raise BadRequest("Provide 'title' and/or 'archived' to update")

        try:
            conversation = service.update_conversation(
                history_id,
                title=title,
                archived=archived,
            )
        except ConversationHistoryError as exc:
            return _map_store_error(exc)
        except Exception:
            logger.exception("conversations_update failed for %s", history_id)
            return _generic_error(500)
        if not conversation:
            raise NotFound("Conversation not found")
        return jsonify(conversation)

    @app.route("/api/conversations/<history_id>", methods=["DELETE"])
    @require_login
    def conversations_delete(history_id: str):
        """Hard delete a conversation."""
        if not _is_valid_history_id(history_id):
            raise BadRequest("history_id must be a UUID")
        workspace_id = _require_workspace()
        service = get_conversation_service_for_workspace(workspace_id)
        if not service.enabled:
            return jsonify({"deleted": False})

        try:
            deleted = service.delete_conversation(history_id)
        except ConversationHistoryError as exc:
            return _map_store_error(exc)
        except Exception:
            logger.exception("conversations_delete failed for %s", history_id)
            return _generic_error(500)
        if not deleted:
            raise NotFound("Conversation not found")
        return jsonify({"deleted": True})

    @app.route(
        "/api/conversations/<history_id>/reset-continuity", methods=["POST"]
    )
    @require_login
    def conversations_reset_continuity(history_id: str):
        """Clean up expired turn leases and return the current parent turn id.

        Useful when a client observes a stale ``generating`` turn that is
        blocking new turns: this endpoint fails expired leases and reports
        the ``parent_turn_id`` the next ``begin_turn`` should reference.
        """
        if not _is_valid_history_id(history_id):
            raise BadRequest("history_id must be a UUID")
        workspace_id = _require_workspace()
        service = get_conversation_service_for_workspace(workspace_id)
        if not service.enabled:
            return jsonify(
                {"parent_turn_id": None, "cleaned_up_leases": 0}
            )

        try:
            result = service.reset_continuity(history_id)
        except ConversationHistoryError as exc:
            return _map_store_error(exc)
        except Exception:
            logger.exception(
                "conversations_reset_continuity failed for %s", history_id
            )
            return _generic_error(500)
        return jsonify(result)
