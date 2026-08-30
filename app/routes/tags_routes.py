"""
Tags routes for managing tags and recording tags.
"""
import sqlite3
from flask import Blueprint, jsonify, request
from flasgger import swag_from
from datetime import datetime
from .route_utils import load_tags, save_tags, DB_PATH
from ..services.settings_manager import get_settings_manager

tags_bp = Blueprint('tags', __name__)

@tags_bp.route('/tags', methods=['GET'])
@swag_from({
    'tags': ['Tags'],
    'summary': 'List tags',
    'parameters': [
        {
            'name': 'search',
            'in': 'query',
            'type': 'string',
            'required': False,
            'description': 'Search string to filter tags'
        },
        {
            'name': 'category',
            'in': 'query',
            'type': 'string',
            'required': False,
            'description': 'Filter by category'
        }
    ],
    'responses': {
        '200': {'description': 'List of tags'}
    }
})
def list_tags():
    """
    Query parameters:
      - search: string to filter tag.name (case-insensitive)
      - category: exact match against tag.category; 'All' or missing = no filter
    """
    tags = load_tags()
    search = request.args.get('search', '').strip().lower()
    category = request.args.get('category', '').strip()
    if search:
        tags = [t for t in tags if search in t['name'].lower()]
    if category and category.lower() != 'all':
        tags = [t for t in tags if t['category'] == category]
    return jsonify(tags), 200

@tags_bp.route('/tags', methods=['POST'])
@swag_from({
    'tags': ['Tags'],
    'summary': 'Create a tag',
    'parameters': [
        {
            'name': 'body',
            'in': 'body',
            'required': True,
            'schema': {
                'type': 'object',
                'required': ['name', 'category'],
                'properties': {
                    'name': {'type': 'string'},
                    'category': {'type': 'string'},
                    'color': {'type': 'string'},
                    'usageCount': {'type': 'integer'}
                }
            }
        }
    ],
    'responses': {
        '201': {'description': 'Tag created successfully'},
        '400': {'description': 'Bad request'}
    }
})
def create_tag():
    """
    Expects JSON body with:
      - name (string)
      - category (string)
      - (optional) color (string, e.g. 'bg-red-500')
    """
    data = request.get_json()
    if not isinstance(data, dict):
        return jsonify({'error': 'Invalid JSON'}), 400

    for field in ('name', 'category'):
        if field not in data or not data[field].strip():
            return jsonify({'error': f'Missing required field: {field}'}), 400

    tags = load_tags()
    new_id = max((t['id'] for t in tags), default=0) + 1

    tag = {
        'id': new_id,
        'name': data['name'].strip(),
        'category': data['category'].strip(),
        'usageCount': data.get('usageCount', 0),
        'color': data.get('color', ''),
        'created_at': datetime.utcnow().isoformat() + 'Z'
    }

    tags.append(tag)
    save_tags(tags)
    return jsonify(tag), 201

@tags_bp.route('/tags/<int:tag_id>', methods=['PUT', 'PATCH'])
@swag_from({
    'tags': ['Tags'],
    'summary': 'Update a tag',
    'parameters': [
        {
            'name': 'tag_id',
            'in': 'path',
            'type': 'integer',
            'required': True,
            'description': 'Tag ID'
        },
        {
            'name': 'body',
            'in': 'body',
            'required': True,
            'schema': {
                'type': 'object',
                'properties': {
                    'name': {'type': 'string'},
                    'category': {'type': 'string'},
                    'color': {'type': 'string'},
                    'usageCount': {'type': 'integer'}
                }
            }
        }
    ],
    'responses': {
        '200': {'description': 'Tag updated successfully'},
        '400': {'description': 'Bad request'},
        '404': {'description': 'Tag not found'}
    }
})
def update_tag(tag_id):
    """
    Body may include any of:
      - name, category, color, usageCount
    """
    data = request.get_json()
    if not isinstance(data, dict):
        return jsonify({'error': 'Invalid JSON'}), 400

    tags = load_tags()
    for tag in tags:
        if tag['id'] == tag_id:
            # Only update allowed fields
            for field in ('name', 'category', 'color', 'usageCount'):
                if field in data:
                    tag[field] = data[field]
            save_tags(tags)
            return jsonify(tag), 200

    return jsonify({'error': 'Tag not found'}), 404

@tags_bp.route('/tags/<int:tag_id>', methods=['DELETE'])
@swag_from({
    'tags': ['Tags'],
    'summary': 'Delete a tag',
    'parameters': [
        {
            'name': 'tag_id',
            'in': 'path',
            'type': 'integer',
            'required': True,
            'description': 'Tag ID'
        }
    ],
    'responses': {
        '200': {'description': 'Tag deleted successfully'},
        '404': {'description': 'Tag not found'}
    }
})
def delete_tag(tag_id):
    tags = load_tags()
    filtered = [t for t in tags if t['id'] != tag_id]
    if len(filtered) == len(tags):
        return jsonify({'error': 'Tag not found'}), 404

    save_tags(filtered)
    return jsonify({'message': 'Tag deleted'}), 200


@tags_bp.route('/recordings_tag/<int:recording_id>/tags', methods=['GET'])
@swag_from({
    'tags': ['Recordings'],
    'summary': 'Get tags for a recording',
    'parameters': [
        {
            'name': 'recording_id',
            'in': 'path',
            'type': 'integer',
            'required': True,
            'description': 'Recording ID'
        }
    ],
    'responses': {
        '200': {'description': 'List of tags for the recording'}
    }
})
def get_recording_tags(recording_id):
    """Return list of tags attached to a recording."""
    
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row  # Enable dict-like access to rows
    cur = conn.cursor()
    cur.execute(
        "SELECT tag FROM tag_relation WHERE recording_id = ?",
        (recording_id,)
    )
    tags = [row['tag'] for row in cur.fetchall()]
    conn.close()
    return jsonify(tags), 200

@tags_bp.route('/recordings_tag/batch/tags', methods=['POST'])
@swag_from({
    'tags': ['Recordings'],
    'summary': 'Get tags for multiple recordings',
    'parameters': [
        {
            'name': 'body',
            'in': 'body',
            'required': True,
            'schema': {
                'type': 'object',
                'required': ['recording_ids'],
                'properties': {
                    'recording_ids': {
                        'type': 'array',
                        'items': {'type': 'integer'},
                        'description': 'Array of recording IDs (max 1000)'
                    }
                }
            }
        }
    ],
    'responses': {
        '200': {'description': 'Tags grouped by recording ID'},
        '400': {'description': 'Bad request'}
    }
})
def get_batch_recording_tags():
    """Return tags for multiple recordings in a single request."""
    data = request.get_json() or {}
    recording_ids = data.get('recording_ids', [])
    
    if not recording_ids or not isinstance(recording_ids, list):
        return jsonify({'error': 'Missing or invalid recording_ids array'}), 400
    
    if len(recording_ids) > 1000:  # Limit batch size to prevent abuse
        return jsonify({'error': 'Batch size too large (max 1000)'}), 400
    
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row  # Enable dict-like access to rows
    cur = conn.cursor()
    
    # Create placeholders for the IN clause
    placeholders = ','.join(['?' for _ in recording_ids])
    cur.execute(
        f"SELECT recording_id, tag FROM tag_relation WHERE recording_id IN ({placeholders})",
        recording_ids
    )
    
    # Group tags by recording_id
    tags_by_recording = {}
    for row in cur.fetchall():
        recording_id = row['recording_id']
        tag = row['tag']
        if recording_id not in tags_by_recording:
            tags_by_recording[recording_id] = []
        tags_by_recording[recording_id].append(tag)
    
    conn.close()
    return jsonify(tags_by_recording), 200

@tags_bp.route('/recordings_tag/<int:recording_id>/tags', methods=['POST'])
@swag_from({
    'tags': ['Recordings'],
    'summary': 'Add a tag to a recording',
    'parameters': [
        {
            'name': 'recording_id',
            'in': 'path',
            'type': 'integer',
            'required': True,
            'description': 'Recording ID'
        },
        {
            'name': 'body',
            'in': 'body',
            'required': True,
            'schema': {
                'type': 'object',
                'required': ['tag'],
                'properties': {
                    'tag': {'type': 'string'}
                }
            }
        }
    ],
    'responses': {
        '201': {'description': 'Tag added successfully'},
        '400': {'description': 'Bad request'},
        '409': {'description': 'Tag already applied'}
    }
})
def add_recording_tag(recording_id):
    """Attach a tag (by name) to a given recording."""
    data = request.get_json() or {}
    tag = data.get('tag', '').strip()
    if not tag:
        return jsonify({'error': 'Missing tag'}), 400

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    try:
        cur.execute(
            "INSERT INTO tag_relation (recording_id, tag) VALUES (?, ?)",
            (recording_id, tag)
        )
        conn.commit()
        
        # Increment usage_count for the tag
        settings_manager = get_settings_manager()
        settings_manager.increment_tag_usage(tag)
    except sqlite3.IntegrityError:
        return jsonify({'error': 'Tag already applied'}), 409
    finally:
        conn.close()
    return jsonify({'recording_id': recording_id, 'tag': tag}), 201

@tags_bp.route('/recordings_tag/<int:recording_id>/tags/<string:tag>', methods=['DELETE'])
@swag_from({
    'tags': ['Recordings'],
    'summary': 'Remove a tag from a recording',
    'parameters': [
        {
            'name': 'recording_id',
            'in': 'path',
            'type': 'integer',
            'required': True,
            'description': 'Recording ID'
        },
        {
            'name': 'tag',
            'in': 'path',
            'type': 'string',
            'required': True,
            'description': 'Tag name'
        }
    ],
    'responses': {
        '200': {'description': 'Tag removed successfully'}
    }
})
def delete_recording_tag(recording_id, tag):
    """Remove a tag from a recording."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "DELETE FROM tag_relation WHERE recording_id = ? AND tag = ?",
        (recording_id, tag)
    )
    deleted = cur.rowcount > 0
    conn.commit()
    conn.close()
    
    # Decrement usage_count for the tag if it was actually deleted
    if deleted:
        settings_manager = get_settings_manager()
        settings_manager.decrement_tag_usage(tag)
    
    return jsonify({'recording_id': recording_id, 'tag': tag}), 200

