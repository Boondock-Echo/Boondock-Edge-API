#!/usr/bin/env python3
"""
Boondock Edge Server - Simplified startup script
Works on both Windows and Ubuntu/Linux

To enable auto-reload on code changes (development mode):
  Windows: set PRODUCTION_MODE=false && python run.py
  Linux:   PRODUCTION_MODE=false python run.py
  
Or set environment variable before running:
  export PRODUCTION_MODE=false  # Linux/Mac
  set PRODUCTION_MODE=false      # Windows
"""
import json
import os
import sys
import logging
import signal
import subprocess
import threading
from pathlib import Path

# Setup logging with database handler
from app.utils.db_logging_handler import DatabaseLoggingHandler

# Create formatter
formatter = logging.Formatter(
    '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

# Setup handlers
handlers = [
    logging.StreamHandler(sys.stdout)  # Console output
]

# Add database handler for app logs (stored in logs.db)
app_db_handler = DatabaseLoggingHandler(log_type='app', level=logging.INFO)
app_db_handler.setFormatter(formatter)
handlers.append(app_db_handler)

# Configure root logger
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=handlers
)
logger = logging.getLogger(__name__)


def _select_socketio_async_mode():
    """Select an async backend without importing native extensions in this process.

    A native extension compiled for a newer CPU can terminate Python with
    ``SIGILL`` (systemd reports ``status=4/ILL``); Python exception handling
    cannot catch that signal.  Probe gevent in a short-lived child first so an
    incompatible greenlet/gevent wheel cannot take down the API process.
    ``SOCKETIO_ASYNC_MODE=threading`` can be used to bypass gevent explicitly.
    """
    requested = os.environ.get("SOCKETIO_ASYNC_MODE", "auto").strip().lower()
    if requested == "threading":
        logger.info("Using threading mode for WebSocket (configured by SOCKETIO_ASYNC_MODE)")
        return "threading"
    if requested not in ("auto", "gevent"):
        logger.warning("Unknown SOCKETIO_ASYNC_MODE=%r; using automatic selection", requested)

    probe = subprocess.run(
        [sys.executable, "-c", "import gevent.websocket"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
        timeout=15,
    )
    if probe.returncode == 0:
        return "gevent"

    if probe.returncode < 0:
        signal_name = signal.Signals(-probe.returncode).name
        logger.error(
            "gevent compatibility probe terminated by %s; using threading mode. "
            "This usually means the installed native wheel does not support this CPU.",
            signal_name,
        )
    else:
        detail = (probe.stderr or "gevent is unavailable").strip().splitlines()[-1]
        logger.info("Using threading mode for WebSocket (%s)", detail)
    return "threading"

# Suppress noisy loggers
logging.getLogger('werkzeug').setLevel(logging.WARNING)
logging.getLogger('waitress').setLevel(logging.WARNING)
logging.getLogger('flask_socketio').setLevel(logging.WARNING)
logging.getLogger('socketio').setLevel(logging.WARNING)
logging.getLogger('engineio').setLevel(logging.WARNING)

# Import Flask app components
from flask_socketio import SocketIO
from app import create_app
from config import Config, CODE_ROOT
from flask_cors import CORS
from app.services.settings_manager import get_settings_manager
from app.routes.radio_routes import initialize_scanner_inventory
from app.routes.recorder_routes import initialize_recorder_inventory


def load_settings():
    """Load settings from database"""
    try:
        settings_manager = get_settings_manager()
        return settings_manager.get_all_settings()
    except Exception as e:
        logger.warning(f"Could not load settings from database: {e}. Using defaults.")
        return {}


def main():
    """Main application entry point"""
    logger.info("=" * 60)
    logger.info("Starting Boondock Edge Server")
    logger.info("Working directory: %s", CODE_ROOT)
    logger.info("=" * 60)
    
    # Load settings (from database)
    settings = load_settings()
    
    # Create Flask app
    try:
        logger.info("Creating Flask application...")
        app = create_app(Config)
        
        # Enable CORS — origins sourced from the environment so each deployment
        # can lock down to its own domain.  The wildcard fallback is acceptable
        # for LAN-only edge devices but should be overridden in production.
        # Example: CORS_ALLOWED_ORIGINS="https://app.example.com,https://www.example.com"
        _cors_env = os.environ.get('CORS_ALLOWED_ORIGINS', '*')
        cors_origins = [o.strip() for o in _cors_env.split(',')] if _cors_env != '*' else '*'
        if cors_origins == '*':
            logger.warning(
                "CORS is open to all origins (CORS_ALLOWED_ORIGINS='*'). "
                "Set CORS_ALLOWED_ORIGINS to a comma-separated list of allowed origins for production."
            )
        CORS(
            app,
            resources={
                r"/api/*": {
                    "origins": cors_origins,
                    "allow_headers": [
                        "Content-Type",
                        "Authorization",
                        "X-Requested-With",
                    ],
                    "methods": ["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
                }
            },
        )

        # Initialize SocketIO
        try:
            async_mode = _select_socketio_async_mode()
        except (OSError, subprocess.SubprocessError) as exc:
            async_mode = "threading"
            logger.warning("Could not probe gevent safely; using threading mode: %s", exc)

        socketio = SocketIO(app, cors_allowed_origins=cors_origins, async_mode=async_mode)
        
        logger.info("Flask application created successfully")
    except Exception as e:
        logger.error(f"Failed to create Flask application: {e}")
        sys.exit(1)
    
    # Initialize optional services (non-blocking)
    gpio_enabled = settings.get('global_enable_gpio', False)
    led_enabled = settings.get('led_enabled', False)
    
    if gpio_enabled:
        try:
            from app.services.gpio_service import get_gpio_service
            get_gpio_service().start()
            logger.info("GPIO service started")
        except Exception as e:
            logger.warning(f"GPIO service failed: {e}")
    
    if led_enabled:
        try:
            from app.services.led_status_service import get_led_status_service
            get_led_status_service().set_busy(retry_on_failure=True)
            logger.info("LED status service initialized")
        except Exception as e:
            logger.debug(f"LED status service failed: {e}")
    
    # Initialize scanners and recorders in background
    enable_scanners = settings.get('global_enable_uniden_scanners', True)
    enable_recorders = settings.get('global_enable_edge_devices', False)
    
    if enable_scanners:
        def init_scanners():
            try:
                logger.info("Initializing scanner inventory...")
                summary = initialize_scanner_inventory()
                logger.info(f"Scanner inventory initialized: {summary}")
            except Exception as e:
                logger.warning(f"Scanner initialization failed: {e}")
        
        threading.Thread(target=init_scanners, daemon=True).start()
    
    if enable_recorders:
        def init_recorders():
            try:
                logger.info("Initializing recorder inventory...")
                summary = initialize_recorder_inventory()
                logger.info(f"Recorder inventory initialized: {summary}")
            except Exception as e:
                logger.warning(f"Recorder initialization failed: {e}")
        
        threading.Thread(target=init_recorders, daemon=True).start()
    
    # Initialize other optional services
    try:
        from app.services.s3_scheduler import start_scheduler
        start_scheduler()
        logger.info("S3 backup scheduler started")
    except Exception as e:
        logger.debug(f"S3 scheduler failed: {e}")
    
    # Start maintenance scheduler
    try:
        from app.services.maintenance_scheduler import start_scheduler as start_maintenance_scheduler
        start_maintenance_scheduler()
        logger.info("Maintenance scheduler started")
    except Exception as e:
        logger.debug(f"Maintenance scheduler failed: {e}")
    
    # Set LED online status if enabled
    if led_enabled:
        try:
            from app.services.led_status_service import get_led_status_service
            led_service = get_led_status_service()
            led_service.set_online()
            led_service.start_inactivity_monitor()
        except Exception as e:
            logger.debug(f"LED online status failed: {e}")
    
    # Start server
    host = Config.FLASK_HOST
    port = Config.FLASK_PORT
    production_mode = Config.PRODUCTION_MODE
    server_type = Config.PRODUCTION_SERVER if production_mode else 'dev'
    
    logger.info("=" * 60)
    logger.info(f"Starting server: {server_type} mode")
    logger.info(f"Host: {host}, Port: {port}")
    logger.info("=" * 60)
    
    try:
        if production_mode and server_type == 'waitress':
            logger.warning(
                "Waitress was selected as production server but it does not support WebSockets. "
                "Falling back to socketio.run() with gevent. "
                "Set PRODUCTION_SERVER=gevent to silence this warning."
            )

        # Always use SocketIO server — Waitress breaks WebSocket support.
        logger.info(f"Using SocketIO server (async_mode: {async_mode})")
        debug_mode = not production_mode
        reload_enabled = not production_mode
        if reload_enabled:
            logger.info("Auto-reload enabled: server will restart on code changes")
        socketio.run(
            app,
            host=host,
            port=port,
            debug=debug_mode,
            use_reloader=reload_enabled,
            allow_unsafe_werkzeug=True
        )
    except KeyboardInterrupt:
        logger.info("Server stopped by user")
    except Exception as e:
        logger.error(f"Server error: {e}")
        sys.exit(1)
    finally:
        # Cleanup
        logger.info("Shutting down...")
        
        # Flush database logs
        try:
            from app.services.db_logging_manager import get_db_logging_manager
            db_logging_manager = get_db_logging_manager()
            db_logging_manager.shutdown()
            logger.info("Database logs flushed")
        except Exception as e:
            logger.debug(f"Error flushing database logs: {e}")
        
        if led_enabled:
            try:
                from app.services.led_status_service import get_led_status_service
                led_service = get_led_status_service()
                led_service.stop_inactivity_monitor_thread()
                led_service.send_shutdown()
            except Exception:
                pass
        
        if gpio_enabled:
            try:
                from app.services.gpio_service import get_gpio_service
                get_gpio_service().stop()
            except Exception:
                pass


if __name__ == '__main__':
    main()
