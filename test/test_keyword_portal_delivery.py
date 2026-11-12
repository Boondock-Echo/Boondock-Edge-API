"""Keyword email actions delivered through the Boondock portal."""

import sqlite3

import pytest
import requests

from app.services import keyword_actions as service
from app.services import portal_alerts
from app.services.settings_manager import get_settings_manager
from test.test_keyword_actions import attach, receive_transcription, rows, scenario  # noqa: F401


def connect_to_portal(key='bdk_portalkey1234'):
    get_settings_manager().set_all_settings({
        'boondock_cloud_connected': 'true',
        'transcription_api_key': key,
    })


def portal_action(group, **changes):
    return attach(group, service.save_action({
        'name': 'Fire portal email', 'kind': 'email', 'delivery': 'portal',
        'to': ['ops@example.test'], **changes,
    }))


class FakeResponse:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload if payload is not None else {'data': {'sent': 1, 'failed': 0}}

    def json(self):
        return self._payload


def test_portal_action_requires_a_connected_edge(scenario):
    _, _, group = scenario
    with pytest.raises(ValueError, match='Connect this Edge'):
        portal_action(group)


def test_portal_action_needs_no_smtp_and_limits_recipients(scenario):
    _, _, group = scenario
    connect_to_portal()
    assert not service.get_email_settings()['configured']
    action = portal_action(group)
    assert action['delivery'] == 'portal'
    assert action['to'] == ['ops@example.test']

    with pytest.raises(ValueError, match='at most 5'):
        portal_action(group, name='Too many', to=[f'u{i}@example.test' for i in range(6)])
    with pytest.raises(ValueError, match='delivery'):
        portal_action(group, name='Bad route', delivery='carrier-pigeon')


def test_portal_delivery_posts_the_alert_with_the_key(scenario, monkeypatch):
    _, database, group = scenario
    connect_to_portal('bdk_secretkey')
    portal_action(group)
    with sqlite3.connect(database) as connection:
        receive_transcription(connection, 1, 'fire at the track')

    sent = {}

    def post(url, json=None, headers=None, timeout=None):
        sent.update(url=url, body=json, headers=headers)
        return FakeResponse()

    monkeypatch.setenv('BOONDOCK_PORTAL_URL', 'https://portal.example.test/')
    monkeypatch.setattr(requests, 'post', post)
    assert service.process_email_delivery(database)

    assert sent['url'] == 'https://portal.example.test/api/edge/alerts'
    assert sent['headers']['X-Boondock-Key'] == 'bdk_secretkey'
    assert sent['body']['recipients'] == ['ops@example.test']
    assert sent['body']['keyword'] == 'fire'
    assert sent['body']['transcript'] == 'fire at the track'
    assert sent['body']['channel'] == 'Race control'
    assert rows(database)[0]['state'] == 'sent'


def test_portal_failures_retry_without_leaking_responses(scenario, monkeypatch):
    _, database, group = scenario
    connect_to_portal()
    portal_action(group)
    with sqlite3.connect(database) as connection:
        receive_transcription(connection, 1, 'fire')

    monkeypatch.setattr(requests, 'post', lambda *a, **k: FakeResponse(503, {'error': 'private'}))
    for now, state in [(1000, 'pending'), (1030, 'pending'), (1090, 'failed')]:
        assert service.process_email_delivery(database, now=now)
        assert rows(database)[0]['state'] == state
        assert rows(database)[0]['last_error'] == 'PortalAlertError'


def test_partial_portal_delivery_is_not_retried(scenario, monkeypatch):
    _, database, group = scenario
    connect_to_portal()
    portal_action(group)
    with sqlite3.connect(database) as connection:
        receive_transcription(connection, 1, 'fire')

    monkeypatch.setattr(
        requests, 'post',
        lambda *a, **k: FakeResponse(200, {'data': {'sent': 1, 'failed': 1}}),
    )
    assert service.process_email_delivery(database)
    assert rows(database)[0]['state'] == 'failed'
    assert rows(database)[0]['attempts'] == 1


def test_disconnecting_leaves_portal_deliveries_queued(scenario, monkeypatch):
    _, database, group = scenario
    connect_to_portal()
    portal_action(group)
    with sqlite3.connect(database) as connection:
        receive_transcription(connection, 1, 'fire')

    get_settings_manager().set_all_settings({'boondock_cloud_connected': 'false'})
    monkeypatch.setattr(requests, 'post', lambda *a, **k: pytest.fail('Sent while disconnected'))
    assert not service.process_email_delivery(database)
    assert rows(database)[0]['state'] == 'pending'
    assert rows(database)[0]['attempts'] == 0


def test_smtp_and_portal_deliveries_do_not_block_each_other(scenario, monkeypatch):
    _, database, group = scenario
    connect_to_portal()
    portal_action(group)
    with sqlite3.connect(database) as connection:
        receive_transcription(connection, 1, 'fire')

    # SMTP is not configured, so only the portal delivery is claimable.
    monkeypatch.setattr(requests, 'post', lambda *a, **k: FakeResponse())
    assert service.process_email_delivery(database)
    assert rows(database)[0]['state'] == 'sent'
    assert portal_alerts.portal_connected()
