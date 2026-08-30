"""
Recording history/versioning routes.
Handles recording history operations including version management and audio file retrieval.
"""
import os
import sqlite3
import logging
from flask import Blueprint, jsonify, request, send_file
from flasgger import swag_from

from ..routes.route_utils import (
    DB_PATH,
    RECORDINGS_DIR,
    get_history_versions,
    revert_to_version,
    convert_to_timezone,
    get_timezone,
)

history_bp = Blueprint('history', __name__)


@history_bp.route('/recording/<int:recording_id>/history', methods=['GET'])
@swag_from({
    'tags': ['Recordings'],
    'summary': 'Get recording history versions',
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
        '200': {'description': 'History versions'},
        '500': {'description': 'Server error'}
    }
})
def get_recording_history(recording_id):
    """Get all history versions for a recording."""
    try:
        history = get_history_versions(recording_id)
        # Get timezone info for response
        timezone_info = get_timezone()
        
        return jsonify({
            'recording_id': recording_id,
            'history': history,
            'total_versions': len(history),
            'timezone': timezone_info
        }), 200
    except Exception as e:
        return jsonify({'error': f'Failed to get history: {str(e)}'}), 500


@history_bp.route('/recording/<int:recording_id>/history/<int:version_number>', methods=['GET'])
@swag_from({
    'tags': ['Recordings'],
    'summary': 'Get a specific history version',
    'parameters': [
        {
            'name': 'recording_id',
            'in': 'path',
            'type': 'integer',
            'required': True,
            'description': 'Recording ID'
        },
        {
            'name': 'version_number',
            'in': 'path',
            'type': 'integer',
            'required': True,
            'description': 'Version number'
        }
    ],
    'responses': {
        '200': {'description': 'History version details'},
        '404': {'description': 'Version not found'},
        '500': {'description': 'Database error'}
    }
})
def get_history_version(recording_id, version_number):
    """Get a specific history version."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    
    try:
        cur.execute(
            "SELECT transcription, audio_filename, created_at, description FROM recording_history WHERE recording_id = ? AND version_number = ?",
            (recording_id, version_number)
        )
        row = cur.fetchone()
        
        if not row:
            return jsonify({'error': f'Version {version_number} not found'}), 404
        
        # Convert timestamp to timezone
        created_at = row[2]
        if created_at:
            created_at_tz = convert_to_timezone(created_at)
        else:
            created_at_tz = created_at
        
        return jsonify({
            'recording_id': recording_id,
            'version_number': version_number,
            'transcription': row[0],
            'audio_filename': row[1],
            'created_at': created_at_tz,
            'description': row[3]
        }), 200
        
    except sqlite3.Error as e:
        return jsonify({'error': f'Database error: {str(e)}'}), 500
    finally:
        conn.close()


@history_bp.route('/recording/<int:recording_id>/history/<int:version_number>/revert', methods=['POST'])
@swag_from({
    'tags': ['Recordings'],
    'summary': 'Revert recording to a specific version',
    'parameters': [
        {
            'name': 'recording_id',
            'in': 'path',
            'type': 'integer',
            'required': True,
            'description': 'Recording ID'
        },
        {
            'name': 'version_number',
            'in': 'path',
            'type': 'integer',
            'required': True,
            'description': 'Version number to revert to'
        }
    ],
    'responses': {
        '200': {'description': 'Recording reverted successfully'},
        '404': {'description': 'Version not found'},
        '500': {'description': 'Server error'}
    }
})
def revert_recording_version(recording_id, version_number):
    """Revert a recording to a specific version."""
    try:
        result = revert_to_version(recording_id, version_number)
        return jsonify({
            'recording_id': recording_id,
            'version_number': version_number,
            'message': result.get('message', 'Successfully reverted to version'),
            'data': result,
            'audio_restored': bool(result.get('audio_filename')),
            'transcription': result.get('transcription')
        }), 200
    except ValueError as e:
        return jsonify({'error': str(e)}), 404
    except Exception as e:
        return jsonify({'error': f'Failed to revert: {str(e)}'}), 500


@history_bp.route('/recording/<int:recording_id>/history/<int:version_number>', methods=['DELETE'])
@swag_from({
    'tags': ['Recordings'],
    'summary': 'Delete a history version',
    'parameters': [
        {
            'name': 'recording_id',
            'in': 'path',
            'type': 'integer',
            'required': True,
            'description': 'Recording ID'
        },
        {
            'name': 'version_number',
            'in': 'path',
            'type': 'integer',
            'required': True,
            'description': 'Version number to delete'
        }
    ],
    'responses': {
        '200': {'description': 'Version deleted successfully'},
        '403': {'description': 'Cannot delete original version'},
        '404': {'description': 'Version not found'},
        '500': {'description': 'Database error'}
    }
})
def delete_history_version(recording_id, version_number):
    """Delete a specific history version."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    
    try:
        # Prevent deletion of original version (version 0)
        if version_number == 0:
            return jsonify({'error': 'Cannot delete the original version'}), 403
        
        # Get the audio filename before deleting
        cur.execute(
            "SELECT audio_filename FROM recording_history WHERE recording_id = ? AND version_number = ?",
            (recording_id, version_number)
        )
        result = cur.fetchone()
        
        if not result:
            return jsonify({'error': f'Version {version_number} not found'}), 404
        
        audio_filename = result[0]
        
        # Delete the version
        cur.execute(
            "DELETE FROM recording_history WHERE recording_id = ? AND version_number = ?",
            (recording_id, version_number)
        )
        
        # If there was an audio file, delete it too
        if audio_filename:
            try:
                audio_path = os.path.join(RECORDINGS_DIR, audio_filename)
                if os.path.exists(audio_path):
                    os.remove(audio_path)
            except Exception as e:
                logging.warning(f"Failed to delete history audio file: {e}")
        
        # Reorder remaining versions to be sequential (but keep original as 0)
        cur.execute(
            "UPDATE recording_history SET version_number = (SELECT COUNT(*) FROM recording_history rh2 WHERE rh2.recording_id = recording_history.recording_id AND rh2.version_number <= recording_history.version_number) WHERE recording_id = ? AND version_number > 0",
            (recording_id,)
        )
        
        conn.commit()
        return jsonify({
            'recording_id': recording_id,
            'version_number': version_number,
            'message': 'Version deleted successfully'
        }), 200
        
    except sqlite3.Error as e:
        conn.rollback()
        return jsonify({'error': f'Database error: {str(e)}'}), 500
    finally:
        conn.close()


@history_bp.route('/recording/<int:recording_id>/history/<int:version_number>/audio', methods=['GET'])
@swag_from({
    'tags': ['Recordings'],
    'summary': 'Get audio file for a history version',
    'parameters': [
        {
            'name': 'recording_id',
            'in': 'path',
            'type': 'integer',
            'required': True,
            'description': 'Recording ID'
        },
        {
            'name': 'version_number',
            'in': 'path',
            'type': 'integer',
            'required': True,
            'description': 'Version number'
        }
    ],
    'responses': {
        '200': {'description': 'Audio file as attachment'},
        '404': {'description': 'History version or audio file not found'},
        '500': {'description': 'Server error'}
    }
})
def get_history_audio(recording_id, version_number):
    """Get the audio file for a specific history version."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    
    try:
        # Get the audio filename for this history version
        cur.execute(
            "SELECT audio_filename FROM recording_history WHERE recording_id = ? AND version_number = ?",
            (recording_id, version_number)
        )
        result = cur.fetchone()
        
        if not result:
            return jsonify({'error': 'History version not found'}), 404
        
        audio_filename = result[0]
        
        if not audio_filename:
            return jsonify({'error': 'No audio file for this history version'}), 404
        
        # Check if the audio file exists
        audio_path = os.path.join(RECORDINGS_DIR, audio_filename)
        if not os.path.exists(audio_path):
            return jsonify({'error': 'Audio file not found'}), 404
        
        # Return the audio file
        return send_file(audio_path, as_attachment=True)
        
    except sqlite3.Error as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()

