"""
Logs routes for managing application logs.
All logs are now stored in logs.db database.
"""
import os
import logging
from flask import Blueprint, jsonify, request
from flasgger import swag_from
from datetime import datetime
from ..services.db_logging_manager import get_db_logging_manager

logs_bp = Blueprint('logs', __name__)

@logs_bp.route('/logs', methods=['GET'])
@swag_from({
    'tags': ['Logs'],
    'summary': 'Get all logs',
    'parameters': [
        {
            'name': 'date',
            'in': 'query',
            'type': 'string',
            'format': 'date',
            'required': False,
            'description': 'Date in YYYY-MM-DD format. Defaults to all logs.'
        },
        {
            'name': 'limit',
            'in': 'query',
            'type': 'integer',
            'required': False,
            'description': 'Maximum number of logs per type to retrieve.'
        }
    ],
    'responses': {
        '200': {'description': 'All logs by type'}
    }
})
def get_all_logs():
    # Get date parameter
    date_str = request.args.get('date', None)
    if date_str:
        try:
            # Validate date format
            datetime.strptime(date_str, '%Y-%m-%d')
        except ValueError:
            return jsonify({'error': 'Invalid date format. Use YYYY-MM-DD'}), 400
    
    # Get limit parameter (omit to return all logs for the date; used for EVENTS and other tabs)
    limit = request.args.get('limit', None, type=int)
    
    try:
        db_manager = get_db_logging_manager()
        all_logs = {}
        
        # Get logs for each log type (no limit = all events for the date)
        log_types = ['error', 'warning', 'transcription', 'database', 'event', 'app', 'com_port']
        for log_type in log_types:
            try:
                logs = db_manager.get_logs(log_type, date=date_str, limit=limit)
                all_logs[log_type] = logs
            except Exception as e:
                logging.error(f"Error reading logs for {log_type}: {str(e)}")
                all_logs[log_type] = []
        
        return jsonify(all_logs)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@logs_bp.route('/logs/<log_type>', methods=['GET'])
@swag_from({
    'tags': ['Logs'],
    'summary': 'Get logs by type',
    'parameters': [
        {
            'name': 'log_type',
            'in': 'path',
            'type': 'string',
            'enum': ['error', 'warning', 'transcription', 'database', 'event', 'app', 'com_port'],
            'required': True,
            'description': 'Type of log'
        },
        {
            'name': 'date',
            'in': 'query',
            'type': 'string',
            'format': 'date',
            'required': False,
            'description': 'Date in YYYY-MM-DD format. Defaults to all logs.'
        },
        {
            'name': 'level',
            'in': 'query',
            'type': 'string',
            'enum': ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'],
            'required': False,
            'description': 'Filter by log level.'
        },
        {
            'name': 'port',
            'in': 'query',
            'type': 'string',
            'required': False,
            'description': 'Filter by COM port (for com_port log type only).'
        },
        {
            'name': 'limit',
            'in': 'query',
            'type': 'integer',
            'required': False,
            'description': 'Maximum number of logs to retrieve.'
        },
        {
            'name': 'offset',
            'in': 'query',
            'type': 'integer',
            'required': False,
            'description': 'Offset for pagination.'
        }
    ],
    'responses': {
        '200': {'description': 'Logs retrieved successfully'},
        '400': {'description': 'Invalid log type or date format'},
        '500': {'description': 'Server error'}
    }
})
def get_logs_by_type(log_type):
    valid_log_types = ['error', 'warning', 'transcription', 'database', 'event', 'app', 'com_port']
    if log_type not in valid_log_types:
        return jsonify({'error': 'Invalid log type'}), 400
    
    # Get date parameter
    date_str = request.args.get('date', None)
    if date_str:
        try:
            # Validate date format
            datetime.strptime(date_str, '%Y-%m-%d')
        except ValueError:
            return jsonify({'error': 'Invalid date format. Use YYYY-MM-DD'}), 400
    
    # Get optional filters
    level = request.args.get('level', None)
    limit = request.args.get('limit', None, type=int)
    offset = request.args.get('offset', 0, type=int)
    port = request.args.get('port', None)  # For com_port logs
    
    try:
        db_manager = get_db_logging_manager()
        logs = db_manager.get_logs(
            log_type=log_type,
            date=date_str,
            level=level,
            limit=limit,
            offset=offset,
            port=port
        )
        return jsonify(logs)
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    
@logs_bp.route('/logs/<log_type>', methods=['DELETE'])
@swag_from({
    'tags': ['Logs'],
    'summary': 'Clear logs by type',
    'parameters': [
        {
            'name': 'log_type',
            'in': 'path',
            'type': 'string',
            'enum': ['error', 'warning', 'transcription', 'database', 'event', 'app'],
            'required': True,
            'description': 'Type of log to clear'
        },
        {
            'name': 'date',
            'in': 'query',
            'type': 'string',
            'format': 'date',
            'required': False,
            'description': 'Date in YYYY-MM-DD format. If not provided, clears all logs of this type.'
        }
    ],
    'responses': {
        '200': {'description': 'Logs cleared successfully'},
        '400': {'description': 'Invalid log type or date format'},
        '500': {'description': 'Server error'}
    }
})
def clear_logs(log_type):
    valid_log_types = ['error', 'warning', 'transcription', 'database', 'event', 'app']
    if log_type not in valid_log_types:
        return jsonify({'error': 'Invalid log type'}), 400
    
    # Get date parameter
    date_str = request.args.get('date', None)
    if date_str:
        try:
            # Validate date format
            datetime.strptime(date_str, '%Y-%m-%d')
        except ValueError:
            return jsonify({'error': 'Invalid date format. Use YYYY-MM-DD'}), 400
    
    try:
        db_manager = get_db_logging_manager()
        success = db_manager.clear_logs(log_type=log_type, date=date_str)
        if success:
            message = f'{log_type} logs cleared successfully'
            if date_str:
                message += f' for date {date_str}'
            return jsonify({'message': message})
        else:
            return jsonify({'error': 'Failed to clear logs'}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@logs_bp.route('/logs_clear', methods=['POST'])
@swag_from({
    'tags': ['Logs'],
    'summary': 'Clear all logs',
    'parameters': [
        {
            'name': 'date',
            'in': 'query',
            'type': 'string',
            'format': 'date',
            'required': False,
            'description': 'Date in YYYY-MM-DD format. If not provided, clears all logs.'
        }
    ],
    'responses': {
        '200': {'description': 'All logs cleared successfully'},
        '400': {'description': 'Invalid date format'},
        '500': {'description': 'Server error'}
    }
})
def clear_all_logs():
    """
    Clears all logs across all log types for a specific date (or all dates if not provided).
    """
    # Get date parameter
    date_str = request.args.get('date', None)
    if date_str:
        try:
            # Validate date format
            datetime.strptime(date_str, '%Y-%m-%d')
        except ValueError:
            return jsonify({'error': 'Invalid date format. Use YYYY-MM-DD'}), 400
    
    try:
        db_manager = get_db_logging_manager()
        cleared_logs = []
        log_types = ['error', 'warning', 'transcription', 'database', 'event', 'app']

        for log_type in log_types:
            success = db_manager.clear_logs(log_type=log_type, date=date_str)
            if success:
                cleared_logs.append(log_type)

        message = 'All logs cleared successfully'
        if date_str:
            message += f' for date {date_str}'
        
        return jsonify({
            'message': message,
            'cleared_logs': cleared_logs
        }), 200

    except Exception as e:
        return jsonify({'error': str(e)}), 500


@logs_bp.route('/logs/statistics', methods=['GET'])
@swag_from({
    'tags': ['Logs'],
    'summary': 'Get log statistics',
    'parameters': [
        {
            'name': 'log_type',
            'in': 'query',
            'type': 'string',
            'enum': ['error', 'warning', 'transcription', 'database', 'event', 'app', 'com_port'],
            'required': False,
            'description': 'Type of log. If not provided, returns statistics for all types.'
        },
        {
            'name': 'date',
            'in': 'query',
            'type': 'string',
            'format': 'date',
            'required': False,
            'description': 'Date in YYYY-MM-DD format. If not provided, returns statistics for all dates.'
        }
    ],
    'responses': {
        '200': {'description': 'Log statistics retrieved successfully'},
        '400': {'description': 'Invalid date format'},
        '500': {'description': 'Server error'}
    }
})
def get_log_statistics():
    """Get statistics about logs."""
    date_str = request.args.get('date', None)
    if date_str:
        try:
            datetime.strptime(date_str, '%Y-%m-%d')
        except ValueError:
            return jsonify({'error': 'Invalid date format. Use YYYY-MM-DD'}), 400
    
    log_type = request.args.get('log_type', None)
    
    try:
        db_manager = get_db_logging_manager()
        stats = db_manager.get_log_statistics(log_type=log_type, date=date_str)
        return jsonify(stats), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500

