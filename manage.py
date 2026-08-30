#!/usr/bin/env python3
"""Install-time setup for the Boondock Edge API.

This is intentionally the single entry point for persistent application
initialization.  ``install.sh`` writes the requested configuration to
``setup.json`` and invokes this program; normal server startup must not change
installation settings.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from config import DATA_ROOT

LOGGER = logging.getLogger("boondock.setup")

DEVICE_SETTINGS = {
    "boondock_edge": "global_enable_edge_devices",
    "uniden_scanner": "global_enable_uniden_scanners",
    "usb_audio": "global_enable_usb_audio_devices",
    "gpio": "global_enable_gpio",
}
INBOX_VIEWS = {"continuous", "pagination"}
MESSAGE_SORTING = {"newest", "oldest"}


class SetupError(ValueError):
    """Raised when setup.json is missing or contains an invalid option."""


def load_setup(path: Path) -> dict[str, Any]:
    """Read and validate an installer-generated setup document."""
    try:
        with path.open("r", encoding="utf-8") as setup_file:
            setup = json.load(setup_file)
    except FileNotFoundError as exc:
        raise SetupError(f"Setup file does not exist: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise SetupError(f"Unable to read setup file {path}: {exc}") from exc

    if not isinstance(setup, dict):
        raise SetupError("The setup document must be a JSON object")

    admin = setup.get("admin")
    if not isinstance(admin, dict):
        raise SetupError("admin must be an object")
    email = admin.get("email")
    password = admin.get("password")
    if not isinstance(email, str) or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email.strip()):
        raise SetupError("admin.email must be a valid email address")
    if not isinstance(password, str) or len(password) < 8:
        raise SetupError("admin.password must contain at least 8 characters")
    admin["email"] = email.strip().lower()

    selected_devices = setup.get("selected_devices", [])
    if not isinstance(selected_devices, list) or not all(isinstance(item, str) for item in selected_devices):
        raise SetupError("selected_devices must be an array of device names")
    unknown_devices = sorted(set(selected_devices) - DEVICE_SETTINGS.keys())
    if unknown_devices:
        raise SetupError(f"Unsupported selected_devices: {', '.join(unknown_devices)}")

    wifi = setup.get("wifi")
    if wifi is not None and (
        not isinstance(wifi, dict)
        or not wifi.get("ssid")
        or "password" not in wifi
    ):
        setup['wifi'] = {
            "ssid": "boondockedge",
            "password": "edge@123",
            "ip_address": "10.42.0.1",
            "external_wifi": False,
        }
    else:
        setup['wifi']['external_wifi'] = True

    preferences = setup.get("preferences")
    if not isinstance(preferences, dict):
        raise SetupError("preferences must be an object")
    if preferences.get("inbox_view") not in INBOX_VIEWS:
        raise SetupError(f"preferences.inbox_view must be one of: {', '.join(sorted(INBOX_VIEWS))}")
    if preferences.get("message_sorting") not in MESSAGE_SORTING:
        raise SetupError(f"preferences.message_sorting must be one of: {', '.join(sorted(MESSAGE_SORTING))}")
    return setup


def auto_configure_connected_edge_devices(settings_manager: Any) -> dict[str, Any]:
    """Apply the saved network settings to every connected Edge USB device."""
    from serial.tools import list_ports

    from app.services.recorder_monitor import (
        _matches_esp32_bridge,
        run_autoconfig_sequence,
    )

    settings = settings_manager.get_all_settings() or {}
    host_ssid = settings.get("host_ssid")
    host_password = settings.get("host_password")
    host_ip = settings.get("host_ip")
    host_port = settings.get("host_port", "4000")
    if not all((host_ssid, host_password, host_ip, host_port)):
        LOGGER.warning("Skipping USB device autoconfiguration: host settings are incomplete")
        LOGGER.warning("%s, %s, %s, %s",host_ssid, host_password, host_ip, host_port)
        return {}

    results = {}
    try:
        connected_ports = list_ports.comports()
    except Exception as exc:  # USB discovery should not prevent application setup.
        LOGGER.warning("Unable to enumerate USB devices for autoconfiguration: %s", exc)
        return results

    for port_info in connected_ports:
        if not _matches_esp32_bridge(port_info):
            continue
        port = port_info.device
        LOGGER.info("Autoconfiguring connected Boondock Edge device on %s", port)
        results[port] = run_autoconfig_sequence(
            port,
            host_ssid,
            host_password,
            host_ip,
            host_port,
        )

    if not results:
        LOGGER.info("No connected Boondock Edge USB devices found during setup")
    return results


def initialize(setup: dict[str, Any]) -> None:
    """Create the databases and apply all setup options idempotently."""
    from app.services.db_initializer import initialize_settings_database
    from app.services.recordings_db_initializer import initialize_db
    from app.services.settings_manager import get_settings_manager
    from app.utils.auth import load_tokens
    from app.utils.password_utils import hash_password

    initialize_settings_database()
    settings_manager = get_settings_manager()
    admin = setup["admin"]
    existing_admin = settings_manager.get_user(admin["email"]) or {}
    existing_admin.update({
        "name": existing_admin.get("name", "Administrator"),
        "password": hash_password(admin["password"]),
        "role": "admin",
        "profile": "Admin",
        "status": "Active",
        "accessLevel": "Level 1",
        "mfa_enabled": existing_admin.get("mfa_enabled", False),
        "created_at": existing_admin.get("created_at") or datetime.now(timezone.utc).isoformat(),
        "login_history": existing_admin.get("login_history", []),
    })
    if not settings_manager.save_user(admin["email"], existing_admin):
        raise RuntimeError("Unable to save the administrator account")

    selected = set(setup["selected_devices"])
    preferences = setup["preferences"]
    settings = {
        **{setting: device in selected for device, setting in DEVICE_SETTINGS.items()},
        "global_inbox_view_mode": preferences["inbox_view"],
        "host_ssid": setup['wifi']['ssid'],
        "host_password": setup['wifi']['password'],
        "host_ip": setup['wifi']['ip_address'],
    }
    if not settings_manager.set_all_settings(settings):
        raise RuntimeError("Unable to save installation settings")
    if not settings_manager.save_pagination_prefs(admin["email"], {
        "recordsPerPage": 20,
        "currentPage": 1,
        "reverseSort": preferences["message_sorting"] == "oldest",
        "showFullTimestamps": False,
    }):
        raise RuntimeError("Unable to save administrator preferences")

    initialize_db()
    load_tokens()

    if "boondock_edge" in selected:
        auto_configure_connected_edge_devices(settings_manager)


    LOGGER.info("API setup completed successfully in %s", DATA_ROOT)

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage Boondock Edge")
    commands = parser.add_subparsers(dest="command", required=True)

    setup_parser = commands.add_parser(
        "setup",
        help="initialize the application from an installer setup file",
    )
    setup_parser.add_argument(
        "--config",
        type=Path,
        required=True,
        help="path to setup.json",
    )
    return parser

def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s: %(message)s",
    )

    try:
        if args.command == "setup":
            initialize(load_setup(args.config))

    except SetupError as exc:
        LOGGER.error("Setup failed: %s", exc)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

