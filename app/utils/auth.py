"""Database-authoritative authentication helpers."""
import logging
from datetime import datetime, timedelta, timezone

from flask import g, has_request_context, jsonify, request

from ..services.settings_manager import get_settings_manager

log = logging.getLogger(__name__)
TOKEN_EXPIRY_HOURS = 24 * 7
_settings_manager = get_settings_manager()

def get_request_token():
    """Read a credential without inferring its principal type from its header."""
    authorization = (request.headers.get('Authorization') or '').strip()
    if authorization.lower().startswith('bearer '):
        return authorization[7:].strip() or None
    return (request.headers.get('X-API-Key') or '').strip() or None

def authenticate():
    """Get the token and authenticate it."""
    token = get_request_token()
    if not token:
        return jsonify({'error': 'Authentication required'}), 401
    principal = authenticate_token(token)
    if principal is None:
        return jsonify({'error': 'Invalid or expired token'}), 401
    g.principal = principal
    request.current_user = principal
    return None

def authenticate_token(token):
    """Resolve a token to one current, flat principal dictionary."""
    credential, result = _settings_manager.inspect_credential(token)
    diagnostic_credential = credential
    if diagnostic_credential is None and result == 'expired':
        diagnostic_credential = _settings_manager.get_credential_record(token)
    if diagnostic_credential and diagnostic_credential['principal_type'] == 'device':
        device = _settings_manager.get_principal(
            'device', diagnostic_credential['principal_id']
        )
        _device_activity(
            'used', device.get('mac') if device else '', token,
            result if device else 'not_found',
        )
    if result != 'success' or not credential:
        return None
    principal = _settings_manager.get_principal(
        credential['principal_type'], credential['principal_id']
    )
    if principal is None:
        return None
    principal['credential_id'] = credential['id']
    principal['credential_expires_at'] = credential.get('expires_at')
    return principal

def _normalize_mac(value):
    compact = ''.join(character for character in (value or '') if character.isalnum())
    compact = compact.upper()
    return ':'.join(compact[index:index + 2] for index in range(0, 12, 2))

def _device_activity(action, mac_address, token, result):
    log.info(
        'time=%s action=%s route=%s mac_address=%s token=%s result=%s',
        datetime.now(timezone.utc).isoformat(), action,
        request.path if has_request_context() else '', mac_address or '', token or '', result,
    )

def is_mac_registered(mac_address):
    """Return whether a MAC has an existing unexpired device credential."""
    channel = _settings_manager.get_channel_by_mac(_normalize_mac(mac_address)) if mac_address else None
    return bool(channel and _settings_manager.has_current_credential('device', channel['id']))

def generate_token(mac_address, expiry_hours=None):
    """Issue an additional device credential for an existing channel."""
    channel = _settings_manager.get_channel_by_mac(_normalize_mac(mac_address))
    if not channel:
        _device_activity('issued', mac_address, '', 'not_found')
        return None, None
    replaced = _settings_manager.delete_expired_credentials(
        principal_type='device', principal_id=channel['id']
    )
    expires_at = datetime.now(timezone.utc) + timedelta(
        hours=expiry_hours if expiry_hours is not None else TOKEN_EXPIRY_HOURS
    )
    token, _ = _settings_manager.issue_credential(
        'device', str(channel['id']), expires_at.isoformat()
    )
    _device_activity('replaced' if replaced else 'issued', mac_address, token, 'success')
    return token, expires_at.isoformat()

def get_mac_for_token(token, expected_mac=None):
    """Resolve a device token and log the diagnostic result."""
    credential, result = _settings_manager.inspect_credential(token)
    mac_address = expected_mac or ''
    if credential and credential['principal_type'] == 'device':
        principal = _settings_manager.get_principal('device', credential['principal_id'])
        if principal:
            actual_mac = principal.get('mac') or ''
            if expected_mac and _normalize_mac(actual_mac) != _normalize_mac(expected_mac):
                _device_activity('used', expected_mac, token, 'mismatch')
                return None
            _device_activity('used', actual_mac, token, 'success')
            return actual_mac
        result = 'not_found'
    elif credential:
        result = 'mismatch'
    _device_activity('used', mac_address, token, result)
    return None
