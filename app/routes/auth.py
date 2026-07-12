from __future__ import annotations

from flask import flash, redirect, render_template, request, session, url_for

from utils.auth import authenticate_user, require_login
from utils.http_security import safe_next_url


def register_auth_routes(app) -> None:
    @app.route("/admin/login", methods=["GET", "POST"])
    def admin_login():
        if request.method == "POST":
            email = request.form.get("email", "")
            password = request.form.get("password", "")
            user = authenticate_user(email, password)
            if user:
                token = session.get("_csrf_token")
                session.clear()
                if token:
                    session["_csrf_token"] = token
                session["user_id"] = user["id"]
                return redirect(safe_next_url(request.args.get("next")) or url_for("admin_config"))
            flash("Password non valida", "error")
        return render_template("admin_login.html")

    @app.route("/admin/logout", methods=["POST"])
    @require_login
    def admin_logout():
        session.clear()
        return redirect(url_for("admin_login"))
