# app/utils/password_utils.py
from __future__ import annotations

import bcrypt
import logging
from typing import Optional

log = logging.getLogger(__name__)

def hash_password(password: str) -> str:
    """Hash a password using bcrypt with automatic salt generation."""
    if not password:
        raise ValueError("Password cannot be empty")
    
    # Generate salt and hash password (bcrypt handles salt automatically)
    salt = bcrypt.gensalt(rounds=12)  # 12 rounds = good balance of security and performance
    hashed = bcrypt.hashpw(password.encode('utf-8'), salt)
    return hashed.decode('utf-8')

def verify_password(password: str, hashed_password: str) -> bool:
    """Verify a password against a bcrypt hash."""
    if not password or not hashed_password:
        return False
    
    try:
        # bcrypt automatically extracts salt from the hash
        return bcrypt.checkpw(password.encode('utf-8'), hashed_password.encode('utf-8'))
    except Exception as e:
        log.error(f"Error verifying password: {e}")
        return False

def is_md5_hash(value: str) -> bool:
    """Check if a string is an MD5 hash (for migration purposes)."""
    if not value or len(value) != 32:
        return False
    return all(c in '0123456789abcdef' for c in value.lower())


def verify_and_migrate(password: str, stored_hash: str) -> tuple[bool, Optional[str]]:
    """Verify a password and return (is_valid, new_bcrypt_hash_or_None).

    If the stored hash is an MD5 hash (legacy) and the password matches,
    returns (True, new_bcrypt_hash) so the caller can persist the upgraded hash.
    If the hash is already bcrypt, returns (is_valid, None) — no migration needed.
    """
    if not password or not stored_hash:
        return False, None

    if is_md5_hash(stored_hash):
        import hashlib
        md5_digest = hashlib.md5(password.encode('utf-8')).hexdigest()  # nosec
        if md5_digest == stored_hash:
            # Password matches the legacy MD5 — re-hash with bcrypt for next login
            new_hash = hash_password(password)
            log.info("MD5 hash migrated to bcrypt for login")
            return True, new_hash
        return False, None

    # Standard bcrypt verification
    return verify_password(password, stored_hash), None

