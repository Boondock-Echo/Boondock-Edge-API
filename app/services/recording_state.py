"""Shared classification and persistence helpers for recording state."""

import json
import re
import time
from typing import Iterable, Mapping, Optional

from .settings_manager import get_settings_manager


def is_hallucination(
    transcription: str,
    patterns: Optional[Iterable[Mapping]] = None,
) -> bool:
    """Classify one successfully returned transcription.

    An empty or whitespace-only successful result is a hallucination. Literal
    patterns use substring matching; regex patterns use Python ``re`` syntax.
    This function is only for successful transcription results. Failed or
    skipped attempts must not call it.
    """
    if not isinstance(transcription, str):
        raise TypeError('Successful transcription must be a string')
    if not transcription.strip():
        return True

    if patterns is None:
        patterns = get_settings_manager().get_all_hallucinations()

    for item in patterns:
        pattern = item['pattern']
        case_sensitive = item.get('case_sensitive', False)
        if item.get('match_type', 'literal') == 'regex':
            flags = 0 if case_sensitive else re.IGNORECASE
            if re.search(pattern, transcription, flags=flags):
                return True
        else:
            haystack = transcription if case_sensitive else transcription.casefold()
            needle = pattern if case_sensitive else pattern.casefold()
            if needle in haystack:
                return True
    return False


def match_keywords(transcription: str, keywords=None):
    """Snapshot matching group keywords; range offsets use JavaScript UTF-16 units."""
    if keywords is None:
        groups = get_settings_manager().get_all_groups()
        keywords = {
            keyword['id']: keyword
            for group in groups.values()
            for keyword in group.get('keyword_details', [])
        }.values()

    matches = []
    # Build offsets once, including surrogate pairs for characters such as emoji.
    offsets = [0]
    for character in transcription:
        offsets.append(offsets[-1] + (2 if ord(character) > 0xFFFF else 1))
    for keyword in keywords:
        pattern = keyword.get('pattern', '')
        if not pattern:
            continue
        expression = pattern if keyword.get('match_type') == 'regex' else re.escape(pattern)
        flags = 0 if keyword.get('case_sensitive') else re.IGNORECASE
        try:
            ranges = [
                {'start': offsets[match.start()], 'end': offsets[match.end()]}
                for match in re.finditer(expression, transcription, flags)
                if match.end() > match.start()
            ]
        except re.error:
            continue
        if ranges:
            matches.append({
                'id': keyword['id'], 'pattern': pattern,
                'match_type': keyword.get('match_type', 'literal'),
                'case_sensitive': bool(keyword.get('case_sensitive', False)),
                'count': len(ranges), 'ranges': ranges,
            })
    return matches


def update_transcription(connection, recording_id: int, transcription: str) -> bool:
    """Update transcript matches and classification without triggering actions."""
    hallucination = is_hallucination(transcription)
    matches = match_keywords(transcription)
    cursor = connection.execute(
        '''UPDATE recordings
           SET transcription = ?, is_hallucination = ?, matched_keywords = ?,
               updated_at = MAX(updated_at + 1, ?)
           WHERE id = ?''',
        (
            transcription, hallucination, json.dumps(matches),
            time.time_ns() // 1_000_000, recording_id,
        ),
    )
    return cursor.rowcount > 0
