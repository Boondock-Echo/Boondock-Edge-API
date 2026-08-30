"""
Settings Manager - Centralized settings management with SQLite backend.

This module provides a fully encapsulated interface for all application settings.
No other part of the application should access settings.db directly.
All access must go through this SettingsManager class.
"""

import sqlite3
import json
import logging
import threading
from config import Config, DATA_ROOT
from typing import Any, Dict, List, Optional, Union
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# Get the database directory from centralized config
SETTINGS_DB_PATH = Config.get_settings_db_path()

# Thread lock for database access
_db_lock = threading.RLock()


def _deserialize_setting(value: str, setting_type: str) -> Any:
    """Convert a persisted setting string back to its declared Python type."""
    try:
        if setting_type == 'bool':
            parsed = json.loads(value)
            if not isinstance(parsed, bool):
                raise ValueError(f'Invalid bool value: {value}')
            return parsed
        if setting_type == 'int':
            return int(value)
        if setting_type == 'float':
            return float(value)
        if setting_type == 'json':
            return json.loads(value)
        if setting_type == 'datetime':
            return datetime.fromisoformat(value)
        return value
    except (json.JSONDecodeError, TypeError, ValueError):
        logger.warning("Could not deserialize setting value %r as %s", value, setting_type)
        return value


def _serialize_setting(value: Any) -> tuple[str, str]:
    """Serialize a setting and return its persisted value and type."""
    if isinstance(value, bool):
        return json.dumps(value), 'bool'
    if isinstance(value, datetime):
        return value.isoformat(), 'datetime'
    if isinstance(value, int):
        return json.dumps(value), 'int'
    if isinstance(value, float):
        return json.dumps(value), 'float'
    if isinstance(value, str):
        return value, 'string'
    return json.dumps(value), 'json'

def normalize_mac_address(mac: str) -> str:
    """
    Normalize MAC address to uppercase and remove colons/dashes.
    
    Args:
        mac: MAC address in any format (e.g., "AA:BB:CC:DD:EE:FF" or "aabbccddeeff")
        
    Returns:
        Normalized MAC address (12 uppercase characters, no separators)
    """
    if not mac:
        return ""
    # Remove colons, dashes, and spaces, then convert to uppercase
    normalized = mac.replace(':', '').replace('-', '').replace(' ', '').upper()
    return normalized


class SettingsManager:
    """
    Centralized settings manager with full encapsulation.
    All settings are stored in settings.db SQLite database.
    """
    
    _instance = None
    _lock = threading.Lock()
    
    def __new__(cls):
        """Singleton pattern to ensure only one instance exists."""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super(SettingsManager, cls).__new__(cls)
                    cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        """Initialize the SettingsManager (only once)."""
        if self._initialized:
            return
        
        self.db_path = SETTINGS_DB_PATH
        self._ensure_db_dir()
        self._initialize_database()
        self._initialized = True
    
    def _ensure_db_dir(self):
        """Ensure the database directory exists."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
    
    def _get_connection(self):
        """Get a database connection with proper isolation."""
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn
    
    def _initialize_database(self):
        """Initialize the database schema with all required tables."""
        with _db_lock:
            conn = self._get_connection()
            try:
                cursor = conn.cursor()
                
                # Settings table (key-value pairs)
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS settings (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL,
                        type TEXT NOT NULL CHECK (type IN ('bool', 'string', 'json', 'datetime', 'int', 'float'))
                    )
                ''')
                
                # Users table
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS users (
                        email TEXT PRIMARY KEY,
                        name TEXT NOT NULL,
                        password TEXT NOT NULL,
                        role TEXT,
                        status TEXT,
                        profile TEXT,
                        access_level TEXT,
                        mfa_enabled INTEGER DEFAULT 0,
                        created_at TEXT,
                        login_history TEXT,
                        devices TEXT
                    )
                ''')
                
                # Profiles table
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS profiles (
                        name TEXT PRIMARY KEY,
                        description TEXT,
                        is_default INTEGER DEFAULT 0,
                        features TEXT
                    )
                ''')
                
                # Tags table
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS tags (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT NOT NULL,
                        category TEXT,
                        usage_count INTEGER DEFAULT 0,
                        color TEXT,
                        created_at TEXT
                    )
                ''')
                
                # Frequencies table
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS frequencies (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT,
                        frequency REAL,
                        type TEXT,
                        tone TEXT,
                        tag TEXT,
                        person TEXT,
                        status TEXT
                    )
                ''')
                
                # Channels table
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS channels (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT,
                        status TEXT,
                        model TEXT,
                        src_language TEXT,
                        target_language TEXT,
                        color TEXT,
                        background_color TEXT,
                        team_color TEXT,
                        car TEXT,
                        driver TEXT,
                        person TEXT,
                        tag TEXT,
                        mac TEXT UNIQUE NOT NULL,
                        audio_stream_enabled INTEGER DEFAULT 0,
                        threshold TEXT,
                        silence TEXT,
                        min_rec TEXT,
                        max_rec TEXT,
                        audio_gain TEXT,
                        frequency REAL,
                        tone TEXT,
                        type TEXT,
                        deleted INTEGER DEFAULT 0,
                        audio_stream_port INTEGER,
                        speaker_enabled INTEGER DEFAULT 0,
                        speaker_volume INTEGER
                    )
                ''')
                
                # Try to add UNIQUE constraint if table already exists without it
                try:
                    cursor.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_channels_mac_unique ON channels(mac)')
                except sqlite3.OperationalError:
                    # Index might already exist or table might not exist yet
                    pass
                
                # Tokens table
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS tokens (
                        token TEXT PRIMARY KEY,
                        email TEXT,
                        user_id TEXT,
                        role TEXT,
                        mac_address TEXT,
                        created_at TEXT,
                        expires_at TEXT,
                        last_activity TEXT,
                        device_info TEXT
                    )
                ''')

                # Pagination preferences table
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS pagination_preferences (
                        email TEXT PRIMARY KEY,
                        records_per_page INTEGER,
                        current_page INTEGER,
                        reverse_sort INTEGER DEFAULT 0,
                        show_full_timestamps INTEGER DEFAULT 0
                    )
                ''')
                
                # Branding table
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS branding (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        organization_name TEXT,
                        tagline TEXT,
                        brand_colors TEXT,
                        font TEXT,
                        assets TEXT
                    )
                ''')
                
                # Hallucinations table
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS hallucinations (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        data TEXT
                    )
                ''')
                
                # Backup history table
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS backup_history (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        start_time TEXT,
                        end_time TEXT,
                        duration INTEGER,
                        status TEXT,
                        manual INTEGER DEFAULT 0,
                        backup_type TEXT,
                        destination TEXT,
                        uploaded_files INTEGER DEFAULT 0,
                        skipped_files INTEGER DEFAULT 0,
                        error_files INTEGER DEFAULT 0,
                        total_files INTEGER DEFAULT 0
                    )
                ''')
                
                # Reboot history table
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS reboot_history (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        mac_address TEXT,
                        timestamp TEXT,
                        port TEXT
                    )
                ''')
                
                # Scanner inventory table
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS scanner_inventory (
                        scanner_id TEXT PRIMARY KEY,
                        port TEXT,
                        model TEXT,
                        version TEXT,
                        status TEXT
                    )
                ''')
                
                # Recorders inventory table
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS recorders_inventory (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        data TEXT
                    )
                ''')
                
                # Firmware metadata is now stored in firmware/firmware.json (not in database)
                
                # Queue table
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS queue (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        mac TEXT,
                        relative_path TEXT,
                        channel_id INTEGER,
                        timestamp TEXT,
                        error TEXT,
                        attempt_time TEXT
                    )
                ''')
                
                # GPIO config table
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS gpio_config (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        config_type TEXT,
                        config_key TEXT,
                        config_value TEXT,
                        UNIQUE(config_type, config_key)
                    )
                ''')
                
                conn.commit()
                logger.info("Database schema initialized successfully")
                
            except Exception as e:
                logger.error(f"Error initializing database: {e}")
                conn.rollback()
                raise
            finally:
                conn.close()
    
    # ==================== SETTINGS METHODS ====================
    
    def get_setting(self, key: str, default: Any = None) -> Any:
        """Get a setting value by key."""
        with _db_lock:
            conn = self._get_connection()
            try:
                cursor = conn.cursor()
                cursor.execute('SELECT value, type FROM settings WHERE key = ?', (key,))
                row = cursor.fetchone()
                if row:
                    return _deserialize_setting(row['value'], row['type'])
                return default
            finally:
                conn.close()
    
    def set_setting(self, key: str, value: Any) -> bool:
        """Set a setting value."""
        with _db_lock:
            conn = self._get_connection()
            try:
                # Convert value to JSON string if it's not a string
                value_str, setting_type = _serialize_setting(value)
                
                cursor = conn.cursor()
                cursor.execute(
                    'INSERT OR REPLACE INTO settings (key, value, type) VALUES (?, ?, ?)',
                    (key, value_str, setting_type)
                )
                conn.commit()
                return True
            except Exception as e:
                logger.error(f"Error setting {key}: {e}")
                conn.rollback()
                return False
            finally:
                conn.close()
    
    def get_all_settings(self) -> Dict[str, Any]:
        """Get all settings as a dictionary."""
        with _db_lock:
            conn = self._get_connection()
            try:
                cursor = conn.cursor()
                cursor.execute('SELECT key, value, type FROM settings')
                settings = {}
                for row in cursor.fetchall():
                    settings[row['key']] = _deserialize_setting(row['value'], row['type'])

                return settings
            finally:
                conn.close()
    
    def set_all_settings(self, settings: Dict[str, Any]) -> bool:
        """Set multiple settings at once."""
        with _db_lock:
            conn = self._get_connection()
            try:
                cursor = conn.cursor()
                for key, value in settings.items():
                    value_str, setting_type = _serialize_setting(value)
                    
                    cursor.execute(
                        'INSERT OR REPLACE INTO settings (key, value, type) VALUES (?, ?, ?)',
                        (key, value_str, setting_type)
                    )
                conn.commit()
                return True
            except Exception as e:
                logger.error(f"Error setting all settings: {e}")
                conn.rollback()
                return False
            finally:
                conn.close()
    
    # ==================== USERS METHODS ====================
    
    def get_user(self, email: str) -> Optional[Dict[str, Any]]:
        """Get a user by email."""
        with _db_lock:
            conn = self._get_connection()
            try:
                cursor = conn.cursor()
                cursor.execute('SELECT * FROM users WHERE email = ?', (email,))
                row = cursor.fetchone()
                if row:
                    user = dict(row)
                    # Parse JSON fields
                    if user.get('login_history'):
                        try:
                            user['login_history'] = json.loads(user['login_history'])
                        except (json.JSONDecodeError, ValueError, TypeError):
                            user['login_history'] = []
                    if user.get('devices'):
                        try:
                            user['devices'] = json.loads(user['devices'])
                        except (json.JSONDecodeError, ValueError, TypeError):
                            user['devices'] = []
                    return user
                return None
            finally:
                conn.close()
    
    def get_all_users(self) -> Dict[str, Dict[str, Any]]:
        """Get all users as a dictionary keyed by email."""
        with _db_lock:
            conn = self._get_connection()
            try:
                cursor = conn.cursor()
                cursor.execute('SELECT * FROM users')
                users = {}
                for row in cursor.fetchall():
                    user = dict(row)
                    # Parse JSON fields
                    if user.get('login_history'):
                        try:
                            user['login_history'] = json.loads(user['login_history'])
                        except (json.JSONDecodeError, ValueError, TypeError):
                            user['login_history'] = []
                    if user.get('devices'):
                        try:
                            user['devices'] = json.loads(user['devices'])
                        except (json.JSONDecodeError, ValueError, TypeError):
                            user['devices'] = []
                    users[user['email']] = user
                return users
            finally:
                conn.close()
    
    def save_user(self, email: str, user_data: Dict[str, Any]) -> bool:
        """Save or update a user."""
        with _db_lock:
            conn = self._get_connection()
            try:
                cursor = conn.cursor()
                # Convert JSON fields to strings
                login_history = json.dumps(user_data.get('login_history', []))
                devices = json.dumps(user_data.get('devices', []))
                
                cursor.execute('''
                    INSERT OR REPLACE INTO users 
                    (email, name, password, role, status, profile, access_level, 
                     mfa_enabled, created_at, login_history, devices)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    email,
                    user_data.get('name'),
                    user_data.get('password'),
                    user_data.get('role'),
                    user_data.get('status'),
                    user_data.get('profile'),
                    user_data.get('accessLevel'),
                    user_data.get('mfa_enabled', 0),
                    user_data.get('created_at'),
                    login_history,
                    devices
                ))
                conn.commit()
                return True
            except Exception as e:
                logger.error(f"Error saving user {email}: {e}")
                conn.rollback()
                return False
            finally:
                conn.close()
    
    def delete_user(self, email: str) -> bool:
        """Delete a user."""
        with _db_lock:
            conn = self._get_connection()
            try:
                cursor = conn.cursor()
                cursor.execute('DELETE FROM users WHERE email = ?', (email,))
                conn.commit()
                return True
            except Exception as e:
                logger.error(f"Error deleting user {email}: {e}")
                conn.rollback()
                return False
            finally:
                conn.close()
    
    # ==================== PROFILES METHODS ====================
    
    def get_profile(self, name: str) -> Optional[Dict[str, Any]]:
        """Get a profile by name."""
        with _db_lock:
            conn = self._get_connection()
            try:
                cursor = conn.cursor()
                cursor.execute('SELECT * FROM profiles WHERE name = ?', (name,))
                row = cursor.fetchone()
                if row:
                    profile = dict(row)
                    if profile.get('features'):
                        try:
                            profile['features'] = json.loads(profile['features'])
                        except (json.JSONDecodeError, ValueError, TypeError):
                            profile['features'] = {}
                    return profile
                return None
            finally:
                conn.close()
    
    def get_all_profiles(self) -> Dict[str, Dict[str, Any]]:
        """Get all profiles as a dictionary keyed by name."""
        with _db_lock:
            conn = self._get_connection()
            try:
                cursor = conn.cursor()
                cursor.execute('SELECT * FROM profiles')
                profiles = {}
                for row in cursor.fetchall():
                    profile = dict(row)
                    if profile.get('features'):
                        try:
                            profile['features'] = json.loads(profile['features'])
                        except (json.JSONDecodeError, ValueError, TypeError):
                            profile['features'] = {}
                    profiles[profile['name']] = profile
                return profiles
            finally:
                conn.close()
    
    def save_profile(self, name: str, profile_data: Dict[str, Any]) -> bool:
        """Save or update a profile."""
        with _db_lock:
            conn = self._get_connection()
            try:
                cursor = conn.cursor()
                features = json.dumps(profile_data.get('features', {}))
                
                cursor.execute('''
                    INSERT OR REPLACE INTO profiles 
                    (name, description, is_default, features)
                    VALUES (?, ?, ?, ?)
                ''', (
                    name,
                    profile_data.get('description'),
                    profile_data.get('isDefault', 0),
                    features
                ))
                conn.commit()
                return True
            except Exception as e:
                logger.error(f"Error saving profile {name}: {e}")
                conn.rollback()
                return False
            finally:
                conn.close()
    
    def delete_profile(self, name: str) -> bool:
        """Delete a profile."""
        with _db_lock:
            conn = self._get_connection()
            try:
                cursor = conn.cursor()
                cursor.execute('DELETE FROM profiles WHERE name = ?', (name,))
                conn.commit()
                return True
            except Exception as e:
                logger.error(f"Error deleting profile {name}: {e}")
                conn.rollback()
                return False
            finally:
                conn.close()
    
    # ==================== TAGS METHODS ====================
    
    def get_all_tags(self) -> List[Dict[str, Any]]:
        """Get all tags."""
        with _db_lock:
            conn = self._get_connection()
            try:
                cursor = conn.cursor()
                cursor.execute('SELECT * FROM tags ORDER BY id')
                return [dict(row) for row in cursor.fetchall()]
            finally:
                conn.close()
    
    def save_tag(self, tag_data: Dict[str, Any]) -> int:
        """Save a tag and return its ID."""
        with _db_lock:
            conn = self._get_connection()
            try:
                cursor = conn.cursor()
                
                # Check if tag exists in database (if id is provided)
                tag_exists = False
                if 'id' in tag_data and tag_data['id']:
                    cursor.execute('SELECT id FROM tags WHERE id = ?', (tag_data['id'],))
                    tag_exists = cursor.fetchone() is not None
                
                # Ensure created_at has a value
                created_at = tag_data.get('created_at')
                if not created_at:
                    created_at = datetime.utcnow().isoformat() + 'Z'
                
                if tag_exists:
                    # Update existing
                    cursor.execute('''
                        UPDATE tags SET name=?, category=?, usage_count=?, 
                        color=?, created_at=? WHERE id=?
                    ''', (
                        tag_data.get('name'),
                        tag_data.get('category'),
                        tag_data.get('usageCount', 0),
                        tag_data.get('color'),
                        created_at,
                        tag_data['id']
                    ))
                    conn.commit()
                    return tag_data['id']
                else:
                    # Insert new (ignore provided id if tag doesn't exist, let DB auto-increment)
                    cursor.execute('''
                        INSERT INTO tags (name, category, usage_count, color, created_at)
                        VALUES (?, ?, ?, ?, ?)
                    ''', (
                        tag_data.get('name'),
                        tag_data.get('category'),
                        tag_data.get('usageCount', 0),
                        tag_data.get('color'),
                        created_at
                    ))
                    conn.commit()
                    return cursor.lastrowid
            except Exception as e:
                logger.error(f"Error saving tag: {e}")
                conn.rollback()
                return -1
            finally:
                conn.close()
    
    def delete_tag(self, tag_id: int) -> bool:
        """Delete a tag."""
        with _db_lock:
            conn = self._get_connection()
            try:
                cursor = conn.cursor()
                cursor.execute('DELETE FROM tags WHERE id = ?', (tag_id,))
                conn.commit()
                return True
            except Exception as e:
                logger.error(f"Error deleting tag {tag_id}: {e}")
                conn.rollback()
                return False
            finally:
                conn.close()
    
    def increment_tag_usage(self, tag_name: str) -> bool:
        """Increment usage_count for a tag by name."""
        with _db_lock:
            conn = self._get_connection()
            try:
                cursor = conn.cursor()
                cursor.execute('''
                    UPDATE tags SET usage_count = usage_count + 1 
                    WHERE name = ?
                ''', (tag_name,))
                conn.commit()
                return cursor.rowcount > 0
            except Exception as e:
                logger.error(f"Error incrementing usage count for tag '{tag_name}': {e}")
                conn.rollback()
                return False
            finally:
                conn.close()
    
    def decrement_tag_usage(self, tag_name: str) -> bool:
        """Decrement usage_count for a tag by name (minimum 0)."""
        with _db_lock:
            conn = self._get_connection()
            try:
                cursor = conn.cursor()
                cursor.execute('''
                    UPDATE tags SET usage_count = MAX(usage_count - 1, 0) 
                    WHERE name = ?
                ''', (tag_name,))
                conn.commit()
                return cursor.rowcount > 0
            except Exception as e:
                logger.error(f"Error decrementing usage count for tag '{tag_name}': {e}")
                conn.rollback()
                return False
            finally:
                conn.close()
    
    # ==================== FREQUENCIES METHODS ====================
    
    def get_all_frequencies(self) -> List[Dict[str, Any]]:
        """Get all frequencies."""
        with _db_lock:
            conn = self._get_connection()
            try:
                cursor = conn.cursor()
                cursor.execute('SELECT * FROM frequencies ORDER BY id')
                return [dict(row) for row in cursor.fetchall()]
            finally:
                conn.close()
    
    def save_frequency(self, freq_data: Dict[str, Any]) -> int:
        """Save a frequency and return its ID."""
        with _db_lock:
            conn = self._get_connection()
            try:
                cursor = conn.cursor()
                if 'id' in freq_data and freq_data['id']:
                    cursor.execute('''
                        UPDATE frequencies SET name=?, frequency=?, type=?, 
                        tone=?, tag=?, person=?, status=? WHERE id=?
                    ''', (
                        freq_data.get('name'),
                        freq_data.get('frequency'),
                        freq_data.get('type'),
                        freq_data.get('tone'),
                        freq_data.get('tag'),
                        freq_data.get('person'),
                        freq_data.get('status'),
                        freq_data['id']
                    ))
                    conn.commit()
                    return freq_data['id']
                else:
                    cursor.execute('''
                        INSERT INTO frequencies (name, frequency, type, tone, tag, person, status)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        freq_data.get('name'),
                        freq_data.get('frequency'),
                        freq_data.get('type'),
                        freq_data.get('tone'),
                        freq_data.get('tag'),
                        freq_data.get('person'),
                        freq_data.get('status')
                    ))
                    conn.commit()
                    return cursor.lastrowid
            except Exception as e:
                logger.error(f"Error saving frequency: {e}")
                conn.rollback()
                return -1
            finally:
                conn.close()
    
    def delete_frequency(self, freq_id: int) -> bool:
        """Delete a frequency."""
        with _db_lock:
            conn = self._get_connection()
            try:
                cursor = conn.cursor()
                cursor.execute('DELETE FROM frequencies WHERE id = ?', (freq_id,))
                conn.commit()
                return True
            except Exception as e:
                logger.error(f"Error deleting frequency {freq_id}: {e}")
                conn.rollback()
                return False
            finally:
                conn.close()
    
    # ==================== CHANNELS METHODS ====================
    
    def get_all_channels(self) -> List[Dict[str, Any]]:
        """Get all channels."""
        with _db_lock:
            conn = self._get_connection()
            try:
                cursor = conn.cursor()
                cursor.execute('SELECT * FROM channels ORDER BY id')
                channels = []
                for row in cursor.fetchall():
                    channel = dict(row)
                    # Convert deleted integer to boolean for compatibility
                    channel['deleted'] = bool(channel.get('deleted', 0))
                    channels.append(channel)
                return channels
            finally:
                conn.close()
    
    def get_channel(self, channel_id: int) -> Optional[Dict[str, Any]]:
        """Get a channel by ID (excludes soft-deleted channels)."""
        with _db_lock:
            conn = self._get_connection()
            try:
                cursor = conn.cursor()
                cursor.execute('SELECT * FROM channels WHERE id = ? AND deleted = 0', (channel_id,))
                row = cursor.fetchone()
                if row:
                    channel = dict(row)
                    # Convert deleted integer to boolean for compatibility
                    channel['deleted'] = bool(channel.get('deleted', 0))
                    return channel
                return None
            finally:
                conn.close()
    
    def get_channel_by_mac(self, mac: str) -> Optional[Dict[str, Any]]:
        """Get a channel by MAC address."""
        # Normalize MAC address before querying
        normalized_mac = normalize_mac_address(mac)
        if not normalized_mac:
            return None
            
        with _db_lock:
            conn = self._get_connection()
            try:
                cursor = conn.cursor()
                cursor.execute('SELECT * FROM channels WHERE mac = ?', (normalized_mac,))
                row = cursor.fetchone()
                if row:
                    channel = dict(row)
                    # Convert deleted integer to boolean for compatibility
                    channel['deleted'] = bool(channel.get('deleted', 0))
                    return channel
                return None
            finally:
                conn.close()
    
    def save_channel(self, channel_data: Dict[str, Any]) -> int:
        """Save a channel and return its ID."""
        # Normalize MAC address before saving
        if 'mac' in channel_data and channel_data['mac']:
            channel_data['mac'] = normalize_mac_address(channel_data['mac'])
        
        logger.debug(f"save_channel called with channel_data keys: {list(channel_data.keys())}, has 'id': {'id' in channel_data and channel_data.get('id')}")
        
        with _db_lock:
            conn = self._get_connection()
            try:
                cursor = conn.cursor()
                if 'id' in channel_data and channel_data['id']:
                    cursor.execute('''
                        UPDATE channels SET name=?, status=?, model=?, src_language=?,
                        target_language=?, color=?, background_color=?, team_color=?,
                        car=?, driver=?, person=?, tag=?, mac=?, audio_stream_enabled=?,
                        threshold=?, silence=?, min_rec=?, max_rec=?, audio_gain=?,
                        frequency=?, tone=?, type=?, deleted=?, audio_stream_port=?,
                        speaker_enabled=?, speaker_volume=? WHERE id=?
                    ''', (
                        channel_data.get('name'),
                        channel_data.get('status'),
                        channel_data.get('model'),
                        channel_data.get('src_language'),
                        channel_data.get('target_language'),
                        channel_data.get('color'),
                        channel_data.get('background_color'),
                        channel_data.get('team_color'),
                        channel_data.get('car'),
                        channel_data.get('driver'),
                        channel_data.get('person'),
                        channel_data.get('tag'),
                        channel_data.get('mac'),
                        channel_data.get('audio_stream_enabled', 0),
                        channel_data.get('threshold'),
                        channel_data.get('silence'),
                        channel_data.get('min_rec'),
                        channel_data.get('max_rec'),
                        channel_data.get('audio_gain'),
                        channel_data.get('frequency'),
                        channel_data.get('tone'),
                        channel_data.get('type'),
                        channel_data.get('deleted', 0),
                        channel_data.get('audio_stream_port'),
                        channel_data.get('speaker_enabled', 0),
                        channel_data.get('speaker_volume'),
                        channel_data['id']
                    ))
                    conn.commit()
                    logger.debug(f"Updated channel with id {channel_data['id']}")
                    return channel_data['id']
                else:
                    # Check if channel with this MAC already exists (handles race conditions)
                    normalized_mac = channel_data.get('mac')
                    if normalized_mac:
                        cursor.execute('SELECT id FROM channels WHERE mac = ? AND deleted = 0', (normalized_mac,))
                        existing = cursor.fetchone()
                        if existing:
                            logger.warning(f"Channel with MAC {normalized_mac} already exists (id: {existing[0]}), returning existing ID")
                            return existing[0]
                    
                    logger.debug(f"Inserting new channel with MAC: {channel_data.get('mac')}")
                    try:
                        cursor.execute('''
                            INSERT INTO channels (name, status, model, src_language, target_language,
                            color, background_color, team_color, car, driver, person, tag, mac,
                            audio_stream_enabled, threshold, silence, min_rec, max_rec, audio_gain,
                            frequency, tone, type, deleted, audio_stream_port, speaker_enabled, speaker_volume)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ''', (
                            channel_data.get('name'),
                            channel_data.get('status'),
                            channel_data.get('model'),
                            channel_data.get('src_language'),
                            channel_data.get('target_language'),
                            channel_data.get('color'),
                            channel_data.get('background_color'),
                            channel_data.get('team_color'),
                            channel_data.get('car'),
                            channel_data.get('driver'),
                            channel_data.get('person'),
                            channel_data.get('tag'),
                            channel_data.get('mac'),
                            channel_data.get('audio_stream_enabled', 0),
                            channel_data.get('threshold'),
                            channel_data.get('silence'),
                            channel_data.get('min_rec'),
                            channel_data.get('max_rec'),
                            channel_data.get('audio_gain'),
                            channel_data.get('frequency'),
                            channel_data.get('tone'),
                            channel_data.get('type'),
                            channel_data.get('deleted', 0),
                            channel_data.get('audio_stream_port'),
                            channel_data.get('speaker_enabled', 0),
                            channel_data.get('speaker_volume')
                        ))
                        conn.commit()
                        lastrowid = cursor.lastrowid
                        logger.debug(f"Inserted new channel, lastrowid: {lastrowid}")
                        return lastrowid
                    except Exception as insert_error:
                        # Handle unique constraint violation (race condition)
                        error_str = str(insert_error).lower()
                        if 'unique' in error_str or 'constraint' in error_str:
                            logger.warning(f"Unique constraint violation for MAC {normalized_mac}, checking for existing channel")
                            cursor.execute('SELECT id FROM channels WHERE mac = ? AND deleted = 0', (normalized_mac,))
                            existing = cursor.fetchone()
                            if existing:
                                logger.info(f"Found existing channel with MAC {normalized_mac} (id: {existing[0]})")
                                return existing[0]
                        # Re-raise if it's not a unique constraint error
                        raise
            except Exception as e:
                logger.error(f"Error saving channel: {e}", exc_info=True)
                conn.rollback()
                return -1
            finally:
                conn.close()
    
    def delete_channel(self, channel_id: int) -> bool:
        """Delete a channel (soft delete by setting deleted flag)."""
        with _db_lock:
            conn = self._get_connection()
            try:
                cursor = conn.cursor()
                cursor.execute('UPDATE channels SET deleted = 1 WHERE id = ?', (channel_id,))
                conn.commit()
                return True
            except Exception as e:
                logger.error(f"Error deleting channel {channel_id}: {e}")
                conn.rollback()
                return False
            finally:
                conn.close()
    
    # ==================== TOKENS METHODS ====================
    
    def get_token(self, token: str) -> Optional[Dict[str, Any]]:
        """Get a token."""
        with _db_lock:
            conn = self._get_connection()
            try:
                cursor = conn.cursor()
                cursor.execute('SELECT * FROM tokens WHERE token = ?', (token,))
                row = cursor.fetchone()
                if row:
                    token_data = dict(row)
                    if token_data.get('device_info'):
                        try:
                            token_data['device_info'] = json.loads(token_data['device_info'])
                        except (json.JSONDecodeError, ValueError, TypeError):
                            token_data['device_info'] = {}
                    return token_data
                return None
            finally:
                conn.close()

    def get_all_tokens(self) -> Dict[str, Dict[str, Any]]:
        """Get all tokens as a dictionary keyed by token."""
        with _db_lock:
            conn = self._get_connection()
            try:
                cursor = conn.cursor()
                cursor.execute('SELECT * FROM tokens')
                tokens = {}
                for row in cursor.fetchall():
                    token_data = dict(row)
                    if token_data.get('device_info'):
                        try:
                            token_data['device_info'] = json.loads(token_data['device_info'])
                        except (json.JSONDecodeError, ValueError, TypeError):
                            token_data['device_info'] = {}
                    tokens[token_data['token']] = token_data
                return tokens
            finally:
                conn.close()
    
    def save_token(self, token: str, token_data: Dict[str, Any]) -> bool:
        """Save or update a token."""
        with _db_lock:
            conn = self._get_connection()
            try:
                cursor = conn.cursor()
                device_info = json.dumps(token_data.get('device_info', {}))
                
                cursor.execute('''
                    INSERT OR REPLACE INTO tokens 
                    (token, email, user_id, role, mac_address, created_at, 
                     expires_at, last_activity, device_info)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    token,
                    token_data.get('email'),
                    token_data.get('user_id'),
                    token_data.get('role'),
                    token_data.get('mac_address'),
                    token_data.get('created_at'),
                    token_data.get('expires_at'),
                    token_data.get('last_activity'),
                    device_info
                ))
                conn.commit()
                return True
            except Exception as e:
                logger.error(f"Error saving token: {e}")
                conn.rollback()
                return False
            finally:
                conn.close()
    
    def delete_token(self, token: str) -> bool:
        """Delete a token."""
        with _db_lock:
            conn = self._get_connection()
            try:
                cursor = conn.cursor()
                cursor.execute('DELETE FROM tokens WHERE token = ?', (token,))
                conn.commit()
                return True
            except Exception as e:
                logger.error(f"Error deleting token: {e}")
                conn.rollback()
                return False
            finally:
                conn.close()
    
    def delete_expired_tokens(self) -> int:
        """Delete all expired tokens. Returns count of deleted tokens."""
        with _db_lock:
            conn = self._get_connection()
            try:
                cursor = conn.cursor()
                # Match the timezone-aware UTC ("+00:00") format that tokens are
                # stored with, so this string comparison is chronologically correct.
                now = datetime.now(timezone.utc).isoformat()
                cursor.execute('DELETE FROM tokens WHERE expires_at < ?', (now,))
                deleted_count = cursor.rowcount
                conn.commit()
                return deleted_count
            except Exception as e:
                logger.error(f"Error deleting expired tokens: {e}")
                conn.rollback()
                return 0
            finally:
                conn.close()
    
    # ==================== PAGINATION PREFERENCES METHODS ====================
    
    def get_pagination_prefs(self, email: str) -> Optional[Dict[str, Any]]:
        """Get pagination preferences for a user."""
        with _db_lock:
            conn = self._get_connection()
            try:
                cursor = conn.cursor()
                cursor.execute('SELECT * FROM pagination_preferences WHERE email = ?', (email,))
                row = cursor.fetchone()
                return dict(row) if row else None
            finally:
                conn.close()
    
    def save_pagination_prefs(self, email: str, prefs: Dict[str, Any]) -> bool:
        """Save pagination preferences for a user."""
        with _db_lock:
            conn = self._get_connection()
            try:
                cursor = conn.cursor()
                cursor.execute('''
                    INSERT OR REPLACE INTO pagination_preferences 
                    (email, records_per_page, current_page, reverse_sort, show_full_timestamps)
                    VALUES (?, ?, ?, ?, ?)
                ''', (
                    email,
                    prefs.get('recordsPerPage'),
                    prefs.get('currentPage'),
                    prefs.get('reverseSort', 0),
                    prefs.get('showFullTimestamps', 0)
                ))
                conn.commit()
                return True
            except Exception as e:
                logger.error(f"Error saving pagination prefs for {email}: {e}")
                conn.rollback()
                return False
            finally:
                conn.close()
    
    # ==================== BRANDING METHODS ====================
    
    def get_branding(self) -> Optional[Dict[str, Any]]:
        """Get branding configuration."""
        with _db_lock:
            conn = self._get_connection()
            try:
                cursor = conn.cursor()
                cursor.execute('SELECT * FROM branding ORDER BY id DESC LIMIT 1')
                row = cursor.fetchone()
                if row:
                    branding = dict(row)
                    if branding.get('brand_colors'):
                        try:
                            branding['brand_colors'] = json.loads(branding['brand_colors'])
                        except (json.JSONDecodeError, ValueError, TypeError):
                            branding['brand_colors'] = {}
                    if branding.get('assets'):
                        try:
                            branding['assets'] = json.loads(branding['assets'])
                        except (json.JSONDecodeError, ValueError, TypeError):
                            branding['assets'] = {}
                    return branding
                return None
            finally:
                conn.close()
    
    def save_branding(self, branding_data: Dict[str, Any]) -> bool:
        """Save branding configuration (upsert — always keeps a single row)."""
        with _db_lock:
            conn = self._get_connection()
            try:
                cursor = conn.cursor()
                brand_colors = json.dumps(branding_data.get('brand_colors', {}))
                assets = json.dumps(branding_data.get('assets', {}))

                cursor.execute('SELECT id FROM branding ORDER BY id DESC LIMIT 1')
                existing = cursor.fetchone()
                if existing:
                    cursor.execute('''
                        UPDATE branding
                        SET organization_name=?, tagline=?, brand_colors=?, font=?, assets=?
                        WHERE id=?
                    ''', (
                        branding_data.get('organization_name'),
                        branding_data.get('tagline'),
                        brand_colors,
                        branding_data.get('font'),
                        assets,
                        existing['id']
                    ))
                else:
                    cursor.execute('''
                        INSERT INTO branding (organization_name, tagline, brand_colors, font, assets)
                        VALUES (?, ?, ?, ?, ?)
                    ''', (
                        branding_data.get('organization_name'),
                        branding_data.get('tagline'),
                        brand_colors,
                        branding_data.get('font'),
                        assets
                    ))
                conn.commit()
                return True
            except Exception as e:
                logger.error(f"Error saving branding: {e}")
                conn.rollback()
                return False
            finally:
                conn.close()
    
    # ==================== HALLUCINATIONS METHODS ====================
    
    def get_all_hallucinations(self) -> List[Dict[str, Any]]:
        """Get all hallucinations."""
        with _db_lock:
            conn = self._get_connection()
            try:
                cursor = conn.cursor()
                cursor.execute('SELECT * FROM hallucinations ORDER BY id')
                results = []
                for row in cursor.fetchall():
                    data = dict(row)
                    if data.get('data'):
                        try:
                            data['data'] = json.loads(data['data'])
                        except (json.JSONDecodeError, ValueError, TypeError):
                            pass
                    results.append(data)
                return results
            finally:
                conn.close()
    
    def delete_hallucination(self, hallucination_id: int) -> bool:
        """Delete a hallucination by ID."""
        with _db_lock:
            conn = self._get_connection()
            try:
                cursor = conn.cursor()
                cursor.execute('DELETE FROM hallucinations WHERE id = ?', (hallucination_id,))
                conn.commit()
                return cursor.rowcount > 0
            except Exception as e:
                logger.error(f"Error deleting hallucination {hallucination_id}: {e}")
                conn.rollback()
                return False
            finally:
                conn.close()
    
    def save_hallucination(self, data: Any) -> int:
        """Save a hallucination entry."""
        with _db_lock:
            conn = self._get_connection()
            try:
                cursor = conn.cursor()
                data_str = json.dumps(data) if not isinstance(data, str) else data
                cursor.execute('INSERT INTO hallucinations (data) VALUES (?)', (data_str,))
                conn.commit()
                return cursor.lastrowid
            except Exception as e:
                logger.error(f"Error saving hallucination: {e}")
                conn.rollback()
                return -1
            finally:
                conn.close()
    
    # ==================== BACKUP HISTORY METHODS ====================
    
    def get_all_backup_history(self) -> List[Dict[str, Any]]:
        """Get all backup history entries."""
        with _db_lock:
            conn = self._get_connection()
            try:
                cursor = conn.cursor()
                cursor.execute('SELECT * FROM backup_history ORDER BY start_time DESC')
                return [dict(row) for row in cursor.fetchall()]
            finally:
                conn.close()
    
    def save_backup_history(self, backup_data: Dict[str, Any]) -> int:
        """Save a backup history entry."""
        with _db_lock:
            conn = self._get_connection()
            try:
                cursor = conn.cursor()
                cursor.execute('''
                    INSERT INTO backup_history 
                    (start_time, end_time, duration, status, manual, backup_type,
                     destination, uploaded_files, skipped_files, error_files, total_files)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    backup_data.get('start_time'),
                    backup_data.get('end_time'),
                    backup_data.get('duration'),
                    backup_data.get('status'),
                    backup_data.get('manual', 0),
                    backup_data.get('backup_type'),
                    backup_data.get('destination'),
                    backup_data.get('uploaded_files', 0),
                    backup_data.get('skipped_files', 0),
                    backup_data.get('error_files', 0),
                    backup_data.get('total_files', 0)
                ))
                conn.commit()
                return cursor.lastrowid
            except Exception as e:
                logger.error(f"Error saving backup history: {e}")
                conn.rollback()
                return -1
            finally:
                conn.close()
    
    # ==================== REBOOT HISTORY METHODS ====================
    
    def get_reboot_history(self, mac_address: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get reboot history, optionally filtered by MAC address."""
        with _db_lock:
            conn = self._get_connection()
            try:
                cursor = conn.cursor()
                if mac_address:
                    cursor.execute(
                        'SELECT * FROM reboot_history WHERE mac_address = ? ORDER BY timestamp DESC',
                        (mac_address,)
                    )
                else:
                    cursor.execute('SELECT * FROM reboot_history ORDER BY timestamp DESC')
                return [dict(row) for row in cursor.fetchall()]
            finally:
                conn.close()
    
    def save_reboot_history(self, mac_address: str, timestamp: str, port: str) -> int:
        """Save a reboot history entry."""
        with _db_lock:
            conn = self._get_connection()
            try:
                cursor = conn.cursor()
                cursor.execute('''
                    INSERT INTO reboot_history (mac_address, timestamp, port)
                    VALUES (?, ?, ?)
                ''', (mac_address, timestamp, port))
                conn.commit()
                return cursor.lastrowid
            except Exception as e:
                logger.error(f"Error saving reboot history: {e}")
                conn.rollback()
                return -1
            finally:
                conn.close()
    
    # ==================== SCANNER INVENTORY METHODS ====================
    
    def get_all_scanners(self) -> Dict[str, Dict[str, Any]]:
        """Get all scanners as a dictionary keyed by scanner_id."""
        with _db_lock:
            conn = self._get_connection()
            try:
                cursor = conn.cursor()
                cursor.execute('SELECT * FROM scanner_inventory')
                scanners = {}
                for row in cursor.fetchall():
                    scanners[row['scanner_id']] = dict(row)
                return scanners
            finally:
                conn.close()
    
    def save_scanner(self, scanner_id: str, scanner_data: Dict[str, Any]) -> bool:
        """Save or update a scanner."""
        with _db_lock:
            conn = self._get_connection()
            try:
                cursor = conn.cursor()
                cursor.execute('''
                    INSERT OR REPLACE INTO scanner_inventory 
                    (scanner_id, port, model, version, status)
                    VALUES (?, ?, ?, ?, ?)
                ''', (
                    scanner_id,
                    scanner_data.get('port'),
                    scanner_data.get('model'),
                    scanner_data.get('version'),
                    scanner_data.get('status')
                ))
                conn.commit()
                return True
            except Exception as e:
                logger.error(f"Error saving scanner {scanner_id}: {e}")
                conn.rollback()
                return False
            finally:
                conn.close()
    
    def delete_scanner(self, scanner_id: str) -> bool:
        """Delete a scanner."""
        with _db_lock:
            conn = self._get_connection()
            try:
                cursor = conn.cursor()
                cursor.execute('DELETE FROM scanner_inventory WHERE scanner_id = ?', (scanner_id,))
                conn.commit()
                return True
            except Exception as e:
                logger.error(f"Error deleting scanner {scanner_id}: {e}")
                conn.rollback()
                return False
            finally:
                conn.close()
    
    # ==================== RECORDERS INVENTORY METHODS ====================
    
    def get_all_recorders(self) -> List[Dict[str, Any]]:
        """Get all recorders."""
        with _db_lock:
            conn = self._get_connection()
            try:
                cursor = conn.cursor()
                cursor.execute('SELECT * FROM recorders_inventory ORDER BY id')
                results = []
                for row in cursor.fetchall():
                    data = dict(row)
                    if data.get('data'):
                        try:
                            data['data'] = json.loads(data['data'])
                        except (json.JSONDecodeError, ValueError, TypeError):
                            pass
                    results.append(data)
                return results
            finally:
                conn.close()
    
    def save_recorder(self, recorder_data: Any) -> int:
        """Save a recorder entry. Updates if a recorder with the same port exists, otherwise inserts."""
        with _db_lock:
            conn = self._get_connection()
            try:
                cursor = conn.cursor()
                data_str = json.dumps(recorder_data) if not isinstance(recorder_data, str) else recorder_data
                
                # Check if recorder with same port already exists
                port = recorder_data.get('port') if isinstance(recorder_data, dict) else None
                if port:
                    # Search for existing recorder with same port
                    cursor.execute('SELECT id, data FROM recorders_inventory')
                    for row in cursor.fetchall():
                        try:
                            existing_data = json.loads(row['data']) if isinstance(row['data'], str) else row['data']
                            if isinstance(existing_data, dict) and existing_data.get('port') == port:
                                # Update existing record
                                cursor.execute('UPDATE recorders_inventory SET data = ? WHERE id = ?', (data_str, row['id']))
                                conn.commit()
                                return row['id']
                        except (json.JSONDecodeError, TypeError):
                            continue
                
                # No existing recorder found, insert new one
                cursor.execute('INSERT INTO recorders_inventory (data) VALUES (?)', (data_str,))
                conn.commit()
                return cursor.lastrowid
            except Exception as e:
                logger.error(f"Error saving recorder: {e}")
                conn.rollback()
                return -1
            finally:
                conn.close()
    
    def delete_recorder(self, port: str) -> bool:
        """Delete a recorder entry by port."""
        with _db_lock:
            conn = self._get_connection()
            try:
                cursor = conn.cursor()
                # Find and delete recorder with matching port
                cursor.execute('SELECT id, data FROM recorders_inventory')
                deleted = False
                for row in cursor.fetchall():
                    try:
                        data = json.loads(row['data']) if isinstance(row['data'], str) else row['data']
                        if isinstance(data, dict) and data.get('port') == port:
                            cursor.execute('DELETE FROM recorders_inventory WHERE id = ?', (row['id'],))
                            deleted = True
                            break
                    except (json.JSONDecodeError, TypeError):
                        continue
                
                if deleted:
                    conn.commit()
                    logger.info(f"Deleted recorder with port: {port}")
                else:
                    logger.warning(f"No recorder found with port: {port}")
                
                return deleted
            except Exception as e:
                logger.error(f"Error deleting recorder: {e}")
                conn.rollback()
                return False
            finally:
                conn.close()
    
    # ==================== FIRMWARE METADATA METHODS ====================
    # Firmware metadata is stored in firmware/firmware.json (not in the database).

    def _get_firmware_json_path(self) -> str:
        """Return the path to firmware/firmware.json."""
        return DATA_ROOT / 'firmware' / 'firmware.json'

    def get_firmware_metadata(self) -> Dict[str, Any]:
        """Get firmware metadata from firmware/firmware.json."""
        firmware_path = self._get_firmware_json_path()
        try:
            if not firmware_path.exists():
                return {}
            with open(firmware_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Error reading firmware metadata from {firmware_path}: {e}")
            return {}

    def save_firmware_metadata(self, metadata: Dict[str, Any]) -> bool:
        """Save firmware metadata to firmware/firmware.json."""
        firmware_path = self._get_firmware_json_path()
        try:
            firmware_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = firmware_path.with_name(firmware_path.name + ".tmp")
            with open(tmp_path, 'w', encoding='utf-8') as f:
                json.dump(metadata, f, indent=2, ensure_ascii=False)
            tmp_path.replace(firmware_path)
            return True
        except Exception as e:
            logger.error(f"Error saving firmware metadata to {firmware_path}: {e}")
            return False
    
    # ==================== QUEUE METHODS ====================
    
    def get_all_queue_items(self) -> List[Dict[str, Any]]:
        """Get all queue items."""
        with _db_lock:
            conn = self._get_connection()
            try:
                cursor = conn.cursor()
                cursor.execute('SELECT * FROM queue ORDER BY id')
                return [dict(row) for row in cursor.fetchall()]
            finally:
                conn.close()
    
    def save_queue_item(self, queue_data: Dict[str, Any]) -> int:
        """Save a queue item."""
        with _db_lock:
            conn = self._get_connection()
            try:
                cursor = conn.cursor()
                cursor.execute('''
                    INSERT INTO queue (mac, relative_path, channel_id, timestamp, error, attempt_time)
                    VALUES (?, ?, ?, ?, ?, ?)
                ''', (
                    queue_data.get('mac'),
                    queue_data.get('relative_path'),
                    queue_data.get('channel_id'),
                    queue_data.get('timestamp'),
                    queue_data.get('error'),
                    queue_data.get('attempt_time')
                ))
                conn.commit()
                return cursor.lastrowid
            except Exception as e:
                logger.error(f"Error saving queue item: {e}")
                conn.rollback()
                return -1
            finally:
                conn.close()
    
    def delete_queue_item(self, item_id: int) -> bool:
        """Delete a queue item."""
        with _db_lock:
            conn = self._get_connection()
            try:
                cursor = conn.cursor()
                cursor.execute('DELETE FROM queue WHERE id = ?', (item_id,))
                conn.commit()
                return True
            except Exception as e:
                logger.error(f"Error deleting queue item {item_id}: {e}")
                conn.rollback()
                return False
            finally:
                conn.close()
    
    # ==================== GPIO CONFIG METHODS ====================
    
    def get_gpio_config(self) -> Dict[str, Any]:
        """Get GPIO configuration."""
        with _db_lock:
            conn = self._get_connection()
            try:
                cursor = conn.cursor()
                cursor.execute('SELECT * FROM gpio_config')
                config = {}
                for row in cursor.fetchall():
                    config_type = row['config_type']
                    config_key = row['config_key']
                    value = row['config_value']
                    
                    if config_type not in config:
                        config[config_type] = {}
                    
                    # Try to parse as JSON
                    try:
                        value = json.loads(value)
                    except (json.JSONDecodeError, ValueError, TypeError):
                        pass
                    
                    config[config_type][config_key] = value
                
                return config
            finally:
                conn.close()
    
    def save_gpio_config(self, config: Dict[str, Any]) -> bool:
        """Save GPIO configuration."""
        with _db_lock:
            conn = self._get_connection()
            try:
                cursor = conn.cursor()
                # Clear existing config
                cursor.execute('DELETE FROM gpio_config')
                
                # Insert new config
                for config_type, type_data in config.items():
                    for key, value in type_data.items():
                        value_str = json.dumps(value) if not isinstance(value, str) else value
                        cursor.execute('''
                            INSERT INTO gpio_config (config_type, config_key, config_value)
                            VALUES (?, ?, ?)
                        ''', (config_type, key, value_str))
                
                conn.commit()
                return True
            except Exception as e:
                logger.error(f"Error saving GPIO config: {e}")
                conn.rollback()
                return False
            finally:
                conn.close()


# Global instance getter
def get_settings_manager() -> SettingsManager:
    """Get the global SettingsManager instance."""
    return SettingsManager()
