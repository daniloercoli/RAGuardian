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
from functools import wraps
from typing import Optional

from flask import jsonify, request
from werkzeug.exceptions import BadRequest, NotFound

from utils.auth import is_logged_in
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


class ConversationApiBadRequest(BadRequest):
    """Validation error limited to the conversation JSON API."""


class ConversationApiNotFound(NotFound):
    """Missing resource limited to the conversation JSON API."""


def _is_valid_history_id(value: str) -> bool:
    return bool(_HISTORY_ID_RE.match(str(value or "")))


def _parse_int(value: str, *, name: str, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise ConversationApiBadRequest(f"{name} must be an integer")
    if parsed < minimum:
        raise ConversationApiBadRequest(f"{name} must be >= {minimum}")
    if parsed > maximum:
        return maximum
    return parsed


def _require_workspace():
    workspace = workspace_from_request()
    return workspace.workspace_id


def _generic_error(status: int, message: str = "Internal error"):
    code = "server_error" if status >= 500 else "request_error"
    return jsonify({"error": message, "status": code, "code": code}), status


def _json_error(message: str, code: str) -> dict:
    return {"error": message, "status": code, "code": code}


def _require_login_json(view):
    """Session auth for JSON endpoints without an HTML redirect fallback."""

    @wraps(view)
    def wrapper(*args, **kwargs):
        if not is_logged_in():
            return jsonify(_json_error("Autenticazione richiesta", "unauthorized")), 401
        return view(*args, **kwargs)

    return wrapper


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
        return jsonify(_json_error("Conversation not found", exc.code)), 404
    if isinstance(exc, TurnConflictError):
        return jsonify(_json_error(str(exc), exc.code)), 409
    if isinstance(exc, TurnInProgressError):
        payload = _json_error(str(exc), exc.code)
        payload["retry_after"] = exc.retry_after
        resp = jsonify(payload)
        resp.headers["Retry-After"] = str(exc.retry_after)
        return resp, 409
    if isinstance(exc, ContinuityError):
        return (
            jsonify(
                {
                    "error": str(exc),
                    "status": exc.code,
                    "code": exc.code,
                    "expected_parent_turn_id": exc.expected_parent_turn_id,
                }
            ),
            409,
        )
    if isinstance(exc, QuotaExceededError):
        return jsonify(_json_error(str(exc), exc.code)), 429
    return jsonify(_json_error(str(exc), exc.code)), 400


def register_conversation_routes(app) -> None:
    @app.errorhandler(ConversationApiBadRequest)
    def _conversations_bad_request(err):
        return jsonify(
            _json_error(err.description or "Bad request", "validation_error")
        ), 400

    @app.errorhandler(ConversationApiNotFound)
    def _conversations_not_found(err):
        return jsonify(
            _json_error(err.description or "Not found", "not_found")
        ), 404

    @app.route("/api/conversations", methods=["GET"])
    @_require_login_json
    def conversations_list():
        """List conversations for current workspace with pagination."""
        workspace_id = _require_workspace()
        page = _parse_int(
            request.args.get("page", "1"),
            name="page",
            minimum=1,
            maximum=10_000,
        )
        per_page = _parse_int(
            request.args.get("per_page", "20"),
            name="per_page",
            minimum=1,
            maximum=100,
        )

        status_filter: Optional[str] = request.args.get("status")
        archived_param = request.args.get("archived")
        if status_filter not in {"active", "archived", None}:
            raise ConversationApiBadRequest("status must be 'active' or 'archived'")
        if archived_param is not None:
            archived_value = archived_param.lower()
            if archived_value not in {"true", "false"}:
                raise ConversationApiBadRequest("archived must be 'true' or 'false'")
            if status_filter is None:
                status_filter = "archived" if archived_value == "true" else "active"

        service = get_conversation_service_for_workspace(workspace_id, app=app)
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
    @_require_login_json
    def conversations_get(history_id: str):
        """Get a specific conversation by history_id."""
        if not _is_valid_history_id(history_id):
            raise ConversationApiBadRequest("history_id must be a UUID")
        workspace_id = _require_workspace()
        service = get_conversation_service_for_workspace(workspace_id, app=app)
        if not service.enabled:
            raise ConversationApiNotFound("History disabled")

        try:
            conversation = service.get_conversation_by_id(history_id)
        except ConversationHistoryError as exc:
            return _map_store_error(exc)
        except Exception:
            logger.exception("conversations_get failed for %s", history_id)
            return _generic_error(500)
        if not conversation:
            raise ConversationApiNotFound("Conversation not found")
        return jsonify(conversation)

    @app.route("/api/conversations/<history_id>/messages", methods=["GET"])
    @_require_login_json
    def conversations_get_messages(history_id: str):
        """Get messages for a conversation with cursor pagination."""
        if not _is_valid_history_id(history_id):
            raise ConversationApiBadRequest("history_id must be a UUID")
        workspace_id = _require_workspace()
        service = get_conversation_service_for_workspace(workspace_id, app=app)
        if not service.enabled:
            return jsonify({"messages": [], "next_cursor": None})

        before_sequence_arg = request.args.get("before_sequence")
        before_sequence: Optional[int] = None
        if before_sequence_arg:
            try:
                before_sequence = int(before_sequence_arg)
            except ValueError:
                raise ConversationApiBadRequest("before_sequence must be an integer")
            if before_sequence < 0:
                raise ConversationApiBadRequest("before_sequence must be >= 0")

        limit = _parse_int(
            request.args.get("limit", "50"),
            name="limit",
            minimum=1,
            maximum=200,
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
    @_require_login_json
    def conversations_update(history_id: str):
        """Rename and/or archive/unarchive a conversation."""
        if not _is_valid_history_id(history_id):
            raise ConversationApiBadRequest("history_id must be a UUID")
        workspace_id = _require_workspace()
        service = get_conversation_service_for_workspace(workspace_id, app=app)
        if not service.enabled:
            raise ConversationApiNotFound("History disabled")

        if not request.is_json:
            raise ConversationApiBadRequest("Content-Type must be application/json")
        try:
            data = request.get_json(silent=False)
        except BadRequest as exc:
            raise ConversationApiBadRequest("Malformed JSON body") from exc
        if not isinstance(data, dict):
            raise ConversationApiBadRequest("JSON body must be an object")
        unknown = sorted(set(data) - {"title", "archived"})
        if unknown:
            raise ConversationApiBadRequest(f"Unknown field: {unknown[0]}")

        has_title = "title" in data
        has_archived = "archived" in data
        title = data.get("title")
        archived = data.get("archived")

        if has_title:
            if not isinstance(title, str):
                raise ConversationApiBadRequest("title must be a string")
            title = title.strip()
            if not title:
                raise ConversationApiBadRequest("title cannot be empty")
            if len(title) > TITLE_RENAME_MAX_LEN:
                raise ConversationApiBadRequest(
                    f"title must be <= {TITLE_RENAME_MAX_LEN} characters"
                )

        if has_archived:
            if not isinstance(archived, bool):
                raise ConversationApiBadRequest("archived must be a boolean")

        if not has_title and not has_archived:
            raise ConversationApiBadRequest(
                "Provide 'title' and/or 'archived' to update"
            )

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
            raise ConversationApiNotFound("Conversation not found")
        return jsonify(conversation)

    @app.route("/api/conversations/<history_id>", methods=["DELETE"])
    @_require_login_json
    def conversations_delete(history_id: str):
        """Hard delete a conversation."""
        if not _is_valid_history_id(history_id):
            raise ConversationApiBadRequest("history_id must be a UUID")
        workspace = workspace_from_request(app)
        workspace_id = workspace.workspace_id
        service = get_conversation_service_for_workspace(workspace_id, app=app)
        if not service.enabled:
            return jsonify({"deleted": False})

        try:
            deleted = service.delete_conversation(
                history_id,
                workspace_upload_folder=workspace.workspace_upload_folder,
            )
        except ConversationHistoryError as exc:
            return _map_store_error(exc)
        except Exception:
            logger.exception("conversations_delete failed for %s", history_id)
            return _generic_error(500)
        if not deleted:
            raise ConversationApiNotFound("Conversation not found")
        return jsonify({"deleted": True})

    @app.route(
        "/api/conversations/<history_id>/reset-continuity", methods=["POST"]
    )
    @_require_login_json
    def conversations_reset_continuity(history_id: str):
        """Clean up expired turn leases and return the current parent turn id.

        Useful when a client observes a stale ``generating`` turn that is
        blocking new turns: this endpoint fails expired leases and reports
        the ``parent_turn_id`` the next ``begin_turn`` should reference.
        """
        if not _is_valid_history_id(history_id):
            raise ConversationApiBadRequest("history_id must be a UUID")
        workspace_id = _require_workspace()
        service = get_conversation_service_for_workspace(workspace_id, app=app)
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
