"""
Branding routes for managing application branding settings.
"""
import os
import json
from flask import Blueprint, jsonify, request
from flasgger import swag_from
from ..services.settings_manager import get_settings_manager

_settings_manager = get_settings_manager()
branding_bp = Blueprint('branding', __name__)

@branding_bp.route('', methods=['GET'])
@swag_from({
    'tags': ['Branding'],
    'summary': 'Get branding settings',
    'responses': {
        '200': {'description': 'Branding settings'}
    }
})
def get_branding():
    """Fetch and return branding settings."""
    try:
        branding_data = _settings_manager.get_branding()
        
        # Set default values if missing
        branding_data.setdefault('organization_name', '')
        branding_data.setdefault('tagline', '')
        branding_data.setdefault('assets', {
            'logo': None,
            'favicon': None,
            'loader': None
        })
        
        # Return only the fields we support (remove brand_colors and font if present)
        return jsonify({
            'organization_name': branding_data.get('organization_name', ''),
            'tagline': branding_data.get('tagline', ''),
            'assets': branding_data.get('assets', {
                'logo': None,
                'favicon': None,
                'loader': None
            })
        })
    except Exception:
        # Return defaults if branding not found
        return jsonify({
            'organization_name': '',
            'tagline': '',
            'assets': {
                'logo': None,
                'favicon': None,
                'loader': None
            }
        })

@branding_bp.route('', methods=['PUT'])
@swag_from({
    'tags': ['Branding'],
    'summary': 'Update branding settings',
    'parameters': [
        {
            'name': 'body',
            'in': 'body',
            'required': True,
            'schema': {
                'type': 'object',
                'properties': {
                    'organization_name': {'type': 'string'},
                    'tagline': {'type': 'string'},
                    'assets': {
                        'type': 'object',
                        'properties': {
                            'logo': {'type': 'string'},
                            'favicon': {'type': 'string'},
                            'loader': {'type': 'string'}
                        }
                    }
                }
            }
        }
    ],
    'responses': {
        '200': {'description': 'Branding settings updated successfully'},
        '400': {'description': 'Bad request'}
    }
})
def update_branding():
    """Update branding settings in the JSON file."""
    if not request.is_json:
        return jsonify({'error': 'Invalid request format. JSON expected'}), 400
    
    branding_data = request.get_json()

    # Ensure assets exists in request
    assets = branding_data.get('assets', {})
    
    updated_branding = {
        'organization_name': branding_data.get('organization_name', ''),
        'tagline': branding_data.get('tagline', ''),
        'assets': {
            'logo': assets.get('logo', None),
            'favicon': assets.get('favicon', None),
            'loader': assets.get('loader', None)
        }
    }
    
    # Save using SettingsManager
    _settings_manager.save_branding(updated_branding)
    
    return jsonify({'message': 'Branding settings updated successfully'}), 200

