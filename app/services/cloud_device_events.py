"""Persist cloud-style device lifecycle events (DEVICE_API JSON contract) to SQLite."""
import json
import logging
import queue
import threading
import time
import atexit
from config import Config
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from app.utils.sqlite_utils import connect_sqlite, log_slow_operation

logger = logging.getLogger(__name__)

_EVENT_QUEUE_MAX_SIZE = 1000
_EVENT_BATCH_SIZE = 50
_EVENT_BATCH_WAIT_SECONDS = 0.25
_event_queue = queue.Queue(maxsize=_EVENT_QUEUE_MAX_SIZE)
_writer_lock = threading.Lock()
_writer_thread = None
_writer_stop = threading.Event()

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


def _ensure_cloud_events_table(conn) -> None:
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


def _write_cloud_event_batch(events) -> None:
    started_at = time.perf_counter()
    try:
        conn = connect_sqlite(Config.get_recordings_db_path(), timeout=10)
        try:
            _ensure_cloud_events_table(conn)
            conn.executemany(
                """
                INSERT INTO cloud_device_events
                (mac, event_type_id, event_type, payload, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                events,
            )
            conn.commit()
        finally:
            conn.close()
        log_slow_operation(
            database="recordings",
            operation="insert_cloud_event_batch",
            started_at=started_at,
            rows=len(events),
        )
    except Exception as e:
        logger.warning(
            "cloud_device_events batch insert failed (non-fatal) count=%s: %s",
            len(events),
            e,
        )


def _cloud_event_writer() -> None:
    while not _writer_stop.is_set() or not _event_queue.empty():
        try:
            first_event = _event_queue.get(timeout=_EVENT_BATCH_WAIT_SECONDS)
        except queue.Empty:
            continue

        batch = [first_event]
        deadline = time.monotonic() + _EVENT_BATCH_WAIT_SECONDS
        while len(batch) < _EVENT_BATCH_SIZE:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                batch.append(_event_queue.get(timeout=remaining))
            except queue.Empty:
                break

        try:
            _write_cloud_event_batch(batch)
        finally:
            for _ in batch:
                _event_queue.task_done()


def _ensure_cloud_event_writer() -> None:
    global _writer_thread
    if _writer_thread is not None and _writer_thread.is_alive():
        return
    with _writer_lock:
        if _writer_thread is None or not _writer_thread.is_alive():
            _writer_stop.clear()
            _writer_thread = threading.Thread(
                target=_cloud_event_writer,
                name="CloudDeviceEventWriter",
                daemon=True,
            )
            _writer_thread.start()


def persist_cloud_device_event_async(
    mac_normalized: str,
    event_type_id: int,
    event_type: str,
    event_data: Optional[dict],
) -> None:
    payload = json.dumps(event_data) if event_data is not None else None
    if payload and len(payload) > 60000:
        payload = payload[:59980] + "... [TRUNCATED]"

    event = (
        mac_normalized,
        event_type_id,
        event_type,
        payload,
        datetime.now(timezone.utc).isoformat(),
    )
    _ensure_cloud_event_writer()
    try:
        _event_queue.put_nowait(event)
    except queue.Full:
        logger.warning(
            "cloud_device_events queue full; dropping event mac=%s type=%s",
            mac_normalized,
            event_type,
        )


def shutdown_cloud_event_writer(timeout: float = 2.0) -> None:
    """Drain queued events and stop the writer during a graceful shutdown."""
    global _writer_thread
    _writer_stop.set()
    thread = _writer_thread
    if thread is not None and thread.is_alive():
        thread.join(timeout=timeout)
    if thread is not None and not thread.is_alive():
        _writer_thread = None


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
        conn = connect_sqlite(Config.get_recordings_db_path(), timeout=10, row_factory=True)
        try:
            _ensure_cloud_events_table(conn)
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


atexit.register(shutdown_cloud_event_writer)
