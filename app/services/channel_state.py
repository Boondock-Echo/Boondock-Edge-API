"""
In-memory channel visual state management.
Tracks recording/idle/error/warning states for visual feedback without persisting to channels.json
"""
import threading
import time
from typing import Dict, Optional, Set
from datetime import datetime

from .settings_manager import normalize_mac_address

# Canonical MAC = 12 hex uppercase (no colons)
# In-memory state store: {mac_key: {state, timestamp}}
_channel_states: Dict[str, Dict] = {}
# Last API/cloud activity per device (for offline detection)
_last_seen: Dict[str, float] = {}
_state_lock = threading.Lock()

# > 2× typical device ping interval (60s)
CLOUD_ACTIVITY_STALE_SECONDS = 150


def _mac_key(mac_address: str) -> str:
    if not mac_address:
        return ""
    k = normalize_mac_address(mac_address)
    return k if len(k) == 12 else mac_address.replace(":", "").replace("-", "").upper()


def touch_device_activity(mac_address: str) -> None:
    """Record that the device was recently seen (legacy ping or cloud event)."""
    k = _mac_key(mac_address)
    if not k or len(k) != 12:
        return
    with _state_lock:
        _last_seen[k] = time.time()


class ChannelVisualState:
    """Enum-like class for visual state constants."""
    IDLE = "idle"
    RECORDING = "recording"
    ERROR = "error"
    WARNING = "warning"


def set_channel_visual_state(mac_address: str, state: str) -> None:
    """
    Set the visual state for a channel in memory.

    Args:
        mac_address: Channel MAC (any format)
        state: recording, idle, error, warning, online, offline, etc.
    """
    mac = _mac_key(mac_address)
    if not mac:
        return
    with _state_lock:
        _channel_states[mac] = {
            "state": state,
            "timestamp": datetime.now().isoformat(),
        }


def get_stored_visual_state(mac_address: str) -> Optional[str]:
    """State without offline stale check (e.g. ping must not clear recording)."""
    mac = _mac_key(mac_address)
    if not mac:
        return None
    with _state_lock:
        if mac in _channel_states:
            return _channel_states[mac].get("state")
    return None


def get_channel_visual_state(mac_address: str) -> Optional[str]:
    """
    Effective visual state: offline if last activity is stale; else stored state.
    """
    mac = _mac_key(mac_address)
    if not mac:
        return None
    with _state_lock:
        last = _last_seen.get(mac)
        if last is not None and (time.time() - last) > CLOUD_ACTIVITY_STALE_SECONDS:
            return "offline"
        if mac in _channel_states:
            return _channel_states[mac].get("state")
    return None


def get_all_channel_visual_states() -> Dict[str, Dict]:
    """All devices that have state or recent activity; effective state includes offline."""
    with _state_lock:
        keys: Set[str] = set(_channel_states) | set(_last_seen)
        out: Dict[str, Dict] = {}
        now = time.time()
        for k in keys:
            last = _last_seen.get(k)
            if last is not None and (now - last) > CLOUD_ACTIVITY_STALE_SECONDS:
                st = "offline"
            elif k in _channel_states:
                st = _channel_states[k].get("state")
            else:
                st = None
            ts = _channel_states.get(k, {}).get("timestamp")
            if st is not None or k in _channel_states or (
                last is not None and (now - last) <= CLOUD_ACTIVITY_STALE_SECONDS
            ):
                out[k] = {"state": st, "timestamp": ts}
        return out


def clear_channel_visual_state(mac_address: str) -> None:
    mac = _mac_key(mac_address)
    with _state_lock:
        if mac in _channel_states:
            del _channel_states[mac]
        _last_seen.pop(mac, None)


def clear_all_visual_states() -> None:
    """Clear all channel visual states (useful for testing)."""
    with _state_lock:
        _channel_states.clear()
