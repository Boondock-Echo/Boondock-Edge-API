"""
API Key Manager.

Issues and validates long-lived API keys for external (machine-to-machine)
integrations. Keys are stored in the shared settings.db database.

Security model:
  - The full key is shown to the caller exactly once, at creation time.
  - Only a SHA-256 hash of the key is persisted; the plaintext is never stored.
  - A short, non-secret prefix is kept for identification in listings.
  - Keys can carry scopes, an optional expiry, and can be revoked.

Key format: ``bk_live_<43 url-safe chars>``
"""
import hashlib
import json
import logging
import secrets
import sqlite3
import threading
import uuid
from config import Config
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

KEY_PREFIX = "bk_live_"
DEFAULT_SCOPES = ["transcriptions:read"]

_db_lock = threading.RLock()


def _hash_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class APIKeyManager:
    """Singleton manager for API key storage and validation."""

    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self.db_path = Config.get_settings_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize_table()
        self._initialized = True

    def _get_connection(self):
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _initialize_table(self):
        with _db_lock:
            conn = self._get_connection()
            try:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS api_keys (
                        id TEXT PRIMARY KEY,
                        name TEXT NOT NULL,
                        key_prefix TEXT NOT NULL,
                        key_hash TEXT NOT NULL UNIQUE,
                        scopes TEXT,
                        created_at TEXT,
                        created_by TEXT,
                        expires_at TEXT,
                        last_used_at TEXT,
                        revoked INTEGER DEFAULT 0
                    )
                """)
                conn.commit()
            except Exception as e:
                logger.error(f"Error initializing api_keys table: {e}")
            finally:
                conn.close()

    # -- creation ---------------------------------------------------------
    def create_key(self, name, scopes=None, created_by=None, expires_at=None):
        """
        Create a new API key.

        Returns a tuple (metadata_dict, raw_key). The raw_key is only returned
        here and never retrievable again.
        """
        raw_key = KEY_PREFIX + secrets.token_urlsafe(32)
        key_id = str(uuid.uuid4())
        key_hash = _hash_key(raw_key)
        # First 12 chars are enough to recognise a key without exposing it.
        key_prefix = raw_key[:12]
        scopes = scopes or list(DEFAULT_SCOPES)
        created_at = _now_iso()

        with _db_lock:
            conn = self._get_connection()
            try:
                conn.execute(
                    """INSERT INTO api_keys
                       (id, name, key_prefix, key_hash, scopes, created_at,
                        created_by, expires_at, last_used_at, revoked)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, 0)""",
                    (key_id, name, key_prefix, key_hash, json.dumps(scopes),
                     created_at, created_by, expires_at),
                )
                conn.commit()
            finally:
                conn.close()

        metadata = {
            "id": key_id,
            "name": name,
            "key_prefix": key_prefix,
            "scopes": scopes,
            "created_at": created_at,
            "created_by": created_by,
            "expires_at": expires_at,
            "last_used_at": None,
            "revoked": False,
        }
        return metadata, raw_key

    # -- validation -------------------------------------------------------
    def validate_key(self, raw_key):
        """
        Validate a presented API key.

        Returns the key metadata dict if the key exists, is not revoked, and
        has not expired; otherwise returns None.
        """
        if not raw_key:
            return None
        key_hash = _hash_key(raw_key.strip())

        with _db_lock:
            conn = self._get_connection()
            try:
                row = conn.execute(
                    "SELECT * FROM api_keys WHERE key_hash = ?", (key_hash,)
                ).fetchone()
            finally:
                conn.close()

        if row is None:
            return None
        if row["revoked"]:
            return None

        expires_at = row["expires_at"]
        if expires_at:
            try:
                exp = datetime.fromisoformat(expires_at)
                if exp.tzinfo is None:
                    exp = exp.replace(tzinfo=timezone.utc)
                if datetime.now(timezone.utc) > exp:
                    return None
            except (ValueError, TypeError):
                return None

        # Best-effort "last used" tracking; never block validation on failure.
        try:
            self._touch(row["id"])
        except Exception:
            pass

        return self._row_to_meta(row)

    def _touch(self, key_id):
        with _db_lock:
            conn = self._get_connection()
            try:
                conn.execute(
                    "UPDATE api_keys SET last_used_at = ? WHERE id = ?",
                    (_now_iso(), key_id),
                )
                conn.commit()
            finally:
                conn.close()

    # -- listing / revocation --------------------------------------------
    def list_keys(self):
        with _db_lock:
            conn = self._get_connection()
            try:
                rows = conn.execute(
                    "SELECT * FROM api_keys ORDER BY created_at DESC"
                ).fetchall()
            finally:
                conn.close()
        return [self._row_to_meta(r) for r in rows]

    def get_key(self, key_id):
        with _db_lock:
            conn = self._get_connection()
            try:
                row = conn.execute(
                    "SELECT * FROM api_keys WHERE id = ?", (key_id,)
                ).fetchone()
            finally:
                conn.close()
        return self._row_to_meta(row) if row else None

    def revoke_key(self, key_id):
        """Mark a key revoked. Returns True if a key was updated."""
        with _db_lock:
            conn = self._get_connection()
            try:
                cur = conn.execute(
                    "UPDATE api_keys SET revoked = 1 WHERE id = ? AND revoked = 0",
                    (key_id,),
                )
                conn.commit()
                return cur.rowcount > 0
            finally:
                conn.close()

    @staticmethod
    def _row_to_meta(row):
        try:
            scopes = json.loads(row["scopes"]) if row["scopes"] else []
        except (ValueError, TypeError):
            scopes = []
        return {
            "id": row["id"],
            "name": row["name"],
            "key_prefix": row["key_prefix"],
            "scopes": scopes,
            "created_at": row["created_at"],
            "created_by": row["created_by"],
            "expires_at": row["expires_at"],
            "last_used_at": row["last_used_at"],
            "revoked": bool(row["revoked"]),
        }


_manager = None


def get_api_key_manager():
    global _manager
    if _manager is None:
        _manager = APIKeyManager()
    return _manager
