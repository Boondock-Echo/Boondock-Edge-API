"""
Hallucinations management routes.
Handles creation, listing, and deletion of hallucination entries.
"""
import json
import os
import logging
from datetime import datetime
from flask import Blueprint, jsonify, request
from flasgger import swag_from

from ..services.settings_manager import get_settings_manager

_settings_manager = get_settings_manager()

hallucinations_bp = Blueprint('hallucinations', __name__)


@hallucinations_bp.route('/hallucinations', methods=['POST'])
@swag_from({
    'tags': ['Hallucinations'],
    'summary': 'Create a hallucination entry',
    'parameters': [
        {
            'name': 'body',
            'in': 'body',
            'required': True,
            'schema': {
                'type': 'object',
                'required': ['text'],
                'properties': {
                    'text': {'type': 'string'},
                    'type': {'type': 'array', 'items': {'type': 'string'}},
                    'created_by': {'type': 'string'}
                }
            }
        }
    ],
    'responses': {
        '201': {'description': 'Hallucination created successfully'},
        '400': {'description': 'Bad request'}
    }
})
def create_hallucination():
    """
    Expects JSON body with:
      - text (string): The hallucination text to insert.
      - (optional) tags (list of strings)
      - (optional) created_by (string)
    """
    data = request.get_json()
    if not isinstance(data, dict):
        return jsonify({'error': 'Invalid JSON'}), 400

    text = data.get('text', '').strip()
    if not text:
        return jsonify({'error': 'Missing required field: text'}), 400

    # Load existing hallucinations
    hallucinations = _settings_manager.get_all_hallucinations()
    
    new_id = max((h.get('id', 0) for h in hallucinations), default=0) + 1

    hallucination = {
        'id': new_id,
        'text': text,
        'type': data.get('type', []),
        'created_by': data.get('created_by', ''),
        'created_at': datetime.utcnow().isoformat() + 'Z'
    }

    # Save using SettingsManager
    _settings_manager.save_hallucination(hallucination)

    return jsonify(hallucination), 201


@hallucinations_bp.route('/hallucinations', methods=['GET'])
@swag_from({
    'tags': ['Hallucinations'],
    'summary': 'List hallucinations',
    'parameters': [
        {
            'name': 'search',
            'in': 'query',
            'type': 'string',
            'required': False,
            'description': 'Search string to filter hallucinations'
        },
        {
            'name': 'type',
            'in': 'query',
            'type': 'string',
            'required': False,
            'description': 'Filter by type'
        }
    ],
    'responses': {
        '200': {'description': 'List of hallucinations'}
    }
})
def list_hallucinations():
    """
    Returns all hallucinations, with optional search by text and filter by type.
    Query parameters:
      - search: string to filter hallucination.text (case-insensitive)
      - type: exact match against hallucination.type; 'All' or missing = no filter
    """
    # Load hallucinations from database
    raw = _settings_manager.get_all_hallucinations()

    # Each DB row has the shape {id: <row_id>, data: {id, text, type, ...}}.
    # Flatten so callers always receive {id, text, type, ...} at the top level.
    hallucinations = []
    for h in raw:
        inner = h.get('data')
        if isinstance(inner, dict):
            item = dict(inner)
            item['id'] = h['id']  # use the stable DB row id
            hallucinations.append(item)
        else:
            hallucinations.append(h)

    search = request.args.get('search', '').strip().lower()
    halluc_type = request.args.get('type', '').strip()

    if search:
        hallucinations = [h for h in hallucinations if search in h.get('text', '').lower()]
    if halluc_type and halluc_type.lower() != 'all':
        hallucinations = [
            h for h in hallucinations
            if h.get('type') == halluc_type
            or (isinstance(h.get('type'), list) and halluc_type in h.get('type'))
        ]

    return jsonify(hallucinations), 200


@hallucinations_bp.route('/hallucinations/<int:hallucinations_id>', methods=['DELETE'])
@swag_from({
    'tags': ['Hallucinations'],
    'summary': 'Delete a hallucination',
    'parameters': [
        {
            'name': 'hallucinations_id',
            'in': 'path',
            'type': 'integer',
            'required': True,
            'description': 'Hallucination ID'
        }
    ],
    'responses': {
        '200': {'description': 'Hallucination deleted successfully'},
        '404': {'description': 'Hallucination not found'}
    }
})
def delete_hallucination(hallucinations_id):
    """Delete a hallucination entry by ID."""
    # Check if hallucination exists
    hallucinations = _settings_manager.get_all_hallucinations()
    hallucination = next((h for h in hallucinations if h.get('id') == hallucinations_id), None)
    if not hallucination:
        return jsonify({'error': 'Hallucination not found'}), 404

    # Delete using SettingsManager
    success = _settings_manager.delete_hallucination(hallucinations_id)
    if success:
        return jsonify({'message': 'Hallucination deleted'}), 200
    else:
        return jsonify({'error': 'Failed to delete hallucination'}), 500

