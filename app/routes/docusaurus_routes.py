# app/routes/docusaurus_routes.py
from config import CODE_ROOT
from flask import Blueprint, send_from_directory, send_file

docusaurus_bp = Blueprint('docusaurus', __name__)

# Path to Docusaurus build directory
DOCS_BUILD_DIR = CODE_ROOT / 'docs-build'

@docusaurus_bp.route('/docs/')
@docusaurus_bp.route('/docs')
def serve_docs_index():
    """Serve Docusaurus docs index page"""
    index_path = DOCS_BUILD_DIR / 'index.html'
    if index_path.is_file():
        return send_file(index_path)
    return "Documentation not found. Please build the docs site.", 404

@docusaurus_bp.route('/docs/<path:path>')
def serve_docs(path):
    """Serve Docusaurus docs files"""
    # Handle assets (JS, CSS, images, etc.)
    if path.startswith('assets/') or path.startswith('static/'):
        file_path = DOCS_BUILD_DIR / path
        if file_path.is_file():
            return send_from_directory(file_path.parent, file_path.name)

    # Handle other files (robots.txt, favicon, etc.)
    file_path = DOCS_BUILD_DIR / path
    if file_path.is_file():
        return send_from_directory(file_path.parent, file_path.name)    
    # For all other routes, serve index.html (client-side routing)
    index_path = DOCS_BUILD_DIR / 'index.html'
    if index_path.is_file():
        return send_file(index_path)
    
    return "Documentation not found. Please build the docs site.", 404
