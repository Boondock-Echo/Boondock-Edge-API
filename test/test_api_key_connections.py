"""API-key transactions release SQLite handles on success and rollback."""

import sqlite3

import pytest

from app.services import api_key_manager as module


def retain_connections(monkeypatch):
    connections = []
    original = module.connect_sqlite
    def connect(*args, **kwargs):
        connection = original(*args, **kwargs)
        connections.append(connection)
        return connection
    monkeypatch.setattr(module, 'connect_sqlite', connect)
    return connections


def assert_closed(connections):
    assert connections
    for connection in connections:
        with pytest.raises(sqlite3.ProgrammingError, match='closed'):
            connection.execute('SELECT 1')


def test_key_operations_close_connections_even_when_references_are_retained(initialized_settings_manager, monkeypatch):
    manager = module.APIKeyManager(db_path=initialized_settings_manager.db_path)
    connections = retain_connections(monkeypatch)
    # No creator exercises the default-owner read as well as the create/read paths.
    key, token = manager.create_key('Connection lifecycle')
    assert manager.get_key(key['id'])['id'] == key['id']
    assert manager.list_keys()[0]['id'] == key['id']
    assert initialized_settings_manager.get_credential(token) is not None
    assert manager.revoke_key(key['id'])
    assert initialized_settings_manager.get_credential(token) is None
    assert_closed(connections)


def test_failed_key_transaction_rolls_back_and_closes_connection(initialized_settings_manager, monkeypatch):
    manager = module.APIKeyManager(db_path=initialized_settings_manager.db_path)
    connections = retain_connections(monkeypatch)
    monkeypatch.setattr(module.secrets, 'token_urlsafe', lambda length: 'duplicate-token')
    first, token = manager.create_key('First key')
    with pytest.raises(sqlite3.IntegrityError):
        manager.create_key('Must roll back')
    assert [key['id'] for key in manager.list_keys()] == [first['id']]
    assert initialized_settings_manager.get_credential(token) is not None
    assert_closed(connections)
