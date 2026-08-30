# app/utils/profile_utils.py
import os
import logging
from ..services.settings_manager import get_settings_manager

log = logging.getLogger(__name__)

# Get settings manager instance
_settings_manager = get_settings_manager()

def init_profiles():
    """Initialize profiles in database if they don't exist."""
    try:
        profiles = _settings_manager.get_all_profiles()
        if not profiles:
            # Initialize with defaults
            from ..services.db_initializer import initialize_settings_database
            initialize_settings_database()
            profiles = _settings_manager.get_all_profiles()
        return profiles
    except Exception as e:
        log.error(f"Error initializing profiles: {e}")
        raise

def load_profiles():
    """Load profiles from database."""
    try:
        return _settings_manager.get_all_profiles()
    except Exception as e:
        log.error(f"Error loading profiles: {e}")
        return {}

def save_profiles(profiles):
    """Save profiles to database."""
    try:
        for name, profile_data in profiles.items():
            _settings_manager.save_profile(name, profile_data)
    except Exception as e:
        log.error(f"Error saving profiles: {e}")
        raise

def get_profile(profile_name):
    """Get a specific profile by name."""
    profiles = load_profiles()
    return profiles.get(profile_name)

def get_user_profile(user_email, users_data=None):
    """Get the profile for a user. Returns Default profile if not assigned."""
    if users_data is None:
        try:
            users_data = _settings_manager.get_all_users()
        except Exception as e:
            log.error(f"Error loading users data: {e}")
            return get_profile("Default")
    
    user = users_data.get(user_email, {})
    profile_name = user.get('profile', 'Default')
    profile = get_profile(profile_name)
    
    # Fallback to Default if profile not found
    if not profile:
        log.warning(f"Profile '{profile_name}' not found for user {user_email}, using Default")
        profile = get_profile("Default")
    
    return profile or get_profile("Default")

def has_permission(user_email, feature, users_data=None):
    """Check if a user has permission for a specific feature."""
    profile = get_user_profile(user_email, users_data)
    if not profile:
        return False
    
    # Admin role always has all permissions
    try:
        if users_data is None:
            users_data = _settings_manager.get_all_users()
        user = users_data.get(user_email, {})
        if user.get('role') == 'admin':
            return True
    except:
        pass
    
    features = profile.get('features', {})
    return features.get(feature, False)

def get_all_features():
    """Get list of all available features."""
    return [
        {
            "key": "access_settings",
            "label": "Access Settings",
            "description": "Access to all settings menu items"
        },
        {
            "key": "inbox",
            "label": "Inbox",
            "description": "Access to inbox/messages"
        },
        {
            "key": "create_reports",
            "label": "Create Reports",
            "description": "Ability to create new reports"
        },
        {
            "key": "view_reports",
            "label": "View Reports",
            "description": "Ability to view reports"
        },
        {
            "key": "modify_reports",
            "label": "Modify Reports",
            "description": "Ability to modify existing reports"
        },
        {
            "key": "play_audio",
            "label": "Play Audio",
            "description": "Ability to play audio recordings"
        },
        {
            "key": "delete_audio",
            "label": "Delete Audio",
            "description": "Ability to delete audio recordings"
        },
        {
            "key": "access_advanced_player",
            "label": "Access Advanced Player",
            "description": "Access to advanced audio player features"
        }
    ]

