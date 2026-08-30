"""
Maintenance routes.
Handles maintenance settings, history, and manual task execution.
"""
import json
import logging
from flask import Blueprint, jsonify, request
from flasgger import swag_from

from ..services.maintenance_service import (
    get_maintenance_service,
    TASK_ID_BACKUP,
    TASK_ID_LOGS_CLEANUP,
    TASK_ID_HEALTH_CHECKS
)
from ..services.maintenance_scheduler import restart_scheduler
from ..routes.route_utils import init_settings
from ..services.settings_manager import get_settings_manager

_settings_manager = get_settings_manager()

maintenance_bp = Blueprint('maintenance', __name__)


@maintenance_bp.route('/maintenance/settings', methods=['GET'])
@swag_from({
    'tags': ['Maintenance'],
    'summary': 'Get maintenance settings',
    'responses': {
        '200': {'description': 'Maintenance settings retrieved successfully'},
        '500': {'description': 'Server error'}
    }
})
def get_maintenance_settings():
    """Get maintenance settings including time and enabled tasks."""
    init_settings()
    try:
        settings = _settings_manager.get_all_settings()
        
        # Get maintenance time (default: 03:00)
        maintenance_time = settings.get('maintenance_time', '03:00')
        
        # Get backup time (for display)
        backup_time = settings.get('s3_backup_time', '03:00')
        
        # Get enabled tasks (default: all enabled)
        enabled_tasks = settings.get('maintenance_enabled_tasks', [
            TASK_ID_BACKUP,
            TASK_ID_LOGS_CLEANUP,
            TASK_ID_HEALTH_CHECKS
        ])
        
        # Ensure it's a list
        if isinstance(enabled_tasks, str):
            try:
                enabled_tasks = json.loads(enabled_tasks)
            except json.JSONDecodeError:
                enabled_tasks = [TASK_ID_BACKUP, TASK_ID_LOGS_CLEANUP, TASK_ID_HEALTH_CHECKS]
        
        return jsonify({
            'maintenance_time': maintenance_time,
            'backup_time': backup_time,
            'enabled_tasks': enabled_tasks
        })
    except Exception as e:
        logging.error(f"Error getting maintenance settings: {e}")
        return jsonify({'error': str(e)}), 500


@maintenance_bp.route('/maintenance/settings', methods=['PUT'])
@swag_from({
    'tags': ['Maintenance'],
    'summary': 'Update maintenance settings',
    'parameters': [
        {
            'name': 'body',
            'in': 'body',
            'required': True,
            'schema': {
                'type': 'object',
                'properties': {
                    'maintenance_time': {'type': 'string', 'description': 'Time in HH:MM format (e.g., "03:00")'},
                    'enabled_tasks': {
                        'type': 'array',
                        'items': {'type': 'string'},
                        'description': 'List of enabled task IDs'
                    }
                }
            }
        }
    ],
    'responses': {
        '200': {'description': 'Maintenance settings updated successfully'},
        '400': {'description': 'Bad request'},
        '500': {'description': 'Server error'}
    }
})
def update_maintenance_settings():
    """Update maintenance settings."""
    init_settings()
    try:
        data = request.get_json()
        
        if not isinstance(data, dict):
            return jsonify({'error': 'Invalid data format'}), 400
        
        current_settings = _settings_manager.get_all_settings()
        
        # Update maintenance time if provided
        if 'maintenance_time' in data:
            maintenance_time = data['maintenance_time']
            # Validate time format (HH:MM)
            try:
                hour, minute = map(int, maintenance_time.split(':'))
                if not (0 <= hour <= 23 and 0 <= minute <= 59):
                    return jsonify({'error': 'Invalid time format. Use HH:MM (00:00-23:59)'}), 400
                current_settings['maintenance_time'] = maintenance_time
            except (ValueError, AttributeError):
                return jsonify({'error': 'Invalid time format. Use HH:MM (e.g., "03:00")'}), 400
        
        # Update enabled tasks if provided
        if 'enabled_tasks' in data:
            enabled_tasks = data['enabled_tasks']
            if not isinstance(enabled_tasks, list):
                return jsonify({'error': 'enabled_tasks must be a list'}), 400
            # Validate task IDs
            valid_task_ids = [TASK_ID_BACKUP, TASK_ID_LOGS_CLEANUP, TASK_ID_HEALTH_CHECKS]
            for task_id in enabled_tasks:
                if task_id not in valid_task_ids:
                    return jsonify({'error': f'Invalid task ID: {task_id}'}), 400
            current_settings['maintenance_enabled_tasks'] = enabled_tasks
        
        # Save settings using SettingsManager
        _settings_manager.set_all_settings(current_settings)
        
        # Restart scheduler if maintenance time or enabled tasks changed
        if 'maintenance_time' in data or 'enabled_tasks' in data:
            try:
                restart_scheduler()
                logging.info("Maintenance scheduler restarted with new settings")
            except Exception as e:
                logging.warning(f"Failed to restart maintenance scheduler: {str(e)}")
        
        return jsonify({'message': 'Maintenance settings updated successfully'})
    except Exception as e:
        logging.error(f"Error updating maintenance settings: {e}")
        return jsonify({'error': str(e)}), 500


@maintenance_bp.route('/maintenance/history', methods=['GET'])
@swag_from({
    'tags': ['Maintenance'],
    'summary': 'Get maintenance history',
    'parameters': [
        {
            'name': 'page',
            'in': 'query',
            'type': 'integer',
            'required': False,
            'default': 1,
            'description': 'Page number (1-indexed)'
        },
        {
            'name': 'per_page',
            'in': 'query',
            'type': 'integer',
            'required': False,
            'default': 50,
            'description': 'Number of records per page'
        },
        {
            'name': 'task_id',
            'in': 'query',
            'type': 'string',
            'required': False,
            'description': 'Filter by task ID'
        }
    ],
    'responses': {
        '200': {'description': 'Maintenance history retrieved successfully'},
        '500': {'description': 'Server error'}
    }
})
def get_maintenance_history():
    """Get maintenance history with pagination."""
    try:
        page = request.args.get('page', 1, type=int)
        per_page = request.args.get('per_page', 50, type=int)
        task_id = request.args.get('task_id', None)
        
        maintenance_service = get_maintenance_service()
        result = maintenance_service.get_maintenance_history(
            page=page,
            per_page=per_page,
            task_id=task_id
        )
        
        return jsonify(result)
    except Exception as e:
        logging.error(f"Error getting maintenance history: {e}")
        return jsonify({'error': str(e)}), 500


@maintenance_bp.route('/maintenance/run', methods=['POST'])
@swag_from({
    'tags': ['Maintenance'],
    'summary': 'Run maintenance tasks manually',
    'parameters': [
        {
            'name': 'body',
            'in': 'body',
            'required': False,
            'schema': {
                'type': 'object',
                'properties': {
                    'tasks': {
                        'type': 'array',
                        'items': {'type': 'string'},
                        'description': 'List of task IDs to run (optional, defaults to all enabled tasks)'
                    }
                }
            }
        }
    ],
    'responses': {
        '200': {'description': 'Maintenance tasks started'},
        '400': {'description': 'Bad request'},
        '500': {'description': 'Server error'}
    }
})
def run_maintenance_tasks():
    """Run maintenance tasks manually."""
    try:
        data = request.get_json() or {}
        tasks = data.get('tasks', None)
        
        maintenance_service = get_maintenance_service()
        
        # If no tasks specified, use enabled tasks from settings
        if tasks is None:
            init_settings()
            settings = _settings_manager.get_all_settings()
            tasks = settings.get('maintenance_enabled_tasks', [
                TASK_ID_BACKUP,
                TASK_ID_LOGS_CLEANUP,
                TASK_ID_HEALTH_CHECKS
            ])
            if isinstance(tasks, str):
                try:
                    tasks = json.loads(tasks)
                except json.JSONDecodeError:
                    tasks = [TASK_ID_BACKUP, TASK_ID_LOGS_CLEANUP, TASK_ID_HEALTH_CHECKS]
        
        # Validate task IDs
        valid_task_ids = [TASK_ID_BACKUP, TASK_ID_LOGS_CLEANUP, TASK_ID_HEALTH_CHECKS]
        for task_id in tasks:
            if task_id not in valid_task_ids:
                return jsonify({'error': f'Invalid task ID: {task_id}'}), 400
        
        # Run tasks in background (async)
        import threading
        def run_tasks():
            try:
                maintenance_service.run_maintenance_tasks(tasks)
            except Exception as e:
                logging.error(f"Error running maintenance tasks: {e}", exc_info=True)
        
        thread = threading.Thread(target=run_tasks, daemon=True)
        thread.start()
        
        return jsonify({
            'message': 'Maintenance tasks started',
            'tasks': tasks
        })
    except Exception as e:
        logging.error(f"Error starting maintenance tasks: {e}")
        return jsonify({'error': str(e)}), 500


@maintenance_bp.route('/maintenance/system-usage', methods=['GET'])
@swag_from({
    'tags': ['Maintenance'],
    'summary': 'Get latest system usage information',
    'responses': {
        '200': {'description': 'System usage retrieved successfully'},
        '404': {'description': 'No system usage data available'},
        '500': {'description': 'Server error'}
    }
})
def get_system_usage():
    """Get the latest system usage record."""
    try:
        maintenance_service = get_maintenance_service()
        usage = maintenance_service.get_latest_system_usage()
        
        if usage is None:
            return jsonify({'error': 'No system usage data available'}), 404
        
        return jsonify(usage)
    except Exception as e:
        logging.error(f"Error getting system usage: {e}")
        return jsonify({'error': str(e)}), 500

