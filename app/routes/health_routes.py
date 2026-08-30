"""
Health monitoring routes.
Provides endpoints for retrieving device health statistics.
"""

from flask import Blueprint, jsonify, request
from flasgger import swag_from
from datetime import datetime

from ..services.device_health_monitor import (
    get_health_stats,
    get_current_health_stats,
    force_persist as force_persist_devices
)
from ..services.system_health_monitor import (
    get_health_stats as get_system_health_stats,
    get_current_metrics,
    force_persist as force_persist_system
)
from ..services.notification_service import (
    get_active_notifications,
    remove_notification,
    clear_all_notifications
)

health_bp = Blueprint('health', __name__)
notification_bp = Blueprint('notification', __name__)


@health_bp.route('/devices', methods=['GET'])
@swag_from({
    'tags': ['Health'],
    'summary': 'Get device health statistics',
    'parameters': [
        {
            'name': 'mac',
            'in': 'query',
            'type': 'string',
            'required': False,
            'description': 'Filter by MAC address (optional)'
        },
        {
            'name': 'date',
            'in': 'query',
            'type': 'string',
            'required': False,
            'description': 'Date filter in YYYY-MM-DD format (optional, defaults to today)'
        },
        {
            'name': 'current',
            'in': 'query',
            'type': 'boolean',
            'required': False,
            'description': 'Get current in-memory stats instead of persisted stats (optional, defaults to false)'
        }
    ],
    'responses': {
        '200': {
            'description': 'Health statistics retrieved successfully',
            'schema': {
                'type': 'object',
                'properties': {
                    'stats': {
                        'type': 'array',
                        'items': {
                            'type': 'object',
                            'properties': {
                                'mac_address': {'type': 'string'},
                                'date': {'type': 'string'},
                                'device_created_at': {'type': 'string'},
                                'connection_count': {'type': 'integer'},
                                'event_count': {'type': 'integer'},
                                'file_upload_count': {'type': 'integer'},
                                'error_count': {'type': 'integer'},
                                'connection_loss_count': {'type': 'integer'},
                                'uptime_seconds': {'type': 'integer'},
                                'uptime_formatted': {'type': 'string'},
                                'first_activity': {'type': 'string'},
                                'last_activity': {'type': 'string'}
                            }
                        }
                    }
                }
            }
        }
    }
})
def get_device_health():
    """Get device health statistics."""
    try:
        mac = request.args.get('mac')
        date = request.args.get('date')
        current = request.args.get('current', 'false').lower() == 'true'
        
        if current:
            # Get current in-memory stats
            stats = get_current_health_stats(mac)
        else:
            # Get persisted stats from database
            stats = get_health_stats(mac, date)
        
        return jsonify({
            'stats': stats,
            'date': date or datetime.now().strftime('%Y-%m-%d'),
            'current': current
        }), 200
    except Exception as e:
        return jsonify({'error': f'Failed to retrieve health stats: {str(e)}'}), 500


@health_bp.route('/devices/<mac>', methods=['GET'])
@swag_from({
    'tags': ['Health'],
    'summary': 'Get health statistics for a specific device',
    'parameters': [
        {
            'name': 'mac',
            'in': 'path',
            'type': 'string',
            'required': True,
            'description': 'MAC address'
        },
        {
            'name': 'date',
            'in': 'query',
            'type': 'string',
            'required': False,
            'description': 'Date filter in YYYY-MM-DD format (optional, defaults to today)'
        },
        {
            'name': 'current',
            'in': 'query',
            'type': 'boolean',
            'required': False,
            'description': 'Get current in-memory stats instead of persisted stats (optional, defaults to false)'
        }
    ],
    'responses': {
        '200': {'description': 'Health statistics retrieved successfully'},
        '404': {'description': 'Device not found'},
        '500': {'description': 'Server error'}
    }
})
def get_device_health_by_mac(mac):
    """Get health statistics for a specific device."""
    try:
        date = request.args.get('date')
        current = request.args.get('current', 'false').lower() == 'true'
        
        if current:
            stats = get_current_health_stats(mac)
        else:
            stats = get_health_stats(mac, date)
        
        if not stats:
            return jsonify({'error': 'Device not found'}), 404
        
        return jsonify({
            'stats': stats,
            'date': date or datetime.now().strftime('%Y-%m-%d'),
            'current': current
        }), 200
    except Exception as e:
        return jsonify({'error': f'Failed to retrieve health stats: {str(e)}'}), 500


@health_bp.route('/system', methods=['GET'])
@swag_from({
    'tags': ['Health'],
    'summary': 'Get system health statistics',
    'parameters': [
        {
            'name': 'date',
            'in': 'query',
            'type': 'string',
            'required': False,
            'description': 'Date filter in YYYY-MM-DD format (optional, defaults to today)'
        },
        {
            'name': 'current',
            'in': 'query',
            'type': 'boolean',
            'required': False,
            'description': 'Get current in-memory stats instead of persisted stats (optional, defaults to false)'
        }
    ],
    'responses': {
        '200': {
            'description': 'System health statistics retrieved successfully',
            'schema': {
                'type': 'object',
                'properties': {
                    'stats': {
                        'type': 'object',
                        'properties': {
                            'cpu': {'type': 'object'},
                            'memory': {'type': 'object'},
                            'disk': {'type': 'object'}
                        }
                    }
                }
            }
        }
    }
})
def get_system_health():
    """Get system health statistics (CPU, RAM, disk)."""
    try:
        date = request.args.get('date')
        current = request.args.get('current', 'false').lower() == 'true'
        
        if current:
            # Get current in-memory stats
            metrics = get_current_metrics()
            return jsonify({
                'stats': metrics,
                'date': date or datetime.now().strftime('%Y-%m-%d'),
                'current': True
            }), 200
        else:
            # Get persisted stats from database
            stats = get_system_health_stats(date)
            if stats is None:
                return jsonify({
                    'stats': None,
                    'date': date or datetime.now().strftime('%Y-%m-%d'),
                    'current': False,
                    'message': 'No system health data available for the selected date'
                }), 200
            
            return jsonify({
                'stats': stats,
                'date': date or datetime.now().strftime('%Y-%m-%d'),
                'current': False
            }), 200
    except Exception as e:
        return jsonify({'error': f'Failed to retrieve system health stats: {str(e)}'}), 500


@health_bp.route('/purge', methods=['POST'])
@swag_from({
    'tags': ['Health'],
    'summary': 'Force immediate persistence of health data to database',
    'responses': {
        '200': {
            'description': 'Health data persisted successfully',
            'schema': {
                'type': 'object',
                'properties': {
                    'message': {'type': 'string'},
                    'devices_persisted': {'type': 'boolean'},
                    'system_persisted': {'type': 'boolean'}
                }
            }
        },
        '500': {'description': 'Server error'}
    }
})
def force_purge_health():
    """Force immediate persistence of all health data to database."""
    try:
        devices_persisted = force_persist_devices()
        system_persisted = force_persist_system()
        
        return jsonify({
            'message': 'Health data persisted successfully',
            'devices_persisted': devices_persisted,
            'system_persisted': system_persisted
        }), 200
    except Exception as e:
        return jsonify({'error': f'Failed to persist health data: {str(e)}'}), 500


@notification_bp.route('', methods=['GET'])
@swag_from({
    'tags': ['Notifications'],
    'summary': 'Get active notifications',
    'responses': {
        '200': {
            'description': 'Notifications retrieved successfully',
            'schema': {
                'type': 'object',
                'properties': {
                    'notifications': {
                        'type': 'array',
                        'items': {
                            'type': 'object',
                            'properties': {
                                'id': {'type': 'string'},
                                'type': {'type': 'string'},
                                'mode': {'type': 'string'},
                                'title': {'type': 'string'},
                                'message': {'type': 'string'},
                                'timestamp': {'type': 'string'},
                                'data': {'type': 'object'}
                            }
                        }
                    }
                }
            }
        }
    }
})
def get_notifications():
    """Get all active notifications."""
    try:
        notifications = get_active_notifications()
        return jsonify({
            'notifications': notifications
        }), 200
    except Exception as e:
        return jsonify({'error': f'Failed to retrieve notifications: {str(e)}'}), 500


@notification_bp.route('/<notification_id>', methods=['DELETE'])
@swag_from({
    'tags': ['Notifications'],
    'summary': 'Remove a notification',
    'parameters': [
        {
            'name': 'notification_id',
            'in': 'path',
            'type': 'string',
            'required': True,
            'description': 'Notification ID'
        }
    ],
    'responses': {
        '200': {'description': 'Notification removed successfully'},
        '404': {'description': 'Notification not found'},
        '500': {'description': 'Server error'}
    }
})
def delete_notification(notification_id):
    """Remove a notification by ID."""
    try:
        success = remove_notification(notification_id)
        if success:
            return jsonify({'message': 'Notification removed successfully'}), 200
        else:
            return jsonify({'error': 'Notification not found'}), 404
    except Exception as e:
        return jsonify({'error': f'Failed to remove notification: {str(e)}'}), 500


@notification_bp.route('/clear', methods=['POST'])
@swag_from({
    'tags': ['Notifications'],
    'summary': 'Clear all notifications',
    'responses': {
        '200': {'description': 'All notifications cleared successfully'},
        '500': {'description': 'Server error'}
    }
})
def clear_notifications():
    """Clear all notifications."""
    try:
        clear_all_notifications()
        return jsonify({'message': 'All notifications cleared successfully'}), 200
    except Exception as e:
        return jsonify({'error': f'Failed to clear notifications: {str(e)}'}), 500
