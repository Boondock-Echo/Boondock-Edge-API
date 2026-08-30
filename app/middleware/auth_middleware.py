# app/middleware/auth_middleware.py
from functools import wraps
from flask import request, jsonify
from ..utils.auth import get_valid_token_data
import logging

log = logging.getLogger(__name__)

def _bearer_from_request():
    raw = request.headers.get('Authorization') or request.headers.get('authorization') or ''
    raw = (raw or '').strip()
    if raw.lower().startswith('bearer '):
        return raw[7:].strip()
    return raw or None


def require_auth(f):
    """Decorator to require authentication for a route."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        token = _bearer_from_request()

        if not token:
            return jsonify({'error': 'Authentication required'}), 401

        # Single atomic lookup — avoids TOCTOU between existence check and data read
        token_data = get_valid_token_data(token)
        if token_data is None:
            return jsonify({'error': 'Invalid or expired token'}), 401

        request.current_user = {
            'email': token_data.get('email'),
            'role': token_data.get('role', 'member')
        }

        return f(*args, **kwargs)
    return decorated_function

def require_admin(f):
    """Decorator to require admin role."""
    @wraps(f)
    @require_auth
    def decorated_function(*args, **kwargs):
        if request.current_user.get('role') != 'admin':
            return jsonify({'error': 'Admin access required'}), 403
        return f(*args, **kwargs)
    return decorated_function

