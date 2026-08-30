"""
S3 backup and restore routes.
Handles S3 backup operations, status, history, and restore functionality.
"""
import json
import logging
import threading
from config import DATA_ROOT
from flask import Blueprint, jsonify, request
from flasgger import swag_from
from botocore.exceptions import ClientError

from ..utils.s3_utils import get_s3_client, get_s3_settings, is_s3_enabled
from ..services.s3_backup_service import test_samba_connection
from ..services.settings_manager import get_settings_manager

_settings_manager = get_settings_manager()

s3_bp = Blueprint('s3', __name__)


@s3_bp.route('/s3/backup/start', methods=['POST'])
@swag_from({
    'tags': ['S3 Backup'],
    'summary': 'Start manual backup',
    'parameters': [
        {
            'name': 'body',
            'in': 'body',
            'required': False,
            'schema': {
                'type': 'object',
                'properties': {
                    'backup_type': {
                        'type': 'string',
                        'enum': ['full', 'incremental'],
                        'default': 'incremental',
                        'description': 'Type of backup: full (all files) or incremental (only new files)'
                    },
                    'destination': {
                        'type': 'string',
                        'enum': ['cloud', 'samba', 'both'],
                        'default': 'both',
                        'description': 'Backup destination: cloud (S3), samba (network share), or both'
                    }
                }
            }
        }
    ],
    'responses': {
        '200': {'description': 'Backup started successfully'},
        '400': {'description': 'Backup already in progress'},
        '500': {'description': 'Server error'}
    }
})
def start_backup():
    """Start a manual backup job."""
    try:
        from ..services.s3_backup_service import run_backup_job, get_backup_progress
        
        # Get backup type and destination from request
        data = request.get_json() or {}
        backup_type = data.get('backup_type', 'incremental')
        if backup_type not in ['full', 'incremental']:
            backup_type = 'incremental'
        destination = data.get('destination', 'both')
        if destination not in ['cloud', 'samba', 'both']:
            destination = 'both'
        
        logging.info(f"=== MANUAL BACKUP REQUESTED (Type: {backup_type}, Destination: {destination}) ===")
        
        # Check if backup is already running
        progress = get_backup_progress()
        logging.info(f"Current backup status: {progress['status']}")
        if progress['status'] == 'running':
            logging.warning("Backup already in progress, rejecting request")
            return jsonify({'error': 'Backup already in progress'}), 400
        
        # Start backup in background thread
        def run_backup():
            try:
                logging.info(f"Starting backup thread (Type: {backup_type}, Destination: {destination})...")
                run_backup_job(manual=True, backup_type=backup_type, destination=destination)
                logging.info("Backup thread completed")
            except Exception as e:
                logging.error(f"ERROR in backup thread: {str(e)}", exc_info=True)
        
        backup_thread = threading.Thread(target=run_backup, daemon=True)
        backup_thread.start()
        logging.info("Backup thread started successfully")
        
        return jsonify({'message': f'Backup started successfully (Type: {backup_type}, Destination: {destination})'}), 200
    except Exception as e:
        logging.error(f"ERROR starting backup: {str(e)}", exc_info=True)
        return jsonify({'error': str(e)}), 500


@s3_bp.route('/s3/backup/status', methods=['GET'])
@swag_from({
    'tags': ['S3 Backup'],
    'summary': 'Get S3 backup progress status',
    'responses': {
        '200': {
            'description': 'Backup status retrieved successfully',
            'schema': {
                'type': 'object',
                'properties': {
                    'status': {'type': 'string', 'enum': ['idle', 'running', 'completed', 'error']},
                    'current_operation': {'type': 'string'},
                    'total_files': {'type': 'integer'},
                    'processed_files': {'type': 'integer'},
                    'uploaded_files': {'type': 'integer'},
                    'skipped_files': {'type': 'integer'},
                    'error_files': {'type': 'integer'},
                    'message': {'type': 'string'},
                    'start_time': {'type': 'string'},
                    'end_time': {'type': 'string'}
                }
            }
        },
        '500': {'description': 'Server error'}
    }
})
def get_backup_status():
    """Get current S3 backup progress status."""
    try:
        from ..services.s3_backup_service import get_backup_progress
        progress = get_backup_progress()
        return jsonify(progress), 200
    except Exception as e:
        logging.error(f"Error getting backup status: {str(e)}")
        return jsonify({'error': str(e)}), 500


@s3_bp.route('/s3/backup/history', methods=['GET'])
@swag_from({
    'tags': ['S3 Backup'],
    'summary': 'Get backup history with pagination',
    'parameters': [
        {
            'name': 'page',
            'in': 'query',
            'type': 'integer',
            'required': False,
            'default': 1,
            'description': 'Page number'
        },
        {
            'name': 'per_page',
            'in': 'query',
            'type': 'integer',
            'required': False,
            'default': 10,
            'description': 'Records per page'
        }
    ],
    'responses': {
        '200': {'description': 'Backup history retrieved successfully'},
        '500': {'description': 'Server error'}
    }
})
def get_backup_history():
    """Get backup history with pagination."""
    try:
        from ..services.s3_backup_service import get_backup_history
        page = request.args.get('page', 1, type=int)
        per_page = request.args.get('per_page', 10, type=int)
        result = get_backup_history(page, per_page)
        return jsonify(result), 200
    except Exception as e:
        logging.error(f"Error getting backup history: {str(e)}")
        return jsonify({'error': str(e)}), 500


@s3_bp.route('/s3/restore/list', methods=['GET'])
@swag_from({
    'tags': ['S3 Restore'],
    'summary': 'List files available for restore from S3',
    'parameters': [
        {
            'name': 'type',
            'in': 'query',
            'type': 'string',
            'required': True,
            'enum': ['settings', 'database'],
            'description': 'Type of files to list'
        }
    ],
    'responses': {
        '200': {'description': 'Files listed successfully'},
        '500': {'description': 'Server error'}
    }
})
def list_restore_files():
    """List files available for restore from S3."""
    try:
        if not is_s3_enabled():
            return jsonify({'error': 'S3 is not enabled'}), 400
        
        file_type = request.args.get('type')
        if file_type not in ['settings', 'database']:
            return jsonify({'error': 'Invalid type. Must be "settings" or "database"'}), 400
        
        s3_settings = get_s3_settings()
        bucket = s3_settings.get('bucket_name', '')
        if not bucket:
            return jsonify({'error': 'S3 bucket not configured'}), 400
        
        client = get_s3_client()
        if not client:
            return jsonify({'error': 'S3 client not available'}), 500
        
        # List files from backups/db/{weekday}/ prefix
        # We'll check all weekdays to get all available backups
        weekdays = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']
        files = set()
        
        for weekday in weekdays:
            prefix = f"backups/db/{weekday}/"
            try:
                paginator = client.get_paginator('list_objects_v2')
                pages = paginator.paginate(Bucket=bucket, Prefix=prefix)
                for page in pages:
                    if 'Contents' in page:
                        for obj in page['Contents']:
                            filename = obj['Key'].split('/')[-1]
                            if filename:
                                files.add(filename)
            except ClientError as e:
                logging.warning(f"Error listing files for {weekday}: {str(e)}")
                continue
        
        return jsonify({'files': sorted(list(files))}), 200
    except Exception as e:
        logging.error(f"Error listing restore files: {str(e)}")
        return jsonify({'error': str(e)}), 500


@s3_bp.route('/s3/restore/channels', methods=['GET'])
@swag_from({
    'tags': ['S3 Restore'],
    'summary': 'List channels with audio files in S3',
    'responses': {
        '200': {'description': 'Channels listed successfully'},
        '500': {'description': 'Server error'}
    }
})
def list_restore_channels():
    """List channels with audio files available in S3."""
    try:
        if not is_s3_enabled():
            return jsonify({'error': 'S3 is not enabled'}), 400
        
        s3_settings = get_s3_settings()
        bucket = s3_settings.get('bucket_name', '')
        if not bucket:
            return jsonify({'error': 'S3 bucket not configured'}), 400
        
        client = get_s3_client()
        if not client:
            return jsonify({'error': 'S3 client not available'}), 500
        
        # Get channels from database
        try:
            local_channels = _settings_manager.get_all_channels()
        except Exception as e:
            logging.warning(f"Error reading channels: {str(e)}")
            local_channels = []
        
        # For each channel, check if it has files in S3
        channels = []
        for channel in local_channels:
            mac_address = channel.get('mac', '').lower()
            if not mac_address:
                continue
            
            # List objects with this MAC address prefix
            prefix = f"{mac_address}/"
            file_count = 0
            try:
                paginator = client.get_paginator('list_objects_v2')
                pages = paginator.paginate(Bucket=bucket, Prefix=prefix)
                for page in pages:
                    if 'Contents' in page:
                        file_count += len(page['Contents'])
            except ClientError as e:
                logging.warning(f"Error counting files for {mac_address}: {str(e)}")
                continue
            
            if file_count > 0:
                channels.append({
                    'mac_address': mac_address,
                    'name': channel.get('name', f'Channel {channel.get("id", "")}'),
                    'file_count': file_count
                })
        
        return jsonify({'channels': channels}), 200
    except Exception as e:
        logging.error(f"Error listing restore channels: {str(e)}")
        return jsonify({'error': str(e)}), 500


@s3_bp.route('/s3/restore/years', methods=['GET'])
@swag_from({
    'tags': ['S3 Restore'],
    'summary': 'List years available for a channel',
    'parameters': [
        {
            'name': 'channel',
            'in': 'query',
            'type': 'string',
            'required': True,
            'description': 'Channel MAC address'
        }
    ],
    'responses': {
        '200': {'description': 'Years listed successfully'},
        '500': {'description': 'Server error'}
    }
})
def list_restore_years():
    """List years available for a channel in S3."""
    try:
        channel_mac = request.args.get('channel', '').lower()
        if not channel_mac:
            return jsonify({'error': 'Channel MAC address required'}), 400
        
        s3_settings = get_s3_settings()
        bucket = s3_settings.get('bucket_name', '')
        client = get_s3_client()
        
        prefix = f"{channel_mac}/"
        years = set()
        
        try:
            paginator = client.get_paginator('list_objects_v2')
            pages = paginator.paginate(Bucket=bucket, Prefix=prefix, Delimiter='/')
            for page in pages:
                if 'CommonPrefixes' in page:
                    for prefix_obj in page['CommonPrefixes']:
                        # Extract year from path like "mac_address/2024/"
                        parts = prefix_obj['Prefix'].split('/')
                        if len(parts) >= 2:
                            year = parts[1]
                            if year.isdigit() and len(year) == 4:
                                years.add(year)
        except ClientError as e:
            logging.error(f"Error listing years: {str(e)}")
            return jsonify({'error': str(e)}), 500
        
        return jsonify({'years': sorted(list(years), reverse=True)}), 200
    except Exception as e:
        logging.error(f"Error listing restore years: {str(e)}")
        return jsonify({'error': str(e)}), 500


@s3_bp.route('/s3/restore/months', methods=['GET'])
@swag_from({
    'tags': ['S3 Restore'],
    'summary': 'List months available for a channel and year',
    'parameters': [
        {
            'name': 'channel',
            'in': 'query',
            'type': 'string',
            'required': True,
            'description': 'Channel MAC address'
        },
        {
            'name': 'year',
            'in': 'query',
            'type': 'string',
            'required': True,
            'description': 'Year (YYYY)'
        }
    ],
    'responses': {
        '200': {'description': 'Months listed successfully'},
        '500': {'description': 'Server error'}
    }
})
def list_restore_months():
    """List months available for a channel and year in S3."""
    try:
        channel_mac = request.args.get('channel', '').lower()
        year = request.args.get('year', '')
        if not channel_mac or not year:
            return jsonify({'error': 'Channel MAC address and year required'}), 400
        
        s3_settings = get_s3_settings()
        bucket = s3_settings.get('bucket_name', '')
        client = get_s3_client()
        
        prefix = f"{channel_mac}/{year}/"
        months = set()
        
        try:
            paginator = client.get_paginator('list_objects_v2')
            pages = paginator.paginate(Bucket=bucket, Prefix=prefix, Delimiter='/')
            for page in pages:
                if 'CommonPrefixes' in page:
                    for prefix_obj in page['CommonPrefixes']:
                        # Extract month from path like "mac_address/2024/01/"
                        parts = prefix_obj['Prefix'].split('/')
                        if len(parts) >= 3:
                            month = parts[2]
                            if month.isdigit() and len(month) == 2:
                                months.add(month)
        except ClientError as e:
            logging.error(f"Error listing months: {str(e)}")
            return jsonify({'error': str(e)}), 500
        
        return jsonify({'months': sorted(list(months))}), 200
    except Exception as e:
        logging.error(f"Error listing restore months: {str(e)}")
        return jsonify({'error': str(e)}), 500


@s3_bp.route('/s3/restore/days', methods=['GET'])
@swag_from({
    'tags': ['S3 Restore'],
    'summary': 'List days available for a channel, year, and month',
    'parameters': [
        {
            'name': 'channel',
            'in': 'query',
            'type': 'string',
            'required': True,
            'description': 'Channel MAC address'
        },
        {
            'name': 'year',
            'in': 'query',
            'type': 'string',
            'required': True,
            'description': 'Year (YYYY)'
        },
        {
            'name': 'month',
            'in': 'query',
            'type': 'string',
            'required': True,
            'description': 'Month (MM)'
        }
    ],
    'responses': {
        '200': {'description': 'Days listed successfully'},
        '500': {'description': 'Server error'}
    }
})
def list_restore_days():
    """List days available for a channel, year, and month in S3."""
    try:
        channel_mac = request.args.get('channel', '').lower()
        year = request.args.get('year', '')
        month = request.args.get('month', '')
        if not channel_mac or not year or not month:
            return jsonify({'error': 'Channel MAC address, year, and month required'}), 400
        
        s3_settings = get_s3_settings()
        bucket = s3_settings.get('bucket_name', '')
        client = get_s3_client()
        
        prefix = f"{channel_mac}/{year}/{month}/"
        days = set()
        file_count = 0
        
        try:
            # List all objects with the month prefix
            paginator = client.get_paginator('list_objects_v2')
            pages = paginator.paginate(Bucket=bucket, Prefix=prefix)
            for page in pages:
                if 'Contents' in page:
                    for obj in page['Contents']:
                        # Extract day from path like "mac_address/2024/01/15/12-30-45.wav"
                        parts = obj['Key'].split('/')
                        if len(parts) >= 4:
                            day = parts[3]
                            if day.isdigit() and len(day) == 2:
                                days.add(day)
                                file_count += 1
        except ClientError as e:
            logging.error(f"Error listing days: {str(e)}")
            return jsonify({'error': str(e)}), 500
        
        return jsonify({'days': sorted(list(days)), 'file_count': file_count}), 200
    except Exception as e:
        logging.error(f"Error listing restore days: {str(e)}")
        return jsonify({'error': str(e)}), 500


@s3_bp.route('/s3/restore/execute', methods=['POST'])
@swag_from({
    'tags': ['S3 Restore'],
    'summary': 'Execute restore from S3',
    'parameters': [
        {
            'name': 'body',
            'in': 'body',
            'required': True,
            'schema': {
                'type': 'object',
                'properties': {
                    'settings_files': {'type': 'array', 'items': {'type': 'string'}},
                    'database_files': {'type': 'array', 'items': {'type': 'string'}},
                    'audio_files': {
                        'type': 'object',
                        'properties': {
                            'channel_mac': {'type': 'string'},
                            'channel_name': {'type': 'string'},
                            'years': {'type': 'array', 'items': {'type': 'string'}},
                            'months': {'type': 'array', 'items': {'type': 'string'}},
                            'days': {'type': 'array', 'items': {
                                'type': 'object',
                                'properties': {
                                    'year': {'type': 'string'},
                                    'month': {'type': 'string'},
                                    'day': {'type': 'string'}
                                }
                            }}
                        }
                    }
                }
            }
        }
    ],
    'responses': {
        '200': {'description': 'Restore completed successfully'},
        '500': {'description': 'Server error'}
    }
})
def execute_restore():
    """Execute restore from S3."""
    try:
        if not is_s3_enabled():
            return jsonify({'error': 'S3 is not enabled'}), 400
        
        data = request.get_json()
        settings_files = data.get('settings_files', [])
        database_files = data.get('database_files', [])
        audio_files = data.get('audio_files')
        
        s3_settings = get_s3_settings()
        bucket = s3_settings.get('bucket_name', '')
        client = get_s3_client()
        
        restored_count = 0
        errors = []
        
        # Restore settings and database files
        weekdays = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']
        all_files = list(set(settings_files + database_files))
        
        for filename in all_files:
            restored = False
            for weekday in weekdays:
                s3_key = f"backups/db/{weekday}/{filename}"
                try:
                    # Download file
                    local_path = DATA_ROOT / 'db' / filename
                    local_path.parent.mkdir(parents=True, exist_ok=True)
                    client.download_file(bucket, s3_key, local_path)
                    restored = True
                    restored_count += 1
                    logging.info(f"Restored file: {filename}")
                    break
                except ClientError as e:
                    if e.response['Error']['Code'] != '404':
                        logging.warning(f"Error restoring {filename} from {weekday}: {str(e)}")
                    continue
            
            if not restored:
                errors.append(f"File not found in S3: {filename}")
        
        # Restore audio files
        if audio_files:
            channel_mac = audio_files.get('channel_mac', '').lower()
            days = audio_files.get('days', [])
            
            if channel_mac and days:
                # Get channel ID from database
                channel_id = None
                try:
                    channel = _settings_manager.get_channel_by_mac(channel_mac)
                    if channel:
                        channel_id = channel.get('id')
                except Exception as e:
                    logging.warning(f"Error reading channels: {str(e)}")
                
                recordings_dir = DATA_ROOT / 'recordings'
                
                # Process each day (which contains year, month, day)
                for day_info in days:
                    year = day_info.get('year', '')
                    month = day_info.get('month', '')
                    day = day_info.get('day', '')
                    
                    if not year or not month or not day:
                        continue
                    
                    # Use new structure: recordings/<MAC>/YYYY/MM/DD/
                    mac_dir = recordings_dir / channel_mac.lower() / year / month
                    mac_dir.mkdir(parents=True, exist_ok=True)
                    
                    prefix = f"{channel_mac}/{year}/{month}/{day}/"
                    try:
                        paginator = client.get_paginator('list_objects_v2')
                        pages = paginator.paginate(Bucket=bucket, Prefix=prefix)
                        for page in pages:
                            if 'Contents' in page:
                                for obj in page['Contents']:
                                    s3_key = obj['Key']
                                    filename = s3_key.split('/')[-1]
                                    
                                    # Download to recordings/<MAC>/YYYY/MM/DD/ directory
                                    day_dir = mac_dir / day
                                    day_dir.mkdir(parents=True, exist_ok=True)
                                    local_path = day_dir / filename
                                    client.download_file(bucket, s3_key, str(local_path))
                                    restored_count += 1
                                    logging.info(f"Restored audio file: {filename}")
                    except Exception as e:
                        errors.append(f"Error restoring audio files for {year}/{month}/{day}: {str(e)}")
        
        return jsonify({
            'message': 'Restore completed',
            'restored_count': restored_count,
            'errors': errors
        }), 200
    except Exception as e:
        logging.error(f"Error executing restore: {str(e)}")
        return jsonify({'error': str(e)}), 500


@s3_bp.route('/s3/backup/test-samba', methods=['POST'])
@swag_from({
    'tags': ['S3 Backup'],
    'summary': 'Test Samba / network share connection',
    'parameters': [
        {
            'name': 'body',
            'in': 'body',
            'required': False,
            'schema': {
                'type': 'object',
                'properties': {
                    'share_path': {
                        'type': 'string',
                        'description': 'Samba share path to test (optional, uses settings if not provided)'
                    },
                    'username': {
                        'type': 'string',
                        'description': 'Samba username (optional, uses settings if not provided)'
                    },
                    'password': {
                        'type': 'string',
                        'description': 'Samba password (optional, uses settings if not provided)'
                    }
                }
            }
        }
    ],
    'responses': {
        '200': {
            'description': 'Connection test result',
            'schema': {
                'type': 'object',
                'properties': {
                    'success': {'type': 'boolean'},
                    'message': {'type': 'string'},
                    'details': {
                        'type': 'object',
                        'properties': {
                            'path': {'type': 'string'},
                            'exists': {'type': 'boolean'},
                            'writable': {'type': 'boolean'},
                            'is_directory': {'type': 'boolean'},
                            'error': {'type': 'string'}
                        }
                    }
                }
            }
        },
        '500': {'description': 'Server error'}
    }
})
def test_samba_backup_connection():
    """Test Samba / network share backup connection."""
    try:
        data = request.get_json() or {}
        share_path = data.get('share_path')
        username = data.get('username')
        password = data.get('password')
        
        result = test_samba_connection(
            share_path=share_path,
            username=username,
            password=password
        )
        
        status_code = 200 if result['success'] else 400
        return jsonify(result), status_code
    except Exception as e:
        logging.error(f"Error testing Samba connection: {str(e)}")
        return jsonify({
            'success': False,
            'message': f'Error testing connection: {str(e)}',
            'details': {
                'path': '',
                'exists': False,
                'writable': False,
                'error': str(e)
            }
        }), 500

