"""
Documentation routes.
Serves documentation markdown files.
"""
import os
from config import DATA_ROOT
from flask import Blueprint, send_file, abort
from werkzeug.exceptions import HTTPException
from flasgger import swag_from

docs_bp = Blueprint('docs', __name__)

# Path to docs directory (relative to backend directory)
# __file__ is at backend/app/routes/docs_routes.py
# Go up 3 levels: routes -> app -> backend -> root
DOCS_DIR = DATA_ROOT / 'docs'  # docs is at project root level


@docs_bp.route('/docs/<path:filename>')
@swag_from({
    'tags': ['Documentation'],
    'summary': 'Get documentation file',
    'parameters': [
        {
            'name': 'filename',
            'in': 'path',
            'type': 'string',
            'required': True,
            'description': 'Documentation filename (e.g., EDGE_HARDWARE.md)'
        }
    ],
    'responses': {
        '200': {'description': 'Documentation file'},
        '404': {'description': 'File not found'}
    }
})
def get_documentation(filename):
    """Serve documentation markdown files."""
    try:
        # Security check: ensure filename doesn't contain path traversal
        if '..' in filename or '/' in filename or '\\' in filename:
            abort(403, description="Invalid filename")
        
        # Ensure .md extension
        if not filename.endswith('.md'):
            filename = f"{filename}.md"
        
        # Try docs directory first
        file_path = (DOCS_DIR / filename).resolve()
        
        # Security check: ensure the path is within DOCS_DIR
        
        if file_path.is_relative_to(DOCS_DIR.resolve()) and file_path.is_file():
            return send_file(file_path, mimetype='text/markdown')
        
        # If not found in docs, try root directory (for API.md)
        # root_file_path = os.path.join(ROOT_DIR, filename)
        # root_file_path = os.path.normpath(root_file_path)
        # root_dir_abs = os.path.abspath(ROOT_DIR)
        # root_file_path_abs = os.path.abspath(root_file_path)
        
        # if root_file_path_abs.startswith(root_dir_abs) and os.path.exists(root_file_path) and os.path.isfile(root_file_path):
        #     return send_file(root_file_path, mimetype='text/markdown')
        
        # If the file doesn't exist, return a 404 error
        abort(404, description="Documentation file not found")
    except HTTPException:
        raise
    except Exception as e:
        abort(500, description=f"Error serving documentation: {str(e)}")
