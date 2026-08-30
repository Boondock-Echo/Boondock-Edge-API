"""
External REST API (v1).

A standardized, versioned integration surface for third-party companies.

Authentication
--------------
* Data endpoints are authenticated with a long-lived **API key**, presented as
  ``Authorization: Bearer <api_key>`` (RFC 6750 style).
* API key *management* endpoints are restricted to platform administrators and
  use the app's normal admin session token.

Conventions
-----------
* JSON request/response bodies, ``snake_case`` fields.
* Errors follow RFC 7807 (``application/problem+json``).
* List endpoints return ``{ "data": [...], "pagination": {...} }``.
"""
import logging
import sqlite3
from datetime import datetime, timedelta, timezone

from flask import Blueprint, jsonify, request
from flasgger import swag_from

from ..middleware.auth_middleware import require_admin
from ..middleware.api_key_auth import require_api_key
from ..services.api_key_manager import get_api_key_manager
from ..utils.api_responses import problem, build_pagination
from ..routes.route_utils import DB_PATH

logger = logging.getLogger(__name__)

external_api_bp = Blueprint('external_api', __name__)

DEFAULT_PER_PAGE = 50
MAX_PER_PAGE = 200

# Full catalog of API key scopes. Keys may include any subset.
# ``enforced`` means the backend currently checks this scope on an endpoint.
SCOPE_CATALOG = [
    {
        'id': 'transcriptions:read',
        'label': 'Read transcriptions',
        'description': 'List and search transcription text via GET /api/v1/transcriptions.',
        'group': 'Transcriptions',
        'enforced': True,
    },
    {
        'id': 'transcriptions:write',
        'label': 'Write transcriptions',
        'description': 'Create or update transcription text for recordings.',
        'group': 'Transcriptions',
        'enforced': False,
    },
    {
        'id': 'recordings:read',
        'label': 'Read recordings',
        'description': 'List recording metadata, inbox, and status.',
        'group': 'Recordings',
        'enforced': False,
    },
    {
        'id': 'recordings:write',
        'label': 'Upload recordings',
        'description': 'Upload audio files and queue them for processing.',
        'group': 'Recordings',
        'enforced': False,
    },
    {
        'id': 'audio:read',
        'label': 'Download audio',
        'description': 'Download recording audio files by id or path.',
        'group': 'Audio',
        'enforced': False,
    },
    {
        'id': 'channels:read',
        'label': 'Read channels',
        'description': 'List channels, stations, and channel details.',
        'group': 'Channels',
        'enforced': False,
    },
    {
        'id': 'channels:write',
        'label': 'Manage channels',
        'description': 'Create or update channels and related settings.',
        'group': 'Channels',
        'enforced': False,
    },
    {
        'id': 'devices:read',
        'label': 'Read devices',
        'description': 'List Edge recorders and device status.',
        'group': 'Devices',
        'enforced': False,
    },
    {
        'id': 'devices:write',
        'label': 'Manage devices',
        'description': 'Register or update device configuration.',
        'group': 'Devices',
        'enforced': False,
    },
    {
        'id': 'queue:read',
        'label': 'Read queue',
        'description': 'View transcription queue status and logs.',
        'group': 'Queue',
        'enforced': False,
    },
    {
        'id': 'queue:write',
        'label': 'Manage queue',
        'description': 'Start, stop, requeue, or purge transcription jobs.',
        'group': 'Queue',
        'enforced': False,
    },
    {
        'id': 'settings:read',
        'label': 'Read settings',
        'description': 'Read non-sensitive global configuration values.',
        'group': 'Settings',
        'enforced': False,
    },
]

ALLOWED_SCOPES = [s['id'] for s in SCOPE_CATALOG]
DEFAULT_SCOPES = ['transcriptions:read']

# When a caller does not specify an expiry, keys default to this many days.
# Lifetime (non-expiring) keys are still allowed, but must be requested
# explicitly via "never_expires": true.
DEFAULT_KEY_TTL_DAYS = 90


def _resolve_expires_at(data):
    """
    Decide the expiry for a new key from the request body.

    Rules:
      * ``never_expires: true``                -> lifetime key (None).
      * ``expires_at`` provided (ISO-8601)     -> that instant (normalised to UTC).
      * neither provided                        -> now + DEFAULT_KEY_TTL_DAYS.

    Returns a tuple ``(expires_at_iso_or_none, error)`` where ``error`` is a
    ready-to-return problem response (or ``None`` on success).
    """
    if data.get('never_expires') is True:
        return None, None

    raw = data.get('expires_at')
    if raw not in (None, ''):
        try:
            exp = datetime.fromisoformat(str(raw).replace('Z', '+00:00'))
        except (ValueError, TypeError):
            return None, problem(
                400, 'Validation failed',
                "'expires_at' must be an ISO-8601 timestamp.",
                error_type='validation-error',
                invalid_params=[{'name': 'expires_at', 'reason': 'invalid format'}])
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if exp <= datetime.now(timezone.utc):
            return None, problem(
                400, 'Validation failed',
                "'expires_at' must be in the future.",
                error_type='validation-error',
                invalid_params=[{'name': 'expires_at', 'reason': 'must be in the future'}])
        return exp.astimezone(timezone.utc).isoformat(), None

    # No expiry specified -> apply the default TTL.
    default_exp = datetime.now(timezone.utc) + timedelta(days=DEFAULT_KEY_TTL_DAYS)
    return default_exp.isoformat(), None

# Ensure the api_keys table exists as soon as this blueprint is imported.
get_api_key_manager()


# =====================================================================
# Scope catalog (admin)
# =====================================================================

@external_api_bp.route('/v1/scopes', methods=['GET'])
@require_admin
@swag_from({
    'tags': ['API Keys (admin)'],
    'summary': 'List all available API key scopes',
    'description': 'Returns the full catalog of scopes that can be assigned to an API key.',
    'security': [{'BearerAuth': []}],
    'responses': {
        '200': {'description': 'Scope catalog'},
        '401': {'description': 'Admin authentication required'},
        '403': {'description': 'Admin access required'}
    }
})
def list_scopes():
    """Return the full API key scope catalog."""
    return jsonify({
        'data': SCOPE_CATALOG,
        'total_items': len(SCOPE_CATALOG),
        'default_scopes': list(DEFAULT_SCOPES),
    }), 200


# =====================================================================
# API key management (admin only)
# =====================================================================

@external_api_bp.route('/v1/api-keys', methods=['POST'])
@require_admin
@swag_from({
    'tags': ['API Keys (admin)'],
    'summary': 'Create an API key for an external integrator',
    'description': 'Issues a long-lived API key. The full key is returned **once** '
                   'in this response and cannot be retrieved again. Admin session required. '
                   f'Allowed scopes: {ALLOWED_SCOPES}.',
    'security': [{'BearerAuth': []}],
    'parameters': [{
        'name': 'body',
        'in': 'body',
        'required': True,
        'schema': {
            'type': 'object',
            'required': ['name'],
            'properties': {
                'name': {'type': 'string', 'example': 'Acme Corp integration'},
                'scopes': {
                    'type': 'array',
                    'items': {'type': 'string', 'enum': ALLOWED_SCOPES},
                    'example': ['transcriptions:read']
                },
                'expires_at': {
                    'type': 'string',
                    'description': f'Optional ISO-8601 expiry. If omitted, the key '
                                   f'defaults to {DEFAULT_KEY_TTL_DAYS} days. '
                                   f'For a non-expiring key, send "never_expires": true.',
                    'example': '2027-01-01T00:00:00Z'
                },
                'never_expires': {
                    'type': 'boolean',
                    'description': 'Set true to create a lifetime (non-expiring) key. '
                                   'Overrides expires_at.',
                    'example': False
                }
            }
        }
    }],
    'responses': {
        '201': {'description': 'API key created (includes the one-time plaintext key)'},
        '400': {'description': 'Validation error (application/problem+json)'},
        '401': {'description': 'Admin authentication required'},
        '403': {'description': 'Admin access required'}
    }
})
def create_api_key():
    """Create and return a new API key (plaintext shown once)."""
    data = request.get_json(silent=True) or {}
    name = (data.get('name') or '').strip()
    if not name:
        return problem(400, 'Validation failed', "Field 'name' is required.",
                       error_type='validation-error',
                       invalid_params=[{'name': 'name', 'reason': 'required'}])

    scopes = data.get('scopes') if data.get('scopes') is not None else list(DEFAULT_SCOPES)
    if not isinstance(scopes, list) or len(scopes) == 0:
        return problem(400, 'Validation failed',
                       "Field 'scopes' must be a non-empty array.",
                       error_type='validation-error',
                       invalid_params=[{'name': 'scopes', 'reason': 'required'}])
    # Deduplicate while preserving order
    seen = set()
    scopes = [s for s in scopes if not (s in seen or seen.add(s))]
    if any(s not in ALLOWED_SCOPES for s in scopes):
        return problem(400, 'Validation failed',
                       f"'scopes' must be a subset of {ALLOWED_SCOPES}.",
                       error_type='validation-error',
                       invalid_params=[{'name': 'scopes', 'reason': 'unknown scope'}])

    expires_at, expiry_error = _resolve_expires_at(data)
    if expiry_error is not None:
        return expiry_error

    created_by = getattr(request, 'current_user', {}).get('email')

    metadata, raw_key = get_api_key_manager().create_key(
        name=name, scopes=scopes, created_by=created_by, expires_at=expires_at
    )

    body = dict(metadata)
    body['api_key'] = raw_key  # one-time plaintext
    body['warning'] = 'Store this key now. It will not be shown again.'
    response = jsonify(body)
    response.status_code = 201
    return response


@external_api_bp.route('/v1/api-keys', methods=['GET'])
@require_admin
@swag_from({
    'tags': ['API Keys (admin)'],
    'summary': 'List issued API keys (metadata only)',
    'security': [{'BearerAuth': []}],
    'responses': {
        '200': {'description': 'List of API key metadata (no plaintext keys)'},
        '401': {'description': 'Admin authentication required'},
        '403': {'description': 'Admin access required'}
    }
})
def list_api_keys():
    """List API key metadata. Plaintext keys are never returned here."""
    keys = get_api_key_manager().list_keys()
    return jsonify({'data': keys, 'total_items': len(keys)}), 200


@external_api_bp.route('/v1/api-keys/<key_id>', methods=['DELETE'])
@require_admin
@swag_from({
    'tags': ['API Keys (admin)'],
    'summary': 'Revoke an API key',
    'security': [{'BearerAuth': []}],
    'parameters': [{
        'name': 'key_id', 'in': 'path', 'type': 'string', 'required': True
    }],
    'responses': {
        '204': {'description': 'Key revoked'},
        '404': {'description': 'Key not found or already revoked'},
        '401': {'description': 'Admin authentication required'},
        '403': {'description': 'Admin access required'}
    }
})
def revoke_api_key(key_id):
    """Revoke an API key by id."""
    revoked = get_api_key_manager().revoke_key(key_id)
    if not revoked:
        return problem(404, 'Not found',
                       'No active API key with that id exists.',
                       error_type='resource-not-found')
    return '', 204


# =====================================================================
# Transcriptions
# =====================================================================

@external_api_bp.route('/v1/transcriptions', methods=['GET'])
@require_api_key('transcriptions:read')
@swag_from({
    'tags': ['External API'],
    'summary': 'List transcriptions (paginated)',
    'description': 'Returns a paginated list of transcriptions, newest first. '
                   'Requires an API key with scope `transcriptions:read` '
                   '(Authorization: Bearer bk_live_… or X-API-Key).',
    'security': [{'ApiKeyAuth': []}],
    'produces': ['application/json', 'application/problem+json'],
    'parameters': [
        {'name': 'page', 'in': 'query', 'type': 'integer', 'required': False,
         'default': 1, 'description': '1-based page number'},
        {'name': 'per_page', 'in': 'query', 'type': 'integer', 'required': False,
         'default': DEFAULT_PER_PAGE, 'description': f'Items per page (max {MAX_PER_PAGE})'},
        {'name': 'channel_id', 'in': 'query', 'type': 'integer', 'required': False,
         'description': 'Filter by channel id'},
        {'name': 'search', 'in': 'query', 'type': 'string', 'required': False,
         'description': 'Case-insensitive substring match on transcription text'},
    ],
    'responses': {
        '200': {
            'description': 'Paginated list of transcriptions',
            'examples': {
                'application/json': {
                    'data': [{
                        'id': 123, 'channel_id': 1,
                        'filename': 'recordings/.../2026-06-26-10-06-41.wav',
                        'timestamp': '20260626_100641',
                        'transcription': 'example text',
                        'status': 'transcribed', 'is_duplicate': False,
                        'duration': 16.6, 'filesize': 265260
                    }],
                    'pagination': {
                        'page': 1, 'per_page': 50, 'total_items': 1240,
                        'total_pages': 25, 'has_next': True, 'has_prev': False,
                        'next_page': 2, 'prev_page': None
                    }
                }
            }
        },
        '400': {'description': 'Invalid query parameter (application/problem+json)'},
        '401': {'description': 'Missing/invalid API key (application/problem+json)'},
        '403': {'description': 'API key lacks required scope (application/problem+json)'},
        '500': {'description': 'Server error (application/problem+json)'}
    }
})
def list_transcriptions():
    """Return a paginated list of transcriptions for the authenticated client."""
    # -- pagination params --
    try:
        page = int(request.args.get('page', 1))
    except (TypeError, ValueError):
        return problem(400, 'Invalid parameter', "'page' must be an integer.",
                       error_type='validation-error')
    try:
        per_page = int(request.args.get('per_page', DEFAULT_PER_PAGE))
    except (TypeError, ValueError):
        return problem(400, 'Invalid parameter', "'per_page' must be an integer.",
                       error_type='validation-error')

    page = max(page, 1)
    per_page = max(1, min(per_page, MAX_PER_PAGE))
    offset = (page - 1) * per_page

    # -- filters --
    filters = ["transcription IS NOT NULL", "transcription != ''"]
    params = []

    channel_id = request.args.get('channel_id')
    if channel_id not in (None, ''):
        try:
            filters.append("channel_id = ?")
            params.append(int(channel_id))
        except (TypeError, ValueError):
            return problem(400, 'Invalid parameter',
                           "'channel_id' must be an integer.",
                           error_type='validation-error')

    search = (request.args.get('search') or '').strip()
    if search:
        filters.append("LOWER(transcription) LIKE ?")
        params.append(f"%{search.lower()}%")

    where_sql = f"WHERE {' AND '.join(filters)}" if filters else ""

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.cursor()
        cur.execute(f"SELECT COUNT(*) FROM recordings {where_sql}", params)
        total_items = cur.fetchone()[0]

        cur.execute(
            f"""
            SELECT id, channel_id, filename, timestamp, transcription,
                   status, is_duplicate, duration, filesize
            FROM recordings
            {where_sql}
            ORDER BY timestamp DESC, id DESC
            LIMIT ? OFFSET ?
            """,
            params + [per_page, offset]
        )
        rows = cur.fetchall()

        data = [{
            'id': row['id'],
            'channel_id': row['channel_id'],
            'filename': row['filename'],
            'timestamp': row['timestamp'],
            'transcription': row['transcription'],
            'status': row['status'],
            'is_duplicate': bool(row['is_duplicate']),
            'duration': row['duration'],
            'filesize': row['filesize'],
        } for row in rows]

        return jsonify({
            'data': data,
            'pagination': build_pagination(page, per_page, total_items)
        }), 200

    except sqlite3.Error as e:
        logger.error(f"Database error listing transcriptions: {e}")
        return problem(500, 'Internal server error',
                       'Failed to query transcriptions.',
                       error_type='internal-error')
    finally:
        conn.close()
