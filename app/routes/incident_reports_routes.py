"""
Incident reports routes for managing incident reports.
"""
import os
import json
from flask import Blueprint, jsonify, request
from flasgger import swag_from
from datetime import datetime
from .route_utils import REPORTS_FOLDER

incident_reports_bp = Blueprint('incident_reports', __name__)

@incident_reports_bp.route('/incident-reports', methods=['POST'])
@swag_from({
    'tags': ['Incident Reports'],
    'summary': 'Create an incident report',
    'parameters': [
        {
            'name': 'body',
            'in': 'body',
            'required': True,
            'schema': {
                'type': 'object',
                'required': ['name', 'severity', 'messages', 'messageCount', 'channels_involved', 'created_at'],
                'properties': {
                    'name': {'type': 'string'},
                    'severity': {'type': 'string'},
                    'messages': {'type': 'array'},
                    'messageCount': {'type': 'integer'},
                    'channels_involved': {'type': 'array'},
                    'created_at': {'type': 'string'},
                    'startTime': {'type': 'string'},
                    'endTime': {'type': 'string'},
                    'description': {'type': 'string'},
                    'tags': {'type': 'object'}
                }
            }
        }
    ],
    'responses': {
        '201': {'description': 'Incident report created successfully'},
        '400': {'description': 'Bad request'},
        '500': {'description': 'Server error'}
    }
})
def create_incident_report():
    try:
        data = request.get_json()
        if not isinstance(data, dict):
            return jsonify({'error': 'Invalid data format'}), 400
        
        required_fields = ['name', 'severity', 'messages', 'messageCount', 'channels_involved', 'created_at']
        for field in required_fields:
            if field not in data:
                return jsonify({'error': f'Missing required field: {field}'}), 400

        timestamp = datetime.utcnow().strftime('%Y%m%d_%H%M%S')
        report_id = f'report_{timestamp}'
        file_path = os.path.join(REPORTS_FOLDER, f'{report_id}.json')

        report_data = {
            'id': report_id,
            'name': data['name'],
            'startTime': data.get('startTime', ''),
            'endTime': data.get('endTime', ''),
            'description': data.get('description', ''),
            'severity': data['severity'],
            'messages': data['messages'],
            'messageCount': data['messageCount'],
            'channels_involved': data['channels_involved'],
            'tags': data.get('tags', {}),
            'created_at': data['created_at']
        }

        with open(file_path, 'w') as f:
            json.dump(report_data, f, indent=4)

        return jsonify({
            'message': 'Incident report created successfully',
            'report_id': report_id,
            'file_path': file_path
        }), 201

    except Exception as e:
        return jsonify({'error': str(e)}), 500

@incident_reports_bp.route('/incident-reports', methods=['GET'])
@swag_from({
    'tags': ['Incident Reports'],
    'summary': 'Get all incident reports',
    'responses': {
        '200': {'description': 'List of incident reports'},
        '500': {'description': 'Server error'}
    }
})
def get_all_incident_reports():
    try:
        reports = []
        for filename in os.listdir(REPORTS_FOLDER):
            if filename.endswith('.json'):
                file_path = os.path.join(REPORTS_FOLDER, filename)
                try:
                    with open(file_path, 'r') as f:
                        report_data = json.load(f)
                        reports.append(report_data)
                except (json.JSONDecodeError, IOError) as e:
                    # Log error but continue processing other files
                    print(f"Error reading {filename}: {str(e)}")
                    continue
        return jsonify(reports), 200
    except Exception as e:
        return jsonify({'error': f'Failed to retrieve reports: {str(e)}'}), 500


@incident_reports_bp.route('/incident-reports/<report_id>', methods=['POST', 'PATCH'])
@swag_from({
    'tags': ['Incident Reports'],
    'summary': 'Update an incident report',
    'parameters': [
        {
            'name': 'report_id',
            'in': 'path',
            'type': 'string',
            'required': True,
            'description': 'Report ID'
        },
        {
            'name': 'body',
            'in': 'body',
            'required': True,
            'schema': {
                'type': 'object',
                'description': 'Fields to update'
            }
        }
    ],
    'responses': {
        '200': {'description': 'Incident report updated successfully'},
        '400': {'description': 'Bad request'},
        '404': {'description': 'Report not found'},
        '500': {'description': 'Server error'}
    }
})
def update_incident_report(report_id):
    """
    Update an existing incident report by report_id.
    The report_id should be the string like 'report_20240609_123456'.
    The file is stored at db/reports/{report_id}.json.
    Accepts a JSON body with any fields to update.
    """
    try:
        file_path = os.path.join(REPORTS_FOLDER, f'{report_id}.json')
        if not os.path.exists(file_path):
            return jsonify({'error': 'Report not found'}), 404

        data = request.get_json()
        if not isinstance(data, dict):
            return jsonify({'error': 'Invalid data format'}), 400

        # Load existing report
        with open(file_path, 'r') as f:
            report_data = json.load(f)

        # Update only provided fields
        for key, value in data.items():
            report_data[key] = value

        # Save updated report
        with open(file_path, 'w') as f:
            json.dump(report_data, f, indent=4)

        return jsonify({'message': 'Incident report updated successfully', 'report_id': report_id, 'file_path': file_path}), 200

    except Exception as e:
        return jsonify({'error': str(e)}), 500


@incident_reports_bp.route('/incident-reports/<report_id>', methods=['DELETE'])
@swag_from({
    'tags': ['Incident Reports'],
    'summary': 'Delete an incident report',
    'parameters': [
        {
            'name': 'report_id',
            'in': 'path',
            'type': 'string',
            'required': True,
            'description': 'Report ID'
        }
    ],
    'responses': {
        '200': {'description': 'Incident report deleted successfully'},
        '404': {'description': 'Report not found'},
        '500': {'description': 'Server error'}
    }
})
def delete_incident_report(report_id):
    try:
        file_path = os.path.join(REPORTS_FOLDER, f'{report_id}.json')
        if not os.path.exists(file_path):
            return jsonify({'error': 'Report not found'}), 404

        os.remove(file_path)
        return jsonify({'message': 'Incident report deleted successfully', 'report_id': report_id}), 200

    except Exception as e:
        return jsonify({'error': str(e)}), 500

