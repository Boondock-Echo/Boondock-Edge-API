import json
import hashlib
import sqlite3
from datetime import datetime, timedelta, timezone

from app.services.settings_manager import SettingsManager
from migrations.authorization import upgrade_authorization_schema


def _manager_for_database(path):
    manager = object.__new__(SettingsManager)
    manager.db_path = path
    manager._initialized = True
    return manager


def _base_database(path, legacy=False):
    connection = sqlite3.connect(path)
    connection.executescript("""
        CREATE TABLE users (
            email TEXT PRIMARY KEY, name TEXT NOT NULL, password TEXT NOT NULL,
            role TEXT, status TEXT, profile TEXT, access_level TEXT,
            mfa_enabled INTEGER DEFAULT 0, created_at TEXT,
            login_history TEXT, devices TEXT
        );
        CREATE TABLE channels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            mac TEXT UNIQUE NOT NULL
        );
    """)
    if legacy:
        connection.executescript("""
            CREATE TABLE profiles (
                name TEXT PRIMARY KEY, description TEXT,
                is_default INTEGER, features TEXT
            );
            CREATE TABLE tokens (
                token TEXT PRIMARY KEY, email TEXT, role TEXT,
                mac_address TEXT, created_at TEXT, expires_at TEXT,
                last_activity TEXT
            );
            CREATE TABLE api_keys (
                id TEXT PRIMARY KEY, name TEXT, key_prefix TEXT,
                key_hash TEXT UNIQUE, scopes TEXT, created_at TEXT,
                created_by TEXT, expires_at TEXT, last_used_at TEXT,
                revoked INTEGER DEFAULT 0
            );
        """)
    connection.commit()
    connection.close()


def _legacy_database(path):
    _base_database(path, legacy=True)
    connection = sqlite3.connect(path)
    connection.execute(
        "INSERT INTO profiles VALUES ('Default', 'Members', 1, ?)",
        (json.dumps({"channel.read": True, "channel.update": False}),),
    )
    connection.execute(
        "INSERT INTO profiles VALUES ('Admin', 'Legacy admin profile', 1, '{}')"
    )
    connection.execute(
        "INSERT INTO users VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, '[]', '[]')",
        ("member@example.com", "Member", "hash", "member", "Active", "Default",
         None, datetime.now(timezone.utc).isoformat()),
    )
    connection.execute("INSERT INTO channels(name, mac) VALUES ('Radio', 'AABBCCDDEEFF')")
    expires = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    connection.execute(
        "INSERT INTO api_keys VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, 0)",
        ("key-1", "Test", "bk_live_", hashlib.sha256(b"legacy-api-token").hexdigest(),
         json.dumps(["transcriptions:read"]), datetime.now(timezone.utc).isoformat(),
         "member@example.com", expires),
    )
    connection.execute(
        "INSERT INTO tokens VALUES ('keep-until-route-cutover', NULL, NULL, NULL, '', ?, '')",
        (expires,),
    )
    connection.commit()
    connection.close()


def test_new_install_creates_complete_authorization_schema(monkeypatch, tmp_path):
    from app.services import db_initializer
    from app.services import settings_manager as settings_module

    database = tmp_path / "settings.db"
    monkeypatch.setattr(settings_module, "SETTINGS_DB_PATH", database)
    monkeypatch.setattr(db_initializer.Config, "get_settings_db_path", lambda: database)
    db_initializer._create_database_schema()
    settings_module.SettingsManager._instance = None
    settings_module.SettingsManager()

    connection = sqlite3.connect(database)
    tables = {
        row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    user_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(users)")
    }
    api_key_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(api_keys)")
    }
    group_id_column = connection.execute(
        "PRAGMA table_info(groups)"
    ).fetchone()
    migration_count = connection.execute(
        "SELECT COUNT(*) FROM schema_migrations"
    ).fetchone()[0]
    connection.close()
    settings_module.SettingsManager._instance = None

    assert {"credentials", "groups", "channel_owners", "api_keys"}.issubset(tables)
    assert "profiles" not in tables
    assert "tokens" not in tables
    assert "groups" in user_columns
    assert "profile" not in user_columns
    assert api_key_columns == {
        "id", "name", "permissions", "owner", "created_at", "created_by"
    }
    assert group_id_column[1:3] == ("id", "INTEGER")
    assert migration_count == 1


def test_upgrade_is_idempotent_and_completes_authorization_migration(tmp_path):
    database = tmp_path / "settings.db"
    _legacy_database(database)

    assert upgrade_authorization_schema(database) is True
    assert upgrade_authorization_schema(database) is False

    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    default_group_id = connection.execute(
        "SELECT id FROM groups WHERE name='Default'"
    ).fetchone()[0]
    tables = {
        row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert "api_keys" in tables
    assert "profiles" not in tables
    assert "tokens" not in tables
    assert "profile" not in {
        row[1] for row in connection.execute("PRAGMA table_info(users)")
    }
    assert json.loads(connection.execute(
        "SELECT groups FROM users WHERE email='member@example.com'"
    ).fetchone()[0]) == [default_group_id]
    assert connection.execute(
        "SELECT token_hash FROM credentials WHERE principal_type='api_key'"
    ).fetchone()[0] == hashlib.sha256(b"legacy-api-token").hexdigest()
    api_key = connection.execute(
        "SELECT permissions, owner FROM api_keys WHERE id='key-1'"
    ).fetchone()
    assert json.loads(api_key["permissions"]) == ["transcriptions.read"]
    assert api_key["owner"] == f"group:{default_group_id}"
    api_key_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(api_keys)")
    }
    assert api_key_columns == {
        "id", "name", "permissions", "owner", "created_at", "created_by"
    }
    assert tuple(connection.execute(
        "SELECT owner_type, owner_id FROM channel_owners"
    ).fetchone()) == ("group", str(default_group_id))
    connection.close()


def test_credential_repository_hashes_and_resolves_credentials(tmp_path):
    database = tmp_path / "settings.db"
    _base_database(database, legacy=True)
    upgrade_authorization_schema(database)
    repository = _manager_for_database(database)
    expires_at = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()

    raw_token, credential_id = repository.issue_credential(
        "user", "member@example.com", expires_at
    )
    credential = repository.get_credential(raw_token)

    assert credential["id"] == credential_id
    assert credential["principal_type"] == "user"
    assert credential["principal_id"] == "member@example.com"
    connection = sqlite3.connect(database)
    persisted = connection.execute(
        "SELECT token_hash FROM credentials WHERE id=?", (credential_id,)
    ).fetchone()[0]
    connection.close()
    assert persisted == hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    assert persisted != raw_token


def test_credential_repository_reports_missing_and_expired(tmp_path):
    database = tmp_path / "settings.db"
    _base_database(database, legacy=True)
    upgrade_authorization_schema(database)
    repository = _manager_for_database(database)
    expired_at = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    expired_token, _ = repository.issue_credential("device", "1", expired_at)

    assert repository.inspect_credential("") == (None, "missing")
    assert repository.inspect_credential("unknown") == (None, "not_found")
    assert repository.inspect_credential(expired_token) == (None, "expired")
    assert repository.delete_expired_credentials() == 1


def test_new_channel_receives_default_group_owner(monkeypatch, tmp_path):
    from app.services import db_initializer
    from app.services import settings_manager as settings_module

    database = tmp_path / "settings.db"
    monkeypatch.setattr(settings_module, "SETTINGS_DB_PATH", database)
    monkeypatch.setattr(db_initializer.Config, "get_settings_db_path", lambda: database)
    db_initializer._create_database_schema()
    settings_module.SettingsManager._instance = None
    manager = settings_module.SettingsManager()
    default_group_id = manager.save_group({
        "name": "Default",
        "description": "Default user group",
        "is_default": True,
        "permissions": [],
    })

    channel_id = manager.save_channel({"name": "New radio", "mac": "112233445566"})

    connection = sqlite3.connect(database)
    owner = connection.execute(
        """SELECT owner_type, owner_id FROM channel_owners
           WHERE channel_id=?""",
        (channel_id,),
    ).fetchone()
    connection.close()
    settings_module.SettingsManager._instance = None
    assert owner == ("group", str(default_group_id))
