"""Keyword alert emails delivered through the Boondock portal.

A connected Edge holds a portal API key (stored as the transcription key), and
that key authorises ``POST /api/edge/alerts``. Alerts sent this way need no SMTP
settings on the device.
"""
import os
import socket

import requests

from .settings_manager import get_settings_manager

MAX_PORTAL_RECIPIENTS = 5
REQUEST_TIMEOUT_SECONDS = 15


class PortalAlertError(Exception):
    """The portal rejected or could not deliver the alert."""


class PortalAlertPartialError(PortalAlertError):
    """Some recipients were reached; retrying everybody would duplicate mail."""


def portal_base_url():
    explicit = os.environ.get('BOONDOCK_PORTAL_URL', '').strip()
    return (explicit or 'https://portal.boondock.cloud').rstrip('/')


def portal_api_key():
    settings = get_settings_manager().get_all_settings()
    if str(settings.get('boondock_cloud_connected') or '').lower() != 'true':
        return ''
    return str(settings.get('transcription_api_key') or '').strip()


def portal_connected():
    return bool(portal_api_key())


def send_keyword_alert(recipients, context):
    """Ask the portal to email one keyword alert. Raises PortalAlertError."""
    api_key = portal_api_key()
    if not api_key:
        raise PortalAlertError('Edge is not connected to the portal')

    body = {
        'recipients': list(recipients),
        'keyword': context.get('keywords') or context.get('keyword') or '',
        'transcript': context.get('transcript') or '',
        'channel': context.get('channel') or '',
        'device_name': socket.gethostname(),
    }
    if context.get('timestamp'):
        body['matched_at'] = context['timestamp']

    try:
        response = requests.post(
            f'{portal_base_url()}/api/edge/alerts',
            json=body,
            headers={'X-Boondock-Key': api_key, 'User-Agent': 'BoondockEdge/1.0'},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except requests.RequestException as error:
        raise PortalAlertError(type(error).__name__) from None

    if response.status_code != 200:
        # Status only: the response can echo content that must not reach logs.
        raise PortalAlertError(f'portal returned {response.status_code}')

    try:
        failed = int(((response.json() or {}).get('data') or {}).get('failed', 0))
    except (ValueError, TypeError):
        failed = 0
    if failed:
        raise PortalAlertPartialError(f'{failed} recipient(s) not reached')
