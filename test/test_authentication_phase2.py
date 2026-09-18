import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone

from flask import Flask, g, jsonify

from app.middleware.auth_middleware import require_admin, require_auth, require_permission
from app.services.api_key_manager import APIKeyManager
from app.services.settings_manager import SettingsManager
from app.utils import auth


def _manager(path):
    manager = object.__new__(SettingsManager)
    manager.db_path = path
    manager._initialized = True
    return manager


def _database(path):
    connection = sqlite3.connect(path)
    connection.executescript("""
        CREATE TABLE users (
            email TEXT PRIMARY KEY, name TEXT, password TEXT, role TEXT,
            status TEXT, access_level TEXT, groups TEXT
        );
        CREATE TABLE groups (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE,
            description TEXT, is_default INTEGER, permissions TEXT
        );
        CREATE TABLE credentials (
            id TEXT PRIMARY KEY, principal_type TEXT, principal_id TEXT,
            token_hash TEXT UNIQUE, created_at TEXT, expires_at TEXT
        );
        CREATE TABLE api_keys (
            id TEXT PRIMARY KEY, name TEXT, permissions TEXT, owner TEXT,
            created_at TEXT, created_by TEXT
        );
        CREATE TABLE channels (
            id INTEGER PRIMARY KEY, name TEXT, mac TEXT, deleted INTEGER DEFAULT 0
        );
    """)
    group_id = connection.execute(
        "INSERT INTO groups(name, is_default, permissions) VALUES ('Members', 1, ?)",
        (json.dumps(['channel.read']),),
    ).lastrowid
    connection.execute(
        "INSERT INTO users VALUES (?, ?, ?, ?, ?, ?, ?)",
        ('member@example.com', 'Member', 'secret', 'member', 'Active', None,
         json.dumps([group_id])),
    )
    connection.execute(
        "INSERT INTO users VALUES (?, ?, ?, ?, ?, ?, ?)",
        ('admin@example.com', 'Admin', 'secret', 'admin', 'Active', None, '[]'),
    )
    connection.execute(
        "INSERT INTO api_keys VALUES (?, ?, ?, ?, ?, ?)",
        ('key-1', 'Reader', json.dumps(['recording.create']),
         'user:admin@example.com', datetime.now(timezone.utc).isoformat(),
         'admin@example.com'),
    )
    connection.execute(
        "INSERT INTO channels VALUES (1, 'Radio', 'AABBCCDDEEFF', 0)"
    )
    connection.commit()
    connection.close()
    return group_id


def test_principal_retrieval_and_unified_authentication(tmp_path, monkeypatch):
    path = tmp_path / 'settings.db'
    group_id = _database(path)
    manager = _manager(path)
    expires = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    manager.issue_credential('user', 'member@example.com', expires, token='member-token')
    manager.issue_credential('user', 'admin@example.com', expires, token='admin-token')
    manager.issue_credential('api_key', 'key-1', expires, token='api-token')
    manager.issue_credential('device', '1', expires, token='device-token')
    monkeypatch.setattr(auth, '_settings_manager', manager)

    member = auth.authenticate_token('member-token')
    assert member['type'] == 'user'
    assert member['permissions'] == ['channel.read']
    assert member['owner_ids'] == ['user:member@example.com', f'group:{group_id}']
    assert 'password' not in member

    assert auth.authenticate_token('admin-token')['owner_ids'] is None
    api_key = auth.authenticate_token('api-token')
    assert api_key['permissions'] == ['recording.create']
    assert api_key['owner_ids'] is None
    device = auth.authenticate_token('device-token')
    assert device['permissions'] == ['device']
    assert device['mac'] == 'AABBCCDDEEFF'
    assert auth.authenticate_token('missing') is None


def test_decorators_use_flat_principal_any_permission_and_admin_bypass(monkeypatch):
    app = Flask(__name__)
    principals = {
        'member': {'type': 'user', 'email': 'm@example.com', 'role': 'member',
                   'permissions': ['channel.read']},
        'admin': {'type': 'user', 'email': 'a@example.com', 'role': 'admin',
                  'permissions': []},
        'key': {'type': 'api_key', 'id': 'key-1', 'permissions': ['recording.create']},
    }
    monkeypatch.setattr(
        'app.utils.auth.authenticate_token',
        lambda token: principals.get(token),
    )

    @app.get('/auth')
    @require_auth
    def authenticated():
        return jsonify(g.principal)

    @app.get('/admin')
    @require_admin
    def administrator():
        return jsonify(ok=True)

    @app.get('/resource/<resource_id>')
    @require_permission(
        ['device', 'recording.create'],
        loader=lambda principal, resource_id: {'id': resource_id, 'type': principal['type']},
        id_argument='resource_id', inject_as='resource',
    )
    def resource(resource_id, resource):
        return jsonify(resource)

    client = app.test_client()
    assert client.get('/auth').status_code == 401
    assert client.get('/auth', headers={'X-API-Key': 'key'}).json['type'] == 'api_key'
    assert client.get('/admin', headers={'Authorization': 'Bearer member'}).status_code == 403
    assert client.get('/admin', headers={'Authorization': 'Bearer admin'}).status_code == 200
    assert client.get('/resource/7', headers={'Authorization': 'Bearer key'}).json == {
        'id': '7', 'type': 'api_key'
    }
    assert client.get('/resource/7', headers={'Authorization': 'Bearer member'}).status_code == 403
    assert client.get('/resource/7', headers={'Authorization': 'Bearer admin'}).status_code == 200
    assert authenticated.access_policy == {'type': 'auth'}
    assert administrator.access_policy == {'type': 'admin'}


def test_api_key_issuer_writes_principal_and_shared_credential(tmp_path):
    path = tmp_path / 'settings.db'
    _database(path)
    manager = APIKeyManager(db_path=path)
    manager.settings = _manager(path)

    metadata, token = manager.create_key(
        'Integration', scopes=['recording.create'], created_by='admin@example.com'
    )

    assert token.startswith('bk_live_')
    assert metadata['permissions'] == ['recording.create']
    assert metadata['owner'] == 'user:admin@example.com'
    credential = manager.settings.get_credential(token)
    assert credential['principal_type'] == 'api_key'
    assert credential['principal_id'] == metadata['id']
    assert manager.revoke_key(metadata['id']) is True
    assert manager.settings.get_credential(token) is None


def test_device_authentication_logs_plaintext_success_and_expiry(
    tmp_path, monkeypatch, caplog
):
    path = tmp_path / 'settings.db'
    _database(path)
    manager = _manager(path)
    manager.issue_credential(
        'device', '1',
        (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        token='current-device-token',
    )
    manager.issue_credential(
        'device', '1',
        (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
        token='expired-device-token',
    )
    monkeypatch.setattr(auth, '_settings_manager', manager)

    with caplog.at_level(logging.INFO, logger=auth.__name__):
        assert auth.authenticate_token('current-device-token')['type'] == 'device'
        assert auth.authenticate_token('expired-device-token') is None

    messages = [record.getMessage() for record in caplog.records]
    assert any(
        'action=used' in message
        and 'token=current-device-token' in message
        and 'result=success' in message
        for message in messages
    )
    assert any(
        'action=used' in message
        and 'token=expired-device-token' in message
        and 'result=expired' in message
        for message in messages
    )
