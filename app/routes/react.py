# app/routes/react.py
from config import DATA_ROOT
from flask import Blueprint, abort, send_from_directory, send_file

react_bp = Blueprint('react', __name__)

# Path to React build directory
REACT_DIR = DATA_ROOT / 'dashboard'

# TO-DO Conflicts with another route in __init__.py
@react_bp.route('/static/<path:filename>')
def serve_static(filename):
    """
    Serve static files from React build directory, handling nested folders
    """
    return send_from_directory(REACT_DIR / 'static', filename)

@react_bp.route('/assets/<path:filename>')
def serve_assets(filename):
    """
    Serve asset files from React build directory
    """
    return send_from_directory(REACT_DIR / 'assets', filename)

@react_bp.route('/<path:path>')
def serve_build_files(path):
    """Serve other build files (favicon, manifest, etc.)"""
    if path == "api" or path.startswith("api/"):
        abort(404)
    if (REACT_DIR / path).is_file():
        return send_from_directory(REACT_DIR, path)
    return send_file(REACT_DIR / 'index.html')

@react_bp.route('/', defaults={'path': ''})
def serve_react(path):
    """Serve React app for root route"""
    return send_file(REACT_DIR / 'index.html')
