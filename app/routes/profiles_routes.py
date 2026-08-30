"""
Profile management routes.
Handles profile CRUD operations and feature management.
"""
import json
import logging
from flask import Blueprint, jsonify, request
from flasgger import swag_from

from ..utils.logging_setup import error_logger
from ..utils.auth import (
    is_token_valid,
    VALID_TOKENS,
)
from ..utils.profile_utils import (
    load_profiles,
    save_profiles,
    get_profile,
    get_all_features,
)
from ..services.settings_manager import get_settings_manager

_settings_manager = get_settings_manager()

profiles_bp = Blueprint('profiles', __name__)


@profiles_bp.route('/profiles', methods=['GET'])
@swag_from({
    'tags': ['Profiles'],
    'summary': 'Get all profiles',
    'responses': {
        '200': {'description': 'List of profiles'},
        '500': {'description': 'Server error'}
    }
})
def get_profiles():
    """Get all profiles."""
    try:
        profiles = load_profiles()
        return jsonify(profiles), 200
    except Exception as e:
        logging.error(f"Get profiles error: {e}")
        return jsonify({'error': 'Internal server error'}), 500


@profiles_bp.route('/profiles/<profile_name>', methods=['GET'])
@swag_from({
    'tags': ['Profiles'],
    'summary': 'Get a specific profile',
    'parameters': [
        {
            'name': 'profile_name',
            'in': 'path',
            'type': 'string',
            'required': True
        }
    ],
    'responses': {
        '200': {'description': 'Profile data'},
        '404': {'description': 'Profile not found'},
        '500': {'description': 'Server error'}
    }
})
def get_profile_by_name(profile_name):
    """Get a specific profile by name."""
    try:
        profile = get_profile(profile_name)
        if not profile:
            return jsonify({'error': 'Profile not found'}), 404
        return jsonify(profile), 200
    except Exception as e:
        logging.error(f"Get profile error: {e}")
        return jsonify({'error': 'Internal server error'}), 500


@profiles_bp.route('/profiles', methods=['POST'])
@swag_from({
    'tags': ['Profiles'],
    'summary': 'Create a new profile (admin only)',
    'parameters': [
        {
            'name': 'body',
            'in': 'body',
            'required': True,
            'schema': {
                'type': 'object',
                'required': ['name', 'features'],
                'properties': {
                    'name': {'type': 'string'},
                    'description': {'type': 'string'},
                    'features': {'type': 'object'}
                }
            }
        }
    ],
    'responses': {
        '201': {'description': 'Profile created successfully'},
        '400': {'description': 'Bad request'},
        '403': {'description': 'Admin access required'},
        '409': {'description': 'Profile already exists'},
        '500': {'description': 'Server error'}
    }
})
def create_profile():
    """Create a new profile (admin only)."""
    try:
        token = request.headers.get('Authorization', '').replace('Bearer ', '')
        if not token or not is_token_valid(token):
            return jsonify({'error': 'Unauthorized'}), 401
        
        token_data = VALID_TOKENS.get(token, {})
        requester_role = token_data.get('role', 'member')
        
        if requester_role != 'admin':
            return jsonify({'error': 'Admin access required'}), 403
        
        data = request.get_json()
        profile_name = data.get('name', '').strip()
        
        if not profile_name:
            return jsonify({'error': 'Profile name is required'}), 400
        
        profiles = load_profiles()
        
        if profile_name in profiles:
            return jsonify({'error': 'Profile already exists'}), 409
        
        # Validate features
        all_features = {f['key']: False for f in get_all_features()}
        provided_features = data.get('features', {})
        
        # Merge with defaults
        for key in all_features:
            if key in provided_features:
                all_features[key] = bool(provided_features[key])
        
        new_profile = {
            'name': profile_name,
            'description': data.get('description', ''),
            'isDefault': False,
            'features': all_features
        }
        
        profiles[profile_name] = new_profile
        save_profiles(profiles)
        
        return jsonify({'message': 'Profile created successfully', 'profile': new_profile}), 201
        
    except Exception as e:
        logging.error(f"Create profile error: {e}")
        return jsonify({'error': 'Internal server error'}), 500


@profiles_bp.route('/profiles/<profile_name>', methods=['PUT'])
@swag_from({
    'tags': ['Profiles'],
    'summary': 'Update a profile (admin only)',
    'parameters': [
        {
            'name': 'profile_name',
            'in': 'path',
            'type': 'string',
            'required': True
        },
        {
            'name': 'body',
            'in': 'body',
            'required': True,
            'schema': {
                'type': 'object',
                'properties': {
                    'description': {'type': 'string'},
                    'features': {'type': 'object'}
                }
            }
        }
    ],
    'responses': {
        '200': {'description': 'Profile updated successfully'},
        '400': {'description': 'Bad request'},
        '403': {'description': 'Admin access required'},
        '404': {'description': 'Profile not found'},
        '500': {'description': 'Server error'}
    }
})
def update_profile(profile_name):
    """Update a profile (admin only). Cannot update default profiles."""
    try:
        token = request.headers.get('Authorization', '').replace('Bearer ', '')
        if not token or not is_token_valid(token):
            return jsonify({'error': 'Unauthorized'}), 401
        
        token_data = VALID_TOKENS.get(token, {})
        requester_role = token_data.get('role', 'member')
        
        if requester_role != 'admin':
            return jsonify({'error': 'Admin access required'}), 403
        
        profiles = load_profiles()
        
        if profile_name not in profiles:
            return jsonify({'error': 'Profile not found'}), 404
        
        profile = profiles[profile_name]
        
        # Prevent modification of default profiles
        if profile.get('isDefault', False):
            return jsonify({'error': 'Cannot modify default profiles'}), 400
        
        data = request.get_json()
        
        # Update description
        if 'description' in data:
            profile['description'] = data['description']
        
        # Update features
        if 'features' in data:
            all_features = {f['key']: False for f in get_all_features()}
            provided_features = data.get('features', {})
            
            # Merge with existing features
            for key in all_features:
                if key in provided_features:
                    all_features[key] = bool(provided_features[key])
                elif key in profile.get('features', {}):
                    all_features[key] = profile['features'][key]
            
            profile['features'] = all_features
        
        profiles[profile_name] = profile
        save_profiles(profiles)
        
        return jsonify({'message': 'Profile updated successfully', 'profile': profile}), 200
        
    except Exception as e:
        logging.error(f"Update profile error: {e}")
        return jsonify({'error': 'Internal server error'}), 500


@profiles_bp.route('/profiles/<profile_name>', methods=['DELETE'])
@swag_from({
    'tags': ['Profiles'],
    'summary': 'Delete a profile (admin only)',
    'parameters': [
        {
            'name': 'profile_name',
            'in': 'path',
            'type': 'string',
            'required': True
        }
    ],
    'responses': {
        '200': {'description': 'Profile deleted successfully'},
        '400': {'description': 'Cannot delete default profile'},
        '403': {'description': 'Admin access required'},
        '404': {'description': 'Profile not found'},
        '500': {'description': 'Server error'}
    }
})
def delete_profile(profile_name):
    """Delete a profile (admin only). Cannot delete default profiles."""
    try:
        token = request.headers.get('Authorization', '').replace('Bearer ', '')
        if not token or not is_token_valid(token):
            return jsonify({'error': 'Unauthorized'}), 401
        
        token_data = VALID_TOKENS.get(token, {})
        requester_role = token_data.get('role', 'member')
        
        if requester_role != 'admin':
            return jsonify({'error': 'Admin access required'}), 403
        
        profiles = load_profiles()
        
        if profile_name not in profiles:
            return jsonify({'error': 'Profile not found'}), 404
        
        profile = profiles[profile_name]
        
        # Prevent deletion of default profiles
        if profile.get('isDefault', False):
            return jsonify({'error': 'Cannot delete default profiles'}), 400
        
        # Check if any users are using this profile
        users = _settings_manager.get_all_users()
        
        users_with_profile = [email for email, user in users.items() if user.get('profile') == profile_name]
        if users_with_profile:
            return jsonify({
                'error': f'Cannot delete profile. {len(users_with_profile)} user(s) are using this profile.',
                'users': users_with_profile
            }), 400
        
        del profiles[profile_name]
        save_profiles(profiles)
        
        return jsonify({'message': 'Profile deleted successfully'}), 200
        
    except Exception as e:
        logging.error(f"Delete profile error: {e}")
        return jsonify({'error': 'Internal server error'}), 500


@profiles_bp.route('/profiles/features', methods=['GET'])
@swag_from({
    'tags': ['Profiles'],
    'summary': 'Get list of all available features',
    'responses': {
        '200': {'description': 'List of features'},
        '500': {'description': 'Server error'}
    }
})
def get_features():
    """Get list of all available features."""
    try:
        features = get_all_features()
        return jsonify(features), 200
    except Exception as e:
        logging.error(f"Get features error: {e}")
        return jsonify({'error': 'Internal server error'}), 500

