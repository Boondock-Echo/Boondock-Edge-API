"""
Release Notes route — parses RELEASE_NOTES.md and returns structured JSON.
"""
import re
import logging
from config import CODE_ROOT
from flask import Blueprint, jsonify

logger = logging.getLogger(__name__)

release_notes_bp = Blueprint('release_notes', __name__)

# RELEASE_NOTES.md lives at the project root
RELEASE_NOTES_PATH = CODE_ROOT / 'RELEASE_NOTES.md'

def _parse_release_notes(content: str) -> list:
    """
    Parse RELEASE_NOTES.md into a list of release dicts:
    [
      {
        "title": "v2.0 — USB Audio",
        "branch": "main",
        "date": "10 Mar 2026",
        "sections": [
          { "heading": "What's New", "items": ["Feature A", "Feature B"] }
        ]
      },
      ...
    ]
    """
    releases = []

    # Strip the sentinel comment so it doesn't bleed into the last release
    content = re.sub(r'<!--.*?-->', '', content, flags=re.DOTALL)

    # Split on level-2 headings that look like release entries (skip "How to Use" etc.)
    # A release heading matches ## [anything]
    blocks = re.split(r'\n(?=## \[)', content)

    for block in blocks:
        block = block.strip()
        if not block.startswith('## ['):
            continue

        lines = block.splitlines()

        # --- Title ---
        title_match = re.match(r'^## \[(.+?)\]', lines[0])
        if not title_match:
            continue
        title = title_match.group(1).strip()

        # --- Metadata (Branch / Date) ---
        branch = ''
        date = ''
        for line in lines[1:10]:
            b = re.search(r'\*\*Branch:\*\*\s*`?([^`\n]+)`?', line)
            if b:
                branch = b.group(1).strip()
            d = re.search(r'\*\*Date:\*\*\s*(.+)', line)
            if d:
                date = d.group(1).strip()

        # --- Sections (### headings + bullet lists) ---
        sections = []
        current_section = None
        for line in lines:
            h3 = re.match(r'^### (.+)', line)
            if h3:
                if current_section:
                    sections.append(current_section)
                current_section = {'heading': h3.group(1).strip(), 'items': []}
                continue
            bullet = re.match(r'^[-*]\s+(.+)', line)
            if bullet and current_section is not None:
                current_section['items'].append(bullet.group(1).strip())

        if current_section:
            sections.append(current_section)

        releases.append({
            'title': title,
            'branch': branch,
            'date': date,
            'sections': sections,
        })

    return releases


@release_notes_bp.route('/release-notes', methods=['GET'])
def get_release_notes():
    """Return parsed release notes from RELEASE_NOTES.md."""
    try:
        if not RELEASE_NOTES_PATH.is_file():
            logger.warning('RELEASE_NOTES.md not found at %s', RELEASE_NOTES_PATH)
            return jsonify({'error': 'Release notes file not found', 'releases': []}), 404

        with open(RELEASE_NOTES_PATH, 'r', encoding='utf-8') as f:
            content = f.read()

        releases = _parse_release_notes(content)
        return jsonify({'releases': releases})

    except Exception as exc:
        logger.error('Error reading release notes: %s', exc)
        return jsonify({'error': str(exc), 'releases': []}), 500
