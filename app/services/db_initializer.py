"""
Database initialization utility for settings.db.
Creates the database with default values if it doesn't exist.
"""

import json
import logging
import threading
from config import DATA_ROOT
from datetime import datetime
from .settings_manager import SettingsManager

logger = logging.getLogger(__name__)

# Module-level flag to track initialization status
_initialization_checked = False
_initialization_lock = threading.Lock()

# Default settings rows that must always exist.
DEFAULT_SETTINGS = {
    "event_name": "default",
    "global_model": "tiny.en",
    "global_target_language": "english",
    "global_transcribe_local": True,
    "global_transcribe_openai": False,
    "global_transcribe_node": False,
    "global_hallucination": False,
    "global_enable_uniden_scanners": False,
    "global_enable_edge_devices": True,
    "transcription_endpoint": "https://api.boondock.cloud",
    "api_health_url": "https://api.boondock.cloud/health",
    "api_transcription_url": "https://api.boondock.cloud/transcribe/",
    "global_timezone": "UTC",
    "keywords": ["emergency", "fire", "police", "medical", "police", "medical", "police", "medical"],
    "global_min_record_secs": 1,
    "global_max_record_secs": 30,
    "global_silence_secs": 1,
    "global_pre_record_ms": 200,
    "global_post_record_ms": 1000,
    "global_rms_threshold": 30,
    "global_discard_secs": 1,
    "global_enable_s3_upload": False,
    "s3_endpoint_url": "",
    "s3_access_key": "",
    "s3_secret_key": "",
    "s3_region": "",
    "s3_bucket_name": "",
    "s3_backup_time": "03:00",
    "host_ssid": "boondockedge",
    "host_password": "edge@123",
    "host_ip": "10.42.0.1",
    "host_port": "4000",
    "led_enabled": False,
    "led_gpio_pin": 24,
    "button_enabled": False,
    "button_gpio_pin": None,
    "button_long_press_duration": 3.0,
    "relay_enabled": False,
    "relay_gpio_pin": None,
    "usb_power_enabled": False,
    "samba_backup_enabled": False,
    "samba_share_path": "",
    "samba_username": "",
    "samba_password": "",
    "global_inbox_view_mode": "pagination",
    "global_inbox_records_per_page": 20,
    "global_enable_usb_audio_devices": False,
    "global_show_duplicate_files": False,
    "global_enable_gpio": False,
    "global_live_mode_enabled": False,
    "maintenance_time": "03:00",
    "maintenance_enabled_tasks": ["data_backup", "logs_cleanup", "disk_usage_calculation"],
}


def _ensure_default_settings_rows(settings_manager, existing_settings=None):
    """
    Ensure all required settings rows exist.
    Missing keys are inserted; existing values are never overwritten.
    """
    if existing_settings is None:
        existing_settings = settings_manager.get_all_settings() or {}

    inserted = 0
    for key, default_value in DEFAULT_SETTINGS.items():
        if key not in existing_settings or existing_settings[key] is None:
            settings_manager.set_setting(key, default_value)
            inserted += 1
            logger.info(f"Initialized missing setting row: {key} = {default_value}")
    return inserted


def initialize_settings_database():
    """
    Initialize settings.db with default values.
    This should be called at application startup.
    Idempotent - safe to call multiple times.
    """
    global _initialization_checked
    
    # Quick check without lock for performance
    if _initialization_checked:
        return True
    
    # Thread-safe check and initialization
    with _initialization_lock:
        # Double-check pattern
        if _initialization_checked:
            return True
        
        settings_manager = SettingsManager()
        
        # Check if database already has data BEFORE logging
        try:
            existing_settings = settings_manager.get_all_settings()
            if existing_settings and len(existing_settings) > 0:
                # Ensure all required settings rows exist for upgraded installations.
                _ensure_default_settings_rows(settings_manager, existing_settings)
                _initialization_checked = True
                logger.debug("Database already initialized with data. Skipping defaults.")
                return True
        except Exception as e:
            logger.warning(f"Error checking database initialization status: {e}")
            # Continue with initialization if check fails
        
        # Only log if we're actually initializing
        logger.info("=" * 60)
        logger.info("Initializing Settings Database")
        logger.info("=" * 60)
    
    # Default profiles
    DEFAULT_PROFILES = {
        "Default": {
            "name": "Default",
            "description": "Default member profile",
            "isDefault": True,
            "features": {
                "access_settings": False,
                "inbox": True,
                "create_reports": False,
                "view_reports": True,
                "modify_reports": False,
                "play_audio": True,
                "delete_audio": False,
                "access_advanced_player": False
            }
        },
        "Admin": {
            "name": "Admin",
            "description": "Full administrator access",
            "isDefault": True,
            "features": {
                "access_settings": True,
                "inbox": True,
                "create_reports": True,
                "view_reports": True,
                "modify_reports": True,
                "play_audio": True,
                "delete_audio": True,
                "access_advanced_player": True
            }
        }
    }

    # Default branding
    DEFAULT_BRANDING = {
        "organization_name": "Boondock Edge",
        "tagline": "Emergency Services Communication",
        "brand_colors": {
            "primary": "#2563eb",
            "secondary": "#4f46e5",
            "accent": "#ec4899"
        },
        "font": "inter",
        "assets": {
            "logo": None,
            "favicon": None,
            "loader": None
        }
    }
    
    # Default GPIO config
    DEFAULT_GPIO_CONFIG = {
        "led": {
            "gpio": 13,
            "enabled": True,
            "mode": "source"
        },
        "relays": {
            "relay1": {
                "gpio": 19,
                "state": False,
                "normal_state": "on"
            },
            "relay2": {
                "gpio": 21,
                "state": False,
                "normal_state": "on"
            }
        }
    }
    
    # Default firmware metadata
    FIRMWARE_STORAGE_DIR = DATA_ROOT / 'firmware'
    DEFAULT_FIRMWARE_ID = "default"
    DEFAULT_FIRMWARE_DIR = FIRMWARE_STORAGE_DIR / DEFAULT_FIRMWARE_ID
    DEFAULT_FIRMWARE = {
        DEFAULT_FIRMWARE_ID: {
            "name": "Default Firmware",
            "version": "1.0.0",
            "description": "Default firmware included with the release",
            "folder": DEFAULT_FIRMWARE_ID,
            "created_at": datetime.utcnow().isoformat() + 'Z'
        }
    }
    
    try:
        # Initialize default data - setup wizard is disabled, so always populate defaults
        
        # Initialize settings
        logger.info("Setting default settings...")
        settings_manager.set_all_settings(DEFAULT_SETTINGS)
        # Double-check and insert any missing rows individually (future-safe on migrations)
        _ensure_default_settings_rows(settings_manager)
        
        # Initialize profiles
        logger.info("Setting default profiles...")
        for name, profile_data in DEFAULT_PROFILES.items():
            settings_manager.save_profile(name, profile_data)
        
        # Initialize branding
        logger.info("Setting default branding...")
        existing_branding = settings_manager.get_branding()
        if not existing_branding:
            settings_manager.save_branding(DEFAULT_BRANDING)
        
        # Initialize GPIO config
        logger.info("Setting default GPIO config...")
        existing_gpio = settings_manager.get_gpio_config()
        if not existing_gpio:
            settings_manager.save_gpio_config(DEFAULT_GPIO_CONFIG)
        
        # Initialize default firmware metadata (JSON file)
        logger.info("Setting default firmware metadata...")
        firmware_json_path = FIRMWARE_STORAGE_DIR / 'firmware.json'
        
        # Load existing firmware metadata from JSON file
        existing_firmware = {}
        if firmware_json_path.exists():
            try:
                with open(firmware_json_path, 'r', encoding='utf-8') as f:
                    existing_firmware = json.load(f)
            except Exception as e:
                logger.warning(f"Failed to load existing firmware.json: {e}")
        
        # Only add default firmware if it doesn't exist and the files are present
        if DEFAULT_FIRMWARE_ID not in existing_firmware:
            # Check if default firmware files exist
            bootloader_path = DEFAULT_FIRMWARE_DIR / 'bootloader.bin'
            partitions_path = DEFAULT_FIRMWARE_DIR / 'partitions.bin'
            firmware_path = DEFAULT_FIRMWARE_DIR / 'firmware.bin'
            
            if all(p.exists() for p in [bootloader_path, partitions_path, firmware_path]):
                # Merge with existing firmware metadata (don't overwrite other firmware entries)
                merged_firmware = existing_firmware.copy()
                merged_firmware.update(DEFAULT_FIRMWARE)
                
                # Ensure firmware directory exists
                FIRMWARE_STORAGE_DIR.mkdir(parents=True, exist_ok=True)
                
                # Save to JSON file
                try:
                    with open(firmware_json_path, 'w', encoding='utf-8') as f:
                        json.dump(merged_firmware, f, indent=2, ensure_ascii=False)
                    logger.info("Default firmware metadata added to firmware.json (files found).")
                except Exception as e:
                    logger.error(f"Failed to save firmware.json: {e}")
            else:
                logger.warning("Default firmware files not found. Skipping default firmware entry.")
                logger.debug(f"Expected files: {bootloader_path}, {partitions_path}, {firmware_path}")
        else:
            logger.info("Default firmware already exists in firmware.json.")
        
        logger.info("=" * 60)
        logger.info("✓ Database initialization complete!")
        logger.info("=" * 60)
        
        # Mark as initialized only on success
        _initialization_checked = True
        return True
        
    except Exception as e:
        logger.error(f"\n✗ Database initialization FAILED: {e}")
        raise


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s'
    )
    initialize_settings_database()

