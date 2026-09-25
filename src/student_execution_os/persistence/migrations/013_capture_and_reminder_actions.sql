-- Schema v13: explicit reminder times, native notification actions and assistant
-- data retention.

-- A user-requested reminder moment ("напомни завтра вечером", Snooze 30 from a
-- notification, "not now" on Today). The engine sends one prompt when it is due
-- and no prompt went out after it; it never rewrites this column.
ALTER TABLE reminder_states ADD COLUMN remind_at TEXT;

-- What the installed client can do with a push. Devices that declare
-- "reminder-actions-v1" receive data-only messages and render notifications with
-- working action buttons themselves; older clients keep system-rendered pushes.
ALTER TABLE mobile_devices ADD COLUMN capabilities_json TEXT NOT NULL DEFAULT '[]';

-- Retention sweeps (reminders/maintenance.py) look these up by age.
CREATE INDEX IF NOT EXISTS idx_assistant_batches_expiry ON assistant_batches(expires_at);
CREATE INDEX IF NOT EXISTS idx_assistant_apply_records_created ON assistant_apply_records(created_at);
