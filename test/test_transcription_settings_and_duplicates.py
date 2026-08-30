import pytest

from app.services import transcription_service
from app.utils import crc_utils


def test_duplicate_identity_uses_filename_size_and_channel(monkeypatch, tmp_path):
    monkeypatch.setattr(crc_utils, 'DUPLICATE_CACHE_FILE', tmp_path / 'duplicates.json')
    monkeypatch.setattr(crc_utils, 'check_database_for_file', lambda *args: {'found': False})

    first = crc_utils.check_and_update_duplicate_cache(b'abc', 1, 'dispatch.wav')
    duplicate = crc_utils.check_and_update_duplicate_cache(b'xyz', 1, 'dispatch.wav')
    different_size = crc_utils.check_and_update_duplicate_cache(b'abcd', 1, 'dispatch.wav')
    different_name = crc_utils.check_and_update_duplicate_cache(b'xyz', 1, 'other.wav')
    different_channel = crc_utils.check_and_update_duplicate_cache(b'xyz', 2, 'dispatch.wav')

    assert first['is_duplicate'] is False
    assert duplicate['is_duplicate'] is True
    assert different_size['is_duplicate'] is False
    assert different_name['is_duplicate'] is False
    assert different_channel['is_duplicate'] is False
    assert duplicate['crc'] is None


def test_cloud_transcription_uses_supplied_settings_key(monkeypatch, tmp_path):
    audio = tmp_path / 'audio.wav'
    audio.write_bytes(b'audio')
    captured = {}

    class Response:
        status_code = 200
        text = '{"text": "done"}'
        def raise_for_status(self): pass
        def json(self): return {'text': 'done'}

    def post(url, **kwargs):
        captured.update(kwargs)
        return Response()

    settings = type('Settings', (), {'get_setting': lambda self, key, default: 'dashboard-key'})()
    monkeypatch.setattr(transcription_service, 'get_settings_manager', lambda: settings)
    monkeypatch.setattr(transcription_service.requests, 'post', post)
    service = transcription_service.TranscriptionService.__new__(transcription_service.TranscriptionService)
    result = service._transcribe_boondock_api(audio)

    assert result == 'done'
    assert captured['headers']['X-Boondock-Key'] == 'dashboard-key'
    assert captured['timeout'] == 60
    assert captured['data'] == {'model_id': 'scribe_v1'}


def test_cloud_transcription_rejects_missing_settings_key(monkeypatch, tmp_path):
    audio = tmp_path / 'audio.wav'
    audio.write_bytes(b'audio')
    settings = type('Settings', (), {'get_setting': lambda self, key, default: ''})()
    monkeypatch.setattr(transcription_service, 'get_settings_manager', lambda: settings)

    with audio.open('rb') as audio_file, pytest.raises(
        ValueError, match='Missing Boondock Transcription API Key'
    ):
        transcription_service.request_openai_transcription(audio_file, audio.name)
