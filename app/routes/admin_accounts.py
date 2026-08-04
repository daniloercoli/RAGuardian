import re
from datetime import datetime, timedelta, timezone

from flask import flash, redirect, render_template, request, send_file, url_for

from utils.api_key_logger import ApiKeyLogger
from utils.auth import current_user, require_admin
from utils.settings_store import API_SCOPES, API_SCOPES_REQUIRING_KB
from utils.index_lock import lifecycle_read_lock
from utils.job_store import get_job_store
from utils.user_store import UserStore
from utils.validators import ValidationError
from utils.workspace import (
    remove_workspace_files,
    safe_workspace_id,
    workspace_for_user,
)


def register_admin_account_routes(app) -> None:
    @app.route("/admin/users", methods=["GET", "POST"])
    @require_admin
    def admin_users():
        store = UserStore(app.config["USERS_DB"])
        if request.method == "POST":
            try:
                action = request.form.get("action", "create")
                if action == "create":
                    with lifecycle_read_lock():
                        user = store.create_user(
                            email=request.form.get("email", ""),
                            password=request.form.get("password", ""),
                            display_name=request.form.get(
                                "display_name",
                                "",
                            ),
                            role=request.form.get("role", "user"),
                            enabled=request.form.get("enabled") == "on",
                        )
                        workspace_for_user(user, app=app)
                    flash("Utente creato", "success")
                elif action == "update":
                    user_id = request.form.get("user_id", "")
                    role = request.form.get("role", "user")
                    enabled = request.form.get("enabled") == "on"
                    current = current_user()
                    if current and current.get("id") == user_id and (role != "admin" or not enabled):
                        raise ValueError("Non puoi disabilitare il tuo account o rimuovere il tuo ruolo admin")
                    updated = store.update_user(
                        user_id,
                        display_name=request.form.get("display_name", ""),
                        role=role,
                        enabled=enabled,
                        password=request.form.get("password", ""),
                    )
                    if not updated:
                        raise ValueError("Utente non trovato")
                    flash("Utente aggiornato", "success")
                elif action == "delete":
                    from utils.index_lock import lifecycle_write_lock

                    user_id = request.form.get("user_id", "")
                    current = current_user()
                    if not store.get(user_id):
                        raise ValueError("Utente non trovato")
                    if current and current.get("id") == user_id:
                        raise ValueError("Non puoi eliminare l'utente attualmente loggato")
                    with lifecycle_write_lock():
                        if get_job_store().active_jobs_count(
                            safe_workspace_id(user_id)
                        ):
                            raise RuntimeError(
                                "Impossibile eliminare l'utente mentre ha job attivi"
                            )
                        deleted = store.delete_user(
                            user_id,
                            before_delete=lambda user: remove_workspace_files(
                                user["id"],
                                app=app,
                            ),
                        )
                    if not deleted:
                        raise ValueError("Utente non trovato")
                    flash("Utente eliminato con tutti i dati", "success")
                else:
                    raise ValueError("Azione utente non valida")
            except Exception as exc:
                flash(str(exc), "error")
            return redirect(url_for("admin_users"))

        return render_template("admin_users.html", users=store.list())

    @app.route("/admin/api-keys", methods=["GET", "POST"])
    @require_admin
    def admin_api_keys():
        store = UserStore(app.config["USERS_DB"])

        def render_api_keys(revealed_key: dict | None = None):
            with lifecycle_read_lock():
                users = store.list()
                all_keys = []
                user_map = {user["id"]: user for user in users}
                knowledge_bases_by_user = {}
                for user in users:
                    if user.get("deletion_status") in {
                        "deleting",
                        "delete_failed",
                    }:
                        knowledge_bases_by_user[user["id"]] = []
                    else:
                        workspace = workspace_for_user(user, app=app)
                        from utils.workspace import knowledge_base_store

                        knowledge_bases_by_user[user["id"]] = [
                            item
                            for item in knowledge_base_store(
                                workspace,
                                app=app,
                            ).list()
                            if item.get("status") == "active"
                        ]
                    for key in store.get_api_keys(user["id"]):
                        all_keys.append({
                            **key,
                            "user_id": user["id"],
                            "user_email": user.get("email", ""),
                            "user_display_name": user.get(
                                "display_name",
                                "",
                            ),
                            "user_role": user.get("role", ""),
                        })
                usage_logger = ApiKeyLogger(
                    app.config.get("API_KEY_USAGE_FILE")
                )
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
                    knowledge_bases_by_user=knowledge_bases_by_user,
                )

        if request.method == "POST":
            try:
                action = request.form.get("action")
                user_id = request.form.get("user_id", "")
                if action == "create":
                    name = request.form.get("name", "").strip()
                    scopes = [scope.strip() for scope in request.form.getlist("scopes") if scope.strip()]
                    knowledge_base_ids = [
                        item.strip()
                        for item in request.form.getlist("knowledge_base_ids")
                        if item.strip()
                    ]
                    effective_scopes = [
                        scope
                        for scope in scopes
                        if scope in API_SCOPES
                    ] or ["query"]
                    if (
                        API_SCOPES_REQUIRING_KB & set(effective_scopes)
                        and not knowledge_base_ids
                    ):
                        knowledge_base_ids = ["default"]
                    with lifecycle_read_lock():
                        created = store.create_api_key(
                            user_id=user_id,
                            name=name,
                            scopes=effective_scopes,
                            knowledge_base_ids=knowledge_base_ids,
                            description=request.form.get(
                                "description",
                                "",
                            ).strip(),
                            enabled=request.form.get("enabled") == "on",
                            expires_at=api_key_expires_at_from_ttl(
                                request.form.get(
                                    "expires_in",
                                    "",
                                ).strip()
                            ),
                            validate=lambda: (
                                _validate_api_key_knowledge_bases(
                                    app,
                                    store,
                                    user_id,
                                    knowledge_base_ids,
                                )
                            ),
                        )
                    flash(f"API key '{name}' creata", "success")
                    return render_api_keys(revealed_key={"name": name, "key": created["key"]})
                if action == "delete":
                    key_name = request.form.get("key_name", "").strip()
                    if not store.delete_api_key(user_id=user_id, key_name=key_name):
                        raise ValueError("API key non trovata")
                    flash(f"API key '{key_name}' eliminata", "success")
                elif action == "toggle":
                    key_name = request.form.get("key_name", "").strip()
                    existing = store.toggle_api_key_enabled(user_id=user_id, key_name=key_name)
                    if not existing:
                        raise ValueError("API key non trovata")
                    state = "abilitata" if existing and existing.get("enabled") else "disabilitata"
                    flash(f"API key '{key_name}' {state}", "success")
                elif action == "update_access":
                    key_name = request.form.get("key_name", "").strip()
                    scopes = [
                        scope.strip()
                        for scope in request.form.getlist("scopes")
                        if scope.strip()
                    ]
                    knowledge_base_ids = [
                        item.strip()
                        for item in request.form.getlist("knowledge_base_ids")
                        if item.strip()
                    ]
                    effective_scopes = [
                        scope
                        for scope in scopes
                        if scope in API_SCOPES
                    ] or ["query"]
                    if (
                        API_SCOPES_REQUIRING_KB & set(effective_scopes)
                        and not knowledge_base_ids
                    ):
                        raise ValueError(
                            "Seleziona almeno una knowledge base per gli scope query, ingest o agent_manage"
                        )
                    with lifecycle_read_lock():
                        updated = store.update_api_key_access(
                            user_id=user_id,
                            key_name=key_name,
                            scopes=scopes,
                            knowledge_base_ids=knowledge_base_ids,
                            validate=lambda: (
                                _validate_api_key_knowledge_bases(
                                    app,
                                    store,
                                    user_id,
                                    knowledge_base_ids,
                                )
                            ),
                        )
                    if not updated:
                        raise ValueError("API key non trovata")
                    flash(f"Accesso API key '{key_name}' aggiornato", "success")
                elif action == "download":
                    flash("Le API key sono mostrate solo al momento della creazione", "error")
                elif action not in {"clear_show_key", None}:
                    raise ValueError("Azione API key non valida")
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


def _validate_api_key_knowledge_bases(
    app,
    store: UserStore,
    user_id: str,
    knowledge_base_ids: list[str],
) -> None:
    user = store.get(user_id)
    if not user:
        raise ValueError("Utente non trovato")
    if user.get("deletion_status") in {"deleting", "delete_failed"}:
        raise ValueError(
            "Utente con cancellazione incompleta: ripeti la cancellazione"
        )
    workspace = workspace_for_user(user, app=app)
    from utils.workspace import knowledge_base_store

    available = {
        item["id"]
        for item in knowledge_base_store(workspace, app=app).list()
        if item.get("status") == "active"
    }
    unknown = sorted(set(knowledge_base_ids) - available)
    if unknown:
        raise ValueError("Knowledge base non disponibile")
