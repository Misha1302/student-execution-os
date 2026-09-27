-- Schema v21: botay! notes/captures, durable original audio and privacy-safe beta feedback.
-- Notes are canonical account data. Transcript is distinct from user-edited content.
CREATE TABLE IF NOT EXISTS notes (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    content TEXT NOT NULL DEFAULT '',
    transcript TEXT,
    transcription_state TEXT NOT NULL DEFAULT 'NONE'
        CHECK (transcription_state IN ('NONE','PENDING','READY','FAILED')),
    transcription_error TEXT,
    lifecycle_status TEXT NOT NULL DEFAULT 'ACTIVE'
        CHECK (lifecycle_status IN ('ACTIVE','ARCHIVED')),
    source_kind TEXT NOT NULL DEFAULT 'CAPTURE',
    source_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    UNIQUE(account_id,id)
);
CREATE INDEX IF NOT EXISTS idx_notes_account_updated
ON notes(account_id,lifecycle_status,updated_at DESC,id);

CREATE TABLE IF NOT EXISTS note_audio (
    note_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    original_name TEXT,
    content BLOB NOT NULL,
    size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
    sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(account_id,note_id),
    FOREIGN KEY(account_id,note_id) REFERENCES notes(account_id,id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS note_links (
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    note_id TEXT NOT NULL,
    target_kind TEXT NOT NULL CHECK (target_kind IN ('TASK','EVENT','PROJECT')),
    target_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(account_id,note_id,target_kind,target_id),
    FOREIGN KEY(account_id,note_id) REFERENCES notes(account_id,id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_note_links_target
ON note_links(account_id,target_kind,target_id);

CREATE TABLE IF NOT EXISTS deleted_notes (
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    note_id TEXT NOT NULL,
    deleted_at TEXT NOT NULL,
    PRIMARY KEY(account_id,note_id)
);

CREATE TABLE IF NOT EXISTS beta_feedback (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    message TEXT NOT NULL,
    technical_context_json TEXT NOT NULL DEFAULT '{}',
    server_revision INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_beta_feedback_account_created
ON beta_feedback(account_id,created_at DESC);
