"""Persist cloud-style device lifecycle events (DEVICE_API JSON contract) to SQLite."""
import json
import logging
import sqlite3
import sys
import threading
from config import Config
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# event_type string -> integer ID (DEVICE_API.md)
CLOUD_EVENT_TYPE_TO_ID = {
    "offline": 0,
    "online": 1,
    "info": 2,
    "warning": 3,
    "error": 4,
    "fatal_error": 5,
    "startup": 6,
    "shutdown": 7,
    "heartbeat": 8,
    "status": 9,
    "alert": 10,
    "ping": 11,
    "config": 12,
    "record_begin": 13,
    "record_end": 14,
    "audio_upload_success": 15,
    "audio_upload_failed": 16,
    "audio_upload_skipped": 17,
    "settings_updated": 18,
    "setting_changed": 19,
}


def cloud_event_type_id(event_type: str) -> int:
    return CLOUD_EVENT_TYPE_TO_ID.get((event_type or "").strip().lower(), 99)


def _ensure_cloud_events_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS cloud_device_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            mac TEXT NOT NULL,
            event_type_id INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            payload TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.commit()


def _insert_cloud_event(
    mac_normalized: str,
    event_type_id: int,
    event_type: str,
    payload: Optional[str],
) -> None:
    try:
        conn = sqlite3.connect(Config.get_recordings_db_path(), timeout=10)
        try:
            _ensure_cloud_events_table(conn)
            conn.execute(
                """
                INSERT INTO cloud_device_events
                (mac, event_type_id, event_type, payload, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    mac_normalized,
                    event_type_id,
                    event_type,
                    payload,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        logger.warning("cloud_device_events insert failed (non-fatal): %s", e)


def persist_cloud_device_event_async(
    mac_normalized: str,
    event_type_id: int,
    event_type: str,
    event_data: Optional[dict],
) -> None:
    payload = json.dumps(event_data) if event_data is not None else None
    if payload and len(payload) > 60000:
        payload = payload[:59980] + "... [TRUNCATED]"

    t = threading.Thread(
        target=_insert_cloud_event,
        args=(mac_normalized, event_type_id, event_type, payload),
        daemon=True,
    )
    t.start()


def list_cloud_events_for_mac(
    mac_normalized: str,
    limit: int = 100,
    event_types: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """
    mac_normalized: 12 hex uppercase. Matches rows stored as hex or AA:BB:…
    """
    if not mac_normalized or len(mac_normalized) != 12:
        return []
    limit = max(1, min(int(limit), 500))
    mac_compact = mac_normalized.upper()
    mac_colon = ":".join(mac_compact[i : i + 2] for i in range(0, 12, 2))
    et_filter: Optional[List[str]] = None
    if event_types:
        et_filter = [t.strip().lower() for t in event_types if t and str(t).strip()]
        if not et_filter:
            et_filter = None
    try:
        conn = sqlite3.connect(Config.get_recordings_db_path(), timeout=10)
        try:
            _ensure_cloud_events_table(conn)
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            type_filter = ""
            params: List[Any] = [mac_compact, mac_colon, mac_compact]
            if et_filter:
                placeholders = ",".join("?" * len(et_filter))
                type_filter = f" AND LOWER(event_type) IN ({placeholders})"
                params.extend(et_filter)
            params.append(limit)
            cur.execute(
                f"""
                SELECT id, mac, event_type_id, event_type, payload, created_at
                FROM cloud_device_events
                WHERE (mac = ? OR mac = ? OR REPLACE(UPPER(mac), ':', '') = ?)
                {type_filter}
                ORDER BY id DESC
                LIMIT ?
                """,
                params,
            )
            rows = cur.fetchall()
            out = []
            for r in rows:
                out.append(
                    {
                        "id": r["id"],
                        "mac": r["mac"],
                        "event_type": r["event_type"],
                        "event_type_id": r["event_type_id"],
                        "payload": json.loads(r["payload"])
                        if r["payload"]
                        else None,
                        "created_at": r["created_at"],
                    }
                )
            return out
        finally:
            conn.close()
    except Exception as e:
        logger.warning("list_cloud_events_for_mac failed: %s", e)
        return []
