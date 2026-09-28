# Connect a university schedule

Open **Settings → Class schedule**. botay! accepts either:

- a private HTTPS iCalendar subscription URL for automatic refresh; or
- an `.ics` file exported from the university calendar.

For HSE, use the calendar export/subscription offered by ЕЛК or HSE App X. botay! does
not request or store an HSE password. The exact current HSE live-auth flow has not been
validated without a student account, so the UI intentionally asks for the standard
calendar artifact rather than claiming a direct HSE login integration.

After connection, use **Sync now** to refresh. The status shows the last successful
refresh and a safe failure code. A failed download or malformed file does not erase the
last working schedule. URL subscriptions are also refreshed by the worker when due.

Repeated downloads do not duplicate classes. Moves, cancellations, rooms and teacher
details from the source update the SOURCE layer. A student's personal move, cancellation,
note or other USER occurrence override remains personal and survives source refresh.

Disconnecting retracts imported future classes and deletes the stored subscription URL.
It does not delete personal tasks, notes, events or personal overrides.

## Operator setup

Generate a separate 32-byte key file and make it readable by the API and reminder worker:

```bash
python -m student_execution_os credential-key-generate --output /run/secrets/academic/academic-feed.key
```

Set `SEOS_ACADEMIC_FEED_KEY_FILE` to that path. Do not reuse the LLM credential key and
do not embed this key in the Android app. Back it up separately from the SQLite backup.
The database backup contains encrypted subscription URLs; a user account export does not.

The production compose examples mount `secrets/academic` read-only into the API and
worker. Without the key, file import continues to work, existing canonical classes remain
visible, and URL setup/refresh reports `CREDENTIAL_UNREADABLE` instead of pretending that
sync succeeded.
