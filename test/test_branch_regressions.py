import sqlite3
from datetime import datetime, timezone

from flask import Flask

from app.routes import channels_routes, transcription_routes
from app.services import settings_manager


class StubSettingsManager:
    def __init__(self):
        self.channel = {"id": 1, "name": "Channel 1"}

    def get_channel(self, channel_id):
        return self.channel if channel_id == 1 else None

    def get_all_channels(self):
        return [self.channel]

    def save_channel(self, channel):
        self.channel = channel


def test_channel_update_accepts_documented_threshold_range(monkeypatch):
    settings = StubSettingsManager()
    monkeypatch.setattr(channels_routes, "_settings_manager", settings)
    app = Flask(__name__)
    app.register_blueprint(channels_routes.channels_bp)

    response = app.test_client().put("/channel/1", json={"threshold": "50"})

    assert response.status_code == 200
    assert settings.channel["threshold"] == "50"


def test_channel_update_rejects_threshold_outside_documented_range(monkeypatch):
    settings = StubSettingsManager()
    monkeypatch.setattr(channels_routes, "_settings_manager", settings)
    app = Flask(__name__)
    app.register_blueprint(channels_routes.channels_bp)

    response = app.test_client().put("/channel/1", json={"threshold": "101"})

    assert response.status_code == 400
    assert response.get_json() == {"error": "Threshold must be between 0 and 100."}


def test_transcription_update_uses_resolved_recording_path(monkeypatch, tmp_path):
    recordings_dir = tmp_path / "recordings"
    recordings_dir.mkdir()
    recording = recordings_dir / "sample.wav"
    recording.write_bytes(b"original audio")

    database = tmp_path / "recordings.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE recordings (id INTEGER PRIMARY KEY, filename TEXT, transcription TEXT)"
        )
        connection.execute(
            "INSERT INTO recordings VALUES (?, ?, ?)",
            (1, "recordings/sample.wav", "old text"),
        )

    monkeypatch.setattr(transcription_routes, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(transcription_routes, "DB_PATH", database)
    monkeypatch.setattr(transcription_routes, "create_history_entry", lambda *args: None)
    app = Flask(__name__)
    app.register_blueprint(transcription_routes.transcription_bp)

    response = app.test_client().post(
        "/transcribe_save/1", data={"transcription": "corrected text"}
    )

    assert response.status_code == 200
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT transcription FROM recordings WHERE id = 1"
        ).fetchone() == ("corrected text",)


def test_new_settings_database_can_store_datetime(monkeypatch, tmp_path):
    database = tmp_path / "settings.db"
    monkeypatch.setattr(settings_manager, "SETTINGS_DB_PATH", database)
    monkeypatch.setattr(settings_manager.SettingsManager, "_instance", None)
    manager = settings_manager.SettingsManager()
    timestamp = datetime(2026, 8, 15, 12, 30, tzinfo=timezone.utc)

    assert manager.set_setting("last_run", timestamp)
    assert manager.get_setting("last_run") == timestamp

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT value, type FROM settings WHERE key = 'last_run'"
        ).fetchone() == (timestamp.isoformat(), "datetime")

