"""Action repository operations and migration from the former settings list."""

import pytest

from app.services import keyword_actions as service
from migrations.keyword_actions import upgrade_keyword_actions_schema

def upgrade(path):
    upgrade_keyword_actions_schema(settings_path=path)


def action(action_id='legacy-action'):
    return {
        'id': action_id, 'name': 'Fire alert', 'kind': 'popup', 'autoplay': False,
    }


def test_migration_preserves_ids_configuration_and_newer_rows(initialized_settings_manager):
    manager = initialized_settings_manager
    original = action()
    manager.set_setting('keyword_actions', [original])
    upgrade(manager.db_path)
    assert manager.get_keyword_action(original['id']) == original
    assert manager.get_setting('keyword_actions') is None

    updated = {**original, 'name': 'Updated alert'}
    assert manager.save_keyword_action(updated)
    # A retry with a surviving legacy export must not overwrite newer changes.
    manager.set_setting('keyword_actions', [original])
    upgrade(manager.db_path)
    upgrade(manager.db_path)
    assert manager.get_keyword_actions() == [updated]
    assert manager.get_setting('keyword_actions') is None


def test_migration_rolls_back_instead_of_losing_legacy_data(initialized_settings_manager):
    manager = initialized_settings_manager
    legacy = [action('valid-first'), {'id': 'missing-fields'}]
    manager.set_setting('keyword_actions', legacy)
    with pytest.raises(KeyError):
        upgrade(manager.db_path)
    assert manager.get_setting('keyword_actions') == legacy
    assert manager.get_keyword_actions() == []


def test_repository_targets_one_action_and_filters_lists(initialized_settings_manager):
    manager = initialized_settings_manager
    first = action('first')
    second = {**action('second'), 'kind': 'email'}
    assert manager.save_keyword_action(first, create=True)
    assert manager.save_keyword_action(second, create=True)
    assert manager.save_keyword_action({**first, 'name': 'Changed'})
    assert manager.get_keyword_action('second') == second
    assert [item['id'] for item in manager.get_keyword_actions(kind='popup')] == ['first']
    assert manager.delete_keyword_action('first')
    assert manager.get_keyword_action('first') is None
    assert manager.get_keyword_action('second') == second
    assert not manager.save_keyword_action(first)  # Updating a deleted ID cannot resurrect it.
    assert not manager.delete_keyword_action('first')


def test_save_and_delete_do_not_load_the_action_collection(initialized_settings_manager, monkeypatch):
    manager = initialized_settings_manager
    group_id = manager.save_group({
        'name': 'Dispatch', 'permissions': [], 'keywords': [{'pattern': 'fire'}],
    })
    group = manager.get_group_by_id(group_id)
    created = service.save_action({
        'name': 'Alert', 'kind': 'popup',
    })

    def no_collection_read(**kwargs):
        pytest.fail('A single-action mutation loaded the full collection')

    monkeypatch.setattr(manager, 'get_keyword_actions', no_collection_read)
    updated = service.save_action({'name': 'Renamed'}, created['id'])
    assert updated['name'] == 'Renamed'
    service.delete_action(created['id'])
    assert manager.get_keyword_action(created['id']) is None


def test_legacy_group_rules_become_keyword_assignments_and_sender_moves_to_action(initialized_settings_manager):
    import sqlite3
    manager = initialized_settings_manager
    group_id = manager.save_group({'name': 'Dispatch', 'permissions': [], 'keywords': [{'pattern': 'fire'}]})
    keyword_id = manager.get_group_by_id(group_id)['keywords'][0]
    with sqlite3.connect(manager.db_path) as db:
        db.execute('DROP TABLE keyword_action_links')
        db.execute('DROP TABLE keyword_actions')
        db.execute('''CREATE TABLE keyword_actions (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, group_id INTEGER NOT NULL,
            kind TEXT NOT NULL, enabled BOOLEAN NOT NULL, config JSON NOT NULL)''')
    original = {**action('kept-id'), 'kind': 'email', 'group_id': group_id,
                'enabled': True, 'keyword_ids': [keyword_id], 'to': ['to@example.test']}
    manager.set_setting('keyword_actions', [original, {**original, 'id': 'previously-disabled', 'enabled': False}])
    manager.set_setting('keyword_email', {'host': 'smtp.example.test', 'enabled': True,
        'from_email': 'old-sender@example.test', 'from_name': 'Dispatch', 'reply_to': 'reply@example.test'})
    upgrade(manager.db_path)
    upgrade(manager.db_path)
    migrated = manager.get_keyword_action('kept-id')
    assert migrated['from_email'] == 'old-sender@example.test'
    assert migrated['reply_to'] == 'reply@example.test'
    assert not {'group_id', 'enabled', 'keyword_ids'}.intersection(migrated)
    assert manager.get_group_by_id(group_id)['keyword_details'][0]['action_ids'] == ['kept-id']
    assert not {'from_email', 'from_name', 'reply_to', 'enabled'}.intersection(manager.get_setting('keyword_email'))
