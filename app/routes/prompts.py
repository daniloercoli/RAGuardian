from __future__ import annotations

from flask import current_app, jsonify, render_template, request

from utils.auth import current_user, require_admin, require_login
from utils.prompt_store import PromptStore


def _store() -> PromptStore:
    return PromptStore(current_app.config.get("PROMPTS_DIR", "app/data"))


def _user_id() -> str:
    user = current_user()
    return user["id"] if user else ""


def register_prompt_routes(app) -> None:
    @app.route("/my-prompts", methods=["GET"])
    @require_login
    def admin_my_prompts():
        return render_template("my_prompts.html")

    @app.route("/admin/prompts", methods=["GET"])
    @require_admin
    def admin_prompts():
        return render_template("admin_prompts.html")

    @app.route("/api/prompts", methods=["GET"])
    @require_login
    def api_list_prompts():
        return jsonify({"personal": _store().list_user_prompts(_user_id())})

    @app.route("/api/prompts", methods=["POST"])
    @require_login
    def api_create_prompt():
        data = request.get_json(silent=True)
        if not data:
            return jsonify(error="empty body"), 400
        name = (data.get("name") or "").strip()
        content = (data.get("content") or "").strip()
        if not name or not content:
            return jsonify(error="name and content required"), 400
        try:
            return jsonify(_store().create_user_prompt(_user_id(), name, content)), 201
        except Exception as exc:
            return jsonify(error=str(exc)), 400

    @app.route("/api/prompts/<prompt_id>", methods=["PUT"])
    @require_login
    def api_update_prompt(prompt_id):
        data = request.get_json(silent=True) or {}
        updated = _store().update_user_prompt(
            _user_id(),
            prompt_id,
            name=data.get("name"),
            content=data.get("content"),
        )
        return jsonify(updated) if updated else (jsonify(error="prompt not found"), 404)

    @app.route("/api/prompts/<prompt_id>", methods=["DELETE"])
    @require_login
    def api_delete_prompt(prompt_id):
        if _store().delete_user_prompt(_user_id(), prompt_id):
            return jsonify(ok=True)
        return jsonify(error="prompt not found"), 404

    @app.route("/api/prompts/shared", methods=["GET"])
    @require_login
    def api_list_shared_prompts():
        store = _store()
        user = current_user()
        if not user or user.get("role") != "admin":
            return jsonify({"prompts": store.list_shared()})
        all_prompts = store.all_shared()
        active = [prompt for prompt in all_prompts if prompt.get("is_active", True)]
        inactive = [prompt for prompt in all_prompts if not prompt.get("is_active", True)]
        return jsonify({"prompts": active + inactive})

    @app.route("/api/prompts/shared", methods=["POST"])
    @require_admin
    def api_create_shared_prompt():
        data = request.get_json(silent=True)
        if not data:
            return jsonify(error="empty body"), 400
        name = (data.get("name") or "").strip()
        content = (data.get("content") or "").strip()
        if not name or not content:
            return jsonify(error="name and content required"), 400
        try:
            prompt = _store().create_shared(name, content, created_by=_user_id())
            return jsonify(prompt), 201
        except Exception as exc:
            return jsonify(error=str(exc)), 400

    @app.route("/api/prompts/shared/<prompt_id>", methods=["PUT"])
    @require_admin
    def api_update_shared_prompt(prompt_id):
        data = request.get_json(silent=True) or {}
        updated = _store().update_shared(
            prompt_id,
            name=data.get("name"),
            content=data.get("content"),
        )
        return jsonify(updated) if updated else (jsonify(error="prompt not found"), 404)

    @app.route("/api/prompts/shared/<prompt_id>/toggle", methods=["POST"])
    @require_admin
    def api_toggle_shared_prompt(prompt_id):
        result = _store().toggle_shared(prompt_id)
        return jsonify(result) if result else (jsonify(error="prompt not found"), 404)

    @app.route("/api/prompts/shared/<prompt_id>", methods=["DELETE"])
    @require_admin
    def api_delete_shared_prompt(prompt_id):
        if _store().delete_shared(prompt_id):
            return jsonify(ok=True)
        return jsonify(error="prompt not found"), 404

    @app.route("/api/prompts/resolve", methods=["POST"])
    @require_login
    def api_resolve_prompt():
        data = request.get_json(silent=True)
        if not data or not data.get("content"):
            return jsonify(error="content required"), 400
        return jsonify(resolved=PromptStore.resolve_template(data["content"], current_user()))
