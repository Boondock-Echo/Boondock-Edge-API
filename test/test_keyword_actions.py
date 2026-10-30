"""Keyword actions: transaction boundaries, delivery, templates, and access."""

from email.parser import BytesParser
from email.policy import default
import io
import smtplib
import socketserver
import sqlite3
import threading
import wave

from flask import Flask
import pytest

from app.services import keyword_actions as service
from app.services.recording_state import update_transcription
from app.utils.sqlite_utils import connect_sqlite


@pytest.fixture
def scenario(recording_store, initialized_settings_manager):
    manager = initialized_settings_manager
    group_id = manager.save_group({
        'name': 'Dispatch', 'is_default': True, 'permissions': ['recording.read'],
        'keywords': [{'pattern': 'fire'}, {'pattern': 'urgent'}],
    })
    group = manager.get_group_by_id(group_id)
    channel_id = manager.save_channel({'name': 'Race control', 'mac': 'AABBCCDDEEFF'})
    assert channel_id > 0
    database, _ = recording_store
    with sqlite3.connect(database) as connection:
        connection.execute(
            """INSERT INTO recordings(id,channel_id,filename,timestamp,transcription)
               VALUES (1,?,'recordings/one.wav',1793197800000,'pending')""", (channel_id,),
        )
    return manager, database, group


def attach(group, action):
    from app.services.settings_manager import get_settings_manager
    manager = get_settings_manager()
    current = manager.get_group_by_id(group['id'])
    for keyword in current['keyword_details']:
        manager.set_keyword_actions(keyword['id'], [*keyword['action_ids'], action['id']])
    return action


def popup(group, **changes):
    return attach(group, service.save_action({
        'name': 'Fire popup', 'kind': 'popup', 'autoplay': False, **changes,
    }))


def email(group, **changes):
    service.save_email_settings({'host': 'localhost', 'dashboard_url': 'https://dashboard.example.test'})
    return attach(group, service.save_action({
        'name': 'Fire email', 'kind': 'email', 'from_email': 'sender@example.test',
        'from_name': 'Dispatch', 'reply_to': 'reply@example.test',
        'to': ['to@example.test'], 'cc': ['cc@example.test'], 'bcc': ['hidden@example.test'],
        'subject': '{keywords} on {channel}',
        'body': '{transcript}\n{matched_text}\n{recording_url}', 'body_format': 'text', **changes,
    }))


def receive_transcription(connection, recording_id, text):
    saved = update_transcription(connection, recording_id, text)
    if saved:
        service.enqueue_actions(connection, recording_id)
    return saved


def rows(database):
    with connect_sqlite(database, row_factory=True) as connection:
        return [dict(row) for row in connection.execute('SELECT * FROM keyword_action_deliveries')]


def test_transactional_queue_once_per_recording_and_scoped_popup(scenario):
    _, database, group = scenario
    popup(group)
    connection = sqlite3.connect(database)
    receive_transcription(connection, 1, 'FIRE urgent')
    assert rows(database) == []  # A second connection cannot see uncommitted events.
    connection.rollback()
    assert rows(database) == []
    receive_transcription(connection, 1, 'FIRE urgent')
    connection.commit()
    receive_transcription(connection, 1, 'Fire again')
    connection.commit()
    connection.close()
    assert len(rows(database)) == 1

    from app.services.channel_state import load_inbox
    principal = {'type': 'user', 'role': 'admin', 'owner_ids': None,
                 'groups': [group], 'keywords': group['keyword_details']}
    app = Flask(__name__)
    with app.test_request_context('/?limit=20'):
        result, status = load_inbox(principal)
    assert status == 200
    assert result['messages'][0]['keyword_actions'][0]['autoplay'] is False
    principal['groups'] = []
    with app.test_request_context('/?limit=20'):
        result, _ = load_inbox(principal)
    assert result['messages'][0]['keyword_actions'] == []


@pytest.mark.parametrize('hidden', ['is_duplicate', 'is_hallucination'])
def test_hidden_recordings_do_not_trigger_actions(scenario, hidden):
    manager, database, group = scenario
    popup(group)
    if hidden == 'is_hallucination':
        manager.save_hallucination({'pattern': 'fire'})
    with sqlite3.connect(database) as connection:
        if hidden == 'is_duplicate':
            connection.execute('UPDATE recordings SET is_duplicate=1')
        receive_transcription(connection, 1, 'fire')
    assert rows(database) == []


def test_channel_ownership_limits_action_scope(scenario):
    manager, database, _ = scenario
    other_id = manager.save_group({'name': 'Other', 'permissions': [], 'keywords': [{'pattern': 'fire'}]})
    popup(manager.get_group_by_id(other_id))
    with sqlite3.connect(database) as connection:
        receive_transcription(connection, 1, 'fire')
    assert rows(database) == []


def test_recording_link_query_selects_one_authorized_recording(scenario):
    from app.services.channel_state import load_inbox
    _, database, _ = scenario
    with sqlite3.connect(database) as connection:
        connection.execute("INSERT INTO recordings(id,timestamp) VALUES(2,1793197800001)")
    app = Flask(__name__)
    principal = {'type': 'user', 'role': 'admin', 'owner_ids': None}
    with app.test_request_context('/?recording_id=1&limit=1'):
        result, status = load_inbox(principal)
    assert status == 200
    assert [row['id'] for row in result['messages']] == [1]
    assert result['meta']['total'] == 1
    with app.test_request_context('/?recording_id=1&limit=1'):
        result, _ = load_inbox({**principal, 'role': 'member', 'owner_ids': []})
    assert result['messages'] == []
    with app.test_request_context('/?recording_id=-1'):
        _, status = load_inbox(principal)
    assert status == 400


@pytest.mark.parametrize('body_format', ['text', 'html'])
def test_smtp_delivery_renders_snapshot_and_hides_bcc(scenario, tmp_path, monkeypatch, body_format):
    """Use a real local SMTP conversation; no external messages are sent."""
    _, database, group = scenario
    action = email(group, body_format=body_format)
    monkeypatch.setattr(service, 'DATA_ROOT', tmp_path)
    audio = tmp_path / 'recordings' / 'one.wav'
    audio.parent.mkdir()
    buffer = io.BytesIO()
    with wave.open(buffer, 'wb') as recording:
        recording.setnchannels(1)
        recording.setsampwidth(2)
        recording.setframerate(8000)
        recording.writeframes(b'\x00\x00' * 80)
    audio.write_bytes(buffer.getvalue())
    with sqlite3.connect(database) as connection:
        receive_transcription(connection, 1, '🚒 FIRE and urgent')
    messages, recipients = [], []

    class SMTPHandler(socketserver.StreamRequestHandler):
        def handle(self):
            self.wfile.write(b'220 local test SMTP\r\n')
            while line := self.rfile.readline():
                command = line.upper()
                if command.startswith(b'EHLO'):
                    self.wfile.write(b'250-localhost\r\n250 OK\r\n')
                elif command.startswith(b'RCPT'):
                    recipients.append(line.decode().strip())
                    self.wfile.write(b'250 OK\r\n')
                elif command.startswith(b'DATA'):
                    self.wfile.write(b'354 end with dot\r\n')
                    content = b''
                    while (line := self.rfile.readline()) not in (b'.\r\n', b''):
                        content += line
                    messages.append(BytesParser(policy=default).parsebytes(content))
                    self.wfile.write(b'250 accepted\r\n')
                elif command.startswith(b'QUIT'):
                    self.wfile.write(b'221 bye\r\n')
                    break
                else:
                    self.wfile.write(b'250 OK\r\n')

    with socketserver.TCPServer(('127.0.0.1', 0), SMTPHandler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            service.save_email_settings({'host': '127.0.0.1', 'port': server.server_address[1], 'security': 'none'})
            assert service.process_email_delivery(database)
            assert not service.process_email_delivery(database)
        finally:
            server.shutdown()
            thread.join()
    assert len(messages) == 1
    assert str(messages[0]['Subject']) == 'fire, urgent on Race control'
    assert messages[0]['Bcc'] is None
    assert str(messages[0]['From']) == 'Dispatch <sender@example.test>'
    assert str(messages[0]['Reply-To']) == 'reply@example.test'
    assert any('hidden@example.test' in address for address in recipients)
    body = messages[0].get_body(preferencelist=('html', 'plain')).get_content()
    assert '🚒 FIRE and urgent' in body
    assert 'FIRE, urgent' in body
    assert 'https://dashboard.example.test/recording/1' in body
    attachments = list(messages[0].iter_attachments())
    assert len(attachments) == 1
    assert attachments[0].get_filename() == 'one.wav'
    assert attachments[0].get_content_type() == 'audio/wav'
    assert attachments[0].get_payload(decode=True) == audio.read_bytes()
    assert rows(database)[0]['state'] == 'sent'
    assert rows(database)[0]['action_id'] == action['id']


@pytest.mark.parametrize('filename', ['', 'recordings/missing.wav', '../outside.wav', 'recordings/link.wav'])
def test_unavailable_or_outside_audio_is_not_sent(scenario, tmp_path, monkeypatch, filename):
    _, database, group = scenario
    email(group)
    root = tmp_path / 'data'
    (root / 'recordings').mkdir(parents=True)
    outside = tmp_path / 'outside.wav'
    outside.write_bytes(b'private data')
    (root / 'recordings' / 'link.wav').symlink_to(outside)
    monkeypatch.setattr(service, 'DATA_ROOT', root)
    monkeypatch.setattr(service.smtplib, 'SMTP', lambda **kwargs: pytest.fail('Audio must be read before connecting'))
    with sqlite3.connect(database) as connection:
        connection.execute('UPDATE recordings SET filename=? WHERE id=1', (filename,))
        receive_transcription(connection, 1, 'fire')
    for now in (1000, 1030, 1090):
        assert service.process_email_delivery(database, now=now)
    delivery = rows(database)[0]
    assert delivery['state'] == 'failed'
    assert delivery['last_error'] == ('FileNotFoundError' if filename == 'recordings/missing.wav' else 'ValueError')


def test_delivery_retries_are_bounded_and_do_not_block_transcription(scenario, monkeypatch):
    _, database, group = scenario
    email(group)
    with sqlite3.connect(database) as connection:
        receive_transcription(connection, 1, 'fire')

    def failed(*args):
        raise smtplib.SMTPAuthenticationError(535, b'private SMTP response')

    monkeypatch.setattr(service, 'send_email', failed)
    for now, state in [(1000, 'pending'), (1030, 'pending'), (1090, 'failed')]:
        assert service.process_email_delivery(database, now=now)
        assert rows(database)[0]['state'] == state
        assert rows(database)[0]['last_error'] == 'SMTPAuthenticationError'
    assert not service.process_email_delivery(database, now=2000)
    assert rows(database)[0]['attempts'] == 3


def test_removing_keyword_assignments_cancels_queued_email(scenario, monkeypatch):
    _, database, group = scenario
    action = email(group)
    with sqlite3.connect(database) as connection:
        receive_transcription(connection, 1, 'fire')
    from app.services.settings_manager import get_settings_manager
    for keyword_id in group['keywords']:
        get_settings_manager().set_keyword_actions(keyword_id, [])
    monkeypatch.setattr(service, 'send_email', lambda *args: pytest.fail('Disabled action sent mail'))
    assert service.process_email_delivery(database)
    assert rows(database)[0]['state'] == 'cancelled'


def test_partial_delivery_is_not_retried_to_successful_recipients(scenario, monkeypatch):
    _, database, group = scenario
    email(group)
    with sqlite3.connect(database) as connection:
        receive_transcription(connection, 1, 'fire')

    def partial(*args):
        raise service.PartialEmailDeliveryError()

    monkeypatch.setattr(service, 'send_email', partial)
    assert service.process_email_delivery(database)
    assert rows(database)[0]['state'] == 'failed'
    assert rows(database)[0]['attempts'] == 1


def test_inserted_recording_also_queues_actions(scenario, tmp_path):
    from app.services.audio_handler import AudioChannel
    manager, database, group = scenario
    popup(group)
    channel_id = manager.get_all_channels()[0]['id']
    channel = AudioChannel(channel_id, tmp_path / 'audio')
    assert channel.save_recording('recordings/new.wav', 1793197800001, 'fire')
    assert len(rows(database)) == 1


def test_raw_recording_deletion_also_removes_delivery_content(scenario):
    _, database, group = scenario
    email(group)
    with sqlite3.connect(database) as connection:
        receive_transcription(connection, 1, 'fire')
    assert len(rows(database)) == 1
    with sqlite3.connect(database) as connection:
        connection.execute('DELETE FROM recordings WHERE id=1')
    assert rows(database) == []


def test_expired_delivery_lease_is_recovered_with_same_message_id(scenario, monkeypatch):
    _, database, group = scenario
    email(group)
    with sqlite3.connect(database) as connection:
        receive_transcription(connection, 1, 'fire')
        connection.execute("UPDATE keyword_action_deliveries SET state='sending', lease_until=1100, attempts=1")
    ids = []

    def capture(action, context, settings):
        message, _ = service.render_email(action, context, settings)
        ids.append(str(message['Message-ID']))

    monkeypatch.setattr(service, 'send_email', capture)
    assert not service.process_email_delivery(database, now=1000)
    assert service.process_email_delivery(database, now=1100)
    assert rows(database)[0]['attempts'] == 2
    assert ids[0]


@pytest.mark.parametrize('changes', [
    {'to': ['not-an-email']}, {'subject': 'Header\nInjection'},
    {'subject': '{transcript.__class__}'}, {'keyword_ids': [99999]},
    {'group_id': True}, {'kind': 'unknown'}, {'enabled': 'yes'},
])
def test_action_validation_rejects_invalid_configuration(scenario, changes):
    _, _, group = scenario
    with pytest.raises(ValueError):
        email(group, **changes)


def test_html_template_escapes_transcript_and_rejects_unsafe_fields():
    with pytest.raises(ValueError):
        service.validate_template('{transcript.__class__}', 'body')
    with pytest.raises(ValueError):
        service.validate_template('{missing}', 'body')
    settings = service.EMAIL_DEFAULTS
    message, _ = service.render_email({
        'to': ['recipient@example.test'], 'from_email': 'sender@example.test', 'subject': '{transcript}',
        'body': '<p>{transcript}</p>', 'body_format': 'html',
    }, {'transcript': '<script>alert(1)</script>\nnew line'}, settings)
    assert '\n' not in str(message['Subject'])
    assert '&lt;script&gt;' in message.get_body(preferencelist=('html',)).get_content()


def test_settings_routes_require_admin_and_never_return_password(recording_store, admin_auth):
    from app import create_app
    client = create_app().test_client()
    assert client.get('/api/settings/email').status_code == 401
    response = client.patch('/api/settings/email', headers=admin_auth, json={
        'host': 'smtp.example.test', 'username': 'sender', 'password': 'keep this secret',
    })
    assert response.status_code == 200
    assert response.json['password_configured'] is True
    assert 'password' not in response.json
    assert 'keep this secret' not in client.get('/api/settings/email', headers=admin_auth).get_data(as_text=True)
    assert client.patch('/api/settings/email', headers=admin_auth, json={'port': 'bad'}).status_code == 400
    assert client.patch('/api/settings/email', headers=admin_auth, json={'from_email': 'x@example.test\r\nBcc: other@example.test'}).status_code == 400
    assert client.post('/api/keyword-actions/preview', headers=admin_auth, json={
        'subject': '{keywords}', 'body': '{transcript}', 'body_format': 'text',
    }).status_code == 200

    from app.services.settings_manager import get_settings_manager
    manager = get_settings_manager()
    manager.save_user('member@example.test', {'name': 'Member', 'role': 'member', 'groups': [], 'password': 'unused'})
    token, _ = manager.issue_credential('user', 'member@example.test', None)
    headers = {'Authorization': f'Bearer {token}'}
    for path in ('/api/settings/email', '/api/keyword-actions', '/api/keyword-actions/deliveries'):
        assert client.get(path, headers=headers).status_code == 403


def test_action_migration_preserves_existing_matches_and_is_idempotent(tmp_path):
    from migrations.keyword_actions import upgrade_keyword_actions_schema
    database = tmp_path / 'existing.db'
    with sqlite3.connect(database) as connection:
        connection.execute('CREATE TABLE recordings(id INTEGER PRIMARY KEY, matched_keywords JSON)')
        connection.execute("INSERT INTO recordings VALUES(1, '[1]')")
    upgrade_keyword_actions_schema(recordings_path=database)
    upgrade_keyword_actions_schema(recordings_path=database)
    with sqlite3.connect(database) as connection:
        assert connection.execute('SELECT matched_keywords FROM recordings').fetchone() == ('[1]',)
        assert connection.execute('SELECT COUNT(*) FROM keyword_action_deliveries').fetchone() == (0,)


def test_consolidated_migration_preserves_current_actions_and_pending_deliveries(scenario):
    from migrations.keyword_actions import upgrade_keyword_actions_schema
    manager, database, group = scenario
    email(group)
    with sqlite3.connect(database) as connection:
        receive_transcription(connection, 1, 'fire')
    pending = rows(database)
    definitions = manager.get_keyword_actions()
    selections = manager.get_group_by_id(group['id'])['keyword_details']
    routing = service.get_email_settings()
    upgrade_keyword_actions_schema(manager.db_path, database)
    upgrade_keyword_actions_schema(manager.db_path, database)
    assert rows(database) == pending
    assert manager.get_keyword_actions() == definitions
    assert manager.get_group_by_id(group['id'])['keyword_details'] == selections
    assert service.get_email_settings() == routing


def test_unassigned_actions_are_inactive_and_new_keyword_can_select_them(scenario):
    manager, database, group = scenario
    created = service.save_action({'name': 'Reusable popup', 'kind': 'popup'})
    assert not {'enabled', 'group_id', 'keyword_ids'}.intersection(created)
    with sqlite3.connect(database) as connection:
        receive_transcription(connection, 1, 'fire')
    assert rows(database) == []
    assert manager.save_group({**group, 'keywords': [*group['keywords'], {
        'pattern': 'rescue', 'action_ids': [created['id']],
    }]}, group['id']) > 0
    details = manager.get_group_by_id(group['id'])['keyword_details']
    assert details[-1]['action_ids'] == [created['id']]
    with sqlite3.connect(database) as connection:
        update_transcription(connection, 1, 'rescue')
    assert rows(database) == []
    manager.set_keyword_actions(details[-1]['id'], [])
    assert manager.get_keyword_action(created['id']) is not None
    assert not manager.get_keyword_action_triggers([details[-1]['id']], 1)


def test_email_requires_routing_and_sender_is_per_action(scenario):
    _, _, group = scenario
    with pytest.raises(ValueError, match='routing'):
        service.save_action({'name': 'Email', 'kind': 'email'})
    first = email(group)
    second = service.save_action({**{key: value for key, value in first.items() if key != 'id'},
                                  'name': 'Other sender', 'from_email': 'other@example.test'})
    assert service.get_email_settings(public=True)['configured'] is True
    assert not {'from_email', 'from_name', 'reply_to', 'enabled'}.intersection(service.get_email_settings(public=True))
    assert first['from_email'] != second['from_email']


def test_keyword_assignment_endpoint_validates_ids_and_preserves_previous_selection(scenario, admin_auth):
    from app import create_app
    from app.services.settings_manager import get_settings_manager
    manager = get_settings_manager()
    group_id = manager.save_group({'name': 'Assign', 'permissions': [], 'keywords': [{'pattern': 'help'}]})
    keyword_id = manager.get_group_by_id(group_id)['keywords'][0]
    created = service.save_action({'name': 'Popup', 'kind': 'popup'})
    client = create_app().test_client()
    url = f'/api/keywords/{keyword_id}/actions'
    assert client.put(url, headers=admin_auth, json={'action_ids': [created['id']]}).status_code == 200
    assert client.put(url, headers=admin_auth, json={'action_ids': ['missing']}).status_code == 400
    assert manager.get_group_by_id(group_id)['keyword_details'][0]['action_ids'] == [created['id']]
    service.delete_action(created['id'])
    assert manager.get_group_by_id(group_id)['keyword_details'][0]['action_ids'] == []


def test_reusable_action_shared_across_groups_fires_once_and_is_visible_to_both(scenario):
    from app.services.channel_state import load_inbox
    manager, database, group = scenario
    created = popup(group)
    other_id = manager.save_group({'name': 'Other dispatch', 'permissions': ['recording.read'],
                                  'keywords': [{'pattern': 'rescue', 'action_ids': [created['id']]}]})
    other = manager.get_group_by_id(other_id)
    with manager._get_connection() as settings:
        settings.execute("INSERT INTO channel_owners(channel_id,owner_type,owner_id) VALUES(1,'group',?)", (str(other_id),))
    with sqlite3.connect(database) as connection:
        receive_transcription(connection, 1, 'fire rescue')
    assert len(rows(database)) == 1
    app = Flask(__name__)
    for member_group in (group, other):
        principal = {'type': 'user', 'role': 'admin', 'owner_ids': None,
                     'groups': [member_group], 'keywords': member_group['keyword_details']}
        with app.test_request_context('/?limit=20'):
            result, _ = load_inbox(principal)
        assert len(result['messages'][0]['keyword_actions']) == 1


def test_manual_edits_never_queue_actions(scenario):
    _, database, group = scenario
    popup(group)
    with sqlite3.connect(database) as connection:
        update_transcription(connection, 1, 'fire')
        update_transcription(connection, 1, 'urgent')
    assert rows(database) == []


def test_edits_and_manual_retranscription_cannot_trigger_a_different_action(scenario):
    manager, database, group = scenario
    fire_action = service.save_action({'name': 'Fire alert', 'kind': 'popup'})
    urgent_action = service.save_action({'name': 'Urgent alert', 'kind': 'popup'})
    manager.set_keyword_actions(group['keywords'][0], [fire_action['id']])
    manager.set_keyword_actions(group['keywords'][1], [urgent_action['id']])
    with sqlite3.connect(database) as connection:
        receive_transcription(connection, 1, 'fire')
        update_transcription(connection, 1, 'urgent')
        update_transcription(connection, 1, 'urgent')
    assert [row['action_id'] for row in rows(database)] == [fire_action['id']]


def test_returned_transcription_queues_actions_for_an_existing_recording(scenario, tmp_path):
    from app.services.audio_handler import AudioChannel
    manager, database, group = scenario
    created = popup(group)
    channel_id = manager.get_all_channels()[0]['id']
    channel = AudioChannel(channel_id, tmp_path / 'audio')
    assert channel.save_recording('recordings/one.wav', 1793197800000, 'fire')
    assert [row['action_id'] for row in rows(database)] == [created['id']]
    with sqlite3.connect(database) as connection:
        update_transcription(connection, 1, 'urgent')
    assert len(rows(database)) == 1


@pytest.mark.parametrize('existing', [True, False])
def test_failed_action_queueing_rolls_back_the_recording_save(scenario, tmp_path, monkeypatch, existing):
    from app.services import audio_handler
    manager, database, _ = scenario
    if not existing:
        with sqlite3.connect(database) as connection:
            connection.execute('DELETE FROM recordings')
    channel = audio_handler.AudioChannel(manager.get_all_channels()[0]['id'], tmp_path / 'audio')

    def fail_queue(*args):
        raise RuntimeError('Queue write failed')

    monkeypatch.setattr(audio_handler, 'enqueue_actions', fail_queue)
    assert channel.save_recording('recordings/one.wav', 1793197800000, 'fire') is False
    with sqlite3.connect(database) as connection:
        saved = connection.execute('SELECT transcription FROM recordings').fetchall()
    assert saved == ([('pending',)] if existing else [])
    assert rows(database) == []


def test_kasa_delivery_works_without_email_and_keeps_selected_outlet_snapshot(scenario, monkeypatch):
    _, database, group = scenario
    action = attach(group, service.save_action({
        'name': 'Desk lamp', 'kind': 'kasa', 'device_host': '192.168.1.50',
        'outlet_id': 'strip_socket1', 'command': 'turn_on',
    }))
    with sqlite3.connect(database) as connection:
        receive_transcription(connection, 1, 'fire')
    assert rows(database)[0]['state'] == 'pending'
    service.save_action({'outlet_id': 'strip_socket2'}, action['id'])
    commands = []
    monkeypatch.setattr(service.kasa_actions, 'execute', lambda config: commands.append(config))
    assert service.process_email_delivery(database, kind='kasa')
    assert commands[0]['outlet_id'] == 'strip_socket1'
    assert rows(database)[0]['state'] == 'sent'
    assert not service.process_email_delivery(database, kind='kasa')


def test_kasa_delivery_retries_and_cancels_removed_keyword_assignment(scenario, monkeypatch):
    manager, database, group = scenario
    attach(group, service.save_action({
        'name': 'Lamp', 'kind': 'kasa', 'device_host': '192.168.1.50',
        'outlet_id': 'socket1', 'command': 'turn_off',
    }))
    with sqlite3.connect(database) as connection:
        receive_transcription(connection, 1, 'fire')
    def offline(config):
        raise TimeoutError('private device details')
    monkeypatch.setattr(service.kasa_actions, 'execute', offline)
    assert service.process_email_delivery(database, now=1000, kind='kasa')
    assert rows(database)[0]['state'] == 'pending'
    assert rows(database)[0]['last_error'] == 'TimeoutError'
    for keyword_id in group['keywords']:
        manager.set_keyword_actions(keyword_id, [])
    assert service.process_email_delivery(database, now=1100, kind='kasa')
    assert rows(database)[0]['state'] == 'cancelled'
