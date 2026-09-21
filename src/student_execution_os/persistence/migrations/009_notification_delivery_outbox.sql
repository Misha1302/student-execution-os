CREATE TABLE IF NOT EXISTS notification_delivery_outbox (
    notification_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    delivery_key TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('READY','LEASED','RETRY_WAIT','SENT','SUPPRESSED','DEAD')),
    lease_owner TEXT,
    lease_expires_at TEXT,
    next_attempt_at TEXT NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    provider_message_id TEXT,
    last_error TEXT,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(account_id, notification_id),
    UNIQUE(account_id, delivery_key),
    FOREIGN KEY(account_id, notification_id) REFERENCES notifications(account_id, id) ON DELETE CASCADE,
    CHECK (
      (state='LEASED' AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)
      OR (state!='LEASED' AND lease_owner IS NULL AND lease_expires_at IS NULL)
    )
);
CREATE INDEX IF NOT EXISTS idx_notification_delivery_due
ON notification_delivery_outbox(account_id, state, next_attempt_at, notification_id);
