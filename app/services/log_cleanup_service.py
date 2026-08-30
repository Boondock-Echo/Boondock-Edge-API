# app/services/log_cleanup_service.py
"""
Log Cleanup Service - Periodically removes log files and database records older than 30 days.
Logs are backed up to iDrive overnight, so it's safe to delete old files and records.
"""
import re
import logging
import sqlite3
from config import Config
from datetime import datetime, timedelta

log = logging.getLogger(__name__)

# Log directory to clean - get from centralized config
ROOT_LOGS_DIR = Config.get_logs_dir()
LOGS_DB_PATH = Config.get_logs_db_path()

# Retention period: 30 days
RETENTION_DAYS = 30

# Log types in logs.db
LOG_TABLE_NAMES = ['errors', 'warnings', 'transcription', 'database', 'event', 'app', 'com_port']


def _parse_date_from_filename(filename):
    """
    Parse date from log filename.
    
    Supports formats:
    - YYYY-MM-DD_{log_type}.log (e.g., 2025-11-21_database.log)
    - {PORT}_YYYY-MM-DD.log (e.g., COM3_2025-11-21.log)
    
    Returns:
        datetime.date or None if date cannot be parsed
    """
    try:
        # Try format: YYYY-MM-DD_{log_type}.log (date at start)
        if '_' in filename:
            parts = filename.split('_', 1)  # Split on first underscore only
            date_str = parts[0]
            if len(date_str) == 10 and date_str.count('-') == 2:
                try:
                    return datetime.strptime(date_str, '%Y-%m-%d').date()
                except ValueError:
                    pass  # Not a valid date, continue to next check
        
        # Try format: {PORT}_YYYY-MM-DD.log (date at end)
        if '_' in filename:
            parts = filename.rsplit('_', 1)  # Split from right, take last part
            if len(parts) == 2:
                date_str = parts[1].replace('.log', '')
                if len(date_str) == 10 and date_str.count('-') == 2:
                    try:
                        return datetime.strptime(date_str, '%Y-%m-%d').date()
                    except ValueError:
                        pass  # Not a valid date, continue to next check
        
        # Try to find date pattern anywhere in filename (fallback)
        date_pattern = r'(\d{4}-\d{2}-\d{2})'
        match = re.search(date_pattern, filename)
        if match:
            try:
                return datetime.strptime(match.group(1), '%Y-%m-%d').date()
            except ValueError:
                pass
            
    except (ValueError, AttributeError) as e:
        log.debug(f"Could not parse date from filename {filename}: {e}")
        return None
    
    return None


def _cleanup_directory(log_dir, cutoff_date):
    """
    Clean up log files in a directory that are older than cutoff_date.
    
    Args:
        log_dir: Path to the directory to clean
        cutoff_date: datetime.date - files older than this will be deleted
    
    Returns:
        tuple: (files_deleted, bytes_freed)
    """
    if not log_dir.exists():
        log.debug(f"Log directory does not exist: {log_dir}")
        return 0, 0
    
    files_deleted = 0
    bytes_freed = 0
    
    try:
        # Walk through all files in the directory
        for file_path in log_dir.rglob("*.log"):
            # Try to parse date from filename
            file_date = _parse_date_from_filename(file_path.name)
            
            if file_date is None:
                # If we can't parse date from filename, use file modification time
                try:
                    mtime = file_path.stat().st_mtime
                    file_date = datetime.fromtimestamp(mtime).date()
                    log.debug(f"Using modification time for {file_path.name}: {file_date}")
                except OSError as e:
                    log.warning(f"Could not get modification time for {file_path}: {e}")
                    continue
            
            # Delete if file is older than cutoff date
            if file_date < cutoff_date:
                try:
                    file_size = file_path.stat().st_size
                    file_path.unlink()
                    files_deleted += 1
                    bytes_freed += file_size
                    log.info(f"Deleted old log file: {file_path} (date: {file_date})")
                except OSError as e:
                    log.error(f"Failed to delete log file {file_path}: {e}")
        
        # Clean up empty directories (but keep the main structure)
        # Only remove empty year/month directories, not the main logs directory
        directories = [p for p in log_dir.rglob("*") if p.is_dir()]

        for directory in sorted(directories, key=lambda p: len(p.parts), reverse=True):
            try:
                directory.rmdir()
                log.debug("Removed empty directory: %s", directory)
            except OSError:
                pass                
    except Exception as e:
        log.error(f"Error cleaning up directory {log_dir}: {e}", exc_info=True)
    
    return files_deleted, bytes_freed


def _cleanup_logs_db_tables(cutoff_datetime):
    """
    Clean up old records from logs.db tables.
    
    Args:
        cutoff_datetime: datetime - records older than this will be deleted
    
    Returns:
        tuple: (records_deleted, bytes_freed)
    """
    if not LOGS_DB_PATH.exists():
        log.debug(f"Logs database does not exist: {LOGS_DB_PATH}")
        return 0, 0
    
    total_records_deleted = 0
    total_bytes_freed = 0
    
    try:
        conn = sqlite3.connect(LOGS_DB_PATH, check_same_thread=False)
        cursor = conn.cursor()
        
        cutoff_iso = cutoff_datetime.isoformat()
        
        for table_name in LOG_TABLE_NAMES:
            try:
                # Check if table exists
                cursor.execute(f"SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table_name,))
                if not cursor.fetchone():
                    log.debug(f"Table {table_name} does not exist, skipping")
                    continue
                
                # Get count before deletion
                cursor.execute(f"SELECT COUNT(*) FROM {table_name} WHERE timestamp < ?", (cutoff_iso,))
                count_before = cursor.fetchone()[0]
                
                if count_before > 0:
                    # Delete old records
                    cursor.execute(f"DELETE FROM {table_name} WHERE timestamp < ?", (cutoff_iso,))
                    records_deleted = cursor.rowcount
                    total_records_deleted += records_deleted
                    log.info(f"Deleted {records_deleted} records from {table_name} table")
                    
                    # Vacuum to reclaim space
                    conn.execute("VACUUM")
                    
            except sqlite3.Error as e:
                log.error(f"Error cleaning up {table_name} table: {e}")
                continue
        
        # Calculate approximate bytes freed (rough estimate)
        # SQLite doesn't provide exact size reduction, so we estimate based on deleted records
        # Average log record is roughly 200-500 bytes
        total_bytes_freed = total_records_deleted * 300  # Rough estimate
        
        conn.commit()
        conn.close()
        
        log.info(f"Logs database cleanup: {total_records_deleted} records deleted")
        
    except Exception as e:
        log.error(f"Error cleaning up logs database: {e}", exc_info=True)
    
    return total_records_deleted, total_bytes_freed


def cleanup_old_logs():
    """
    Main cleanup function that removes log files and database records older than 30 days.
    
    Returns:
        dict: Summary of cleanup operation
    """
    log.info("=" * 60)
    log.info("LOG CLEANUP STARTING")
    log.info("=" * 60)
    
    cutoff_date = datetime.now().date() - timedelta(days=RETENTION_DAYS)
    cutoff_datetime = datetime.combine(cutoff_date, datetime.min.time())
    log.info(f"Cutoff date: {cutoff_date} (deleting files/records older than {RETENTION_DAYS} days)")
    
    total_files_deleted = 0
    total_bytes_freed = 0
    total_records_deleted = 0
    
    # Clean root logs directory (all logs are now in project root logs/ folder)
    if ROOT_LOGS_DIR.exists():
        log.info(f"Cleaning logs directory: {ROOT_LOGS_DIR}")
        files_deleted, bytes_freed = _cleanup_directory(ROOT_LOGS_DIR, cutoff_date)
        total_files_deleted += files_deleted
        total_bytes_freed += bytes_freed
        log.info(f"Logs directory cleanup: {files_deleted} files deleted, {bytes_freed / (1024*1024):.2f} MB freed")
    else:
        log.info(f"Logs directory does not exist: {ROOT_LOGS_DIR}")
    
    # Clean logs.db tables
    log.info(f"Cleaning logs database: {LOGS_DB_PATH}")
    records_deleted, db_bytes_freed = _cleanup_logs_db_tables(cutoff_datetime)
    total_records_deleted += records_deleted
    total_bytes_freed += db_bytes_freed
    
    log.info("=" * 60)
    log.info(f"LOG CLEANUP COMPLETED")
    log.info(f"Total files deleted: {total_files_deleted}")
    log.info(f"Total records deleted: {total_records_deleted}")
    log.info(f"Total space freed: {total_bytes_freed / (1024*1024):.2f} MB")
    log.info("=" * 60)
    
    return {
        'status': 'completed',
        'files_deleted': total_files_deleted,
        'records_deleted': total_records_deleted,
        'bytes_freed': total_bytes_freed,
        'cutoff_date': cutoff_date.isoformat(),
        'retention_days': RETENTION_DAYS
    }
