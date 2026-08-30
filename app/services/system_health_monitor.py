"""
System Health Monitoring Service

Tracks system health metrics (CPU, RAM, disk) in memory and periodically
persists them to the logs database. Stores current, peak, and average values.
"""

import sqlite3
import threading
import time
import psutil
import platform
import shutil
import os
from config import Config, DATA_ROOT
from datetime import datetime, timedelta
from typing import Dict, Optional, List, Any
from collections import deque

LOGS_DB_PATH = Config.get_logs_db_path()
_db_lock = threading.RLock()

# In-memory metrics storage
_current_metrics = {
    'cpu_percent': 0.0,
    'cpu_count': 0,
    'memory_total_bytes': 0,
    'memory_available_bytes': 0,
    'memory_used_bytes': 0,
    'memory_percent': 0.0,
    'disk_total_bytes': 0,
    'disk_used_bytes': 0,
    'disk_free_bytes': 0,
    'disk_percent': 0.0,
    'timestamp': None
}

# Historical data for calculating averages (keep last 60 samples = 5 minutes at 5s intervals)
_metrics_history = {
    'cpu_percent': deque(maxlen=60),
    'memory_percent': deque(maxlen=60),
    'disk_percent': deque(maxlen=60),
    'memory_used_bytes': deque(maxlen=60),
    'disk_used_bytes': deque(maxlen=60)
}

# Peak values (reset daily)
_peak_values = {
    'cpu_percent': 0.0,
    'memory_percent': 0.0,
    'disk_percent': 0.0,
    'memory_used_bytes': 0,
    'disk_used_bytes': 0
}

# Thread lock for in-memory stats
_stats_lock = threading.RLock()

# Background threads
_collection_thread = None
_persistence_thread = None
_collection_interval = 5  # Collect metrics every 5 seconds
_persistence_interval = 300  # Persist to DB every 5 minutes
_running = False
_last_reset_date = None


def _get_today_key() -> str:
    """Get today's date as YYYY-MM-DD string."""
    return datetime.now().strftime('%Y-%m-%d')


def _reset_peaks_if_new_day():
    """Reset peak values if it's a new day."""
    global _last_reset_date
    today = _get_today_key()
    
    if _last_reset_date != today:
        with _stats_lock:
            _peak_values = {
                'cpu_percent': 0.0,
                'memory_percent': 0.0,
                'disk_percent': 0.0,
                'memory_used_bytes': 0,
                'disk_used_bytes': 0
            }
        _last_reset_date = today


def _collect_metrics():
    """Collect current system metrics."""
    try:
        _reset_peaks_if_new_day()
        
        # CPU metrics
        cpu_percent = psutil.cpu_percent(interval=1)
        cpu_count = psutil.cpu_count()
        
        # Memory metrics
        memory = psutil.virtual_memory()
        memory_total = memory.total
        memory_available = memory.available
        memory_used = memory.used
        memory_percent = memory.percent
        
        # Disk metrics (use backend directory)
        # Use shutil.disk_usage() which is more reliable cross-platform than psutil.disk_usage()
        disk_total = 0
        disk_used = 0
        disk_free = 0
        disk_percent = 0.0
        
        try:
            # Use shutil.disk_usage() - it's part of standard library and more reliable
            # It works with any valid path, not just mount points
            disk = shutil.disk_usage(DATA_ROOT)
            disk_total = disk.total
            disk_used = disk.used
            disk_free = disk.free
            disk_percent = (disk_used / disk_total * 100) if disk_total > 0 else 0.0
        except (OSError, PermissionError, ValueError) as disk_error:
            # If shutil fails, try psutil as fallback
            try:
                # Try with the backend directory directly
                disk = psutil.disk_usage(DATA_ROOT)
                disk_total = disk.total
                disk_used = disk.used
                disk_free = disk.free
                disk_percent = (disk_used / disk_total * 100) if disk_total > 0 else 0.0
            except Exception:
                # Last resort: try common paths
                import logging
                logger = logging.getLogger(__name__)
                
                fallback_paths = []
                if os.name == 'nt':  # Windows
                    # Try to get drive from backend directory
                    normalized = str(DATA_ROOT)
                    if len(normalized) >= 2 and normalized[1] == ':':
                        drive_letter = normalized[0]
                        fallback_paths = [f"{drive_letter}:\\", "C:\\", "D:\\"]
                    else:
                        fallback_paths = ["C:\\", "D:\\", "E:\\"]
                else:  # Unix/Linux
                    fallback_paths = ['/', '/home', '/var']
                
                disk_found = False
                for fallback_path in fallback_paths:
                    try:
                        if os.path.exists(fallback_path):
                            disk = shutil.disk_usage(fallback_path)
                            disk_total = disk.total
                            disk_used = disk.used
                            disk_free = disk.free
                            disk_percent = (disk_used / disk_total * 100) if disk_total > 0 else 0.0
                            disk_found = True
                            break
                    except Exception:
                        continue
                
                if not disk_found:
                    # If all else fails, log warning and use zero values
                    logger.warning(f"Could not get disk usage after multiple attempts: {disk_error}. Using default values.")
                    disk_total = 0
                    disk_used = 0
                    disk_free = 0
                    disk_percent = 0.0
        
        with _stats_lock:
            # Update current metrics
            _current_metrics.update({
                'cpu_percent': cpu_percent,
                'cpu_count': cpu_count,
                'memory_total_bytes': memory_total,
                'memory_available_bytes': memory_available,
                'memory_used_bytes': memory_used,
                'memory_percent': memory_percent,
                'disk_total_bytes': disk_total,
                'disk_used_bytes': disk_used,
                'disk_free_bytes': disk_free,
                'disk_percent': disk_percent,
                'timestamp': datetime.now().isoformat()
            })
            
            # Add to history for averages
            _metrics_history['cpu_percent'].append(cpu_percent)
            _metrics_history['memory_percent'].append(memory_percent)
            _metrics_history['disk_percent'].append(disk_percent)
            _metrics_history['memory_used_bytes'].append(memory_used)
            _metrics_history['disk_used_bytes'].append(disk_used)
            
            # Update peak values
            if cpu_percent > _peak_values['cpu_percent']:
                _peak_values['cpu_percent'] = cpu_percent
            if memory_percent > _peak_values['memory_percent']:
                _peak_values['memory_percent'] = memory_percent
            if disk_percent > _peak_values['disk_percent']:
                _peak_values['disk_percent'] = disk_percent
            if memory_used > _peak_values['memory_used_bytes']:
                _peak_values['memory_used_bytes'] = memory_used
            if disk_used > _peak_values['disk_used_bytes']:
                _peak_values['disk_used_bytes'] = disk_used
                
    except Exception as e:
        import logging
        logger = logging.getLogger(__name__)
        logger.error(f"Error collecting system metrics: {str(e)}", exc_info=True)


def _calculate_averages() -> Dict[str, float]:
    """Calculate average values from history."""
    averages = {}
    
    with _stats_lock:
        for key, history in _metrics_history.items():
            if history:
                averages[key] = sum(history) / len(history)
            else:
                averages[key] = 0.0
    
    return averages


def _initialize_database():
    """Initialize the system_health table in logs database."""
    try:
        with _db_lock:
            conn = sqlite3.connect(LOGS_DB_PATH, check_same_thread=False)
            cursor = conn.cursor()
            
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS system_health (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    cpu_percent REAL,
                    cpu_count INTEGER,
                    cpu_peak_percent REAL,
                    cpu_avg_percent REAL,
                    memory_total_bytes INTEGER,
                    memory_available_bytes INTEGER,
                    memory_used_bytes INTEGER,
                    memory_percent REAL,
                    memory_peak_percent REAL,
                    memory_peak_used_bytes INTEGER,
                    memory_avg_percent REAL,
                    memory_avg_used_bytes INTEGER,
                    disk_total_bytes INTEGER,
                    disk_used_bytes INTEGER,
                    disk_free_bytes INTEGER,
                    disk_percent REAL,
                    disk_peak_percent REAL,
                    disk_peak_used_bytes INTEGER,
                    disk_avg_percent REAL,
                    disk_avg_used_bytes INTEGER,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(date)
                )
            ''')
            
            # Create indexes for efficient querying
            cursor.execute('''
                CREATE INDEX IF NOT EXISTS idx_system_health_date 
                ON system_health(date DESC)
            ''')
            
            cursor.execute('''
                CREATE INDEX IF NOT EXISTS idx_system_health_timestamp 
                ON system_health(timestamp DESC)
            ''')
            
            conn.commit()
            conn.close()
    except Exception as e:
        import logging
        logger = logging.getLogger(__name__)
        logger.error(f"Error initializing system health database: {str(e)}", exc_info=True)


def _persist_to_database():
    """Persist current metrics and statistics to database (one record per day)."""
    try:
        today = _get_today_key()
        
        with _stats_lock:
            current = _current_metrics.copy()
            peaks = _peak_values.copy()
        
        averages = _calculate_averages()
        
        with _db_lock:
            conn = sqlite3.connect(LOGS_DB_PATH, check_same_thread=False)
            cursor = conn.cursor()
            
            # Check if record exists for today
            cursor.execute('''
                SELECT id FROM system_health WHERE date = ?
            ''', (today,))
            existing = cursor.fetchone()
            
            if existing:
                # Update existing record
                cursor.execute('''
                    UPDATE system_health SET
                        timestamp = ?,
                        cpu_percent = ?,
                        cpu_count = ?,
                        cpu_peak_percent = ?,
                        cpu_avg_percent = ?,
                        memory_total_bytes = ?,
                        memory_available_bytes = ?,
                        memory_used_bytes = ?,
                        memory_percent = ?,
                        memory_peak_percent = ?,
                        memory_peak_used_bytes = ?,
                        memory_avg_percent = ?,
                        memory_avg_used_bytes = ?,
                        disk_total_bytes = ?,
                        disk_used_bytes = ?,
                        disk_free_bytes = ?,
                        disk_percent = ?,
                        disk_peak_percent = ?,
                        disk_peak_used_bytes = ?,
                        disk_avg_percent = ?,
                        disk_avg_used_bytes = ?
                    WHERE date = ?
                ''', (
                    current.get('timestamp'),
                    current.get('cpu_percent', 0.0),
                    current.get('cpu_count', 0),
                    peaks.get('cpu_percent', 0.0),
                    averages.get('cpu_percent', 0.0),
                    current.get('memory_total_bytes', 0),
                    current.get('memory_available_bytes', 0),
                    current.get('memory_used_bytes', 0),
                    current.get('memory_percent', 0.0),
                    peaks.get('memory_percent', 0.0),
                    peaks.get('memory_used_bytes', 0),
                    averages.get('memory_percent', 0.0),
                    int(averages.get('memory_used_bytes', 0)),
                    current.get('disk_total_bytes', 0),
                    current.get('disk_used_bytes', 0),
                    current.get('disk_free_bytes', 0),
                    current.get('disk_percent', 0.0),
                    peaks.get('disk_percent', 0.0),
                    peaks.get('disk_used_bytes', 0),
                    averages.get('disk_percent', 0.0),
                    int(averages.get('disk_used_bytes', 0)),
                    today
                ))
            else:
                # Insert new record
                cursor.execute('''
                    INSERT INTO system_health 
                    (date, timestamp, cpu_percent, cpu_count, cpu_peak_percent, cpu_avg_percent,
                     memory_total_bytes, memory_available_bytes, memory_used_bytes, memory_percent,
                     memory_peak_percent, memory_peak_used_bytes, memory_avg_percent, memory_avg_used_bytes,
                     disk_total_bytes, disk_used_bytes, disk_free_bytes, disk_percent,
                     disk_peak_percent, disk_peak_used_bytes, disk_avg_percent, disk_avg_used_bytes)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    today,
                    current.get('timestamp'),
                    current.get('cpu_percent', 0.0),
                    current.get('cpu_count', 0),
                    peaks.get('cpu_percent', 0.0),
                    averages.get('cpu_percent', 0.0),
                    current.get('memory_total_bytes', 0),
                    current.get('memory_available_bytes', 0),
                    current.get('memory_used_bytes', 0),
                    current.get('memory_percent', 0.0),
                    peaks.get('memory_percent', 0.0),
                    peaks.get('memory_used_bytes', 0),
                    averages.get('memory_percent', 0.0),
                    int(averages.get('memory_used_bytes', 0)),
                    current.get('disk_total_bytes', 0),
                    current.get('disk_used_bytes', 0),
                    current.get('disk_free_bytes', 0),
                    current.get('disk_percent', 0.0),
                    peaks.get('disk_percent', 0.0),
                    peaks.get('disk_used_bytes', 0),
                    averages.get('disk_percent', 0.0),
                    int(averages.get('disk_used_bytes', 0))
                ))
            
            conn.commit()
            conn.close()
    except Exception as e:
        import logging
        logger = logging.getLogger(__name__)
        logger.error(f"Error persisting system health to database: {str(e)}", exc_info=True)


def _collection_worker():
    """Background worker that periodically collects metrics."""
    global _running
    _running = True
    
    while _running:
        try:
            _collect_metrics()
            time.sleep(_collection_interval)
        except Exception as e:
            import logging
            logger = logging.getLogger(__name__)
            logger.error(f"Error in system health collection worker: {str(e)}", exc_info=True)
            time.sleep(_collection_interval)


def _persistence_worker():
    """Background worker that periodically persists stats to database."""
    global _running
    
    while _running:
        try:
            time.sleep(_persistence_interval)
            if _running:
                _persist_to_database()
        except Exception as e:
            import logging
            logger = logging.getLogger(__name__)
            logger.error(f"Error in system health persistence worker: {str(e)}", exc_info=True)


def start_monitoring():
    """Start the system health monitoring service."""
    global _collection_thread, _persistence_thread
    
    import logging
    logger = logging.getLogger(__name__)
    
    try:
        _initialize_database()
        logger.info("System health monitoring database initialized")
    except Exception as e:
        logger.error(f"Failed to initialize system health database: {str(e)}", exc_info=True)
        raise
    
    # Initial collection
    try:
        _collect_metrics()
        logger.info("Initial system health metrics collected")
    except Exception as e:
        logger.error(f"Failed to collect initial system health metrics: {str(e)}", exc_info=True)
        # Don't raise - continue with thread startup
    
    if _collection_thread is None or not _collection_thread.is_alive():
        _collection_thread = threading.Thread(
            target=_collection_worker,
            daemon=True,
            name="SystemHealthCollector"
        )
        _collection_thread.start()
        logger.info("System health collection thread started")
    
    if _persistence_thread is None or not _persistence_thread.is_alive():
        _persistence_thread = threading.Thread(
            target=_persistence_worker,
            daemon=True,
            name="SystemHealthPersistence"
        )
        _persistence_thread.start()
        logger.info("System health persistence thread started")


def force_persist():
    """Force immediate persistence of current metrics to database."""
    try:
        _persist_to_database()
        return True
    except Exception as e:
        import logging
        logger = logging.getLogger(__name__)
        logger.error(f"Error forcing system health persistence: {str(e)}", exc_info=True)
        return False


def stop_monitoring():
    """Stop the system health monitoring service."""
    global _running
    _running = False
    # Final persist before shutdown
    _persist_to_database()


def get_current_metrics() -> Dict[str, Any]:
    """Get current system metrics."""
    with _stats_lock:
        current = _current_metrics.copy()
        peaks = _peak_values.copy()
    
    averages = _calculate_averages()
    
    return {
        'current': current,
        'peaks': peaks,
        'averages': averages,
        'timestamp': datetime.now().isoformat()
    }


def get_health_stats(date: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """
    Get system health statistics from database.
    
    Args:
        date: Date filter in YYYY-MM-DD format (optional, defaults to today)
    
    Returns:
        Health record or None
    """
    if date is None:
        date = _get_today_key()
    
    try:
        with _db_lock:
            conn = sqlite3.connect(LOGS_DB_PATH, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            
            cursor.execute('''
                SELECT * FROM system_health WHERE date = ?
            ''', (date,))
            
            row = cursor.fetchone()
            conn.close()
            
            if row:
                return {
                    'date': row['date'],
                    'timestamp': row['timestamp'],
                    'cpu': {
                        'percent': row['cpu_percent'],
                        'count': row['cpu_count'],
                        'peak_percent': row['cpu_peak_percent'],
                        'avg_percent': row['cpu_avg_percent']
                    },
                    'memory': {
                        'total_bytes': row['memory_total_bytes'],
                        'available_bytes': row['memory_available_bytes'],
                        'used_bytes': row['memory_used_bytes'],
                        'percent': row['memory_percent'],
                        'peak_percent': row['memory_peak_percent'],
                        'peak_used_bytes': row['memory_peak_used_bytes'],
                        'avg_percent': row['memory_avg_percent'],
                        'avg_used_bytes': row['memory_avg_used_bytes']
                    },
                    'disk': {
                        'total_bytes': row['disk_total_bytes'],
                        'used_bytes': row['disk_used_bytes'],
                        'free_bytes': row['disk_free_bytes'],
                        'percent': row['disk_percent'],
                        'peak_percent': row['disk_peak_percent'],
                        'peak_used_bytes': row['disk_peak_used_bytes'],
                        'avg_percent': row['disk_avg_percent'],
                        'avg_used_bytes': row['disk_avg_used_bytes']
                    },
                    'created_at': row['created_at']
                }
            return None
    except Exception as e:
        import logging
        logger = logging.getLogger(__name__)
        logger.error(f"Error retrieving system health stats: {str(e)}", exc_info=True)
        return None


# Initialize on module import
try:
    start_monitoring()
    import logging
    logger = logging.getLogger(__name__)
    logger.info("System health monitoring service started successfully")
except Exception as e:
    import logging
    logger = logging.getLogger(__name__)
    logger.error(f"Could not start system health monitoring: {str(e)}", exc_info=True)

