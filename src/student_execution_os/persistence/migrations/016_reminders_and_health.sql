-- Schema v16: standalone reminders and wake alarms, notification health, and the
-- AI connection test's failure states.
BEGIN;

-- The connection test now tells apart what failed: key, address, model, the JSON
-- contract capture needs, quota, or the provider itself. SQLite cannot widen a
-- CHECK constraint in place, so the table is rebuilt with identical columns.
CREATE TABLE llm_credentials_v16 (
    account_id TEXT PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,
    provider TEXT NOT NULL CHECK (provider IN ('openai', 'anthropic', 'openai-compatible')),
    model TEXT NOT NULL CHECK (length(model) BETWEEN 1 AND 200),
    base_url TEXT,
    key_ciphertext BLOB NOT NULL,
    key_nonce BLOB NOT NULL,
    key_id TEXT NOT NULL,
    key_hint TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'UNTESTED'
        CHECK (status IN ('UNTESTED', 'OK', 'INVALID_KEY', 'MODEL_NOT_FOUND', 'ENDPOINT_NOT_FOUND',
                          'RATE_LIMITED', 'QUOTA_EXCEEDED', 'UNSUPPORTED_FORMAT', 'MALFORMED_RESPONSE',
                          'REJECTED', 'UNREACHABLE', 'PROVIDER_ERROR', 'BLOCKED_URL', 'UNREADABLE')),
    last_checked_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1
);
INSERT INTO llm_credentials_v16(account_id,provider,model,base_url,key_ciphertext,key_nonce,key_id,key_hint,status,
                                last_checked_at,created_at,updated_at,version)
    SELECT account_id,provider,model,base_url,key_ciphertext,key_nonce,key_id,key_hint,status,
           last_checked_at,created_at,updated_at,version FROM llm_credentials;
DROP TABLE llm_credentials;
ALTER TABLE llm_credentials_v16 RENAME TO llm_credentials;

-- "Напомни купить хлеб завтра в 18", "разбуди меня в 7". A reminder is neither a
-- Task (nothing to plan or estimate) nor an Event (no interval): it is a moment and
-- a way to get the user's attention. It may point at a task or event it is about.
--   delivery   PUSH           an ordinary notification
--              ALARM          a ringing alarm on the Android device (no notification)
--              PUSH_AND_ALARM both
--   wake_check after "Я встал", ask again ~25 min later and ring again without an answer
--   raise_volume  the alarm may raise the alarm volume (restored afterwards)
CREATE TABLE IF NOT EXISTS reminders (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    title TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 300),
    note TEXT CHECK (note IS NULL OR length(note) <= 2000),
    remind_at TEXT NOT NULL,
    delivery TEXT NOT NULL DEFAULT 'PUSH' CHECK (delivery IN ('PUSH', 'ALARM', 'PUSH_AND_ALARM')),
    wake_check INTEGER NOT NULL DEFAULT 0 CHECK (wake_check IN (0, 1)),
    raise_volume INTEGER NOT NULL DEFAULT 0 CHECK (raise_volume IN (0, 1)),
    obligation_id TEXT,
    status TEXT NOT NULL DEFAULT 'SCHEDULED' CHECK (status IN ('SCHEDULED', 'FIRED', 'DONE', 'CANCELLED')),
    fired_at TEXT,
    acknowledged_at TEXT,
    awake_confirmed_at TEXT,
    completed_at TEXT,
    snooze_count INTEGER NOT NULL DEFAULT 0 CHECK (snooze_count >= 0),
    actor_category TEXT NOT NULL DEFAULT 'USER_UI',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    UNIQUE (account_id, id),
    FOREIGN KEY (account_id, obligation_id) REFERENCES obligations(account_id, id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_reminders_due ON reminders(status, remind_at);
CREATE INDEX IF NOT EXISTS idx_reminders_account ON reminders(account_id, remind_at);

-- Same role as deleted_obligations: a late offline replay for a deleted reminder
-- is a harmless NOOP and cannot bring it back.
CREATE TABLE IF NOT EXISTS deleted_reminders (
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    reminder_id TEXT NOT NULL,
    deleted_at TEXT NOT NULL,
    PRIMARY KEY (account_id, reminder_id)
);

-- A message about a standalone reminder names it; delivery says whether the device
-- rings an alarm, shows a notification, or both.
ALTER TABLE reminder_messages ADD COLUMN reminder_id TEXT;
ALTER TABLE reminder_messages ADD COLUMN delivery TEXT NOT NULL DEFAULT 'PUSH'
    CHECK (delivery IN ('PUSH', 'ALARM', 'PUSH_AND_ALARM'));

-- What each device last reported about itself (system notifications allowed, exact
-- alarms, full-screen alarms), so no screen promises a reminder that cannot arrive.
ALTER TABLE mobile_devices ADD COLUMN status_json TEXT NOT NULL DEFAULT '{}';
ALTER TABLE mobile_devices ADD COLUMN last_seen_at TEXT;

COMMIT;
