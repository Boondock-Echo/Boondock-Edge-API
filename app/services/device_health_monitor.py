"""
Device Health Monitoring Service

Tracks health metrics for recording devices in memory and periodically
persists them to the logs database. One record per device (MAC) per day.
"""

import sqlite3
import threading
import time
from config import Config
from datetime import datetime, timedelta
from typing import Dict, Optional, List, Any
from collections import defaultdict

LOGS_DB_PATH = Config.get_logs_db_path()


def _health_mac_key(mac: str) -> str:
    if not mac:
        return ""
    from .settings_manager import normalize_mac_address

    return normalize_mac_address(mac)


def _touch_visual_last_seen(mac: str) -> None:
    try:
        from .channel_state import touch_device_activity

        touch_device_activity(mac)
    except Exception:
        pass
_db_lock = threading.RLock()

# In-memory health stats: mac -> day -> stats
_health_stats = defaultdict(lambda: defaultdict(lambda: {
    'device_created_at': None,
    'connection_count': 0,
    'event_count': 0,
    'file_upload_count': 0,
    'error_count': 0,
    'connection_loss_count': 0,
    'first_activity': None,
    'last_activity': None,
    'total_uptime_seconds': 0
}))

# Track last activity time for connection loss detection
_last_activity = {}  # mac -> timestamp
_connection_loss_threshold = 300  # 5 minutes in seconds

# Thread lock for in-memory stats
_stats_lock = threading.RLock()

# Background thread for periodic persistence
_persistence_thread = None
_persistence_interval = 300  # 5 minutes
_running = False


def _get_today_key() -> str:
    """Get today's date as YYYY-MM-DD string."""
    return datetime.now().strftime('%Y-%m-%d')


def _initialize_database():
    """Initialize the health_monitoring table in logs database."""
    try:
        with _db_lock:
            conn = sqlite3.connect(LOGS_DB_PATH, check_same_thread=False)
            cursor = conn.cursor()
            
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS health_monitoring (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    mac_address TEXT NOT NULL,
                    date TEXT NOT NULL,
                    device_created_at TEXT,
                    connection_count INTEGER DEFAULT 0,
                    event_count INTEGER DEFAULT 0,
                    file_upload_count INTEGER DEFAULT 0,
                    error_count INTEGER DEFAULT 0,
                    connection_loss_count INTEGER DEFAULT 0,
                    uptime_seconds INTEGER DEFAULT 0,
                    first_activity TEXT,
                    last_activity TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(mac_address, date)
                )
            ''')
            
            # Create indexes for efficient querying
            cursor.execute('''
                CREATE INDEX IF NOT EXISTS idx_health_monitoring_mac_date 
                ON health_monitoring(mac_address, date DESC)
            ''')
            
            cursor.execute('''
                CREATE INDEX IF NOT EXISTS idx_health_monitoring_date 
                ON health_monitoring(date DESC)
            ''')
            
            conn.commit()
            conn.close()
    except Exception as e:
        import logging
        logger = logging.getLogger(__name__)
        logger.error(f"Error initializing health monitoring database: {str(e)}", exc_info=True)


def _persist_to_database():
    """Persist in-memory stats to database (one record per device per day)."""
    try:
        with _stats_lock:
            # Copy current stats to avoid holding lock during DB operations
            stats_to_persist = {}
            for mac, days in _health_stats.items():
                stats_to_persist[mac] = dict(days)
        
        with _db_lock:
            conn = sqlite3.connect(LOGS_DB_PATH, check_same_thread=False)
            cursor = conn.cursor()
            
            for mac, days in stats_to_persist.items():
                for date, stats in days.items():
                    # Check if record exists for this mac and date
                    cursor.execute('''
                        SELECT id FROM health_monitoring 
                        WHERE mac_address = ? AND date = ?
                    ''', (mac, date))
                    existing = cursor.fetchone()
                    
                    if existing:
                        # Update existing record
                        cursor.execute('''
                            UPDATE health_monitoring SET
                                device_created_at = ?,
                                connection_count = ?,
                                event_count = ?,
                                file_upload_count = ?,
                                error_count = ?,
                                connection_loss_count = ?,
                                uptime_seconds = ?,
                                first_activity = ?,
                                last_activity = ?,
                                updated_at = ?
                            WHERE mac_address = ? AND date = ?
                        ''', (
                            stats.get('device_created_at'),
                            stats.get('connection_count', 0),
                            stats.get('event_count', 0),
                            stats.get('file_upload_count', 0),
                            stats.get('error_count', 0),
                            stats.get('connection_loss_count', 0),
                            stats.get('total_uptime_seconds', 0),
                            stats.get('first_activity'),
                            stats.get('last_activity'),
                            datetime.now().isoformat(),
                            mac,
                            date
                        ))
                    else:
                        # Insert new record
                        cursor.execute('''
                            INSERT INTO health_monitoring 
                            (mac_address, date, device_created_at, connection_count, 
                             event_count, file_upload_count, error_count, 
                             connection_loss_count, uptime_seconds, first_activity, last_activity)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ''', (
                            mac,
                            date,
                            stats.get('device_created_at'),
                            stats.get('connection_count', 0),
                            stats.get('event_count', 0),
                            stats.get('file_upload_count', 0),
                            stats.get('error_count', 0),
                            stats.get('connection_loss_count', 0),
                            stats.get('total_uptime_seconds', 0),
                            stats.get('first_activity'),
                            stats.get('last_activity')
                        ))
            
            conn.commit()
            conn.close()
    except Exception as e:
        import logging
        logger = logging.getLogger(__name__)
        logger.error(f"Error persisting health stats to database: {str(e)}", exc_info=True)


def _check_connection_loss():
    """Check for devices that haven't sent activity in 5 minutes."""
    current_time = time.time()
    today = _get_today_key()
    
    with _stats_lock:
        for mac, last_time in list(_last_activity.items()):
            time_since_last_activity = current_time - last_time
            if time_since_last_activity > _connection_loss_threshold:
                stats = _health_stats[mac][today]
                # Only count connection loss if we haven't already counted it for this period
                # We track this by checking if the last_activity in stats is significantly older
                # than the threshold, meaning we've already counted this loss
                if stats.get('last_activity'):
                    try:
                        last_activity_dt = datetime.fromisoformat(stats['last_activity'])
                        last_activity_ts = last_activity_dt.timestamp()
                        # If the stored last_activity is also old, we may have already counted this
                        # Only increment if the gap is new (i.e., last_activity was recent but now it's old)
                        if current_time - last_activity_ts > _connection_loss_threshold:
                            # Check if we should increment (avoid counting multiple times for same loss period)
                            # Increment only if this is a new loss period (last check was within threshold)
                            stats['connection_loss_count'] += 1
                            # Update to mark that we've counted this loss
                            stats['last_activity'] = datetime.now().isoformat()
                            _last_activity[mac] = current_time
                    except (ValueError, TypeError):
                        # If parsing fails, just update the count
                        stats['connection_loss_count'] += 1
                        stats['last_activity'] = datetime.now().isoformat()
                        _last_activity[mac] = current_time
                else:
                    # No last_activity recorded, count as loss
                    stats['connection_loss_count'] += 1
                    stats['last_activity'] = datetime.now().isoformat()
                    _last_activity[mac] = current_time


def _persistence_worker():
    """Background worker that periodically persists stats to database."""
    global _running
    _running = True
    
    while _running:
        try:
            time.sleep(_persistence_interval)
            if _running:
                _check_connection_loss()
                _persist_to_database()
        except Exception as e:
            import logging
            logger = logging.getLogger(__name__)
            logger.error(f"Error in health monitoring persistence worker: {str(e)}", exc_info=True)


def start_monitoring():
    """Start the health monitoring service."""
    global _persistence_thread
    
    import logging
    logger = logging.getLogger(__name__)
    
    try:
        _initialize_database()
        logger.info("Device health monitoring database initialized")
    except Exception as e:
        logger.error(f"Failed to initialize device health database: {str(e)}", exc_info=True)
        raise
    
    if _persistence_thread is None or not _persistence_thread.is_alive():
        _persistence_thread = threading.Thread(
            target=_persistence_worker,
            daemon=True,
            name="DeviceHealthMonitor"
        )
        _persistence_thread.start()
        logger.info("Device health monitoring persistence thread started")


def force_persist():
    """Force immediate persistence of current stats to database."""
    try:
        _check_connection_loss()
        _persist_to_database()
        return True
    except Exception as e:
        import logging
        logger = logging.getLogger(__name__)
        logger.error(f"Error forcing device health persistence: {str(e)}", exc_info=True)
        return False


def stop_monitoring():
    """Stop the health monitoring service."""
    global _running
    _running = False
    # Final persist before shutdown
    _persist_to_database()


def track_device_created(mac: str):
    """Track when a device was first created."""
    mac = _health_mac_key(mac)
    today = _get_today_key()
    
    with _stats_lock:
        stats = _health_stats[mac][today]
        if not stats.get('device_created_at'):
            stats['device_created_at'] = datetime.now().isoformat()
            # Also set first activity if not set
            if not stats.get('first_activity'):
                stats['first_activity'] = datetime.now().isoformat()
            _last_activity[mac] = time.time()


def track_connection(mac: str):
    """Track a device connection to the API."""
    mac = _health_mac_key(mac)
    if not mac:
        return
    _touch_visual_last_seen(mac)
    today = _get_today_key()
    current_time = time.time()

    with _stats_lock:
        stats = _health_stats[mac][today]
        stats["connection_count"] += 1

        now_iso = datetime.now().isoformat()
        if not stats.get("first_activity"):
            stats["first_activity"] = now_iso
        stats["last_activity"] = now_iso
        _last_activity[mac] = current_time

        if stats.get("first_activity"):
            first_activity = datetime.fromisoformat(stats["first_activity"])
            uptime = (datetime.now() - first_activity).total_seconds()
            stats["total_uptime_seconds"] = int(uptime)


def track_event(mac: str):
    """Track an event sent by a device."""
    mac = _health_mac_key(mac)
    if not mac:
        return
    today = _get_today_key()
    current_time = time.time()
    
    with _stats_lock:
        stats = _health_stats[mac][today]
        stats['event_count'] += 1
        
        # Update activity times
        now_iso = datetime.now().isoformat()
        if not stats.get('first_activity'):
            stats['first_activity'] = now_iso
        stats['last_activity'] = now_iso
        _last_activity[mac] = current_time
        
        # Update uptime
        if stats.get('first_activity'):
            first_activity = datetime.fromisoformat(stats['first_activity'])
            uptime = (datetime.now() - first_activity).total_seconds()
            stats['total_uptime_seconds'] = int(uptime)


def track_file_upload(mac: str):
    """Track a file upload from a device."""
    mac = _health_mac_key(mac)
    if not mac:
        return
    today = _get_today_key()
    current_time = time.time()
    
    with _stats_lock:
        stats = _health_stats[mac][today]
        stats['file_upload_count'] += 1
        
        # Update activity times
        now_iso = datetime.now().isoformat()
        if not stats.get('first_activity'):
            stats['first_activity'] = now_iso
        stats['last_activity'] = now_iso
        _last_activity[mac] = current_time
        
        # Update uptime
        if stats.get('first_activity'):
            first_activity = datetime.fromisoformat(stats['first_activity'])
            uptime = (datetime.now() - first_activity).total_seconds()
            stats['total_uptime_seconds'] = int(uptime)


def track_error(mac: str):
    """Track an error reported by a device."""
    mac = _health_mac_key(mac)
    if not mac:
        return
    today = _get_today_key()
    
    with _stats_lock:
        stats = _health_stats[mac][today]
        stats['error_count'] += 1


def get_health_stats(mac: Optional[str] = None, date: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Get health statistics from database.
    
    Args:
        mac: MAC address filter (optional, returns all devices if not provided)
        date: Date filter in YYYY-MM-DD format (optional, defaults to today)
    
    Returns:
        List of health records
    """
    if date is None:
        date = _get_today_key()
    
    try:
        with _db_lock:
            conn = sqlite3.connect(LOGS_DB_PATH, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            
            query = "SELECT * FROM health_monitoring WHERE date = ?"
            params = [date]
            
            if mac:
                query += " AND mac_address = ?"
                params.append(_health_mac_key(mac))
            
            query += " ORDER BY mac_address"
            
            cursor.execute(query, params)
            rows = cursor.fetchall()
            
            results = []
            for row in rows:
                # Format uptime as HH:MM:SS
                uptime_seconds = row['uptime_seconds'] or 0
                hours = uptime_seconds // 3600
                minutes = (uptime_seconds % 3600) // 60
                seconds = uptime_seconds % 60
                uptime_formatted = f"{int(hours):02d}:{int(minutes):02d}:{int(seconds):02d}"
                
                result = {
                    'mac_address': row['mac_address'],
                    'date': row['date'],
                    'device_created_at': row['device_created_at'],
                    'connection_count': row['connection_count'],
                    'event_count': row['event_count'],
                    'file_upload_count': row['file_upload_count'],
                    'error_count': row['error_count'],
                    'connection_loss_count': row['connection_loss_count'],
                    'uptime_seconds': row['uptime_seconds'],
                    'uptime_formatted': uptime_formatted,
                    'first_activity': row['first_activity'],
                    'last_activity': row['last_activity'],
                    'created_at': row['created_at'],
                    'updated_at': row['updated_at']
                }
                results.append(result)
            
            conn.close()
            return results
    except Exception as e:
        import logging
        logger = logging.getLogger(__name__)
        logger.error(f"Error retrieving health stats: {str(e)}", exc_info=True)
        return []


def get_current_health_stats(mac: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Get current in-memory health statistics (not yet persisted).
    
    Args:
        mac: MAC address filter (optional)
    
    Returns:
        List of current health stats
    """
    today = _get_today_key()
    results = []
    
    mac_filter = _health_mac_key(mac) if mac else None
    with _stats_lock:
        for device_mac, days in _health_stats.items():
            if mac_filter and device_mac != mac_filter:
                continue
            
            if today in days:
                stats = days[today]
                
                # Format uptime
                uptime_seconds = stats.get('total_uptime_seconds', 0)
                hours = uptime_seconds // 3600
                minutes = (uptime_seconds % 3600) // 60
                seconds = uptime_seconds % 60
                uptime_formatted = f"{int(hours):02d}:{int(minutes):02d}:{int(seconds):02d}"
                
                result = {
                    'mac_address': device_mac,
                    'date': today,
                    'device_created_at': stats.get('device_created_at'),
                    'connection_count': stats.get('connection_count', 0),
                    'event_count': stats.get('event_count', 0),
                    'file_upload_count': stats.get('file_upload_count', 0),
                    'error_count': stats.get('error_count', 0),
                    'connection_loss_count': stats.get('connection_loss_count', 0),
                    'uptime_seconds': uptime_seconds,
                    'uptime_formatted': uptime_formatted,
                    'first_activity': stats.get('first_activity'),
                    'last_activity': stats.get('last_activity')
                }
                results.append(result)
    
    return results


# Initialize on module import
try:
    start_monitoring()
    import logging
    logger = logging.getLogger(__name__)
    logger.info("Device health monitoring service started successfully")
except Exception as e:
    import logging
    logger = logging.getLogger(__name__)
    logger.error(f"Could not start device health monitoring: {str(e)}", exc_info=True)

