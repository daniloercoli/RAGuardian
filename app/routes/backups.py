from __future__ import annotations

from flask import flash, jsonify, redirect, url_for

from utils.auth import require_admin
from utils.logging_config import APP_LOGGER as log


def register_backup_routes(app) -> None:
    @app.route("/admin/backup/create", methods=["POST"])
    @require_admin
    def admin_create_backup():
        from utils.vector_store.backup_manager import create_backup

        try:
            result = create_backup()
            flash(
                f"Backup creato: {result.get('id', '?')} "
                f"({result.get('document_count', 0)} documenti)",
                "success",
            )
        except Exception as exc:
            log.error("Backup creation failed: %s", exc)
            flash(str(exc), "error")
        return redirect(url_for("admin_files"))

    @app.route("/admin/backup/list", methods=["GET"])
    @require_admin
    def admin_list_backups():
        from utils.vector_store.backup_manager import list_backups

        try:
            backups = list_backups()
        except Exception as exc:
            log.error("List backups failed: %s", exc)
            backups = []
        return jsonify(backups)

    @app.route("/admin/backup/restore/<backup_id>", methods=["POST"])
    @require_admin
    def admin_restore_backup(backup_id):
        from utils.vector_store.backup_manager import restore_backup

        try:
            result = restore_backup(backup_id)
            flash(
                f"Restore completato: {backup_id} "
                f"({result.get('document_count', 'unknown')} documenti)",
                "success",
            )
        except Exception as exc:
            log.error("Restore failed: %s", exc)
            flash(str(exc), "error")
        return redirect(url_for("admin_files"))

    @app.route("/admin/backup/delete/<backup_id>", methods=["POST"])
    @require_admin
    def admin_delete_backup(backup_id):
        from utils.vector_store.backup_manager import delete_backup

        try:
            delete_backup(backup_id)
            flash(f"Backup {backup_id} eliminato", "success")
        except Exception as exc:
            log.error("Delete backup failed: %s", exc)
            flash(str(exc), "error")
        return redirect(url_for("admin_files"))

    @app.route("/admin/backup/verify/<backup_id>", methods=["GET"])
    @require_admin
    def admin_verify_backup(backup_id):
        from utils.vector_store.backup_manager import verify_backup

        try:
            result = verify_backup(backup_id)
        except Exception as exc:
            result = {"status": "error", "error": str(exc)}
        return jsonify(result)
