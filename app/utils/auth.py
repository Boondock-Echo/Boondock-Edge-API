# app/utils/auth.py
import os
import secrets
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from ..services.settings_manager import get_settings_manager

log = logging.getLogger(__name__)

TOKEN_EXPIRY_HOURS = 24 * 7  # 7 days default

# Get settings manager instance
_settings_manager = get_settings_manager()

# In-memory token storage (for backward compatibility and performance)
VALID_TOKENS = {}

def load_tokens():
    """Load tokens from database into memory.

    Mutates the existing VALID_TOKENS dict *in place* rather than rebinding the
    module global. Several modules import this dict by value
    (``from ..utils.auth import VALID_TOKENS``); rebinding here would leave those
    references pointing at a stale, orphaned dict, so writes from login() would
    never be seen by get_valid_token_data()/save_tokens(). Mutating in place
    keeps every reference pointing at the same authoritative store.
    """
    try:
        loaded = _settings_manager.get_all_tokens()
        VALID_TOKENS.clear()
        VALID_TOKENS.update(loaded)
        log.debug(f"Loaded {len(VALID_TOKENS)} tokens from database")
    except Exception as e:
        log.warning(f"Error loading tokens: {e}, initializing empty tokens")
        VALID_TOKENS.clear()

def save_tokens():
    """Save tokens from memory to database."""
    try:
        # Save all tokens to database
        for token, token_data in VALID_TOKENS.items():
            _settings_manager.save_token(token, token_data)
        log.debug(f"Saved {len(VALID_TOKENS)} tokens to database")
    except Exception as e:
        log.error(f"Error saving tokens: {e}")


def delete_expired_tokens():
    """Remove expired tokens from both in-memory cache and database."""
    global VALID_TOKENS
    try:
        # Use a timezone-aware "now" so it can be compared against the
        # timezone-normalised expiry in _is_token_data_expired(). A naive
        # value raises TypeError on comparison, which was being swallowed and
        # caused every still-valid token to be treated as expired and deleted.
        now = datetime.now(timezone.utc)
        expired = [
            token for token, data in list(VALID_TOKENS.items())
            if _is_token_data_expired(data, now)
        ]
        for token in expired:
            del VALID_TOKENS[token]
        db_deleted = _settings_manager.delete_expired_tokens()
        if expired or db_deleted:
            log.debug(f"Cleaned up {len(expired)} in-memory and {db_deleted} DB expired tokens")
    except Exception as e:
        log.error(f"Error deleting expired tokens: {e}")


def _is_token_data_expired(token_data: dict, now: datetime) -> bool:
    """Return True if the token_data dict represents an expired token."""
    expires_at_str = token_data.get('expires_at')
    if not expires_at_str:
        return True
    try:
        expires_at = datetime.fromisoformat(expires_at_str)
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        return now > expires_at
    except (ValueError, TypeError):
        return True

def is_token_valid(token):
    """Check if a token exists and is not expired."""
    return get_valid_token_data(token) is not None


def get_valid_token_data(token):
    """
    Atomically retrieve token data if the token exists and has not expired.

    Returns the token data dict on success, or None if the token is missing,
    expired, or malformed.  Using a single dict lookup eliminates the TOCTOU
    window that exists when is_token_valid() and VALID_TOKENS.get() are called
    separately.
    """
    if not token:
        return None

    token_data = VALID_TOKENS.get(token)
    if token_data is None:
        return None

    expires_at_str = token_data.get('expires_at')
    if not expires_at_str:
        return None

    try:
        expires_at = datetime.fromisoformat(expires_at_str)
        # Normalise to UTC-aware for comparison
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) > expires_at:
            log.debug(f"Token {token[:8]}... expired")
            return None
        return token_data
    except (ValueError, TypeError) as e:
        log.warning(f"Invalid expiry format for token: {e}")
        return None

def is_mac_registered(mac_address):
    """Check if a MAC address is registered (has a valid token)."""
    if not mac_address:
        return False

    mac_address = mac_address.upper()
    for token, data in VALID_TOKENS.items():
        try:
            if data.get('mac_address', '').upper() == mac_address:
                if is_token_valid(token):
                    return True
        except Exception as e:
            return False
    return False

def get_token_for_mac(mac_address):
    """Get an existing valid token for a MAC address, if one exists."""
    if not mac_address:
        return None

    mac_address = mac_address.upper()
    for token, data in VALID_TOKENS.items():
        try:
            if data.get('mac_address', '').upper() == mac_address:
                if is_token_valid(token):
                    expires_at_str = data.get('expires_at')
                    return token, expires_at_str
        except Exception as e:
            return None
    return None

def generate_token(mac_address, expiry_hours=None):
    """Generate a new token for a MAC address, or return existing valid token if one exists."""
    if expiry_hours is None:
        expiry_hours = TOKEN_EXPIRY_HOURS
    
    # Check if there's already a valid token for this MAC address
    existing_token = get_token_for_mac(mac_address)
    if existing_token:
        token, expires_at_str = existing_token
        log.debug(f"Reusing existing token for MAC address {mac_address}")
        return token, expires_at_str
    
    # Generate a new token if none exists
    now_utc = datetime.now(timezone.utc)
    token = secrets.token_urlsafe(32)
    expires_at = now_utc + timedelta(hours=expiry_hours)

    token_data = {
        'mac_address': mac_address.upper(),
        'created_at': now_utc.isoformat(),
        'expires_at': expires_at.isoformat()
    }

    # Save to both memory and database
    VALID_TOKENS[token] = token_data
    _settings_manager.save_token(token, token_data)

    return token, expires_at.isoformat()

def get_mac_for_token(token):
    """Get the MAC address associated with a token."""
    if not token or token not in VALID_TOKENS:
        return None
    return VALID_TOKENS[token].get('mac_address')

# Initialize tokens on module load
load_tokens()
