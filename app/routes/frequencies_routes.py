"""
Frequencies routes for managing frequency entries.
"""
import json
from flask import Blueprint, jsonify, request
from flasgger import swag_from
from ..services.settings_manager import get_settings_manager

_settings_manager = get_settings_manager()

frequencies_bp = Blueprint('frequencies', __name__)

@frequencies_bp.route('/frequencies', methods=['GET'])
@swag_from({
    'tags': ['Frequencies'],
    'summary': 'Get all frequencies',
    'responses': {
        '200': {'description': 'List of all frequencies'}
    }
})
def get_frequencies():
    """Get all frequency entries."""
    frequencies_data = _settings_manager.get_all_frequencies()
    return jsonify(frequencies_data)

@frequencies_bp.route('/frequencies', methods=['POST'])
@swag_from({
    'tags': ['Frequencies'],
    'summary': 'Add a new frequency',
    'parameters': [
        {
            'name': 'body',
            'in': 'body',
            'required': True,
            'schema': {
                'type': 'object',
                'required': ['name', 'frequency', 'type', 'tone', 'tag', 'person', 'status'],
                'properties': {
                    'name': {'type': 'string'},
                    'frequency': {'type': 'number'},
                    'type': {'type': 'string'},
                    'tone': {'type': 'string'},
                    'tag': {'type': 'string'},
                    'person': {'type': 'string'},
                    'status': {'type': 'string'}
                }
            }
        }
    ],
    'responses': {
        '201': {'description': 'Frequency added successfully'},
        '400': {'description': 'Bad request'}
    }
})
def add_frequency():
    """Add a new frequency entry."""
    try:
        new_freq = request.json
        required_fields = ['name', 'frequency', 'type', 'tone', 'tag', 'person', 'status']
        
        # Validate required fields
        for field in required_fields:
            if field not in new_freq:
                return jsonify({'error': f'Missing required field: {field}'}), 400

        # Ensure proper data types and defaults
        new_freq['frequency'] = float(new_freq['frequency'])
        new_freq['status'] = new_freq.get('status', 'active')
        
        # Don't set ID - let save_frequency auto-generate it for new records
        # Remove ID if present to ensure INSERT instead of UPDATE
        if 'id' in new_freq:
            del new_freq['id']
        
        # Save using SettingsManager - this will return the new ID
        new_id = _settings_manager.save_frequency(new_freq)
        
        if new_id == -1:
            return jsonify({'error': 'Failed to save frequency'}), 500
        
        # Add the ID to the response
        new_freq['id'] = new_id

        return jsonify(new_freq), 201
    except Exception as e:
        return jsonify({'error': str(e)}), 400

@frequencies_bp.route('/frequencies/<int:freq_id>', methods=['PUT'])
@swag_from({
    'tags': ['Frequencies'],
    'summary': 'Update frequency',
    'parameters': [
        {
            'name': 'freq_id',
            'in': 'path',
            'type': 'integer',
            'required': True,
            'description': 'Frequency ID'
        },
        {
            'name': 'body',
            'in': 'body',
            'required': True,
            'schema': {
                'type': 'object',
                'properties': {
                    'name': {'type': 'string'},
                    'frequency': {'type': 'number'},
                    'type': {'type': 'string'},
                    'tone': {'type': 'string'},
                    'tag': {'type': 'string'},
                    'person': {'type': 'string'},
                    'status': {'type': 'string'}
                }
            }
        }
    ],
    'responses': {
        '200': {'description': 'Frequency updated successfully'},
        '400': {'description': 'Bad request'},
        '404': {'description': 'Frequency not found'}
    }
})
def update_frequency(freq_id):
    """Update an existing frequency by ID."""
    try:
        update_data = request.json
        frequencies_data = _settings_manager.get_all_frequencies()

        for freq in frequencies_data:
            if freq.get('id') == freq_id:
                # Update only provided fields
                for key, value in update_data.items():
                    if key in ['name', 'frequency', 'type', 'tone', 'tag', 'person', 'status']:
                        freq[key] = value
                
                # Ensure frequency is stored as float
                if 'frequency' in update_data:
                    freq['frequency'] = float(freq['frequency'])

                # Save using SettingsManager
                _settings_manager.save_frequency(freq)
                    
                return jsonify(freq)

        return jsonify({'error': 'Frequency not found'}), 404
    except Exception as e:
        return jsonify({'error': str(e)}), 400

@frequencies_bp.route('/frequencies/<int:freq_id>', methods=['DELETE'])
@swag_from({
    'tags': ['Frequencies'],
    'summary': 'Delete frequency',
    'parameters': [
        {
            'name': 'freq_id',
            'in': 'path',
            'type': 'integer',
            'required': True,
            'description': 'Frequency ID'
        }
    ],
    'responses': {
        '200': {'description': 'Frequency deleted successfully'},
        '400': {'description': 'Bad request'},
        '404': {'description': 'Frequency not found'}
    }
})
def delete_frequency(freq_id):
    """Delete a frequency entry by ID."""
    try:
        frequencies_data = _settings_manager.get_all_frequencies()
        original_length = len(frequencies_data)
        
        # Find and delete the frequency
        freq_to_delete = next((freq for freq in frequencies_data if freq.get('id') == freq_id), None)
        if not freq_to_delete:
            return jsonify({'error': 'Frequency not found'}), 404

        # Delete using SettingsManager
        _settings_manager.delete_frequency(freq_id)

        return jsonify({'message': 'Frequency deleted successfully'})
    except Exception as e:
        return jsonify({'error': str(e)}), 400

