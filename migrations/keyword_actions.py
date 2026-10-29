"""Upgrade keyword matches, reusable actions, assignments, and delivery storage.

Legacy settings lists and group-scoped action rows convert directly to the
current schema. Each database transaction is independently safe to retry.
Fresh-install schema creation stays in the database initializers.
"""

import json
from pathlib import Path

from app.services.keyword_action_schema import KEYWORD_ACTION_SCHEMA, KEYWORD_ACTION_SETTINGS_SCHEMA
from app.utils.sqlite_utils import connect_sqlite


def _upgrade_settings(settings_path):
    if settings_path is None or not Path(settings_path).exists():
        return
    db = connect_sqlite(settings_path, row_factory=True)
    try:
        db.execute('BEGIN IMMEDIATE')
        email_row = db.execute("SELECT value FROM settings WHERE key='keyword_email'").fetchone()
        routing = json.loads(email_row['value']) if email_row else {}
        columns = {row[1] for row in db.execute('PRAGMA table_info(keyword_actions)')}
        legacy = []
        if 'group_id' in columns:
            for row in db.execute('SELECT * FROM keyword_actions'):
                legacy.append({**json.loads(row['config']), **{key: row[key] for key in ('id', 'name', 'group_id', 'kind', 'enabled')}})
            db.execute('DROP TABLE keyword_actions')
        setting = db.execute("SELECT value FROM settings WHERE key='keyword_actions'").fetchone()
        if setting:
            values = json.loads(setting['value'])
            if not isinstance(values, list):
                raise ValueError('Legacy keyword_actions must be an array')
            legacy.extend(values)
        for statement in KEYWORD_ACTION_SETTINGS_SCHEMA:
            db.execute(statement)
        for action in legacy:
            config = {key: value for key, value in action.items() if key not in ('id', 'name', 'kind', 'group_id', 'enabled', 'keyword_ids')}
            if action['kind'] == 'email':
                for field, default in [('from_email', ''), ('from_name', 'Boondock'), ('reply_to', '')]:
                    config.setdefault(field, routing.get(field, default))
            cursor = db.execute(
                '''INSERT INTO keyword_actions(id,name,kind,config) VALUES(?,?,?,?)
                   ON CONFLICT(id) DO NOTHING''', (action['id'], action['name'], action['kind'], json.dumps(config)),
            )
            if cursor.rowcount and action.get('enabled'):
                for keyword_id in action.get('keyword_ids', []):
                    db.execute(
                        '''INSERT OR IGNORE INTO keyword_action_links(keyword_id,action_id)
                           SELECT ?,? WHERE EXISTS(SELECT 1 FROM keywords WHERE id=?)''',
                        (keyword_id, action['id'], keyword_id),
                    )
        db.execute("DELETE FROM settings WHERE key='keyword_actions'")
        if email_row:
            for key in ('enabled', 'from_email', 'from_name', 'reply_to'):
                routing.pop(key, None)
            db.execute("UPDATE settings SET value=?,type='json' WHERE key='keyword_email'", (json.dumps(routing),))
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _upgrade_recordings(recordings_path):
    if recordings_path is None or not Path(recordings_path).exists():
        return
    db = connect_sqlite(recordings_path)
    try:
        db.execute('BEGIN IMMEDIATE')
        columns = {row[1] for row in db.execute('PRAGMA table_info(recordings)')}
        if not columns:
            raise RuntimeError('Cannot upgrade keyword actions: recordings table is missing')
        if 'matched_keywords' not in columns:
            db.execute("ALTER TABLE recordings ADD COLUMN matched_keywords JSON NOT NULL DEFAULT '[]'")
        for statement in KEYWORD_ACTION_SCHEMA:
            db.execute(statement)
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def upgrade_keyword_actions_schema(settings_path=None, recordings_path=None):
    """Upgrade either database, or both, without rewriting historical matches."""
    _upgrade_settings(settings_path)
    _upgrade_recordings(recordings_path)


if __name__ == '__main__':
    from config import Config
    upgrade_keyword_actions_schema(Config.get_settings_db_path(), Config.get_recordings_db_path())
