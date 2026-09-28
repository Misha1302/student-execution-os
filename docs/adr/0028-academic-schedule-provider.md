# ADR 0028 — Academic schedule provider and iCalendar connection (schema v24)

**Status:** Accepted for R4

## Context

Schema v23 made imported timetable state provider-independent: `SourceApplier` owns
stable external identity and the SOURCE layer, while the command boundary owns the USER
layer. What was missing was a real source adapter and a student-facing connection flow.

Current official HSE pages direct students to schedules in ЕЛК / HSE App X and describe
calendar integration, while RUZ access is restricted to the university network or VPN.
No current, documented, credential-free HSE API was found during R4 validation. A live
student account or subscription URL was not available to this implementation run.
Therefore an HSE-specific scraper or an invented API contract would be unsafe evidence.

References:

- [HSE schedule access](https://www.hse.ru/eduland/schedule/)
- [HSE student schedule guide](https://www.hse.ru/studyspravka/ras_stud)
- [HSE App X](https://www.hse.ru/web/mobile/)
- [RFC 5545 iCalendar](https://www.rfc-editor.org/rfc/rfc5545)

## Decision

`AcademicScheduleProvider` is the university-neutral read boundary. It returns a
`SourceSnapshot`; it cannot access SQLite or create canonical events itself. R4 ships an
RFC 5545 iCalendar implementation and two setup paths:

1. an HTTPS subscription URL, refreshed manually or by the existing worker; and
2. a local `.ics` upload for exports that have no stable subscription URL.

The adapter normalizes `UID`, `DTSTART`, `DTEND` / `DURATION`, a canonical DAILY/WEEKLY
academic `RRULE` subset, `EXDATE`, `RECURRENCE-ID` moves/cancellations, `TZID`, all-day
events, location, teacher, `SEQUENCE`, and `LAST-MODIFIED` / `DTSTAMP`. Unsupported
recurrence rules reject the entire snapshot rather than being approximated.

The R3 identity `(source_system_id, UID, RECURRENCE-ID)` remains authoritative. Duplicate
downloads, record order, title/time/location changes and source disappearance never
create a second canonical class. A complete download may remove absent source records; a
partial provider result may not. SOURCE refresh never overwrites USER occurrence intent.

Fetch/parse happens before canonical mutation. A successful `SourceApplier` change,
connector checkpoint and connection state commit under one SQLite write transaction and
connector-version precondition. Failure records a safe code, preserves the last
canonical snapshot and retries only on a later explicit or scheduled refresh.

Subscription URLs are bearer credentials. They are AES-256-GCM encrypted using a
dedicated `SEOS_ACADEMIC_FEED_KEY[_FILE]`, bound to account and connector by associated
data, omitted from APIs/exports/logs, and removed on disconnect/account deletion. The
reminder worker receives only this dedicated key, not any LLM key. Outbound fetching is
HTTPS/443 only, does not follow redirects, revalidates public DNS on every attempt,
limits body size to 5 MiB, and uses bounded timeout/retry.

Disconnect applies an empty complete source snapshot and removes connection credentials
atomically. It does not delete unrelated personal state.

## Consequences and evidence boundary

The fixture-backed path is a real provider/parser/SourceApplier/API/browser path, not a
mocked canonical write. It proves iCalendar semantics and idempotency. It does **not**
prove that a current HSE account exposes a particular live URL or that HSE authentication
works. That remains an explicit external validation item requiring a consenting student
account or sanitized current feed.

Multi-day/multi-BYDAY and non-DAILY/WEEKLY recurrence are deliberately rejected in R4;
universities can add another `AcademicScheduleProvider` without changing canonical
recurrence logic.
