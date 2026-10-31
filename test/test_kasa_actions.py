"""Local Kasa transport, credentials, API access, and storage upgrades."""
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.services import kasa_actions
from app.services.keyword_action_schema import KEYWORD_ACTION_SCHEMA, KEYWORD_ACTION_SETTINGS_SCHEMA
from migrations.keyword_actions import upgrade_keyword_actions_schema


@pytest.fixture(autouse=True)
def kasa_settings_database(initialized_settings_manager):
    return initialized_settings_manager


@pytest.mark.parametrize('config', [
    {'device_host': 'http://example.com'}, {'device_host': '0.0.0.0'},
    {'outlet_id': ''}, {'command': 'toggle'}, {'command': '__class__'},
])
def test_invalid_config(config):
    with pytest.raises(ValueError):
        kasa_actions.validate_config({
            'device_host': '192.168.1.50', 'outlet_id': 'socket1', 'command': 'turn_on', **config,
        })


def test_read_is_non_mutating_and_control_only_targets_selected_outlet(monkeypatch):
    from kasa import Discover
    from kasa.iot import IotStrip
    first = SimpleNamespace(device_id='socket1', alias='Lamp', is_on=False,
                            turn_on=AsyncMock(), turn_off=AsyncMock())
    second = SimpleNamespace(device_id='socket2', alias='Other', is_on=False,
                             turn_on=AsyncMock(), turn_off=AsyncMock())
    async def on():
        first.is_on = True
    first.turn_on.side_effect = on
    strip = Mock(spec=IotStrip, protocol=None, model='HS300(US)', alias='Desk', children=[first, second],
                 update=AsyncMock(), disconnect=AsyncMock())
    monkeypatch.setattr(Discover, 'discover_single', AsyncMock(return_value=strip))
    assert len(kasa_actions.read_outlets('192.168.1.50')['outlets']) == 2
    first.turn_on.assert_not_awaited()
    result = kasa_actions.execute({'device_host': '192.168.1.50', 'outlet_id': 'socket1', 'command': 'turn_on'})
    assert result['outlets'][0]['is_on'] is True
    first.turn_on.assert_awaited_once()
    second.turn_on.assert_not_awaited()
    with pytest.raises(ValueError, match='not found'):
        kasa_actions.execute({'device_host': '192.168.1.50', 'outlet_id': 'missing', 'command': 'turn_off'})
    assert strip.disconnect.await_count == 3


def test_routes_require_admin_and_report_device_failures(admin_auth, monkeypatch):
    from flask import Flask
    from app.routes.keyword_actions_routes import keyword_actions_bp
    app = Flask(__name__)
    app.register_blueprint(keyword_actions_bp, url_prefix='/api')
    client = app.test_client()
    payload = {'device_host': '192.168.1.50', 'outlet_id': 'socket1', 'command': 'turn_on'}
    for path in ('outlets', 'test', 'discover'):
        assert client.post('/api/keyword-actions/kasa/' + path, json=payload).status_code == 401
    manager = __import__('app.services.settings_manager', fromlist=['get_settings_manager']).get_settings_manager()
    manager.save_user('member@example.test', {'name': 'Member', 'role': 'member', 'password': 'unused', 'groups': []})
    token, _ = manager.issue_credential('user', 'member@example.test', None)
    assert client.post('/api/keyword-actions/kasa/test', json=payload,
                       headers={'Authorization': f'Bearer {token}'}).status_code == 403
    monkeypatch.setattr(kasa_actions, 'discover_devices', lambda: {'devices': []})
    assert client.post('/api/keyword-actions/kasa/discover', headers=admin_auth, json={}).json == {'devices': []}
    assert client.post('/api/keyword-actions/kasa/discover', headers=admin_auth, json=payload).status_code == 400
    monkeypatch.setattr(kasa_actions, 'read_outlets', lambda host: {'outlets': []})
    assert client.post('/api/keyword-actions/kasa/outlets', headers=admin_auth,
                       json={'device_host': payload['device_host']}).status_code == 200
    assert client.post('/api/keyword-actions/kasa/test', headers=admin_auth, json={}).status_code == 400
    def offline(config):
        raise TimeoutError('private information')
    monkeypatch.setattr(kasa_actions, 'execute', offline)
    response = client.post('/api/keyword-actions/kasa/test', headers=admin_auth, json=payload)
    assert response.status_code == 502
    assert 'private information' not in response.get_data(as_text=True)


def test_old_check_constraints_upgrade_preserves_links_deliveries_and_cleanup(tmp_path):
    settings, recordings = tmp_path / 'settings.db', tmp_path / 'recordings.db'
    with sqlite3.connect(settings) as db:
        db.execute('CREATE TABLE settings(key TEXT PRIMARY KEY, value JSON, type TEXT)')
        db.execute('CREATE TABLE keywords(id INTEGER PRIMARY KEY)')
        for statement in KEYWORD_ACTION_SETTINGS_SCHEMA:
            db.execute(statement.replace("'email', 'popup', 'kasa'", "'email', 'popup'"))
        db.execute('INSERT INTO keywords VALUES(1)')
        db.execute("INSERT INTO keyword_actions VALUES('old','Popup','popup','{}')")
        db.execute("INSERT INTO keyword_action_links VALUES(1,'old')")
    with sqlite3.connect(recordings) as db:
        db.execute("CREATE TABLE recordings(id INTEGER PRIMARY KEY, matched_keywords JSON DEFAULT '[]')")
        for statement in KEYWORD_ACTION_SCHEMA:
            db.execute(statement.replace("'email', 'popup', 'kasa'", "'email', 'popup'"))
        db.execute('INSERT INTO recordings(id) VALUES(1)')
        db.execute("INSERT INTO keyword_action_deliveries(recording_id,action_id,group_id,kind,payload,state,created_at) VALUES(1,'old',1,'popup','{}','sent',123)")
    upgrade_keyword_actions_schema(settings, recordings)
    upgrade_keyword_actions_schema(settings, recordings)
    with sqlite3.connect(settings) as db:
        db.execute('PRAGMA foreign_keys=ON')
        assert db.execute('SELECT * FROM keyword_action_links').fetchall() == [(1, 'old')]
        db.execute("INSERT INTO keyword_actions VALUES('new','Lamp','kasa','{}')")
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []
        db.execute("DELETE FROM keyword_actions WHERE id='old'")
        assert db.execute('SELECT * FROM keyword_action_links').fetchall() == []
    with sqlite3.connect(recordings) as db:
        assert db.execute('SELECT state,created_at FROM keyword_action_deliveries').fetchall() == [('sent', 123)]
        db.execute("INSERT INTO keyword_action_deliveries(recording_id,action_id,group_id,kind,payload,created_at) VALUES(1,'new',1,'kasa','{}',124)")
        db.execute('DELETE FROM recordings')
        assert db.execute('SELECT * FROM keyword_action_deliveries').fetchall() == []


def test_discovery_lists_devices_without_switching_and_isolates_unreadable_devices(monkeypatch):
    from kasa import Discover
    from kasa.iot import IotPlug, IotStrip
    from kasa.smart import SmartDevice
    from kasa.exceptions import AuthenticationError
    socket = SimpleNamespace(device_id='socket1', alias='Desk lamp', is_on=False)
    strip = Mock(spec=IotStrip, protocol=None, model='HS300', alias='Desk strip', children=[socket],
                 update=AsyncMock(), disconnect=AsyncMock())
    plug = Mock(spec=IotPlug, protocol=None, model='HS110', alias='Desk plug', children=[], device_id='plug1',
                is_on=True, update=AsyncMock(), disconnect=AsyncMock())
    offline = Mock(spec=IotPlug, protocol=None, model='HS100', alias='Offline', children=[],
                   update=AsyncMock(side_effect=TimeoutError()), disconnect=AsyncMock())
    modern = Mock(spec=SmartDevice, protocol=None, model='KP125M', alias=None, update=AsyncMock(side_effect=AuthenticationError('private password')), disconnect=AsyncMock())
    devices = {'192.168.1.50': strip, '192.168.1.51': plug,
               '192.168.1.52': offline, '192.168.1.53': modern}
    discover = AsyncMock(return_value=devices)
    monkeypatch.setattr(Discover, 'discover', discover)
    found = {device['device_host']: device for device in kasa_actions.discover_devices()['devices']}
    assert found['192.168.1.50']['outlets'][0]['id'] == 'socket1'
    assert found['192.168.1.51']['outlets'][0]['id'] == 'plug1'
    assert found['192.168.1.51']['supported']
    assert not found['192.168.1.52']['supported']
    assert 'could not be read' in found['192.168.1.52']['reason']
    assert not found['192.168.1.53']['supported']
    assert 'credentials' in found['192.168.1.53']['reason'].lower()
    for device in devices.values():
        device.disconnect.assert_awaited_once()
        device.turn_on.assert_not_called()
    discover.assert_awaited_once_with(discovery_timeout=3, credentials=None)
    discover.return_value = {}
    assert kasa_actions.discover_devices() == {'devices': []}


def test_single_plug_can_be_selected_and_wrong_device_id_is_rejected(monkeypatch):
    from kasa import Discover
    from kasa.iot import IotPlug
    plug = Mock(spec=IotPlug, protocol=None, model='HS110', alias='Lamp', children=[], device_id='plug1',
                is_on=True, update=AsyncMock(), disconnect=AsyncMock(), turn_off=AsyncMock())
    async def off():
        plug.is_on = False
    plug.turn_off.side_effect = off
    monkeypatch.setattr(Discover, 'discover_single', AsyncMock(return_value=plug))
    result = kasa_actions.execute({'device_host': '192.168.1.51', 'outlet_id': 'plug1', 'command': 'turn_off'})
    assert result['outlets'][0]['is_on'] is False
    plug.turn_off.assert_awaited_once()
    with pytest.raises(ValueError, match='not found'):
        kasa_actions.execute({'device_host': '192.168.1.51', 'outlet_id': 'different-device', 'command': 'turn_off'})
    plug.turn_off.assert_awaited_once()


def test_account_settings_preserve_replace_clear_password_and_hide_it():
    assert kasa_actions.get_settings() == {'username': '', 'configured': False, 'password_configured': False}
    saved = kasa_actions.save_settings({'username': ' owner@example.test ', 'password': ' secret with spaces '})
    assert saved == {'username': 'owner@example.test', 'configured': True, 'password_configured': True}
    assert 'password' not in saved
    kasa_actions.save_settings({'username': 'owner@example.test', 'password': ''})
    assert kasa_actions.get_settings(public=False)['password'] == ' secret with spaces '
    with pytest.raises(ValueError, match='changing'):
        kasa_actions.save_settings({'username': 'other@example.test'})
    assert kasa_actions.get_settings()['username'] == 'owner@example.test'
    kasa_actions.save_settings({'username': 'other@example.test', 'password': 'replacement'})
    assert kasa_actions.get_settings(public=False)['password'] == 'replacement'
    cleared = kasa_actions.save_settings({'clear_password': True})
    assert not cleared['configured'] and not cleared['password_configured']
    assert kasa_actions._credentials() is None


@pytest.mark.parametrize('payload', [
    {'username': []}, {'username': 'bad\nuser'}, {'password': None},
    {'clear_password': 'true'}, {'clear_password': True, 'password': 'new'},
    {'token': 'unknown'}, {'password': 'without-username'}, {'username': 'without-password'},
])
def test_invalid_account_settings_are_rejected_without_mutation(payload):
    with pytest.raises(ValueError):
        kasa_actions.save_settings(payload)
    assert not kasa_actions.get_settings()['configured']


def test_account_routes_are_admin_only_and_never_return_password(admin_auth):
    from flask import Flask
    from app.routes.keyword_actions_routes import keyword_actions_bp
    app = Flask(__name__)
    app.register_blueprint(keyword_actions_bp, url_prefix='/api')
    client = app.test_client()
    for method in (client.get, client.patch):
        assert method('/api/settings/kasa', json={}).status_code == 401
    result = client.patch('/api/settings/kasa', headers=admin_auth,
                          json={'username': 'owner@example.test', 'password': 'never-return-this'})
    assert result.status_code == 200 and result.json['configured']
    for response in (result, client.get('/api/settings/kasa', headers=admin_auth)):
        assert 'never-return-this' not in response.get_data(as_text=True)
        assert 'password' not in response.json
    assert client.patch('/api/settings/kasa', headers=admin_auth, json={'password': None}).status_code == 400
    from app.services.settings_manager import get_settings_manager
    manager = get_settings_manager()
    manager.save_user('member@example.test', {'name': 'Member', 'role': 'member', 'password': 'unused', 'groups': []})
    token, _ = manager.issue_credential('user', 'member@example.test', None)
    for method in (client.get, client.patch):
        assert method('/api/settings/kasa', headers={'Authorization': f'Bearer {token}'}, json={}).status_code == 403


def test_authenticated_smart_device_discovery_and_commands_use_saved_credentials(monkeypatch):
    from kasa import DeviceType, Discover
    from kasa.smart import SmartDevice
    kasa_actions.save_settings({'username': 'owner@example.test', 'password': 'account-secret'})
    plug = Mock(spec=SmartDevice, protocol=None, device_type=DeviceType.Plug, model='KP125M', alias='Lamp',
                children=[], device_id='smart1', is_on=False, update=AsyncMock(),
                disconnect=AsyncMock(), turn_on=AsyncMock())
    async def on():
        plug.is_on = True
    plug.turn_on.side_effect = on
    discover = AsyncMock(return_value={'192.168.1.55': plug})
    single = AsyncMock(return_value=plug)
    monkeypatch.setattr(Discover, 'discover', discover)
    monkeypatch.setattr(Discover, 'discover_single', single)
    found = kasa_actions.discover_devices()['devices'][0]
    assert found['supported'] and found['outlets'][0]['id'] == 'smart1'
    assert discover.call_args.kwargs['credentials'].username == 'owner@example.test'
    assert discover.call_args.kwargs['credentials'].password == 'account-secret'
    assert 'account-secret' not in str(found)
    plug.turn_on.assert_not_awaited()
    kasa_actions.read_outlets('192.168.1.55')
    assert single.call_args.kwargs['credentials'].password == 'account-secret'
    plug.turn_on.assert_not_awaited()
    kasa_actions.execute({'device_host': '192.168.1.55', 'outlet_id': 'smart1', 'command': 'turn_on'})
    assert single.call_args.kwargs['credentials'].password == 'account-secret'
    plug.turn_on.assert_awaited_once()
    # Workers resolve current credentials, rather than persisting them in events.
    kasa_actions.save_settings({'password': 'updated-secret'})
    kasa_actions.read_outlets('192.168.1.55')
    assert single.call_args.kwargs['credentials'].password == 'updated-secret'


def test_authentication_failures_are_sanitized_and_non_switch_devices_stay_unavailable(monkeypatch):
    from kasa import DeviceType, Discover
    from kasa.exceptions import AuthenticationError
    from kasa.smart import SmartDevice
    plug = Mock(spec=SmartDevice, protocol=None, model='KP125M', alias='Lamp', update=AsyncMock(
        side_effect=AuthenticationError('sensitive-password')), disconnect=AsyncMock())
    monkeypatch.setattr(Discover, 'discover_single', AsyncMock(return_value=plug))
    with pytest.raises(ValueError, match='No Kasa account credentials') as error:
        kasa_actions.read_outlets('192.168.1.55')
    assert 'sensitive-password' not in str(error.value)
    hub = Mock(spec=SmartDevice, protocol=None, device_type=DeviceType.Hub, model='H100', alias='Hub',
               update=AsyncMock(), disconnect=AsyncMock())
    monkeypatch.setattr(Discover, 'discover', AsyncMock(return_value={'192.168.1.56': hub}))
    result = kasa_actions.discover_devices()['devices'][0]
    assert not result['supported'] and 'on/off' in result['reason']
    hub.turn_on.assert_not_called()


@pytest.mark.parametrize('login_version', [1, 2])
def test_real_library_chooses_correct_iot_klap_handshake(login_version, monkeypatch):
    """Exercise the real library handshake, mocking only the device HTTP reply."""
    import asyncio
    import hashlib
    from kasa import Credentials, DeviceConfig
    from kasa.deviceconfig import DeviceConnectionParameters
    from kasa.device_factory import get_protocol
    from kasa.transports import KlapTransport, KlapTransportV2

    async def verify():
        config = DeviceConfig(
            host='192.168.1.21', credentials=Credentials('Owner@example.test', 'test-secret'),
            connection_type=DeviceConnectionParameters.from_values(
                'IOT.SMARTPLUGSWITCH', 'KLAP', login_version=login_version),
        )
        protocol = get_protocol(config)
        transport = protocol._transport
        try:
            assert type(transport) is (KlapTransportV2 if login_version == 2 else KlapTransport)
            local, remote = b'L' * 16, b'R' * 16
            monkeypatch.setattr('kasa.transports.klaptransport.secrets.token_bytes', lambda length: local)
            if login_version == 2:
                auth = hashlib.sha256(hashlib.sha1(b'Owner@example.test').digest() + hashlib.sha1(b'test-secret').digest()).digest()
                server_hash = hashlib.sha256(local + remote + auth).digest()
            else:
                auth = hashlib.md5(hashlib.md5(b'Owner@example.test').digest() + hashlib.md5(b'test-secret').digest()).digest()
                server_hash = hashlib.sha256(local + auth).digest()
            monkeypatch.setattr(transport._http_client, 'post', AsyncMock(return_value=(200, remote + server_hash)))
            assert await transport.perform_handshake1() == (local, remote, auth)
        finally:
            await protocol.close()
    asyncio.run(verify())


def test_iot_connection_uses_sysinfo_to_preserve_power_strip_class(monkeypatch):
    """Discovery calls a KLAP HS300 an IotPlug; connect must resolve IotStrip."""
    import asyncio
    from kasa import Credentials, DeviceConfig
    from kasa.deviceconfig import DeviceConnectionParameters
    from kasa.device_factory import get_protocol
    from kasa.iot import IotPlug, IotStrip

    async def verify():
        config = DeviceConfig(
            host='192.168.1.21', credentials=Credentials('Owner@example.test', 'test-secret'),
            connection_type=DeviceConnectionParameters.from_values(
                'IOT.SMARTPLUGSWITCH', 'KLAP', login_version=2),
        )
        discovered = IotPlug(config.host, config=config)
        protocol = get_protocol(config)
        query = AsyncMock(return_value={'system': {'get_sysinfo': {
            'mic_type': 'IOT.SMARTPLUGSWITCH', 'model': 'HS300(US)',
            'alias': 'Desk strip', 'mac': 'AA:BB:CC:DD:EE:FF', 'children': [{'id': 'socket1'}],
        }}})
        monkeypatch.setattr(protocol, 'query', query)
        monkeypatch.setattr('kasa.device_factory.get_protocol', lambda config: protocol)
        monkeypatch.setattr(IotStrip, 'update', AsyncMock())
        device = await kasa_actions._ready_device(discovered)
        try:
            assert isinstance(device, IotStrip)
            assert device.protocol is protocol
            query.assert_awaited_once()
        finally:
            await device.disconnect()
    asyncio.run(verify())


def test_saved_credentials_authentication_message_does_not_assume_bad_password():
    kasa_actions.save_settings({'username': 'Owner@example.test', 'password': 'test-secret'})
    message = kasa_actions._authentication_message()
    assert 'case-sensitive' in message and 'Third-Party Compatibility' in message
    assert 'library' in message
    assert 'test-secret' not in message
