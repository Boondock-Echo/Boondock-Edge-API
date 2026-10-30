"""Manual transcription must honor saved provider settings on both endpoints."""

import io
from pathlib import Path
import sqlite3
from types import SimpleNamespace

from flask import Flask
import pytest

from app.routes import transcription_routes
from app.services import recordings_db_initializer, transcription_service
from app.services.settings_manager import get_settings_manager


@pytest.fixture
def manual_client(monkeypatch, tmp_path, admin_auth):
    recordings_dir = tmp_path / 'recordings'
    recordings_dir.mkdir()
    (recordings_dir / 'sample.wav').write_bytes(b'audio')
    database = tmp_path / 'recordings.db'
    monkeypatch.setattr(recordings_db_initializer, 'DB_PATH', database)
    recordings_db_initializer.initialize_db()
    with sqlite3.connect(database) as conn:
        conn.execute(
            'INSERT INTO recordings (id, filename, transcription) VALUES (?, ?, ?)',
            (1, 'recordings/sample.wav', 'original text'),
        )
    monkeypatch.setattr(transcription_routes, 'DATA_ROOT', tmp_path)
    monkeypatch.setattr(transcription_routes, 'DB_PATH', database)
    service = transcription_service.TranscriptionService(model_name='old-model')
    monkeypatch.setattr(
        transcription_routes, 'get_audio_handler',
        lambda: SimpleNamespace(transcription_service=service),
    )
    app = Flask(__name__)
    app.register_blueprint(transcription_routes.transcription_bp, url_prefix='/api')
    yield app.test_client(), service, database, admin_auth
    service.shutdown()


def transcribe(client, endpoint, headers):
    data = {'file': (io.BytesIO(b'audio'), 'sample.wav')} if endpoint == '/api/transcribe' else {}
    return client.post(endpoint, data=data, headers=headers)


@pytest.mark.parametrize('endpoint', ['/api/transcribe', '/api/transcribe/1'])
@pytest.mark.parametrize('text', ['local transcript', ''])
def test_manual_local_uses_current_settings_without_cloud_key(
    manual_client, monkeypatch, endpoint, text
):
    client, service, database, headers = manual_client
    settings = get_settings_manager()
    settings.set_all_settings({
        'method': 'local', 'model': 'medium', 'target_language': 'spanish',
        'transcription_api_key': '', 'transcription_queue_enabled': False,
    })
    paths = []

    def local(path, **kwargs):
        paths.append(Path(path))
        assert Path(path).read_bytes() == b'audio'
        assert service.model_name == 'medium'
        assert kwargs['language'] == 'es'
        return text

    def cloud(*args, **kwargs):
        pytest.fail('Local transcription must never call the cloud API')

    monkeypatch.setattr(service, '_transcribe_local', local)
    monkeypatch.setattr(transcription_service.requests, 'post', cloud)
    response = transcribe(client, endpoint, headers)
    assert response.status_code == 200, response.get_json()
    assert response.get_json()['transcription'] == text
    assert response.get_json()['status'] == 'success'
    assert response.headers['Access-Control-Allow-Origin'] == '*'
    assert len(paths) == 1
    if endpoint == '/api/transcribe':
        assert not paths[0].exists()
    with sqlite3.connect(database) as conn:
        assert conn.execute('SELECT transcription FROM recordings WHERE id=1').fetchone()[0] == (
            text if endpoint.endswith('/1') else 'original text'
        )


@pytest.mark.parametrize('endpoint', ['/api/transcribe', '/api/transcribe/1'])
def test_manual_cloud_uses_saved_key_and_provider_changes(manual_client, monkeypatch, endpoint):
    client, service, _, headers = manual_client
    settings = get_settings_manager()
    settings.set_all_settings({'method': 'openai', 'transcription_api_key': 'saved-key'})
    calls = []

    def post(url, **kwargs):
        calls.append(url)
        assert kwargs['headers']['X-Boondock-Key'] == 'saved-key'
        assert kwargs['files']['audio_file'][1].read() == b'audio'
        return SimpleNamespace(
            status_code=200, text='{"text":"cloud transcript"}',
            raise_for_status=lambda: None, json=lambda: {'text': 'cloud transcript'},
        )

    monkeypatch.setattr(transcription_service.requests, 'post', post)
    response = transcribe(client, endpoint, headers)
    assert response.status_code == 200, response.get_json()
    assert response.get_json()['transcription'] == 'cloud transcript'
    assert len(calls) == 1

    # A settings edit takes effect on the next request using the same service.
    settings.set_all_settings({'method': 'local', 'model': 'base.en', 'target_language': 'english'})
    monkeypatch.setattr(service, '_transcribe_local', lambda *args, **kwargs: 'now local')
    response = transcribe(client, endpoint, headers)
    assert response.status_code == 200, response.get_json()
    assert response.get_json()['transcription'] == 'now local'
    assert service.model_name == 'base.en'
    assert len(calls) == 1


@pytest.mark.parametrize('endpoint', ['/api/transcribe', '/api/transcribe/1'])
@pytest.mark.parametrize('error, status', [(RuntimeError('local failed'), 500), (TimeoutError(), 504)])
def test_manual_failure_cleans_upload_and_preserves_recording(
    manual_client, monkeypatch, endpoint, error, status
):
    client, service, database, headers = manual_client
    get_settings_manager().set_setting('method', 'local')
    paths = []

    def fail(path, **kwargs):
        paths.append(Path(path))
        raise error

    monkeypatch.setattr(service, '_transcribe_local', fail)
    response = transcribe(client, endpoint, headers)
    assert response.status_code == status
    assert 'error' in response.get_json()
    if endpoint == '/api/transcribe':
        assert not paths[0].exists()
    with sqlite3.connect(database) as conn:
        assert conn.execute('SELECT transcription FROM recordings WHERE id=1').fetchone()[0] == 'original text'


def test_manual_upload_requires_file_and_auth(manual_client):
    client, _, _, headers = manual_client
    assert client.post('/api/transcribe', headers=headers).status_code == 400
    assert client.post('/api/transcribe').status_code == 401
