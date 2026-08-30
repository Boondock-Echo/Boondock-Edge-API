"""
API key authentication middleware for the External REST API.

Validates a long-lived API key presented either as:
  - ``Authorization: Bearer <api_key>``  (preferred), or
  - ``X-API-Key: <api_key>``

On success the decorated handler can read ``request.api_client``
({id, name, scopes}). On failure it returns an RFC 7807 problem response
with a ``WWW-Authenticate`` challenge header.
"""
from functools import wraps

from flask import request

from ..services.api_key_manager import get_api_key_manager
from ..utils.api_responses import problem


def _extract_api_key():
    auth = (request.headers.get("Authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    x_key = (request.headers.get("X-API-Key") or "").strip()
    return x_key or None


def require_api_key(required_scope=None):
    """
    Decorator factory enforcing a valid API key (and optional scope).

    Usage:
        @require_api_key()                          # any valid key
        @require_api_key("transcriptions:read")     # key must hold this scope
    """
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            api_key = _extract_api_key()
            if not api_key:
                resp = problem(
                    401,
                    "Authentication required",
                    "Provide an API key via the 'Authorization: Bearer <key>' header.",
                    error_type="missing-api-key",
                )
                resp.headers["WWW-Authenticate"] = 'Bearer realm="Boondock External API"'
                return resp

            client = get_api_key_manager().validate_key(api_key)
            if client is None:
                resp = problem(
                    401,
                    "Invalid API key",
                    "The API key is missing, revoked, or expired.",
                    error_type="invalid-api-key",
                )
                resp.headers["WWW-Authenticate"] = 'Bearer realm="Boondock External API"'
                return resp

            if required_scope and required_scope not in client.get("scopes", []):
                return problem(
                    403,
                    "Insufficient scope",
                    f"This API key is not authorized for '{required_scope}'.",
                    error_type="insufficient-scope",
                    required_scope=required_scope,
                )

            request.api_client = client
            return f(*args, **kwargs)
        return wrapper
    return decorator
