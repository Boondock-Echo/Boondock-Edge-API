"""Per-channel duplicate detection based on the original filename and file size.

The historical module name and public function names are retained so older callers do
not need to change at the same time as the duplicate detection implementation.
"""

import json
#import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

from config import Config

DUPLICATE_CACHE_FILE = Config.get_db_dir() / 'duplicate_file_cache.json'
# Compatibility for administrative code which imports the old constant.
CRC_CACHE_FILE = DUPLICATE_CACHE_FILE
_cache_lock = Lock()


def _normalise_filename(filename):
    """Use only the client filename so paths cannot affect identity."""
    return Path(str(filename or '')).name


def _cache_key(channel_id, filename, file_size):
    return f"{channel_id}:{_normalise_filename(filename)}:{file_size}"


def load_crc_cache():
    with _cache_lock:
        if not DUPLICATE_CACHE_FILE.exists():
            return {}
        try:
            with DUPLICATE_CACHE_FILE.open('r', encoding='utf-8') as cache_file:
                return json.load(cache_file)
        except (OSError, ValueError) as exc:
            from app.utils.logging_setup import error_logger
            error_logger.error(f"Error loading duplicate file cache: {exc}")
            return {}


def save_crc_cache(cache):
    with _cache_lock:
        try:
            DUPLICATE_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = DUPLICATE_CACHE_FILE.with_suffix('.json.tmp')
            with tmp_path.open('w', encoding='utf-8') as cache_file:
                json.dump(cache, cache_file, indent=2)
            tmp_path.replace(DUPLICATE_CACHE_FILE)
        except OSError as exc:
            from app.utils.logging_setup import error_logger
            error_logger.error(f"Error saving duplicate file cache: {exc}")


def check_database_for_file(filename, file_size, channel_id):
    """Find a recording with the same client filename and size on this channel."""
    try:
        from app.routes.route_utils import DB_PATH
        normalised = _normalise_filename(filename)
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.execute(
                '''
                SELECT id, filename, timestamp
                FROM recordings
                WHERE channel_id = ? AND filesize = ?
                  AND (filename = ? OR filename LIKE ?)
                ORDER BY timestamp ASC
                LIMIT 1
                ''',
                (channel_id, file_size, normalised, f'%/{normalised}'),
            )
            row = cursor.fetchone()
        if row:
            return {'found': True, 'record_id': row[0], 'filename': row[1], 'timestamp': row[2]}
    except (sqlite3.Error, OSError) as exc:
        from app.utils.logging_setup import error_logger
        error_logger.error(f"Error checking database for duplicate file: {exc}")
    return {'found': False, 'record_id': None, 'filename': None, 'timestamp': None}


def check_and_update_duplicate_cache(file_bytes, channel_id, filename):
    """Check and remember the filename/file-size pair within a single channel."""
    file_size = len(file_bytes)
    normalised = _normalise_filename(filename)
    key = _cache_key(channel_id, normalised, file_size)
    # Only check cache for now, if that doesn't work we can add the db check
    #database_match = check_database_for_file(normalised, file_size, channel_id)
    cache = load_crc_cache()
    #previous_info = database_match if database_match['found'] else cache.get(key)
    previous_info = cache.get(key)

    cache[key] = {
        'channel_id': channel_id,
        'filename': normalised,
        'filesize': file_size,
        'timestamp': datetime.now(timezone.utc).isoformat(),
    }
    while len(cache) > 100:
        del cache[next(iter(cache))]
    save_crc_cache(cache)

    return {
        'is_duplicate': previous_info is not None,
        # Kept for response/database compatibility; CRC is no longer calculated.
        'crc': None,
        'filesize': file_size,
        'previous_channel_id': previous_info.get('channel_id', channel_id) if previous_info else None,
        'previous_filename': previous_info.get('filename') if previous_info else None,
        'previous_timestamp': previous_info.get('timestamp') if previous_info else None,
    }


def clear_crc_cache():
    with _cache_lock:
        try:
            DUPLICATE_CACHE_FILE.unlink(missing_ok=True)
        except OSError as exc:
            from app.utils.logging_setup import error_logger
            error_logger.error(f"Error clearing duplicate file cache: {exc}")


def get_crc_cache_stats():
    cache = load_crc_cache()
    return {
        'total_entries': len(cache),
        'max_entries': 100,
        'is_full': len(cache) >= 100,
        'cache_file': DUPLICATE_CACHE_FILE,
    }


# Backward-compatible alias for third-party integrations importing the former name.
check_and_update_crc_cache = check_and_update_duplicate_cache
