"""Fresh-install schema shared with the dedicated keyword action migration."""

KEYWORD_ACTION_SETTINGS_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS keyword_actions (
        id TEXT PRIMARY KEY NOT NULL,
        name TEXT NOT NULL,
        kind TEXT NOT NULL CHECK(kind IN ('email', 'popup')),
        config JSON NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS keyword_action_links (
        keyword_id INTEGER NOT NULL REFERENCES keywords(id) ON DELETE CASCADE,
        action_id TEXT NOT NULL REFERENCES keyword_actions(id) ON DELETE CASCADE,
        PRIMARY KEY(keyword_id, action_id)
    )""",
    """CREATE INDEX IF NOT EXISTS idx_keyword_action_links_action
        ON keyword_action_links(action_id)""",
)

KEYWORD_ACTION_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS keyword_action_deliveries (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        recording_id INTEGER NOT NULL REFERENCES recordings(id) ON DELETE CASCADE,
        action_id TEXT NOT NULL,
        group_id INTEGER NOT NULL,
        kind TEXT NOT NULL CHECK(kind IN ('email', 'popup')),
        payload JSON NOT NULL,
        state TEXT NOT NULL DEFAULT 'pending'
            CHECK(state IN ('pending', 'sending', 'sent', 'failed', 'cancelled')),
        attempts INTEGER NOT NULL DEFAULT 0,
        next_attempt_at INTEGER NOT NULL DEFAULT 0,
        lease_until INTEGER NOT NULL DEFAULT 0,
        last_error TEXT,
        created_at INTEGER NOT NULL,
        sent_at INTEGER,
        UNIQUE(recording_id, action_id)
    )""",
    """CREATE INDEX IF NOT EXISTS idx_keyword_deliveries_pending
        ON keyword_action_deliveries(kind, state, next_attempt_at)""",
    # Legacy recording deletion paths use raw sqlite3 connections without
    # foreign_keys enabled; clean their events up in the database as well.
    """CREATE TRIGGER IF NOT EXISTS delete_recording_keyword_actions
        AFTER DELETE ON recordings BEGIN
            DELETE FROM keyword_action_deliveries WHERE recording_id=OLD.id;
        END""",
)
