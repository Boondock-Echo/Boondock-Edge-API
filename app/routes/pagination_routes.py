"""
Pagination preferences routes for managing user pagination settings.
"""
from flask import Blueprint, jsonify, request
from flasgger import swag_from
from .route_utils import load_pagination_preferences, save_pagination_preferences

pagination_bp = Blueprint('pagination', __name__)

@pagination_bp.route('/pagination-preferences/<username>', methods=['GET'])
@swag_from({
    'tags': ['Settings'],
    'summary': 'Get pagination preferences for a user',
    'parameters': [
        {
            'name': 'username',
            'in': 'path',
            'type': 'string',
            'required': True,
            'description': 'Username'
        }
    ],
    'responses': {
        '200': {'description': 'Pagination preferences'},
        '500': {'description': 'Server error'}
    }
})
def get_pagination_preferences(username):
    """Get pagination preferences for a specific user."""
    try:
        preferences = load_pagination_preferences()
        user_preferences = preferences.get(username, {
            # Default to a smaller page size for a less jittery inbox
            'recordsPerPage': 10,
            'currentPage': 1,
            'reverseSort': False,
            'showFullTimestamps': False
        })
        return jsonify(user_preferences), 200
    except Exception as e:
        return jsonify({'error': f'Failed to load pagination preferences: {str(e)}'}), 500

@pagination_bp.route('/pagination-preferences/<username>', methods=['POST'])
@swag_from({
    'tags': ['Settings'],
    'summary': 'Save pagination preferences for a user',
    'parameters': [
        {
            'name': 'username',
            'in': 'path',
            'type': 'string',
            'required': True,
            'description': 'Username'
        },
        {
            'name': 'body',
            'in': 'body',
            'required': True,
            'schema': {
                'type': 'object',
                'properties': {
                    'recordsPerPage': {'type': 'integer', 'minimum': 1},
                    'currentPage': {'type': 'integer', 'minimum': 1},
                    'reverseSort': {'type': 'boolean'},
                    'showFullTimestamps': {'type': 'boolean'}
                }
            }
        }
    ],
    'responses': {
        '200': {'description': 'Preferences saved successfully'},
        '400': {'description': 'Bad request'},
        '500': {'description': 'Server error'}
    }
})
def save_pagination_preferences_api(username):
    """Save pagination preferences for a specific user."""
    try:
        data = request.get_json()
        if not data:
            return jsonify({'error': 'No data provided'}), 400
        
        # Default to 10 records per page if not provided
        records_per_page = data.get('recordsPerPage', 10)
        current_page = data.get('currentPage', 1)
        reverse_sort = data.get('reverseSort', False)
        show_full_timestamps = data.get('showFullTimestamps', False)
        
        # Validate inputs
        if not isinstance(records_per_page, int) or records_per_page < 1:
            return jsonify({'error': 'Invalid recordsPerPage value'}), 400
        if not isinstance(current_page, int) or current_page < 1:
            return jsonify({'error': 'Invalid currentPage value'}), 400
        if not isinstance(reverse_sort, bool):
            return jsonify({'error': 'Invalid reverseSort value'}), 400
        if not isinstance(show_full_timestamps, bool):
            return jsonify({'error': 'Invalid showFullTimestamps value'}), 400
        
        preferences = load_pagination_preferences()
        # Preserve existing preferences and only update provided ones
        existing_prefs = preferences.get(username, {})
        preferences[username] = {
            'recordsPerPage': records_per_page if 'recordsPerPage' in data else existing_prefs.get('recordsPerPage', 10),
            'currentPage': current_page if 'currentPage' in data else existing_prefs.get('currentPage', 1),
            'reverseSort': reverse_sort if 'reverseSort' in data else existing_prefs.get('reverseSort', False),
            'showFullTimestamps': show_full_timestamps if 'showFullTimestamps' in data else existing_prefs.get('showFullTimestamps', False)
        }
        
        if save_pagination_preferences(preferences):
            return jsonify({'message': 'Pagination preferences saved successfully'}), 200
        else:
            return jsonify({'error': 'Failed to save pagination preferences'}), 500
            
    except Exception as e:
        return jsonify({'error': f'Failed to save pagination preferences: {str(e)}'}), 500

