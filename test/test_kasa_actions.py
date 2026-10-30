"""HS300 transport selection, API access, and upgrades from popup/email storage."""
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.services import kasa_actions
from app.services.keyword_action_schema import KEYWORD_ACTION_SCHEMA, KEYWORD_ACTION_SETTINGS_SCHEMA
from migrations.keyword_actions import upgrade_keyword_actions_schema


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
    strip = Mock(spec=IotStrip, model='HS300(US)', alias='Desk', children=[first, second],
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
    socket = SimpleNamespace(device_id='socket1', alias='Desk lamp', is_on=False)
    strip = Mock(spec=IotStrip, model='HS300', alias='Desk strip', children=[socket],
                 update=AsyncMock(), disconnect=AsyncMock())
    plug = Mock(spec=IotPlug, model='HS110', alias='Desk plug', children=[], device_id='plug1',
                is_on=True, update=AsyncMock(), disconnect=AsyncMock())
    offline = Mock(spec=IotPlug, model='HS100', alias='Offline', children=[],
                   update=AsyncMock(side_effect=TimeoutError()), disconnect=AsyncMock())
    modern = Mock(spec=SmartDevice, model='KP125M', alias=None, disconnect=AsyncMock())
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
    assert 'authentication' in found['192.168.1.53']['reason']
    for device in devices.values():
        device.disconnect.assert_awaited_once()
        device.turn_on.assert_not_called()
    discover.assert_awaited_once_with(discovery_timeout=3)
    discover.return_value = {}
    assert kasa_actions.discover_devices() == {'devices': []}


def test_single_plug_can_be_selected_and_wrong_device_id_is_rejected(monkeypatch):
    from kasa import Discover
    from kasa.iot import IotPlug
    plug = Mock(spec=IotPlug, model='HS110', alias='Lamp', children=[], device_id='plug1',
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
