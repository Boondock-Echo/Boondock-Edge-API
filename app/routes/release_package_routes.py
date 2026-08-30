"""
Full-stack release upload and rollback (admin only).
"""
import logging
from flask import Blueprint, jsonify, request
from ..middleware.auth_middleware import require_auth
from ..services import release_package_service

log = logging.getLogger(__name__)

release_package_bp = Blueprint("release_package", __name__)


@release_package_bp.route("/version/status", methods=["GET"])
@require_auth
def version_status():
    """Current release metadata and list of on-disk backup snapshots."""
    return jsonify(release_package_service.get_status())


@release_package_bp.route("/version/apply", methods=["POST"])
@require_auth
def version_apply():
    """
    Multipart: field "file" = release .zip
    JSON body (optional) or form: install_dependencies=true
    """
    f = request.files.get("file")
    if f is None or f.filename is None or f.filename.strip() == "":
        return jsonify({"error": "Missing file field in multipart form"}), 400

    raw = f.read()
    if not raw:
        return jsonify({"error": "Empty file"}), 400

    inst = request.form.get("install_dependencies", "").lower() in ("1", "true", "yes")
    if not inst:
        inst = request.args.get("install_dependencies", "").lower() in ("1", "true", "yes")

    try:
        out = release_package_service.apply_release_package(
            raw, install_dependencies=inst
        )
        return jsonify(out)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except OSError as e:
        log.exception("Release apply failed (OS error)")
        return jsonify({"error": str(e)}), 500


@release_package_bp.route("/version/rollback", methods=["POST"])
@require_auth
def version_rollback():
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({"error": "JSON body required"}), 400
    backup_id = (data.get("backup_id") or data.get("id") or "").strip()
    if not backup_id:
        return jsonify({"error": "backup_id is required"}), 400
    try:
        out = release_package_service.rollback(backup_id)
        return jsonify(out)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except OSError as e:
        log.exception("Rollback failed (OS error)")
        return jsonify({"error": str(e)}), 500
