import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

HOTSPOT_CONNECTION_NAME = "BoondockHotspot"
_wifi_interface_cache: Optional[str] = None
_wifi_interface_detected = False
_command_path_cache: Dict[str, Optional[str]] = {}
_COMMAND_TIMEOUT_SECONDS = 10

# systemd units often set PATH to the venv only; nmcli/iw live under /usr/bin.
_SYSTEM_COMMAND_PATHS = (
    "/usr/local/sbin",
    "/usr/local/bin",
    "/usr/sbin",
    "/usr/bin",
    "/sbin",
    "/bin",
)


def _resolve_command(command: str) -> Optional[str]:
    """Resolve executable path (PATH, env override, or common system locations)."""
    if command in _command_path_cache:
        return _command_path_cache[command]

    env_key = f"BOONDOCK_{command.upper().replace('-', '_')}_PATH"
    env_path = os.environ.get(env_key, "").strip()
    if env_path and os.path.isfile(env_path) and os.access(env_path, os.X_OK):
        _command_path_cache[command] = env_path
        return env_path

    found = shutil.which(command)
    if found:
        _command_path_cache[command] = found
        return found

    for prefix in _SYSTEM_COMMAND_PATHS:
        candidate = os.path.join(prefix, command)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            _command_path_cache[command] = candidate
            return candidate

    _command_path_cache[command] = None
    return None


def _command_available(command: str) -> bool:
    """Return True if the given command is available on the system."""
    return _resolve_command(command) is not None


def _run_command(args: List[str]) -> Dict[str, object]:
    """Run a command and return a structured result."""
    if args:
        resolved = _resolve_command(args[0])
        if resolved:
            args = [resolved, *args[1:]]
    try:
        logger.debug("Running command: %s", " ".join(args))
        completed = subprocess.run(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            timeout=_COMMAND_TIMEOUT_SECONDS,
        )
        return {
            "ok": completed.returncode == 0,
            "returncode": completed.returncode,
            "stdout": completed.stdout.strip(),
            "stderr": completed.stderr.strip(),
        }
    except Exception as exc:  # noqa: BLE001
        logger.exception("Error running command %s", args)
        return {
            "ok": False,
            "returncode": -1,
            "stdout": "",
            "stderr": str(exc),
        }


def _parse_nmcli_value(raw: str) -> str:
    """
    Normalize nmcli -t field output.

    Many nmcli -t queries return 'field-name:value'. We only care about the
    value portion for display, so split on the first ':' and return the tail.
    """
    if raw is None:
        return ""
    text = raw.strip()
    if ":" in text:
        return text.split(":", 1)[1]
    return text


def _detect_wifi_interface_nmcli() -> Optional[str]:
    """Pick the connected Wi-Fi netdev, falling back to the first Wi-Fi device."""
    res = _run_command(["nmcli", "-t", "-f", "DEVICE,TYPE,STATE", "device"])
    if not res["ok"]:
        return None

    candidates: List[str] = []
    for line in res.get("stdout", "").splitlines():
        parts = line.split(":")
        if len(parts) < 3:
            continue
        device, dtype, state = parts[0], parts[1], parts[2]
        if dtype == "wifi" and device and not device.startswith("p2p-dev-"):
            if state == "connected":
                return device
            candidates.append(device)
    return candidates[0] if candidates else None


def _detect_wifi_interface_sysfs() -> Optional[str]:
    """Find wlan*/wlp* interfaces that expose /sys/class/net/<if>/wireless."""
    try:
        net_dir = Path("/sys/class/net")
        if not net_dir.is_dir():
            return None
        names = sorted(
            p.name
            for p in net_dir.iterdir()
            if p.name.startswith(("wlan", "wlp")) and (p / "wireless").exists()
        )
        return names[0] if names else None
    except OSError:
        return None


def get_wifi_interface() -> Optional[str]:
    """
    Resolve the Wi-Fi interface for hotspot operations.

    Priority: BOONDOCK_WIFI_INTERFACE env → nmcli → sysfs.
    Works on Raspberry Pi (wlan0) and mini PCs (e.g. wlp2s0 + RTL8821CE).

    ``None`` is returned when the machine has no Wi-Fi device.  Do not invent a
    ``wlan0`` fallback: issuing NetworkManager operations against a nonexistent
    device is both noisy and has triggered failures in older NetworkManager
    builds used by some thin clients.
    """
    global _wifi_interface_cache, _wifi_interface_detected  # noqa: PLW0603
    if _wifi_interface_detected:
        return _wifi_interface_cache

    env_iface = os.environ.get("BOONDOCK_WIFI_INTERFACE", "").strip()
    if env_iface:
        _wifi_interface_cache = env_iface
        _wifi_interface_detected = True
        return env_iface

    _wifi_interface_cache = _detect_wifi_interface_nmcli() or _detect_wifi_interface_sysfs()
    _wifi_interface_detected = True
    return _wifi_interface_cache


def _wifi_ap_supported(iface: Optional[str]) -> bool:
    """Return True if NetworkManager or iw reports AP mode support."""
    if not iface:
        return False
    if _command_available("nmcli"):
        res = _run_command(
            ["nmcli", "-t", "-f", "WIFI-PROPERTIES.AP", "device", "show", iface]
        )
        if res["ok"] and res.get("stdout"):
            return _parse_nmcli_value(res["stdout"]).lower() == "yes"

    if _command_available("iw"):
        res = _run_command(["iw", "dev", iface, "info"])
        if res["ok"]:
            phy_match = None
            for line in res["stdout"].splitlines():
                line = line.strip()
                if line.startswith("wiphy "):
                    phy_match = line.split()[1]
                    break
            if phy_match is not None:
                phy_res = _run_command(["iw", "phy", f"phy{phy_match}", "info"])
                if phy_res["ok"]:
                    return "AP" in phy_res["stdout"]

    # Unknown: attempt hotspot anyway (some drivers report AP=no until prepared).
    return True


def _prepare_wifi_interface(iface: str) -> Optional[str]:
    """
    Enable Wi-Fi radio, ensure NM manages the device, and disconnect STA mode.
    Returns an error message when AP mode is definitely unsupported.
    """
    if not _wifi_ap_supported(iface):
        return (
            f"Wi-Fi adapter '{iface}' does not report AP/hotspot support. "
            "Install or update the wireless driver (e.g. rtw88 for RTL8821CE) "
            "and ensure NetworkManager is managing the interface."
        )
    else:
        _run_command(["nmcli", "radio", "wifi", "on"])
        _run_command(["nmcli", "device", "set", iface, "managed", "yes"])
        _run_command(["nmcli", "device", "disconnect", iface])

        
    return None


def _apply_hotspot_connection_settings(iface: str, ssid: str, password: str) -> None:
    """Ensure the saved NM profile is AP mode on the correct interface."""
    _run_command(
        [
            "nmcli",
            "connection",
            "modify",
            HOTSPOT_CONNECTION_NAME,
            "connection.interface-name",
            iface,
            "802-11-wireless.mode",
            "ap",
            "802-11-wireless.ssid",
            ssid,
            "wifi-sec.key-mgmt",
            "wpa-psk",
            "wifi-sec.psk",
            password,
            "802-11-wireless.band",
            "bg",
            "ipv4.method",
            "shared",
            "connection.autoconnect",
            "yes",
        ]
    )


def _create_hotspot_profile(iface: str, ssid: str, password: str) -> Dict[str, object]:
    """Create hotspot via nmcli; retry without band if 2.4 GHz-only fails."""
    base_args = [
        "nmcli",
        "device",
        "wifi",
        "hotspot",
        "ifname",
        iface,
        "con-name",
        HOTSPOT_CONNECTION_NAME,
        "ssid",
        ssid,
        "password",
        password,
    ]
    attempts = [base_args + ["band", "bg"], base_args]
    last_res: Dict[str, object] = {"ok": False, "stderr": "", "stdout": ""}
    for args in attempts:
        last_res = _run_command(args)
        if last_res["ok"]:
            return last_res
        logger.warning(
            "Hotspot create failed (%s): %s",
            " ".join(args[-2:]) if len(args) > len(base_args) else "no band",
            last_res.get("stderr") or last_res.get("stdout"),
        )
    return last_res


def _delete_hotspot_connection() -> None:
    _run_command(["nmcli", "connection", "down", HOTSPOT_CONNECTION_NAME])
    _run_command(["nmcli", "connection", "delete", HOTSPOT_CONNECTION_NAME])


def _get_active_hotspot_info() -> Dict[str, object]:
    """
    Inspect NetworkManager for the hotspot connection.

    Returns:
        {
          "enabled": bool,
          "connection_name": str | None,
          "interface": str | None,
          "ssid": str | None,
          "ipv4_method": str | None,
          "ip_address": str | None,
          "external_wifi": bool,
        }
    """
    wifi_iface = get_wifi_interface()

    if not _command_available("nmcli") or not wifi_iface:
        return {
            "enabled": False,
            "connection_name": None,
            "interface": None,
            "ssid": None,
            "ipv4_method": None,
            "ip_address": None,
            "external_wifi": False,
        }

    # Check active connections for our hotspot profile first
    active = _run_command(
        ["nmcli", "-t", "-f", "NAME,TYPE,DEVICE", "connection", "show", "--active"]
    )
    enabled = False
    interface = None
    connection_name = None
    for line in active.get("stdout", "").splitlines():
        # Expected format: NAME:TYPE:DEVICE
        parts = line.split(":")
        if len(parts) != 3:
            continue
        name, conn_type, dev = parts
        if name == HOTSPOT_CONNECTION_NAME and conn_type == "wifi":
            enabled = True
            interface = dev or wifi_iface
            connection_name = name
            break

    ssid = None
    ipv4_method = None
    external_wifi = False
    if _command_available("nmcli"):
        if connection_name:
            # Get configured SSID and IPv4 method for the hotspot profile
            ssid_res = _run_command(
                [
                    "nmcli",
                    "-t",
                    "-f",
                    "802-11-wireless.ssid",
                    "connection",
                    "show",
                    connection_name,
                ]
            )
            if ssid_res["ok"] and ssid_res["stdout"]:
                ssid = _parse_nmcli_value(ssid_res["stdout"]) or None

            method_res = _run_command(
                ["nmcli", "-t", "-f", "ipv4.method", "connection", "show", connection_name]
            )
            if method_res["ok"] and method_res["stdout"]:
                ipv4_method = _parse_nmcli_value(method_res["stdout"]) or None
        else:
            # A Wi-Fi interface having an IPv4 address only means that it is
            # connected to a network; it does not mean that it is an access
            # point.  For non-Boondock profiles, inspect the active profile's
            # mode before treating it as a manually configured hotspot.
            dev_info = _run_command(
                [
                    "nmcli",
                    "-t",
                    "-f",
                    "GENERAL.CONNECTION,GENERAL.DEVICE",
                    "device",
                    "show",
                    wifi_iface,
                ]
            )
            if dev_info["ok"]:
                for line in dev_info["stdout"].splitlines():
                    parts = line.split(":")
                    if len(parts) >= 2 and parts[0] == "GENERAL.CONNECTION":
                        connection_name = parts[1] or None
                        break
            if connection_name:
                mode_res = _run_command(
                    [
                        "nmcli",
                        "-t",
                        "-f",
                        "802-11-wireless.mode",
                        "connection",
                        "show",
                        connection_name,
                    ]
                )
                mode = (
                    _parse_nmcli_value(mode_res.get("stdout", "")).lower()
                    if mode_res["ok"]
                    else ""
                )
                if mode == "ap":
                    enabled = True
                    interface = wifi_iface
                elif mode in ("infrastructure", "station"):
                    external_wifi = True
                    interface = wifi_iface

                ssid_res = _run_command(
                    [
                        "nmcli",
                        "-t",
                        "-f",
                        "802-11-wireless.ssid",
                        "connection",
                        "show",
                        connection_name,
                    ]
                )
                if ssid_res["ok"] and ssid_res["stdout"]:
                    ssid = _parse_nmcli_value(ssid_res["stdout"]) or None

                method_res = _run_command(
                    [
                        "nmcli",
                        "-t",
                        "-f",
                        "ipv4.method",
                        "connection",
                        "show",
                        connection_name,
                    ]
                )
                if method_res["ok"] and method_res["stdout"]:
                    ipv4_method = _parse_nmcli_value(method_res["stdout"]) or None

    ip_address = None
    if (enabled or external_wifi) and _command_available("ip"):
        ip_res = _run_command(["ip", "-4", "addr", "show", interface or wifi_iface])
        if ip_res["ok"]:
            # Look for a line like: "inet 10.42.0.1/24 ..."
            for line in ip_res["stdout"].splitlines():
                line = line.strip()
                if line.startswith("inet "):
                    try:
                        ip_part = line.split()[1]  # "10.42.0.1/24"
                        ip_address = ip_part.split("/")[0]
                        break
                    except (IndexError, ValueError):
                        continue

    return {
        "enabled": enabled,
        "connection_name": connection_name if (enabled or external_wifi) else None,
        "interface": interface or wifi_iface,
        "ssid": ssid,
        "ipv4_method": ipv4_method,
        "ip_address": ip_address,
        "external_wifi": external_wifi,
    }


def _get_connected_clients(interface: str) -> Dict[str, object]:
    """
    Use `iw dev <iface> station dump` to approximate connected clients.

    Returns:
        {
          "count": int,
          "stations": [ { "mac": str } ]
        }
    """
    if not _command_available("iw"):
        return {"count": 0, "stations": []}

    result = _run_command(["iw", "dev", interface, "station", "dump"])
    if not result["ok"]:
        return {"count": 0, "stations": []}

    stations: List[Dict[str, str]] = []
    for line in result["stdout"].splitlines():
        line = line.strip()
        # Lines that begin with "Station <MAC> ..."
        if line.lower().startswith("station "):
            parts = line.split()
            if len(parts) >= 2:
                stations.append({"mac": parts[1]})

    return {"count": len(stations), "stations": stations}


def get_hotspot_status() -> Dict[str, object]:
    """
    Public helper to return consolidated hotspot status and client info.
    """
    wifi_iface = get_wifi_interface()

    if not wifi_iface:
        return {
            "supported": False,
            "enabled": False,
            "connection_name": None,
            "interface": None,
            "ssid": None,
            "ipv4_method": None,
            "ip_address": None,
            "clients": {"count": 0, "stations": []},
            "ap_supported": False,
            "external_wifi": False,
            "message": "No Wi-Fi interface was detected on this device.",
        }

    if not _command_available("nmcli"):
        return {
            "supported": False,
            "enabled": False,
            "connection_name": None,
            "interface": wifi_iface,
            "ssid": None,
            "ipv4_method": None,
            "ip_address": None,
            "clients": {"count": 0, "stations": []},
            "ap_supported": False,
            "external_wifi": False,
            "message": (
                "nmcli is not available to the edge server process. "
                "Install NetworkManager or add /usr/bin to the systemd unit PATH."
            ),
        }

    info = _get_active_hotspot_info()
    iface = info.get("interface") or wifi_iface
    clients = _get_connected_clients(iface)

    return {
        "supported": True,
        "enabled": bool(info.get("enabled")),
        "connection_name": info.get("connection_name") if info.get("enabled") else None,
        "interface": iface,
        "ssid": info.get("ssid"),
        "ipv4_method": info.get("ipv4_method"),
        "ip_address": info.get("ip_address"),
        "clients": clients,
        "ap_supported": _wifi_ap_supported(iface),
        "external_wifi": bool(info.get("external_wifi")),
    }


def start_hotspot(ssid: str, password: str) -> Dict[str, object]:
    """
    Create or update and activate the hotspot using nmcli.

    Supports Raspberry Pi and mini-PC Wi-Fi (auto-detected interface).
    """
    if not _command_available("nmcli"):
        return {
            "success": False,
            "error": "nmcli is not available on this system.",
        }

    if not ssid or not password:
        return {
            "success": False,
            "error": "SSID and password are required to start the hotspot.",
        }

    iface = get_wifi_interface()
    if not iface:
        return {
            "success": False,
            "error": "No Wi-Fi interface was detected on this device.",
            "interface": None,
        }
    prep_error = _prepare_wifi_interface(iface)
    if prep_error:
        return {"success": False, "error": prep_error, "interface": iface}

    # Check if the connection already exists
    existing = _run_command(["nmcli", "connection", "show", HOTSPOT_CONNECTION_NAME])
    if not existing["ok"]:
        create_res = _create_hotspot_profile(iface, ssid, password)
        if not create_res["ok"]:
            return {
                "success": False,
                "error": (
                    f"Failed to create hotspot on {iface}: "
                    f"{create_res['stderr'] or create_res['stdout']}"
                ),
                "interface": iface,
            }
    else:
        _apply_hotspot_connection_settings(iface, ssid, password)

    up_res = _run_command(["nmcli", "connection", "up", HOTSPOT_CONNECTION_NAME])
    if not up_res["ok"]:
        logger.warning(
            "Hotspot activation failed, recreating profile: %s",
            up_res.get("stderr") or up_res.get("stdout"),
        )
        _delete_hotspot_connection()
        _prepare_wifi_interface(iface)
        create_res = _create_hotspot_profile(iface, ssid, password)
        if not create_res["ok"]:
            return {
                "success": False,
                "error": (
                    f"Failed to recreate hotspot on {iface}: "
                    f"{create_res['stderr'] or create_res['stdout']}"
                ),
                "interface": iface,
            }
        _apply_hotspot_connection_settings(iface, ssid, password)
        up_res = _run_command(["nmcli", "connection", "up", HOTSPOT_CONNECTION_NAME])

    if not up_res["ok"]:
        return {
            "success": False,
            "error": (
                f"Failed to bring hotspot up on {iface}: "
                f"{up_res['stderr'] or up_res['stdout']}"
            ),
            "interface": iface,
        }

    status = get_hotspot_status()
    status["success"] = True
    return status


def stop_hotspot() -> Dict[str, object]:
    """
    Deactivate the hotspot connection if it exists.
    """
    if not _command_available("nmcli"):
        return {
            "success": False,
            "error": "nmcli is not available on this system.",
        }

    info = _get_active_hotspot_info()
    connection_name = info.get("connection_name") if info.get("enabled") else None
    if not connection_name:
        status = get_hotspot_status()
        status["success"] = True
        return status

    down_res = _run_command(["nmcli", "connection", "down", connection_name])
    if not down_res["ok"]:
        # It's not fatal if the connection was already down or didn't exist.
        logger.warning(
            "Failed to bring hotspot connection down: %s",
            down_res.get("stderr") or down_res.get("stdout"),
        )

    status = get_hotspot_status()
    status["success"] = True
    return status
