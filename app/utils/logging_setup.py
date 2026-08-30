# app/utils/logging_setup.py
"""
Centralized logging setup with database storage.

All logs are stored in logs.db SQLite database with separate tables for each log type.
Logging is asynchronous to prevent blocking the main application thread.
"""
import os
import logging
from datetime import datetime
from logging.handlers import RotatingFileHandler
from app.utils.db_logging_handler import DatabaseLoggingHandler

# Legacy file-based handler (kept for backward compatibility if needed)
class DailyRotatingFileHandler(logging.Handler):
    """
    Custom handler that creates a new log file each day.
    File format: logs/YYYY/MM/YYYY-MM-DD_{log_type}.log
    DEPRECATED: Use DatabaseLoggingHandler instead.
    """
    def __init__(self, log_type, level=logging.INFO):
        super().__init__(level)
        self.log_type = log_type
        self.current_date = None
        self.base_path = 'logs'
        self.file_handler = None
        self._setup_handler()
    
    def _get_log_path(self, date=None):
        """Get the log file path for a given date (or current date if None)"""
        if date is None:
            date = datetime.now()
        elif isinstance(date, str):
            date = datetime.strptime(date, '%Y-%m-%d')
        
        year = date.strftime('%Y')
        month = date.strftime('%m')
        date_str = date.strftime('%Y-%m-%d')
        
        log_dir = os.path.join(self.base_path, year, month)
        log_file = os.path.join(log_dir, f'{date_str}_{self.log_type}.log')
        
        return log_file
    
    def _setup_handler(self):
        """Setup the file handler for the current date"""
        current_date = datetime.now().date()
        
        # If date hasn't changed, keep using the same file
        if self.current_date == current_date and self.file_handler is not None:
            return
        
        # Close previous handler if exists
        if self.file_handler is not None:
            self.file_handler.close()
        
        # Update current date
        self.current_date = current_date
        
        # Get log path for current date
        log_path = self._get_log_path()
        
        # Create directory if it doesn't exist
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        
        # Create new file handler
        self.file_handler = RotatingFileHandler(
            log_path,
            maxBytes=5*1024*1024,  # 5MB
            backupCount=5,
            encoding='utf-8'
        )
        
        # Set formatter
        formatter = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        self.file_handler.setFormatter(formatter)
        self.file_handler.setLevel(self.level)
    
    def emit(self, record):
        """Emit a log record, checking if we need to rotate to a new day"""
        # Check if date has changed
        current_date = datetime.now().date()
        if self.current_date != current_date:
            self._setup_handler()
        
        # Emit using the file handler
        if self.file_handler:
            self.file_handler.emit(record)
    
    def close(self):
        """Close the handler"""
        if self.file_handler:
            self.file_handler.close()
        super().close()

def setup_logger(name, log_type, level=logging.INFO, use_database=True):
    """
    Setup logger with database storage (default) or file storage (legacy).
    
    Args:
        name (str): Logger name
        log_type (str): Type of log (error, warning, transcription, database, event, app)
        level: Logging level
        use_database (bool): If True, use database logging (default). If False, use file logging (legacy).
    
    Returns:
        logging.Logger: Configured logger instance
    """
    # Create formatter
    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    # Setup logger
    logger = logging.getLogger(name)
    logger.setLevel(level)
    
    # Remove existing handlers to prevent duplicate logs
    logger.handlers = []
    
    if use_database:
        # Use database logging handler
        db_handler = DatabaseLoggingHandler(log_type=log_type, level=level)
        db_handler.setFormatter(formatter)
        logger.addHandler(db_handler)
    else:
        # Use legacy file handler (for backward compatibility)
        daily_handler = DailyRotatingFileHandler(log_type, level=level)
        daily_handler.setFormatter(formatter)
        logger.addHandler(daily_handler)
    
    return logger

# Create different loggers for different components
# All loggers use database storage by default
error_logger = setup_logger(
    'error_logger',
    'error',  # Map to 'error' type (stored in 'errors' table)
    level=logging.ERROR,
    use_database=True
)

warning_logger = setup_logger(
    'warning_logger',
    'warning',  # Map to 'warning' type (stored in 'warnings' table)
    level=logging.WARNING,
    use_database=True
)

transcription_logger = setup_logger(
    'transcription_logger',
    'transcription',
    level=logging.INFO,
    use_database=True
)

event_logger = setup_logger(
    'event_logger',
    'event',
    level=logging.INFO,
    use_database=True
)

db_logger = setup_logger(
    'db_logger',
    'database',
    level=logging.INFO,
    use_database=True
)

