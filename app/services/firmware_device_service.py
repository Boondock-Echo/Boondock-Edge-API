"""Device-facing firmware check and file resolution (OTA from Edge server)."""
import json
import logging
import re
from pathlib import Path
from config import DATA_ROOT
from typing import Any, Dict, Optional, Tuple

from .settings_manager import get_settings_manager

logger = logging.getLogger(__name__)

# Same tree as db_initializer / release: <repo>/firmware/<id>/firmware.bin
_REPO_FIRMWARE_DIR = DATA_ROOT / "firmware"


def _load_firmware_metadata_dict() -> Dict[str, Any]:
    m = get_settings_manager().get_firmware_metadata()
    if m:
        return m
    path = _REPO_FIRMWARE_DIR / "firmware.json"
    if path.is_file():
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.warning("Failed to read %s: %s", path, e)
    return {}


def _version_tuple(v: str) -> Tuple[int, ...]:
    if not v:
        return (0,)
    parts = re.findall(r"\d+", str(v))
    if not parts:
        return (0,)
    out = []
    for p in parts[:8]:
        try:
            out.append(int(p))
        except ValueError:
            out.append(0)
    return tuple(out)


def _version_from_entry(firmware_id: str, info: Dict[str, Any]) -> Optional[str]:
    v = info.get("version")
    if v and str(v).strip():
        return str(v).strip()
    name = info.get("name") or ""
    m = re.search(r"(\d+\.\d+\.\d+)", str(name))
    if m:
        return m.group(1)
    return None


def _firmware_dir_for_id(firmware_id: str, metadata: Dict[str, Any]) -> str:
    info = metadata.get(firmware_id) or {}
    folder = info.get("folder", firmware_id)
    return _REPO_FIRMWARE_DIR / folder


def _firmware_bin_exists(firmware_id: str, metadata: Dict[str, Any]) -> bool:
    d = _firmware_dir_for_id(firmware_id, metadata)
    return (d / "firmware.bin").is_file()


def find_upgrade_for_device(current_version: str) -> Optional[Tuple[str, str, str]]:
    """
    Return (firmware_id, target_version, description) if a newer build exists on disk.
    """
    metadata = _load_firmware_metadata_dict()
    if not metadata:
        return None

    cur = _version_tuple(current_version)
    best: Optional[Tuple[Tuple[int, ...], str, str, str]] = None

    for firmware_id, info in metadata.items():
        if not isinstance(info, dict):
            continue
        ver = _version_from_entry(firmware_id, info)
        if not ver:
            continue
        if not _firmware_bin_exists(firmware_id, metadata):
            continue
        vt = _version_tuple(ver)
        if vt <= cur:
            continue
        desc = (info.get("description") or info.get("name") or "").strip()
        if best is None or vt > best[0]:
            best = (vt, firmware_id, ver, desc)

    if not best:
        return None
    _, fid, ver, desc = best
    return (fid, ver, desc or "Firmware update")


def get_firmware_file_path(firmware_id: str, filename: str) -> Optional[Path]:
    metadata = _load_firmware_metadata_dict()
    if firmware_id not in metadata:
        return None
    if filename not in ("firmware.bin", "bootloader.bin", "partitions.bin"):
        return None
    d = _firmware_dir_for_id(firmware_id, metadata)
    path = (d / filename).resolve()
    if not path.is_file():
        return None
    if not path.is_relative_to(_REPO_FIRMWARE_DIR):
        return None
    return path
