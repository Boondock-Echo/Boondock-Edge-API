"""
Database Logging Handler - Python logging handler for database storage.

This handler integrates with Python's logging module and writes logs
to the database asynchronously.
"""

import logging
import traceback
from typing import Optional
from ..services.db_logging_manager import get_db_logging_manager, LOG_TYPES


class DatabaseLoggingHandler(logging.Handler):
    """
    Custom logging handler that writes logs to the database.
    Integrates seamlessly with Python's logging module.
    """
    
    def __init__(self, log_type: str = 'app', level=logging.NOTSET):
        """
        Initialize the database logging handler.
        
        Args:
            log_type: Type of log (error, warning, transcription, database, event, app)
            level: Logging level threshold
        """
        super().__init__(level)
        self.log_type = log_type
        self.db_manager = get_db_logging_manager()
        
        # Validate log type
        if log_type not in LOG_TYPES:
            self.log_type = 'app'
    
    def emit(self, record: logging.LogRecord):
        """
        Emit a log record to the database.
        
        Args:
            record: LogRecord instance from Python logging
        """
        try:
            # Determine log type based on level if not explicitly set
            if self.log_type == 'app':
                if record.levelno >= logging.ERROR:
                    log_type = 'error'
                elif record.levelno >= logging.WARNING:
                    log_type = 'warning'
                else:
                    log_type = 'app'
            else:
                log_type = self.log_type
            
            # Get exception info if present
            exception_info = None
            if record.exc_info:
                exception_info = ''.join(traceback.format_exception(*record.exc_info))
            
            # Extract module, function, and line number
            module = record.module if hasattr(record, 'module') else None
            function = record.funcName if hasattr(record, 'funcName') else None
            line_number = record.lineno if hasattr(record, 'lineno') else None
            
            # Get level name
            level = record.levelname
            
            # Format message
            message = self.format(record)
            
            # Log to database
            self.db_manager.log(
                log_type=log_type,
                level=level,
                message=message,
                logger_name=record.name,
                module=module,
                function=function,
                line_number=line_number,
                exception_info=exception_info
            )
        except Exception:
            # Prevent logging errors from breaking the application
            # Use print as last resort
            self.handleError(record)
    
    def close(self):
        """Close the handler."""
        super().close()

