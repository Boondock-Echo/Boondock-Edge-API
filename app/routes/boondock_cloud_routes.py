"""
Boondock Cloud Connect routes.
Starts a portal Connect session, polls one-time redeem, stores the API key as
the transcription key (so remote transcription and keyword actions work
without pasting a key), and pushes device stats to the portal.
"""
import logging
import os
import platform
import secrets
import socket
import sqlite3
import threading
import time
from datetime import datetime, timezone
from urllib.parse import quote

import requests
from flask import Blueprint, jsonify, request

from ..middleware.auth_middleware import require_admin
from ..services.settings_manager import get_settings_manager

boondock_cloud_bp = Blueprint("boondock_cloud", __name__)

CONNECT_TTL_SECONDS = 10 * 60
POLL_INTERVAL_SECONDS = 2
HEARTBEAT_INTERVAL_SECONDS = 60

_lock = threading.Lock()
# connect_id -> { status, portal_url, error, started_at, stop }
_sessions = {}
_heartbeat_stop = threading.Event()
_heartbeat_thread = None


def _portal_base_url():
    explicit = os.environ.get("BOONDOCK_PORTAL_URL", "").strip()
    return (explicit or "https://portal.boondock.cloud").rstrip("/")


def _device_name():
    return (
        os.environ.get("BOONDOCK_EDGE_DEVICE_NAME")
        or os.environ.get("COMPUTERNAME")
        or os.environ.get("HOSTNAME")
        or "Edge"
    )


def _ensure_device_id(settings=None):
    settings_manager = get_settings_manager()
    current = settings if settings is not None else settings_manager.get_all_settings()
    device_id = str(current.get("boondock_cloud_device_id") or "").strip()
    if device_id:
        return device_id, current
    device_id = secrets.token_hex(16)
    current["boondock_cloud_device_id"] = device_id
    settings_manager.set_all_settings(current)
    return device_id, current


def _device_snapshot(settings=None):
    """Gather Edge identity + recording/channel stats for the portal detail page."""
    settings_manager = get_settings_manager()
    current = settings if settings is not None else settings_manager.get_all_settings()
    device_id, current = _ensure_device_id(current)

    total_recordings = 0
    transcribed_recordings = 0
    today_recordings = 0
    try:
        from config import Config

        recordings_db = Config.get_recordings_db_path()
        conn = sqlite3.connect(recordings_db, timeout=5.0)
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM recordings")
        total_recordings = int(cur.fetchone()[0] or 0)
        cur.execute(
            """
            SELECT COUNT(*) FROM recordings
            WHERE (
                status = 'transcribed'
                OR (transcription IS NOT NULL AND TRIM(transcription) != '')
            )
            """
        )
        transcribed_recordings = int(cur.fetchone()[0] or 0)
        today_prefix = datetime.now(timezone.utc).strftime("%Y%m%d")
        today_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        cur.execute(
            """
            SELECT COUNT(*) FROM recordings
            WHERE timestamp IS NOT NULL AND (
                timestamp LIKE ? OR timestamp LIKE ?
            )
            """,
            (f"{today_prefix}%", f"{today_iso}%"),
        )
        today_recordings = int(cur.fetchone()[0] or 0)
        conn.close()
    except Exception as exc:
        logging.warning("Device snapshot recordings query failed: %s", exc)

    channel_count = 0
    try:
        channels = settings_manager.get_all_channels() or []
        channel_count = len(channels) if isinstance(channels, list) else 0
    except Exception as exc:
        logging.warning("Device snapshot channels query failed: %s", exc)

    hostname = socket.gethostname() or _device_name()
    try:
        mac = (
            (current.get("boondock_cloud_device_mac") or "").strip()
            or _primary_mac()
        )
    except Exception:
        mac = (current.get("boondock_cloud_device_mac") or "").strip()

    now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return {
        "deviceId": device_id,
        "name": _device_name(),
        "model": "Boondock Edge",
        "mac": mac,
        "location": (current.get("boondock_cloud_device_location") or "").strip(),
        "channel": str(channel_count) if channel_count else "",
        "stats": {
            "lastActive": now_iso,
            "totalRecordings": total_recordings,
            "transcribedRecordings": transcribed_recordings,
            "todayRecordings": today_recordings,
            "channelCount": channel_count,
            "hostname": hostname,
            "platform": f"{platform.system()} {platform.release()}".strip(),
            "edgeVersion": (
                os.environ.get("BOONDOCK_EDGE_VERSION")
                or current.get("edge_version")
                or ""
            ).strip()
            or None,
        },
    }


def _primary_mac():
    try:
        import uuid

        node = uuid.getnode()
        if (node >> 40) % 2:
            return ""
        return ":".join(f"{(node >> ele) & 0xFF:02x}" for ele in range(40, -1, -8))
    except Exception:
        return ""


def _reload_transcription():
    try:
        from ..services.audio_handler import reload_transcription_settings

        reload_transcription_settings()
    except Exception as exc:
        logging.warning("Failed to reload transcription settings: %s", exc)


def _apply_connected_key(api_key, key_prefix, key_id=None, device_id=None):
    settings_manager = get_settings_manager()
    settings = settings_manager.get_all_settings()
    settings["transcription_api_key"] = api_key
    settings["boondock_cloud_connected"] = "true"
    settings["boondock_cloud_key_prefix"] = key_prefix or api_key[:12]
    settings["boondock_cloud_auth_mode"] = "connect"
    if key_id is not None and str(key_id).strip() != "":
        settings["boondock_cloud_key_id"] = str(key_id).strip()
    if device_id:
        settings["boondock_cloud_device_id"] = str(device_id).strip()
    # The portal key authenticates the remote transcription proxy.
    settings["method"] = "openai"
    settings_manager.set_all_settings(settings)
    _reload_transcription()


def _clear_cloud_key():
    settings_manager = get_settings_manager()
    settings = settings_manager.get_all_settings()
    settings["transcription_api_key"] = ""
    if settings.get("method") == "openai":
        settings["method"] = "local"
    settings["boondock_cloud_connected"] = "false"
    settings["boondock_cloud_key_prefix"] = ""
    # Keep device_id stable across reconnects; clear key binding only.
    settings["boondock_cloud_key_id"] = ""
    settings_manager.set_all_settings(settings)
    _reload_transcription()


def _notify_portal_disconnect(device_id, key_id):
    if not device_id or not key_id:
        return
    portal = _portal_base_url()
    try:
        requests.post(
            f"{portal}/api/connect/disconnect",
            json={"deviceId": device_id, "keyId": key_id},
            timeout=10,
        )
    except requests.RequestException as exc:
        logging.warning("Portal disconnect notify failed: %s", exc)


def _push_heartbeat():
    settings = get_settings_manager().get_all_settings()
    if str(settings.get("boondock_cloud_connected") or "").lower() != "true":
        return False
    device_id = str(settings.get("boondock_cloud_device_id") or "").strip()
    key_id = str(settings.get("boondock_cloud_key_id") or "").strip()
    if not device_id or not key_id:
        return False

    snapshot = _device_snapshot(settings)
    portal = _portal_base_url()
    try:
        response = requests.post(
            f"{portal}/api/connect/heartbeat",
            json={
                "deviceId": device_id,
                "keyId": key_id,
                "name": snapshot.get("name"),
                "mac": snapshot.get("mac"),
                "channel": snapshot.get("channel"),
                "model": snapshot.get("model"),
                "location": snapshot.get("location"),
                "stats": snapshot.get("stats"),
            },
            timeout=15,
        )
        if response.status_code >= 400:
            logging.warning(
                "Portal heartbeat failed (%s): %s",
                response.status_code,
                response.text[:200],
            )
            return False
        return True
    except requests.RequestException as exc:
        logging.warning("Portal heartbeat error: %s", exc)
        return False


def _heartbeat_loop():
    # Immediate first push, then periodic.
    while not _heartbeat_stop.is_set():
        try:
            _push_heartbeat()
        except Exception:
            logging.exception("Portal heartbeat loop error")
        _heartbeat_stop.wait(HEARTBEAT_INTERVAL_SECONDS)


def ensure_heartbeat_running():
    """Start background heartbeat if Edge is already connected (app startup)."""
    global _heartbeat_thread
    settings = get_settings_manager().get_all_settings()
    if str(settings.get("boondock_cloud_connected") or "").lower() != "true":
        return
    if not str(settings.get("boondock_cloud_device_id") or "").strip():
        return
    if not str(settings.get("boondock_cloud_key_id") or "").strip():
        return

    with _lock:
        if _heartbeat_thread and _heartbeat_thread.is_alive():
            return
        _heartbeat_stop.clear()
        _heartbeat_thread = threading.Thread(
            target=_heartbeat_loop,
            daemon=True,
            name="bc-portal-heartbeat",
        )
        _heartbeat_thread.start()


def _stop_heartbeat():
    _heartbeat_stop.set()


def _poll_redeem(connect_id):
    portal = _portal_base_url()
    redeem_url = f"{portal}/api/connect/redeem"
    deadline = time.time() + CONNECT_TTL_SECONDS

    while time.time() < deadline:
        with _lock:
            session = _sessions.get(connect_id)
            if not session or session.get("stop"):
                return
            if session.get("status") == "connected":
                return

        try:
            response = requests.post(
                redeem_url,
                json={"connectId": connect_id},
                timeout=15,
            )
        except requests.RequestException as exc:
            logging.warning("Boondock Cloud redeem poll failed: %s", exc)
            time.sleep(POLL_INTERVAL_SECONDS)
            continue

        if response.status_code == 202:
            time.sleep(POLL_INTERVAL_SECONDS)
            continue

        if response.status_code == 404:
            with _lock:
                session = _sessions.get(connect_id)
                if session and session.get("status") != "connected":
                    session["status"] = "expired"
                    session["error"] = "Connect session expired or already used"
            return

        if response.status_code == 200:
            try:
                payload = response.json()
            except ValueError:
                payload = {}
            data = payload.get("data") or payload
            api_key = data.get("apiKey") or data.get("api_key")
            if not api_key:
                with _lock:
                    session = _sessions.get(connect_id)
                    if session:
                        session["status"] = "error"
                        session["error"] = "Portal redeem response missing apiKey"
                return

            key_prefix = data.get("keyPrefix") or api_key[:12]
            key_id = data.get("keyId") or data.get("key_id")
            device_id = data.get("deviceId") or data.get("device_id")
            with _lock:
                session = _sessions.get(connect_id)
                if session and not device_id:
                    device_id = session.get("device_id")
            try:
                _apply_connected_key(
                    api_key, key_prefix, key_id=key_id, device_id=device_id
                )
            except Exception as exc:
                logging.exception("Failed to store Boondock Cloud API key")
                with _lock:
                    session = _sessions.get(connect_id)
                    if session:
                        session["status"] = "error"
                        session["error"] = str(exc)
                return

            with _lock:
                session = _sessions.get(connect_id)
                if session:
                    session["status"] = "connected"
                    session["key_prefix"] = key_prefix
                    session["error"] = None

            ensure_heartbeat_running()
            try:
                _push_heartbeat()
            except Exception:
                logging.exception("Initial portal heartbeat failed")
            return

        # Unexpected status
        with _lock:
            session = _sessions.get(connect_id)
            if session:
                session["status"] = "error"
                try:
                    err_body = response.json()
                    session["error"] = err_body.get("error") or response.text[:200]
                except ValueError:
                    session["error"] = f"Redeem failed ({response.status_code})"
        return

    with _lock:
        session = _sessions.get(connect_id)
        if session and session.get("status") == "pending":
            session["status"] = "expired"
            session["error"] = "Timed out waiting for portal Connect"


@boondock_cloud_bp.route("/boondock-cloud/connect/start", methods=["POST"])
@require_admin
def connect_start():
    """Create a connect_id and open URL; start background redeem poller."""
    body = request.get_json(silent=True) or {}
    snapshot = _device_snapshot()
    device_name = (body.get("deviceName") or snapshot.get("name") or _device_name()).strip() or "Edge"
    device_id = snapshot["deviceId"]
    device_model = (body.get("deviceModel") or snapshot.get("model") or "Boondock Edge").strip() or "Boondock Edge"
    device_mac = (body.get("deviceMac") or snapshot.get("mac") or "").strip()

    connect_id = secrets.token_hex(32)
    portal = _portal_base_url()
    params = (
        f"connect_id={connect_id}"
        f"&device_name={quote(device_name)}"
        f"&device_id={quote(device_id)}"
        f"&device_model={quote(device_model)}"
    )
    if device_mac:
        params += f"&device_mac={quote(device_mac)}"
    portal_url = f"{portal}/connect?{params}"

    device_payload = {
        "deviceId": device_id,
        "name": device_name,
        "model": device_model,
        "mac": device_mac,
        "location": snapshot.get("location") or "",
        "channel": snapshot.get("channel") or "",
        "stats": snapshot.get("stats"),
    }

    # Best-effort register pending session on portal (Edge may open browser first)
    try:
        requests.post(
            f"{portal}/api/connect/session",
            json={
                "connectId": connect_id,
                "deviceName": device_name,
                "device": device_payload,
            },
            timeout=10,
        )
    except requests.RequestException as exc:
        logging.info("Portal connect session pre-register skipped: %s", exc)

    with _lock:
        _sessions[connect_id] = {
            "status": "pending",
            "portal_url": portal_url,
            "error": None,
            "started_at": time.time(),
            "stop": False,
            "key_prefix": None,
            "device_name": device_name,
            "device_id": device_id,
            "device": device_payload,
        }

    thread = threading.Thread(
        target=_poll_redeem, args=(connect_id,), daemon=True, name=f"bc-connect-{connect_id[:8]}"
    )
    thread.start()

    return jsonify(
        {
            "connect_id": connect_id,
            "portal_url": portal_url,
            "device_id": device_id,
            "expires_in_seconds": CONNECT_TTL_SECONDS,
            "status": "pending",
        }
    )


@boondock_cloud_bp.route("/boondock-cloud/connect/status", methods=["GET"])
@require_admin
def connect_status():
    connect_id = (request.args.get("connect_id") or "").strip()
    if not connect_id:
        return jsonify({"error": "connect_id is required"}), 400

    with _lock:
        session = _sessions.get(connect_id)

    if not session:
        settings = get_settings_manager().get_all_settings()
        if (
            settings.get("boondock_cloud_connected", "").lower() == "true"
            and settings.get("transcription_api_key")
        ):
            return jsonify(
                {
                    "status": "connected",
                    "key_prefix": settings.get("boondock_cloud_key_prefix") or "",
                }
            )
        return jsonify({"status": "unknown", "error": "No active connect session"}), 404

    return jsonify(
        {
            "status": session["status"],
            "portal_url": session.get("portal_url"),
            "error": session.get("error"),
            "key_prefix": session.get("key_prefix"),
        }
    )


@boondock_cloud_bp.route("/boondock-cloud/connect/cancel", methods=["POST"])
@require_admin
def connect_cancel():
    body = request.get_json(silent=True) or {}
    connect_id = (body.get("connect_id") or body.get("connectId") or "").strip()
    if not connect_id:
        return jsonify({"error": "connect_id is required"}), 400

    with _lock:
        session = _sessions.get(connect_id)
        if session:
            session["stop"] = True
            if session["status"] == "pending":
                session["status"] = "cancelled"

    return jsonify({"status": "cancelled"})


def _install_machine_id():
    """Prefer the persistent id written by boondock-edge-install.sh."""
    candidates = []
    env_root = os.environ.get("BOONDOCK_INSTALL_ROOT", "").strip()
    if env_root:
        candidates.append(os.path.join(env_root, ".boondock_machine_id"))
    # Typical Linux install layout: INSTALL_ROOT/app/.../boondock_cloud_routes.py
    here = os.path.dirname(os.path.abspath(__file__))
    install_root = os.path.abspath(os.path.join(here, "..", "..", ".."))
    candidates.append(os.path.join(install_root, ".boondock_machine_id"))
    candidates.append("/opt/boondock/edge/.boondock_machine_id")
    for path in candidates:
        try:
            with open(path, encoding="utf-8") as handle:
                mid = handle.read().strip()
                if mid:
                    return mid
        except OSError:
            continue
    return ""


@boondock_cloud_bp.route("/boondock-cloud/connect/with-key", methods=["POST"])
@require_admin
def connect_with_account_key():
    """
    Cloud Connect via Account Installation Key (no browser OAuth).
    Body: { key: "bdi_..." }
    """
    body = request.get_json(silent=True) or {}
    account_key = (body.get("key") or body.get("account_key") or "").strip()
    if not account_key:
        return jsonify({"error": "Account installation key is required"}), 400

    snapshot = _device_snapshot()
    machine_id = (
        (body.get("machine_id") or body.get("machineId") or "").strip()
        or _install_machine_id()
        or snapshot.get("deviceId")
        or ""
    )
    if not machine_id:
        return jsonify({"error": "Unable to resolve machine_id"}), 500

    portal = _portal_base_url()
    try:
        resp = requests.post(
            f"{portal}/api/edge/install/connect",
            json={
                "key": account_key,
                "machine_id": machine_id,
                "hostname": snapshot.get("name") or _device_name(),
                "mac": snapshot.get("mac") or "",
                "device_name": snapshot.get("name") or _device_name(),
            },
            timeout=30,
        )
    except requests.RequestException as exc:
        logging.exception("Account-key connect failed contacting portal")
        return jsonify({"error": f"Portal unreachable: {exc}"}), 502

    try:
        payload = resp.json()
    except ValueError:
        payload = {}

    if resp.status_code >= 400:
        err = (
            payload.get("error")
            or (payload.get("data") or {}).get("error")
            or payload.get("message")
            or f"Portal returned {resp.status_code}"
        )
        return jsonify({"error": err}), resp.status_code if resp.status_code < 500 else 502

    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    api_key = (data.get("api_key") or "").strip()
    if not api_key:
        return jsonify({"error": "Portal did not return an API key"}), 502

    key_prefix = (data.get("key_prefix") or api_key[:12]).strip()
    key_id = data.get("key_id")
    device_id = (data.get("device_id") or data.get("machine_id") or machine_id).strip()

    try:
        _apply_connected_key(api_key, key_prefix, key_id=key_id, device_id=device_id)
        ensure_heartbeat_running()
        try:
            _push_heartbeat()
        except Exception:
            logging.exception("Initial portal heartbeat failed after Account Key connect")
    except Exception as exc:
        logging.exception("Failed to apply Account Key connect locally")
        return jsonify({"error": str(exc)}), 500

    return jsonify(
        {
            "status": "connected",
            "key_prefix": key_prefix,
            "key_id": key_id,
            "device_id": device_id,
            "machine_id": machine_id,
        }
    )


@boondock_cloud_bp.route("/boondock-cloud/disconnect", methods=["POST"])
@require_admin
def disconnect():
    """Clear stored Boondock Cloud API key and mark device disconnected on portal."""
    with _lock:
        for session in _sessions.values():
            session["stop"] = True

    _stop_heartbeat()
    settings = get_settings_manager().get_all_settings()
    device_id = str(settings.get("boondock_cloud_device_id") or "").strip()
    key_id = str(settings.get("boondock_cloud_key_id") or "").strip()
    try:
        _notify_portal_disconnect(device_id, key_id)
    except Exception as exc:
        logging.warning("Portal disconnect notify raised: %s", exc)
    try:
        _clear_cloud_key()
    except Exception as exc:
        logging.exception("Failed to clear Boondock Cloud key locally")
        return jsonify({"error": str(exc)}), 500
    return jsonify({"status": "disconnected", "device_id": device_id or None})


@boondock_cloud_bp.route("/boondock-cloud/sync", methods=["POST"])
@require_admin
def sync_now():
    """Push current Edge stats to the portal immediately (manual refresh)."""
    ok = _push_heartbeat()
    if not ok:
        return jsonify({"error": "Not connected or portal unreachable"}), 400
    return jsonify({"status": "synced", "snapshot": _device_snapshot()})


@boondock_cloud_bp.route("/boondock-cloud/status", methods=["GET"])
@require_admin
def cloud_status():
    """Connection summary for the settings page. Never returns the key itself."""
    settings = get_settings_manager().get_all_settings()
    connected = (
        str(settings.get("boondock_cloud_connected") or "").lower() == "true"
        and bool(settings.get("transcription_api_key"))
    )
    return jsonify(
        {
            "connected": connected,
            "key_prefix": settings.get("boondock_cloud_key_prefix") or "",
            "device_id": settings.get("boondock_cloud_device_id") or "",
            "portal_url": _portal_base_url(),
        }
    )
