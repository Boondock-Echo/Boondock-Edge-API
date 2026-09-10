import logging
import time

from app.services import cloud_device_events
from app.utils import sqlite_utils


def test_shared_sqlite_connection_enables_wal_and_foreign_keys(tmp_path):
    database = tmp_path / "test.db"

    with sqlite_utils.connect_sqlite(database, row_factory=True) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA synchronous").fetchone()[0] == 1
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5000


def test_slow_operation_logging_is_thresholded(monkeypatch, caplog):
    monkeypatch.setenv("SQLITE_SLOW_OPERATION_MS", "1000")
    with caplog.at_level(logging.WARNING):
        sqlite_utils.log_slow_operation(
            database="settings",
            operation="fast_test",
            started_at=time.perf_counter(),
        )
    assert "fast_test" not in caplog.text

    monkeypatch.setenv("SQLITE_SLOW_OPERATION_MS", "0")
    with caplog.at_level(logging.WARNING):
        sqlite_utils.log_slow_operation(
            database="settings",
            operation="slow_test",
            started_at=time.perf_counter(),
            lock_wait_ms=12.5,
            rows=3,
        )
    assert "sqlite_slow_operation database=settings operation=slow_test" in caplog.text
    assert "lock_wait_ms=12.50 rows=3" in caplog.text


def test_cloud_events_use_one_batched_writer(monkeypatch):
    batches = []
    monkeypatch.setattr(cloud_device_events, "_write_cloud_event_batch", batches.append)

    cloud_device_events.persist_cloud_device_event_async(
        "AABBCCDDEEFF", 11, "ping", {"sequence": 1}
    )
    cloud_device_events.persist_cloud_device_event_async(
        "AABBCCDDEEFF", 11, "ping", {"sequence": 2}
    )

    deadline = time.monotonic() + 2
    while not batches and time.monotonic() < deadline:
        time.sleep(0.01)
    cloud_device_events.shutdown_cloud_event_writer()

    assert len(batches) == 1
    assert len(batches[0]) == 2
    assert batches[0][0][:4] == (
        "AABBCCDDEEFF",
        11,
        "ping",
        '{"sequence": 1}',
    )


def test_cloud_event_batch_is_persisted_and_listed(monkeypatch, tmp_path):
    database = tmp_path / "recordings.db"
    monkeypatch.setattr(
        cloud_device_events.Config,
        "get_recordings_db_path",
        staticmethod(lambda: database),
    )
    cloud_device_events._write_cloud_event_batch(
        [
            (
                "AABBCCDDEEFF",
                11,
                "ping",
                '{"sequence": 1}',
                "2026-09-07T10:00:00+00:00",
            ),
            (
                "AABBCCDDEEFF",
                9,
                "status",
                None,
                "2026-09-07T10:00:01+00:00",
            ),
        ]
    )

    events = cloud_device_events.list_cloud_events_for_mac("AABBCCDDEEFF")

    assert [event["event_type"] for event in events] == ["status", "ping"]
    assert events[1]["payload"] == {"sequence": 1}
