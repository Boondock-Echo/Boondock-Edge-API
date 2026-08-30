"""
Authentication and MFA routes.
Handles user login, logout, token verification, and MFA operations.
"""
import json
import os
import logging
import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from flask import Blueprint, jsonify, request
from flasgger import swag_from

from ..utils.logging_setup import error_logger
from ..utils.auth import (
    is_token_valid,
    VALID_TOKENS,
    save_tokens,
    delete_expired_tokens,
)
from ..utils.password_utils import hash_password, verify_password, is_md5_hash, verify_and_migrate
from ..utils.mfa_utils import generate_mfa_secret, generate_mfa_qr_code, verify_totp_code, get_totp_uri
from ..utils.profile_utils import (
    get_user_profile,
    get_all_features,
)
from ..routes.route_utils import init_users
from ..services.settings_manager import get_settings_manager

_settings_manager = get_settings_manager()

auth_bp = Blueprint('auth', __name__)
mfa_bp = Blueprint('mfa', __name__)


@auth_bp.route('/login', methods=['POST'])
@swag_from({
    'tags': ['Authentication'],
    'summary': 'User login',
    'parameters': [
        {
            'name': 'body',
            'in': 'body',
            'required': True,
            'schema': {
                'type': 'object',
                'required': ['email', 'password'],
                'properties': {
                    'email': {'type': 'string'},
                    'password': {'type': 'string'}
                }
            }
        }
    ],
    'responses': {
        '200': {'description': 'Login successful'},
        '401': {'description': 'Invalid credentials'},
        '500': {'description': 'Server error'}
    }
})
def login():
    """Authenticate user and return session token."""
    try:
        data = request.get_json()
        email = data.get('email', '').strip().lower()
        password = data.get('password', '')
        
        if not email or not password:
            return jsonify({'error': 'Email and password required'}), 400
        
        # Load users
        init_users()
        users = _settings_manager.get_all_users()
        
        if email not in users:
            # Don't reveal if user exists (prevent user enumeration)
            return jsonify({'error': 'Invalid credentials'}), 401
        
        user = users[email]
        stored_password = user.get('password', '')
        
        # Handle migration from MD5 to bcrypt
        if is_md5_hash(stored_password):
            # Legacy MD5 password - verify and upgrade
            md5_hash = hashlib.md5(password.encode('utf-8')).hexdigest()
            if md5_hash == stored_password:
                # Upgrade to bcrypt
                user['password'] = hash_password(password)
                _settings_manager.save_user(email, user)
                logging.info(f"Upgraded password for user {email} from MD5 to bcrypt")
            else:
                return jsonify({'error': 'Invalid credentials'}), 401
        else:
            # Verify bcrypt password
            if not verify_password(password, stored_password):
                return jsonify({'error': 'Invalid credentials'}), 401
        
        # Check if MFA is enabled
        mfa_enabled = user.get('mfa_enabled', False)
        mfa_secret = user.get('mfa_secret', '')
        
        # If MFA is enabled, require TOTP code
        if mfa_enabled and mfa_secret:
            totp_code = data.get('totp_code', '')
            if not totp_code:
                return jsonify({
                    'error': 'MFA code required',
                    'mfa_required': True
                }), 401
            
            if not verify_totp_code(mfa_secret, totp_code):
                return jsonify({
                    'error': 'Invalid MFA code',
                    'mfa_required': True
                }), 401
        
        # Get device info for tracking
        device_info = {
            'user_agent': request.headers.get('User-Agent', 'Unknown'),
            'ip_address': request.remote_addr or request.headers.get('X-Forwarded-For', 'Unknown'),
            'login_time': datetime.now().isoformat()
        }
        
        # Generate secure session token.
        # Use timezone-aware UTC so the stored expiry is consistent with how
        # auth.get_valid_token_data() and the DB cleanup interpret it. Using a
        # naive datetime.now() here mixed local time with UTC comparisons and
        # made session lifetimes depend on the server's timezone offset.
        now_utc = datetime.now(timezone.utc)
        session_token = secrets.token_urlsafe(32)
        expires_at = now_utc + timedelta(hours=24)  # 24 hour session

        # Store session using the existing token system
        VALID_TOKENS[session_token] = {
            'email': email,
            'user_id': email,
            'role': user.get('role', 'member'),
            'created_at': now_utc.isoformat(),
            'expires_at': expires_at.isoformat(),
            'last_activity': now_utc.isoformat(),
            'device_info': device_info
        }
        save_tokens()
        delete_expired_tokens()

        # Update user's login history and devices
        if 'login_history' not in user:
            user['login_history'] = []
        if 'devices' not in user:
            user['devices'] = []
        
        # Generate device identifier from user agent and IP
        device_fingerprint = f"{device_info['user_agent']}{device_info['ip_address']}"
        device_id = hashlib.md5(device_fingerprint.encode()).hexdigest()[:16]
        
        # Add to login history (keep last 50)
        user['login_history'].insert(0, {
            'timestamp': datetime.now().isoformat(),
            'ip_address': device_info['ip_address'],
            'user_agent': device_info['user_agent'],
            'device_id': device_id
        })
        if len(user['login_history']) > 50:
            user['login_history'] = user['login_history'][:50]
        
        # Track device (if not already tracked)
        device_exists = any(d.get('device_id') == device_id for d in user['devices'])
        if not device_exists:
            # Try to extract device name from user agent
            user_agent = device_info['user_agent']
            device_name = "Unknown Device"
            if 'Windows' in user_agent:
                device_name = "Windows Device"
            elif 'Mac' in user_agent or 'MacOS' in user_agent:
                device_name = "Mac Device"
            elif 'Linux' in user_agent:
                device_name = "Linux Device"
            elif 'Android' in user_agent:
                device_name = "Android Device"
            elif 'iPhone' in user_agent or 'iPad' in user_agent:
                device_name = "iOS Device"
            
            user['devices'].append({
                'device_id': device_id,
                'user_agent': device_info['user_agent'],
                'ip_address': device_info['ip_address'],
                'first_seen': datetime.now().isoformat(),
                'last_seen': datetime.now().isoformat(),
                'name': device_name
            })
        else:
            # Update last seen
            for device in user['devices']:
                if device.get('device_id') == device_id:
                    device['last_seen'] = datetime.now().isoformat()
                    device['ip_address'] = device_info['ip_address']
                    break
        
        # Save updated user data
        _settings_manager.save_user(email, user)
        
        # Check if MFA is enforced but not enabled
        mfa_enforced = user.get('mfa_enforced', False)
        show_mfa_reminder = mfa_enforced and not mfa_enabled
        
        # Get user profile and permissions
        user_profile = get_user_profile(email, users)
        user_permissions = user_profile.get('features', {}) if user_profile else {}
        
        # Admin role always has all permissions
        if user.get('role') == 'admin':
            user_permissions = {f['key']: True for f in get_all_features()}
        
        # Return user info (without password and MFA secret)
        return jsonify({
            'token': session_token,
            'expires_at': expires_at.isoformat(),
            'user': {
                'email': email,
                'name': user.get('name', email),
                'role': user.get('role', 'member'),
                'status': user.get('status', 'Active'),
                'profile': user.get('profile', 'Default'),
                'mfa_enabled': mfa_enabled,
                'mfa_enforced': mfa_enforced,
                'permissions': user_permissions
            },
            'show_mfa_reminder': show_mfa_reminder
        }), 200
        
    except Exception as e:
        logging.error(f"Login error: {e}")
        return jsonify({'error': 'Internal server error'}), 500

@auth_bp.route('/logout', methods=['POST'])
@swag_from({
    'tags': ['Authentication'],
    'summary': 'User logout',
    'responses': {
        '200': {'description': 'Logged out successfully'},
        '500': {'description': 'Server error'}
    }
})
def logout():
    """Invalidate session token."""
    try:
        token = request.headers.get('Authorization', '').replace('Bearer ', '')
        if token and token in VALID_TOKENS:
            del VALID_TOKENS[token]
            save_tokens()
        return jsonify({'message': 'Logged out successfully'}), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@auth_bp.route('/verify', methods=['GET'])
@swag_from({
    'tags': ['Authentication'],
    'summary': 'Verify authentication token',
    'responses': {
        '200': {'description': 'Token is valid'},
        '401': {'description': 'Token is invalid or expired'},
        '500': {'description': 'Server error'}
    }
})
def verify_token():
    """Verify if a token is valid."""
    try:
        token = request.headers.get('Authorization', '').replace('Bearer ', '')
        if not token:
            return jsonify({'valid': False}), 401
        
        if is_token_valid(token):
            token_data = VALID_TOKENS[token]
            # Update last activity
            token_data['last_activity'] = datetime.now().isoformat()
            save_tokens()
            
            return jsonify({
                'valid': True,
                'user': {
                    'email': token_data.get('email'),
                    'role': token_data.get('role')
                }
            }), 200
        else:
            return jsonify({'valid': False}), 401
    except Exception as e:
        return jsonify({'valid': False, 'error': str(e)}), 401

@mfa_bp.route('/setup', methods=['POST'])
@swag_from({
    'tags': ['MFA'],
    'summary': 'Generate MFA secret and QR code for user',
    'responses': {
        '200': {'description': 'MFA setup data returned'},
        '401': {'description': 'Unauthorized'},
        '500': {'description': 'Server error'}
    }
})
def mfa_setup():
    """Generate MFA secret and QR code for the authenticated user."""
    try:
        token = request.headers.get('Authorization', '').replace('Bearer ', '')
        if not token or not is_token_valid(token):
            return jsonify({'error': 'Unauthorized'}), 401
        
        token_data = VALID_TOKENS.get(token, {})
        email = token_data.get('email')
        
        if not email:
            return jsonify({'error': 'User not found'}), 404
        
        # Load users
        init_users()
        users = _settings_manager.get_all_users()
        
        if email not in users:
            return jsonify({'error': 'User not found'}), 404
        
        user = users[email]
        
        # Generate new secret
        secret = generate_mfa_secret()
        
        # Generate QR code
        qr_code = generate_mfa_qr_code(secret, email)
        totp_uri = get_totp_uri(secret, email)
        
        # Store temporary secret (not enabled yet)
        user['mfa_secret_temp'] = secret
        
        # Save user
        _settings_manager.save_user(email, user)
        
        return jsonify({
            'secret': secret,
            'qr_code': qr_code,
            'totp_uri': totp_uri
        }), 200
        
    except Exception as e:
        logging.error(f"MFA setup error: {e}")
        return jsonify({'error': 'Internal server error'}), 500

@mfa_bp.route('/verify-setup', methods=['POST'])
@swag_from({
    'tags': ['MFA'],
    'summary': 'Verify MFA setup with TOTP code',
    'parameters': [
        {
            'name': 'body',
            'in': 'body',
            'required': True,
            'schema': {
                'type': 'object',
                'required': ['totp_code'],
                'properties': {
                    'totp_code': {'type': 'string'}
                }
            }
        }
    ],
    'responses': {
        '200': {'description': 'MFA verified and enabled'},
        '400': {'description': 'Invalid code'},
        '401': {'description': 'Unauthorized'},
        '500': {'description': 'Server error'}
    }
})
def mfa_verify_setup():
    """Verify MFA setup code and enable MFA for user."""
    try:
        token = request.headers.get('Authorization', '').replace('Bearer ', '')
        if not token or not is_token_valid(token):
            return jsonify({'error': 'Unauthorized'}), 401
        
        data = request.get_json()
        totp_code = data.get('totp_code', '')
        
        if not totp_code:
            return jsonify({'error': 'TOTP code required'}), 400
        
        token_data = VALID_TOKENS.get(token, {})
        email = token_data.get('email')
        
        if not email:
            return jsonify({'error': 'User not found'}), 404
        
        # Load users
        init_users()
        users = _settings_manager.get_all_users()
        
        if email not in users:
            return jsonify({'error': 'User not found'}), 404
        
        user = users[email]
        temp_secret = user.get('mfa_secret_temp', '')
        
        if not temp_secret:
            return jsonify({'error': 'No MFA setup in progress'}), 400
        
        # Verify TOTP code
        if not verify_totp_code(temp_secret, totp_code):
            return jsonify({'error': 'Invalid TOTP code'}), 400
        
        # Enable MFA
        user['mfa_secret'] = temp_secret
        user['mfa_enabled'] = True
        user.pop('mfa_secret_temp', None)  # Remove temporary secret
        
        # Save user
        _settings_manager.save_user(email, user)
        
        return jsonify({'message': 'MFA enabled successfully'}), 200
        
    except Exception as e:
        logging.error(f"MFA verify setup error: {e}")
        return jsonify({'error': 'Internal server error'}), 500

@mfa_bp.route('/disable', methods=['POST'])
@swag_from({
    'tags': ['MFA'],
    'summary': 'Disable MFA for user',
    'parameters': [
        {
            'name': 'body',
            'in': 'body',
            'required': True,
            'schema': {
                'type': 'object',
                'required': ['password', 'totp_code'],
                'properties': {
                    'password': {'type': 'string'},
                    'totp_code': {'type': 'string'}
                }
            }
        }
    ],
    'responses': {
        '200': {'description': 'MFA disabled successfully'},
        '400': {'description': 'Invalid credentials'},
        '401': {'description': 'Unauthorized'},
        '500': {'description': 'Server error'}
    }
})
def mfa_disable():
    """Disable MFA for user (requires password and TOTP code)."""
    try:
        token = request.headers.get('Authorization', '').replace('Bearer ', '')
        if not token or not is_token_valid(token):
            return jsonify({'error': 'Unauthorized'}), 401
        
        data = request.get_json()
        password = data.get('password', '')
        totp_code = data.get('totp_code', '')
        
        token_data = VALID_TOKENS.get(token, {})
        email = token_data.get('email')
        
        if not email:
            return jsonify({'error': 'User not found'}), 404
        
        # Load users
        init_users()
        users = _settings_manager.get_all_users()
        
        if email not in users:
            return jsonify({'error': 'User not found'}), 404
        
        user = users[email]
        
        # Verify password
        stored_password = user.get('password', '')
        if not verify_password(password, stored_password):
            return jsonify({'error': 'Invalid password'}), 400
        
        # Verify TOTP code if MFA is enabled
        if user.get('mfa_enabled', False):
            mfa_secret = user.get('mfa_secret', '')
            if not verify_totp_code(mfa_secret, totp_code):
                return jsonify({'error': 'Invalid TOTP code'}), 400
        
        # Disable MFA
        user['mfa_enabled'] = False
        user.pop('mfa_secret', None)
        user.pop('mfa_secret_temp', None)
        
        # Save user
        _settings_manager.save_user(email, user)
        
        return jsonify({'message': 'MFA disabled successfully'}), 200
        
    except Exception as e:
        logging.error(f"MFA disable error: {e}")
        return jsonify({'error': 'Internal server error'}), 500

@mfa_bp.route('/status', methods=['GET'])
@swag_from({
    'tags': ['MFA'],
    'summary': 'Get MFA status for user',
    'responses': {
        '200': {'description': 'MFA status returned'},
        '401': {'description': 'Unauthorized'},
        '500': {'description': 'Server error'}
    }
})
def mfa_status():
    """Get MFA status for the authenticated user."""
    try:
        token = request.headers.get('Authorization', '').replace('Bearer ', '')
        if not token or not is_token_valid(token):
            # Return default MFA status for unauthenticated requests
            # Dashboard should handle this and redirect to login
            return jsonify({
                'mfa_enabled': False,
                'has_secret': False,
                'mfa_enforced': False,
                'authenticated': False
            }), 401
        
        token_data = VALID_TOKENS.get(token, {})
        email = token_data.get('email')
        
        if not email:
            return jsonify({'error': 'User not found'}), 404
        
        # Load users
        init_users()
        users = _settings_manager.get_all_users()
        
        if email not in users:
            return jsonify({'error': 'User not found'}), 404
        
        user = users[email]
        
        return jsonify({
            'mfa_enabled': user.get('mfa_enabled', False),
            'has_secret': bool(user.get('mfa_secret', '')),
            'mfa_enforced': user.get('mfa_enforced', False),
            'authenticated': True
        }), 200
        
    except Exception as e:
        logging.error(f"MFA status error: {e}")
        return jsonify({'error': 'Internal server error'}), 500

