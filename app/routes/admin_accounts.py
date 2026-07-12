import re
from datetime import datetime, timedelta, timezone

from flask import flash, redirect, render_template, request, send_file, url_for

from utils.api_key_logger import ApiKeyLogger
from utils.auth import require_admin
from utils.user_store import UserStore
from utils.validators import ValidationError
from utils.workspace import workspace_for_user


def register_admin_account_routes(app) -> None:
    @app.route("/admin/users", methods=["GET", "POST"])
    @require_admin
    def admin_users():
        store = UserStore(app.config["USERS_FILE"])
        if request.method == "POST":
            try:
                action = request.form.get("action", "create")
                if action == "create":
                    user = store.create_user(
                        email=request.form.get("email", ""),
                        password=request.form.get("password", ""),
                        display_name=request.form.get("display_name", ""),
                        role=request.form.get("role", "user"),
                        enabled=request.form.get("enabled") == "on",
                    )
                    workspace_for_user(user, app=app)
                    flash("Utente creato", "success")
                elif action == "update":
                    store.update_user(
                        request.form.get("user_id", ""),
                        display_name=request.form.get("display_name", ""),
                        role=request.form.get("role", "user"),
                        enabled=request.form.get("enabled") == "on",
                        password=request.form.get("password", ""),
                    )
                    flash("Utente aggiornato", "success")
            except Exception as exc:
                flash(str(exc), "error")
            return redirect(url_for("admin_users"))

        return render_template("admin_users.html", users=store.list())

    @app.route("/admin/api-keys", methods=["GET", "POST"])
    @require_admin
    def admin_api_keys():
        store = UserStore(app.config["USERS_FILE"])

        def render_api_keys(revealed_key: dict | None = None):
            users = store.list()
            all_keys = []
            user_map = {user["id"]: user for user in users}
            for user in users:
                for key in store.get_api_keys(user["id"]):
                    all_keys.append({
                        **key,
                        "user_id": user["id"],
                        "user_email": user.get("email", ""),
                        "user_display_name": user.get("display_name", ""),
                        "user_role": user.get("role", ""),
                    })
            usage_logger = ApiKeyLogger(app.config.get("API_KEY_USAGE_FILE"))
            return render_template(
                "admin_api_keys.html",
                users=users,
                all_keys=all_keys,
                user_map=user_map,
                usage_entries=usage_logger.recent_entries(20),
                usage_entry_limit=20,
                usage_log_exists=usage_logger.file_exists(),
                usage_log_path=str(usage_logger.path),
                revealed_key=revealed_key,
            )

        if request.method == "POST":
            try:
                action = request.form.get("action")
                user_id = request.form.get("user_id", "")
                if action == "create":
                    name = request.form.get("name", "").strip()
                    scopes = [scope.strip() for scope in request.form.getlist("scopes") if scope.strip()]
                    created = store.create_api_key(
                        user_id=user_id,
                        name=name,
                        scopes=scopes or ["query"],
                        description=request.form.get("description", "").strip(),
                        enabled=request.form.get("enabled") == "on",
                        expires_at=api_key_expires_at_from_ttl(
                            request.form.get("expires_in", "").strip()
                        ),
                    )
                    flash(f"API key '{name}' creata", "success")
                    return render_api_keys(revealed_key={"name": name, "key": created["key"]})
                if action == "delete":
                    key_name = request.form.get("key_name", "").strip()
                    store.delete_api_key(user_id=user_id, key_name=key_name)
                    flash(f"API key '{key_name}' eliminata", "success")
                elif action == "toggle":
                    key_name = request.form.get("key_name", "").strip()
                    existing = store.toggle_api_key_enabled(user_id=user_id, key_name=key_name)
                    state = "abilitata" if existing and existing.get("enabled") else "disabilitata"
                    flash(f"API key '{key_name}' {state}", "success")
                elif action == "download":
                    flash("Le API key sono mostrate solo al momento della creazione", "error")
            except Exception as exc:
                flash(str(exc), "error")
            return redirect(url_for("admin_api_keys"))

        return render_api_keys()

    @app.route("/admin/api-keys/usage-log", methods=["GET"])
    @require_admin
    def admin_api_key_usage_log_download():
        usage_file = ApiKeyLogger(app.config.get("API_KEY_USAGE_FILE")).path
        if not usage_file.exists():
            flash("Usage log API key non ancora disponibile", "error")
            return redirect(url_for("admin_api_keys"))
        return send_file(
            str(usage_file),
            mimetype="application/json",
            as_attachment=True,
            download_name=usage_file.name,
        )


def api_key_expires_at_from_ttl(value: str) -> str | None:
    value = str(value or "").strip().lower()
    if not value:
        return None
    match = re.fullmatch(r"([1-9][0-9]*)([dhm])", value)
    if not match:
        raise ValidationError(
            "TTL API key non valido. Usa formati come 30d, 24h o 60m.",
            "expires_in",
        )
    amount = int(match.group(1))
    unit = match.group(2)
    if unit == "d":
        delta = timedelta(days=amount)
    elif unit == "h":
        delta = timedelta(hours=amount)
    else:
        delta = timedelta(minutes=amount)
    return (datetime.now(timezone.utc) + delta).isoformat(timespec="seconds")
