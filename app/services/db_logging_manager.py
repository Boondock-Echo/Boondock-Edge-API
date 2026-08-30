"""
Database Logging Manager - Centralized asynchronous logging system.

This module provides a fully encapsulated interface for all application logging.
All logs are stored in logs.db SQLite database with separate tables for each log type.
Logging is asynchronous to prevent blocking the main application thread.
"""

import sqlite3
import logging
import threading
import queue
from config import Config
from typing import Optional, Dict, List, Any
from datetime import datetime
from pathlib import Path

# Get the database directory from centralized config
LOGS_DB_PATH = Config.get_logs_db_path()

# Thread lock for database access
_db_lock = threading.RLock()

# Log types mapping
LOG_TYPES = {
    'error': 'errors',
    'warning': 'warnings',
    'transcription': 'transcription',
    'database': 'database',
    'event': 'event',
    'app': 'app',
    'com_port': 'com_port'
}

# Log level mapping
LOG_LEVELS = {
    'DEBUG': logging.DEBUG,
    'INFO': logging.INFO,
    'WARNING': logging.WARNING,
    'ERROR': logging.ERROR,
    'CRITICAL': logging.CRITICAL
}


class DatabaseLoggingManager:
    """
    Centralized database logging manager with full encapsulation.
    All logs are stored in logs.db SQLite database.
    Uses asynchronous queue-based logging to prevent blocking.
    """
    
    _instance = None
    _lock = threading.Lock()
    _initialized = False
    
    def __new__(cls):
        """Singleton pattern to ensure only one instance exists."""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super(DatabaseLoggingManager, cls).__new__(cls)
                    cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        """Initialize the DatabaseLoggingManager (only once)."""
        if self._initialized:
            return
        
        self._initialized = True
        self._log_queue = queue.Queue()
        self._worker_thread = None
        self._shutdown = False
        
        # Ensure database directory exists
        LOGS_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        
        # Initialize database
        self._initialize_database()
        
        # Start worker thread for async logging
        self._start_worker()
    
    def _initialize_database(self):
        """Initialize the logs database with tables for each log type."""
        try:
            with _db_lock:
                conn = sqlite3.connect(LOGS_DB_PATH, check_same_thread=False)
                cursor = conn.cursor()
                
                # Create table for each log type
                for log_type, table_name in LOG_TYPES.items():
                    # COM port logs need an additional port column
                    if log_type == 'com_port':
                        cursor.execute(f'''
                            CREATE TABLE IF NOT EXISTS {table_name} (
                                id INTEGER PRIMARY KEY AUTOINCREMENT,
                                timestamp TEXT NOT NULL,
                                logger_name TEXT NOT NULL,
                                level TEXT NOT NULL,
                                message TEXT NOT NULL,
                                port TEXT,
                                module TEXT,
                                function TEXT,
                                line_number INTEGER,
                                exception_info TEXT,
                                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                            )
                        ''')
                    else:
                        cursor.execute(f'''
                            CREATE TABLE IF NOT EXISTS {table_name} (
                                id INTEGER PRIMARY KEY AUTOINCREMENT,
                                timestamp TEXT NOT NULL,
                                logger_name TEXT NOT NULL,
                                level TEXT NOT NULL,
                                message TEXT NOT NULL,
                                module TEXT,
                                function TEXT,
                                line_number INTEGER,
                                exception_info TEXT,
                                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                            )
                        ''')
                    
                    # Create indexes for efficient querying
                    cursor.execute(f'''
                        CREATE INDEX IF NOT EXISTS idx_{table_name}_timestamp 
                        ON {table_name}(timestamp DESC)
                    ''')
                    
                    cursor.execute(f'''
                        CREATE INDEX IF NOT EXISTS idx_{table_name}_level 
                        ON {table_name}(level)
                    ''')
                    
                    cursor.execute(f'''
                        CREATE INDEX IF NOT EXISTS idx_{table_name}_created_at 
                        ON {table_name}(created_at DESC)
                    ''')
                    
                    # Add port index for COM port logs
                    if log_type == 'com_port':
                        cursor.execute(f'''
                            CREATE INDEX IF NOT EXISTS idx_{table_name}_port 
                            ON {table_name}(port)
                        ''')
                
                # Create health_stats table for maintenance health checks
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS health_stats (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        checked_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        database_sizes TEXT,
                        top_tables TEXT,
                        disk_usage TEXT,
                        details TEXT
                    )
                ''')
                
                cursor.execute('''
                    CREATE INDEX IF NOT EXISTS idx_health_stats_checked_at 
                    ON health_stats(checked_at DESC)
                ''')
                
                conn.commit()
                conn.close()
        except Exception as e:
            # Fallback to console logging if database initialization fails
            print(f"Error initializing logs database: {e}")
    
    def _start_worker(self):
        """Start the background worker thread for async logging."""
        if self._worker_thread is None or not self._worker_thread.is_alive():
            self._shutdown = False
            self._worker_thread = threading.Thread(
                target=self._worker_loop,
                daemon=True,
                name="DatabaseLoggingWorker"
            )
            self._worker_thread.start()
    
    def _worker_loop(self):
        """Background worker loop that processes log queue."""
        batch = []
        batch_size = 50
        batch_timeout = 1.0  # seconds
        
        while not self._shutdown:
            try:
                # Collect logs in batch
                try:
                    log_record = self._log_queue.get(timeout=batch_timeout)
                    batch.append(log_record)
                    
                    # Collect more logs up to batch_size
                    while len(batch) < batch_size:
                        try:
                            log_record = self._log_queue.get_nowait()
                            batch.append(log_record)
                        except queue.Empty:
                            break
                except queue.Empty:
                    pass
                
                # Write batch to database
                if batch:
                    self._write_batch(batch)
                    batch = []
            except Exception as e:
                # Log error to console (can't use database logger here)
                print(f"Error in database logging worker: {e}")
                batch = []
    
    def _write_batch(self, batch: List[Dict[str, Any]]):
        """Write a batch of log records to the database."""
        if not batch:
            return
        
        try:
            with _db_lock:
                conn = sqlite3.connect(LOGS_DB_PATH, check_same_thread=False)
                cursor = conn.cursor()
                
                # Group logs by table
                logs_by_table = {}
                for log_record in batch:
                    table_name = log_record.get('table_name')
                    if table_name not in logs_by_table:
                        logs_by_table[table_name] = []
                    logs_by_table[table_name].append(log_record)
                
                # Insert logs into appropriate tables
                for table_name, logs in logs_by_table.items():
                    # COM port logs have an additional port column
                    if table_name == 'com_port':
                        cursor.executemany(f'''
                            INSERT INTO {table_name} 
                            (timestamp, logger_name, level, message, port, module, function, line_number, exception_info)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ''', [
                            (
                                log.get('timestamp'),
                                log.get('logger_name', ''),
                                log.get('level', 'INFO'),
                                log.get('message', ''),
                                log.get('port'),
                                log.get('module'),
                                log.get('function'),
                                log.get('line_number'),
                                log.get('exception_info')
                            )
                            for log in logs
                        ])
                    else:
                        cursor.executemany(f'''
                            INSERT INTO {table_name} 
                            (timestamp, logger_name, level, message, module, function, line_number, exception_info)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        ''', [
                            (
                                log.get('timestamp'),
                                log.get('logger_name', ''),
                                log.get('level', 'INFO'),
                                log.get('message', ''),
                                log.get('module'),
                                log.get('function'),
                                log.get('line_number'),
                                log.get('exception_info')
                            )
                            for log in logs
                        ])
                
                conn.commit()
                conn.close()
        except Exception as e:
            # Fallback to console logging
            print(f"Error writing logs to database: {e}")
    
    def log(self, log_type: str, level: str, message: str, logger_name: str = '',
            module: Optional[str] = None, function: Optional[str] = None,
            line_number: Optional[int] = None, exception_info: Optional[str] = None,
            port: Optional[str] = None):
        """
        Log a message to the database.
        
        Args:
            log_type: Type of log (error, warning, transcription, database, event, app, com_port)
            level: Log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
            message: Log message
            logger_name: Name of the logger
            module: Module name where log was created
            function: Function name where log was created
            line_number: Line number where log was created
            exception_info: Exception information if applicable
            port: COM port name (for com_port log type)
        """
        if log_type not in LOG_TYPES:
            log_type = 'app'  # Default to app logs
        
        table_name = LOG_TYPES[log_type]
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        
        log_record = {
            'table_name': table_name,
            'timestamp': timestamp,
            'logger_name': logger_name,
            'level': level,
            'message': message,
            'module': module,
            'function': function,
            'line_number': line_number,
            'exception_info': exception_info
        }
        
        # Add port for COM port logs
        if log_type == 'com_port' and port:
            log_record['port'] = port
        
        # Add to queue (non-blocking)
        try:
            self._log_queue.put_nowait(log_record)
        except queue.Full:
            # Queue is full, log to console as fallback
            print(f"Log queue full, dropping log: {message}")
    
    def get_logs(self, log_type: str, date: Optional[str] = None, 
                 level: Optional[str] = None, limit: Optional[int] = None,
                 offset: int = 0, port: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Retrieve logs from the database.
        
        Args:
            log_type: Type of log to retrieve
            date: Date filter in YYYY-MM-DD format (optional)
            level: Log level filter (optional)
            limit: Maximum number of logs to retrieve (optional)
            offset: Offset for pagination (default: 0)
        
        Returns:
            List of log records
        """
        if log_type not in LOG_TYPES:
            return []
        
        table_name = LOG_TYPES[log_type]
        
        try:
            with _db_lock:
                conn = sqlite3.connect(LOGS_DB_PATH, check_same_thread=False)
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                
                # Build query
                query = f"SELECT * FROM {table_name} WHERE 1=1"
                params = []
                
                if date:
                    query += " AND DATE(timestamp) = ?"
                    params.append(date)
                
                if level:
                    query += " AND level = ?"
                    params.append(level)
                
                if port and log_type == 'com_port':
                    query += " AND port = ?"
                    params.append(port)
                
                query += " ORDER BY timestamp DESC"
                
                if limit:
                    query += " LIMIT ? OFFSET ?"
                    params.extend([limit, offset])
                
                cursor.execute(query, params)
                rows = cursor.fetchall()
                
                logs = []
                for row in rows:
                    log_entry = {
                        'id': row['id'],
                        'timestamp': row['timestamp'],
                        'logger': row['logger_name'],
                        'level': row['level'],
                        'message': row['message'],
                        'module': row['module'],
                        'function': row['function'],
                        'line_number': row['line_number'],
                        'exception_info': row['exception_info'],
                        'created_at': row['created_at']
                    }
                    # Add port for COM port logs
                    if log_type == 'com_port' and 'port' in row.keys():
                        log_entry['port'] = row['port']
                    logs.append(log_entry)
                
                conn.close()
                return logs
        except Exception as e:
            print(f"Error retrieving logs from database: {e}")
            return []
    
    def clear_logs(self, log_type: str, date: Optional[str] = None) -> bool:
        """
        Clear logs from the database.
        
        Args:
            log_type: Type of log to clear
            date: Date filter in YYYY-MM-DD format (optional, clears all if not provided)
        
        Returns:
            True if successful, False otherwise
        """
        if log_type not in LOG_TYPES:
            return False
        
        table_name = LOG_TYPES[log_type]
        
        try:
            with _db_lock:
                conn = sqlite3.connect(LOGS_DB_PATH, check_same_thread=False)
                cursor = conn.cursor()
                
                if date:
                    cursor.execute(f"DELETE FROM {table_name} WHERE DATE(timestamp) = ?", (date,))
                else:
                    cursor.execute(f"DELETE FROM {table_name}")
                
                conn.commit()
                conn.close()
                return True
        except Exception as e:
            print(f"Error clearing logs from database: {e}")
            return False
    
    def get_log_statistics(self, log_type: Optional[str] = None, 
                          date: Optional[str] = None) -> Dict[str, Any]:
        """
        Get statistics about logs.
        
        Args:
            log_type: Type of log (optional, all types if not provided)
            date: Date filter in YYYY-MM-DD format (optional)
        
        Returns:
            Dictionary with statistics
        """
        stats = {}
        log_types_to_check = [log_type] if log_type and log_type in LOG_TYPES else LOG_TYPES.keys()
        
        try:
            with _db_lock:
                conn = sqlite3.connect(LOGS_DB_PATH, check_same_thread=False)
                cursor = conn.cursor()
                
                for lt in log_types_to_check:
                    table_name = LOG_TYPES[lt]
                    
                    query = f"SELECT COUNT(*) as total, level, COUNT(*) as count FROM {table_name} WHERE 1=1"
                    params = []
                    
                    if date:
                        query += " AND DATE(timestamp) = ?"
                        params.append(date)
                    
                    query += " GROUP BY level"
                    
                    cursor.execute(query, params)
                    rows = cursor.fetchall()
                    
                    level_counts = {row[1]: row[2] for row in rows}
                    total = sum(level_counts.values())
                    
                    stats[lt] = {
                        'total': total,
                        'by_level': level_counts
                    }
                
                conn.close()
                return stats
        except Exception as e:
            print(f"Error getting log statistics: {e}")
            return {}
    
    def shutdown(self):
        """Shutdown the logging manager and flush remaining logs."""
        self._shutdown = True
        
        # Wait for queue to empty (with timeout)
        import time
        timeout = 5.0
        start_time = time.time()
        
        while not self._log_queue.empty() and (time.time() - start_time) < timeout:
            time.sleep(0.1)
        
        # Write any remaining logs
        remaining_logs = []
        while not self._log_queue.empty():
            try:
                remaining_logs.append(self._log_queue.get_nowait())
            except queue.Empty:
                break
        
        if remaining_logs:
            self._write_batch(remaining_logs)
        
        # Wait for worker thread to finish
        if self._worker_thread and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=2.0)


# Singleton instance getter
def get_db_logging_manager() -> DatabaseLoggingManager:
    """Get the singleton DatabaseLoggingManager instance."""
    return DatabaseLoggingManager()

