"""Persisted keyword matches travel with recordings, including update polls."""

import json
import sqlite3

from app.services import audio_handler
from app.services.recording_state import match_keywords, update_transcription


def test_matching_ranges_counts_and_options():
    keywords = [
        {'id': 1, 'pattern': 'fire', 'match_type': 'literal'},
        {'id': 2, 'pattern': r'Car\s+\d+', 'match_type': 'regex', 'case_sensitive': True},
        {'id': 3, 'pattern': 'a.b', 'match_type': 'literal'},
        {'id': 4, 'pattern': '[', 'match_type': 'regex'},
        {'id': 5, 'pattern': r'\b', 'match_type': 'regex'},
    ]
    matches = match_keywords('🚒 FIRE fire Car 12 car 13 a.b axb', keywords)
    assert [keyword['id'] for keyword in matches] == [1, 2, 3]
    assert matches[0]['count'] == 2
    assert matches[0]['ranges'] == [{'start': 3, 'end': 7}, {'start': 8, 'end': 12}]
    assert matches[1]['count'] == 1
    assert matches[2]['count'] == 1


def test_recording_insert_edit_and_inbox_use_stored_matches(
    recording_store, initialized_settings_manager, monkeypatch, tmp_path
):
    from flask import Flask
    from app.services.channel_state import load_inbox

    database, _ = recording_store
    manager = initialized_settings_manager
    group_id = manager.save_group({
        'name': 'Dispatch', 'permissions': ['recording.read'],
        'keywords': [{'pattern': 'fire'}, {'pattern': r'Car\s+\d+', 'match_type': 'regex'}],
    })
    group = manager.get_group_by_id(group_id)
    keyword = group['keyword_details'][0]
    monkeypatch.setattr(audio_handler, '_get_db_path', lambda: database)
    channel = audio_handler.AudioChannel(1, tmp_path / 'audio')
    assert channel.save_recording('recordings/one.wav', 1000, 'FIRE fire Car 42')

    with sqlite3.connect(database) as connection:
        recording_id, stored, updated = connection.execute(
            'SELECT id, matched_keywords, updated_at FROM recordings'
        ).fetchone()
    assert len(json.loads(stored)) == 2

    principal = {'type': 'user', 'role': 'admin', 'owner_ids': None,
                 'keywords': [keyword], 'preferences': {}}
    app = Flask(__name__)
    with app.test_request_context('/?limit=20'):
        initial, status = load_inbox(principal)
    assert status == 200
    matches = initial['messages'][0]['matched_keywords']
    assert len(matches) == 1  # Another group's keyword must not leak.
    assert matches[0]['id'] == keyword['id']
    assert matches[0]['count'] == 2

    # Adding a keyword must not retroactively classify already saved recordings.
    manager.save_group({**group, 'keywords': [*group['keywords'], {'pattern': 'FIRE fire'}]}, group_id)
    principal['keywords'] = manager.get_group_by_id(group_id)['keyword_details']
    with app.test_request_context('/?limit=20'):
        initial, _ = load_inbox(principal)
    assert len(initial['messages'][0]['matched_keywords']) == 2

    # A new transcript replaces old matches and crosses the poll boundary.
    with sqlite3.connect(database) as connection:
        assert update_transcription(connection, recording_id, 'Car 99')
    with app.test_request_context(f'/?updated_since={updated}&limit=20'):
        polled, status = load_inbox(principal)
    assert status == 200
    assert [match['pattern'] for match in polled['messages'][0]['matched_keywords']] == [r'Car\s+\d+']
    assert polled['messages'][0]['updated_at'] > updated

    assert channel.save_recording('recordings/one.wav', 1000, 'ordinary traffic')
    with sqlite3.connect(database) as connection:
        assert json.loads(connection.execute('SELECT matched_keywords FROM recordings').fetchone()[0]) == []


def test_existing_recordings_schema_upgrade_is_idempotent(tmp_path):
    from migrations.keyword_actions import upgrade_keyword_actions_schema

    database = tmp_path / 'old.db'
    with sqlite3.connect(database) as connection:
        connection.execute('CREATE TABLE recordings (id INTEGER PRIMARY KEY, timestamp INTEGER, channel_id INTEGER)')
        connection.execute('INSERT INTO recordings VALUES (1, 1000, 1)')
    upgrade_keyword_actions_schema(recordings_path=database)
    upgrade_keyword_actions_schema(recordings_path=database)
    with sqlite3.connect(database) as connection:
        assert connection.execute('SELECT matched_keywords FROM recordings').fetchone() == ('[]',)
