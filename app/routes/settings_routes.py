"""
Settings and keywords management routes.
Handles global settings CRUD operations and keyword management.
"""
import json
import logging
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from flask import Blueprint, jsonify, request
from flasgger import swag_from
import pytz

from ..routes.route_utils import init_settings
from ..services.settings_manager import get_settings_manager
from ..services.db_logging_manager import LOGS_DB_PATH
import subprocess
import sys
from config import Config, CODE_ROOT

_settings_manager = get_settings_manager()

settings_bp = Blueprint('settings', __name__)

SUMMARY_METRICS_CACHE_TTL_SECONDS = 600  # 10 minutes
_summary_metrics_cache_lock = threading.Lock()
_summary_metrics_cache = {}


@settings_bp.route('/settings', methods=['GET'])
@swag_from({
    'tags': ['Settings'],
    'summary': 'Get all settings',
    'responses': {
        '200': {'description': 'Settings retrieved successfully'},
        '500': {'description': 'Server error'}
    }
})
def get_settings():
    """Fetch all settings."""
    init_settings()
    try:
        settings = _settings_manager.get_all_settings()

        # Mask secret keys for security (don't send actual values to the dashboard)
        # Note: We don't mask host_password because it's needed for Auto Config functionality
        # The password field is already a password input type which provides some security
        if 's3_access_key' in settings and settings['s3_access_key']:
            settings['s3_access_key'] = '***' if settings['s3_access_key'] else ''
        if 's3_secret_key' in settings and settings['s3_secret_key']:
            settings['s3_secret_key'] = '***' if settings['s3_secret_key'] else ''
        # Always mask Samba password when returning to the dashboard
        if 'samba_password' in settings and settings['samba_password']:
            settings['samba_password'] = '***'
        if settings.get('global_transcription_api_key'):
            settings['global_transcription_api_key'] = '***'

        return jsonify(settings)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


def _resolve_timezone_name(explicit_timezone=None):
    """Return a valid timezone name from request/settings with safe fallback."""
    tz_name = explicit_timezone or _settings_manager.get_setting('global_timezone', 'UTC') or 'UTC'
    try:
        pytz.timezone(tz_name)
        return tz_name
    except Exception:
        return 'UTC'


def _get_local_day_bounds(timezone_name):
    """Return local-day UTC bounds and local date string for the supplied timezone."""
    tz = pytz.timezone(timezone_name)
    now_utc = datetime.now(timezone.utc)
    local_now = now_utc.astimezone(tz)
    local_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    local_end = local_start + timedelta(days=1)
    return {
        'local_date': local_now.strftime('%Y-%m-%d'),
        'recordings_start_utc': local_start.astimezone(timezone.utc).strftime('%Y%m%d_%H%M%S'),
        'recordings_end_utc': local_end.astimezone(timezone.utc).strftime('%Y%m%d_%H%M%S'),
        'logs_start': local_start.strftime('%Y-%m-%d %H:%M:%S'),
        'logs_end': local_end.strftime('%Y-%m-%d %H:%M:%S'),
        'timezone': timezone_name,
    }


def _summary_cache_get(cache_key):
    """Return cached summary payload when still fresh."""
    now_ts = datetime.now(timezone.utc).timestamp()
    with _summary_metrics_cache_lock:
        entry = _summary_metrics_cache.get(cache_key)
        if not entry:
            return None
        cached_at = entry.get('cached_at_ts', 0)
        if now_ts - cached_at > SUMMARY_METRICS_CACHE_TTL_SECONDS:
            return None
        return entry.get('payload')


def _summary_cache_set(cache_key, payload):
    """Store summary payload in memory cache."""
    with _summary_metrics_cache_lock:
        _summary_metrics_cache[cache_key] = {
            'cached_at_ts': datetime.now(timezone.utc).timestamp(),
            'payload': payload,
        }


@settings_bp.route('/settings/summary/metrics', methods=['GET'])
@swag_from({
    'tags': ['Settings'],
    'summary': 'Get lightweight dashboard summary metrics',
    'parameters': [
        {
            'name': 'timezone',
            'in': 'query',
            'type': 'string',
            'required': False,
            'description': 'IANA timezone (defaults to global setting)'
        }
    ],
    'responses': {
        '200': {'description': 'Summary metrics returned successfully'},
        '500': {'description': 'Server error'}
    }
})
def get_summary_metrics():
    """
    Return summary metrics optimized for scale.
    Uses aggregate SQL counts instead of fetching full recordings/log datasets.
    """
    try:
        requested_tz = request.args.get('timezone')
        timezone_name = _resolve_timezone_name(requested_tz)
        force_refresh = str(request.args.get('force_refresh', 'false')).lower() == 'true'
        cache_key = timezone_name

        if not force_refresh:
            cached_payload = _summary_cache_get(cache_key)
            if cached_payload is not None:
                payload = dict(cached_payload)
                payload['is_cached'] = True
                return jsonify(payload), 200

        bounds = _get_local_day_bounds(timezone_name)

        total_recordings = 0
        today_recordings = 0
        try:
            recordings_db = Config.get_recordings_db_path()
            conn = sqlite3.connect(recordings_db, timeout=5.0)
            cur = conn.cursor()
            cur.execute('SELECT COUNT(*) FROM recordings')
            total_recordings = int(cur.fetchone()[0] or 0)
            cur.execute(
                '''
                SELECT COUNT(*)
                FROM recordings
                WHERE timestamp >= ? AND timestamp < ?
                ''',
                (bounds['recordings_start_utc'], bounds['recordings_end_utc'])
            )
            today_recordings = int(cur.fetchone()[0] or 0)
            conn.close()
        except Exception as recordings_error:
            logging.warning(f"Summary recordings query failed: {recordings_error}")

        errors = 0
        warnings = 0
        try:
            conn = sqlite3.connect(LOGS_DB_PATH, timeout=5.0)
            cur = conn.cursor()
            cur.execute(
                "SELECT COUNT(*) FROM errors WHERE timestamp >= ? AND timestamp < ?",
                (bounds['logs_start'], bounds['logs_end'])
            )
            errors = int(cur.fetchone()[0] or 0)
            cur.execute(
                "SELECT COUNT(*) FROM warnings WHERE timestamp >= ? AND timestamp < ?",
                (bounds['logs_start'], bounds['logs_end'])
            )
            warnings = int(cur.fetchone()[0] or 0)
            conn.close()
        except Exception as logs_error:
            logging.warning(f"Summary logs query failed: {logs_error}")

        total_users = 0
        user_logins = 0
        try:
            users = _settings_manager.get_all_users()
            tz = pytz.timezone(timezone_name)
            today_str = bounds['local_date']
            for email, user_data in users.items():
                if not (isinstance(user_data, dict) and (user_data.get('name') or user_data.get('role') or user_data.get('email') or email)):
                    continue
                total_users += 1
                history = user_data.get('login_history', [])
                if not isinstance(history, list):
                    continue
                for login in history:
                    ts = login.get('timestamp') if isinstance(login, dict) else None
                    if not ts:
                        continue
                    try:
                        dt = datetime.fromisoformat(str(ts).replace('Z', '+00:00'))
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=timezone.utc)
                        if dt.astimezone(tz).strftime('%Y-%m-%d') == today_str:
                            user_logins += 1
                    except Exception:
                        continue
        except Exception as users_error:
            logging.warning(f"Summary users query failed: {users_error}")

        payload = {
            'total_recordings': total_recordings,
            'today_recordings': today_recordings,
            'errors': errors,
            'warnings': warnings,
            'user_logins': user_logins,
            'total_users': total_users,
            'timezone': bounds['timezone'],
            'local_date': bounds['local_date'],
            'cached_for_seconds': SUMMARY_METRICS_CACHE_TTL_SECONDS,
            'is_cached': False,
        }
        _summary_cache_set(cache_key, payload)
        return jsonify(payload), 200
    except Exception as e:
        logging.error(f"Error building summary metrics: {e}")
        return jsonify({'error': 'Failed to get summary metrics'}), 500


@settings_bp.route('/settings', methods=['PUT'])
@swag_from({
    'tags': ['Settings'],
    'summary': 'Update settings',
    'parameters': [
        {
            'name': 'body',
            'in': 'body',
            'required': True,
            'schema': {
                'type': 'object',
                'properties': {
                    'global_model': {'type': 'string'},
                    'global_target_language': {'type': 'string'},
                    'global_transcribe_method': {'type': 'string', 'enum': ['local', 'openai']},
                    'global_transcription_api_key': {'type': 'string'},
                    'global_hallucination': {'type': 'string'},
                    'global_timezone': {'type': 'string'},
                    'global_enable_uniden_scanners': {'type': 'string'},
                    'global_enable_edge_devices': {'type': 'string'},
                    'global_enable_usb_audio_devices': {'type': 'string'},
                    'global_enable_s3_upload': {'type': 'string'},
                    'global_live_mode_enabled': {'type': 'string', 'description': 'Enable live mode for automatic message playback'},
                    's3_endpoint_url': {'type': 'string'},
                    's3_access_key': {'type': 'string'},
                    's3_secret_key': {'type': 'string'},
                    's3_region': {'type': 'string'},
                    's3_bucket_name': {'type': 'string'},
                    's3_backup_time': {'type': 'string'}
                }
            }
        }
    ],
    'responses': {
        '200': {'description': 'Settings updated successfully'},
        '400': {'description': 'Bad request'},
        '500': {'description': 'Server error'}
    }
})
def update_settings():
    """Update settings."""
    init_settings()
    try:
        data = request.get_json()

        # Validate required fields
        if not isinstance(data, dict):
            return jsonify({'error': 'Invalid data format'}), 400

        current_settings = _settings_manager.get_all_settings()

        # The API cannot safely change the network that is currently carrying
        # this request. External Wi-Fi details are synchronized from
        # NetworkManager by the hotspot status endpoint instead.
        # requested_wifi_fields = {'host_ssid', 'host_password', 'host_ip'} & data.keys()
        # if requested_wifi_fields:
        #     wifi_change_requested = any(
        #         data[field] not in ('', '***', current_settings[field])
        #         for field in requested_wifi_fields
        #     )
        #     if current_settings['external_wifi'] and current_settings['host_password'] and wifi_change_requested:
        #         return jsonify({
        #             'error': (
        #                 'Wi-Fi settings cannot be changed while the API is using '
        #                 'an external Wi-Fi connection.'
        #             )
        #         }), 409

        # The recorder API port is fixed. Accept an unchanged value because
        # the settings form may submit its entire model, but reject mutations.
        # if 'host_port' in data and str(data['host_port']) != str(current_settings.get('host_port', '4000')):
        #     return jsonify({'error': 'The host port cannot be changed.'}), 400

        # Update fields if provided
        updateable_fields = [
            'global_model',
            'global_target_language',
            'global_transcribe_method',
            'global_transcription_api_key',
            'global_hallucination',
            'global_timezone',
            # Inbox / live communications behaviour
            'global_inbox_view_mode',
            'global_inbox_records_per_page',
            'global_enable_uniden_scanners',
            'global_enable_edge_devices',
            'global_enable_usb_audio_devices',
            'global_enable_s3_upload',
            'global_show_duplicate_files',  # Display duplicates in inbox
            'global_live_mode_enabled',  # Live mode for automatic message playback
            's3_endpoint_url',
            's3_access_key',
            's3_secret_key',
            's3_region',
            's3_bucket_name',
            's3_backup_time',
            # Maintenance settings
            'maintenance_time',
            'maintenance_enabled_tasks',
            # Samba / network share backup settings
            'samba_backup_enabled',
            'samba_share_path',
            'samba_username',
            'samba_password',
            'host_ssid',
            'host_password',
            'host_ip',
        ]

        if ('global_transcribe_method' in data and
                data['global_transcribe_method'] not in {'local', 'openai'}):
            return jsonify({'error': "global_transcribe_method must be 'local' or 'openai'"}), 400

        for field in updateable_fields:
            if field in data:
                value = data[field]
                if isinstance(value, bool):
                    value = value
                elif isinstance(value, str) and value.lower() in {'true', 'false'}:
                    value = value.lower() == 'true'
                # For secret keys and passwords, only update if a non-empty value is provided
                # This allows users to update other fields without clearing the keys
                # Also handle the case where the dashboard sends '***' as a placeholder
                if field in ['s3_access_key', 's3_secret_key', 'host_password', 'samba_password',
                             'global_transcription_api_key']:
                    if value and value.strip() and value.strip() != '***':
                        current_settings[field] = value
                    # If empty string or '***', don't update (preserve existing value)
                else:
                    current_settings[field] = value

        # Save all settings using SettingsManager
        _settings_manager.set_all_settings(current_settings)

        # Reload transcription settings at runtime if transcription mode changed
        if 'global_transcribe_method' in data or 'global_transcription_api_key' in data:
            try:
                from ..services.audio_handler import reload_transcription_settings
                reload_transcription_settings()
                logging.info("Audio handler transcription settings reloaded without restart")
            except Exception as e:
                logging.warning(f"Failed to reload audio handler transcription settings: {str(e)}")

        # Restart S3 scheduler if backup time changed
        if 's3_backup_time' in data:
            try:
                from ..services.s3_scheduler import restart_scheduler
                restart_scheduler()
                logging.info("S3 backup scheduler restarted with new backup time")
            except Exception as e:
                logging.warning(f"Failed to restart S3 scheduler: {str(e)}")
        
        # Restart maintenance scheduler if maintenance time or enabled tasks changed
        if 'maintenance_time' in data or 'maintenance_enabled_tasks' in data:
            try:
                from ..services.maintenance_scheduler import restart_scheduler
                restart_scheduler()
                logging.info("Maintenance scheduler restarted with new settings")
            except Exception as e:
                logging.warning(f"Failed to restart maintenance scheduler: {str(e)}")

        return jsonify({'message': 'Settings updated successfully'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@settings_bp.route('/settings/restart-service', methods=['POST', 'OPTIONS'])
@swag_from({
    'tags': ['Settings'],
    'summary': 'Restart the Boondock Edge system service',
    'responses': {
        '200': {'description': 'Service restart triggered successfully'},
        '500': {'description': 'Server error'}
    }
})
def restart_system_service():
    """
    Restart the underlying Boondock Edge systemd service.

    This endpoint is intended to be called from the Settings UI via a restart button.
    """
    try:
        # Handle CORS preflight / OPTIONS requests
        if request.method == 'OPTIONS':
            response = jsonify({'message': 'OK'})
            response.headers['Content-Type'] = 'application/json'
            response.headers['Access-Control-Allow-Origin'] = '*'
            response.headers['Access-Control-Allow-Methods'] = 'POST, OPTIONS'
            response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
            return response, 200

        # Only attempt restart on Linux environments
        if sys.platform != "linux":
            return jsonify({'error': 'Service restart is only supported on Linux targets'}), 500

        # Determine restart script path:
        restart_script = CODE_ROOT / 'restart.sh'

        if not restart_script.exists():
            return jsonify({'error': f'restart.sh not found at {restart_script}'}), 500

        try:
            # Use bash to execute the restart script
            subprocess.run(
                ["/bin/bash", restart_script],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.STDOUT,
            )
        except subprocess.CalledProcessError:
            return jsonify({'error': 'Failed to execute restart.sh'}), 500

        return jsonify({'message': 'restart.sh executed successfully'}), 200
    except Exception as e:
        logging.error(f"Error restarting system service: {str(e)}")
        return jsonify({'error': str(e)}), 500


@settings_bp.route('/reboot', methods=['POST', 'OPTIONS'])
@swag_from({
    'tags': ['Settings'],
    'summary': 'Reboot the Boondock Edge application service',
    'responses': {
        '200': {'description': 'Reboot (service restart) triggered successfully'},
        '500': {'description': 'Server error'}
    }
})
def reboot_application():
    """
    Alias endpoint for restarting the Boondock Edge systemd service.
    Exposed as /api/reboot.
    """
    # Reuse the same logic as restart_system_service (includes OPTIONS handling and Linux guard)
    return restart_system_service()


@settings_bp.route('/settings/keywords', methods=['POST'])
@swag_from({
    'tags': ['Settings'],
    'summary': 'Add a keyword',
    'parameters': [
        {
            'name': 'body',
            'in': 'body',
            'required': True,
            'schema': {
                'type': 'object',
                'required': ['keyword'],
                'properties': {
                    'keyword': {'type': 'string'}
                }
            }
        }
    ],
    'responses': {
        '200': {'description': 'Keyword added successfully'},
        '400': {'description': 'Bad request'},
        '500': {'description': 'Server error'}
    }
})
def add_keyword():
    """Add a new keyword"""
    try:
        # Validate request data
        data = request.get_json()
        if not data:
            return jsonify({'error': 'No data provided'}), 400
            
        if 'keyword' not in data:
            return jsonify({'error': 'Keyword is required'}), 400
            
        keyword = data.get('keyword')
        if not isinstance(keyword, str):
            return jsonify({'error': 'Keyword must be a string'}), 400
            
        keyword = keyword.strip()
        if not keyword:
            return jsonify({'error': 'Keyword cannot be empty'}), 400
        
        # Initialize or get current settings
        try:
            init_settings()
            settings = _settings_manager.get_all_settings()
            
            # Ensure keywords is a list
            if not isinstance(settings.get('keywords', []), list):
                settings['keywords'] = []
            
            # Add keyword if not already present
            if keyword not in settings['keywords']:
                settings['keywords'].append(keyword)
                
                # Save updated settings using SettingsManager
                _settings_manager.set_all_settings(settings)
            
            return jsonify({
                'message': 'Keyword added successfully',
                'keywords': settings['keywords']
            })
            
        except Exception as e:
            logging.error(f"Error processing settings: {str(e)}")
            return jsonify({'error': f'Settings error: {str(e)}'}), 500
        
    except Exception as e:
        logging.error(f"Error adding keyword: {str(e)}")
        return jsonify({'error': str(e)}), 500


@settings_bp.route('/settings/keywords/<keyword>', methods=['DELETE'])
@swag_from({
    'tags': ['Settings'],
    'summary': 'Remove a keyword',
    'parameters': [
        {
            'name': 'keyword',
            'in': 'path',
            'type': 'string',
            'required': True,
            'description': 'Keyword to remove'
        }
    ],
    'responses': {
        '200': {'description': 'Keyword removed successfully'},
        '500': {'description': 'Server error'}
    }
})
def remove_keyword(keyword):
    """Remove a keyword"""
    try:
        # Get current settings
        init_settings()
        settings = _settings_manager.get_all_settings()
        
        # Ensure keywords exists and is a list
        if not isinstance(settings.get('keywords', []), list):
            settings['keywords'] = []
        
        # Remove keyword if it exists
        if keyword in settings['keywords']:
            settings['keywords'].remove(keyword)
            
            # Save updated settings using SettingsManager
            _settings_manager.set_all_settings(settings)
            
        return jsonify({
            'message': 'Keyword removed successfully',
            'keywords': settings['keywords']
        })
        
    except Exception as e:
        logging.error(f"Error removing keyword: {str(e)}")
        return jsonify({'error': str(e)}), 500


@settings_bp.route('/time', methods=['GET'])
@swag_from({
    'tags': ['Settings'],
    'summary': 'Get current GMT time',
    'responses': {
        '200': {'description': 'Current GMT time in ISO 8601 format'}
    }
})
def get_gmt_time():
    """
    API endpoint to return the current GMT time in ISO 8601 format.
    """
    from datetime import datetime, timezone
    current_gmt_time = datetime.now(timezone.utc).isoformat()
    return jsonify({"gmt_time": current_gmt_time})


@settings_bp.route('/ping', methods=['GET'])
@swag_from({
    'tags': ['Settings'],
    'summary': 'Health check endpoint',
    'responses': {
        '200': {'description': 'Server is healthy'}
    }
})
def ping():
    """
    Simple health check endpoint to verify server is running.
    """
    return jsonify({"status": "ok", "message": "Server is running"}), 200
