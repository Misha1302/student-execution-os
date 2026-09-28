# ADR 0026 — botay! Notes, Capture provenance, and original-audio ownership

Status: accepted for closed beta  
Schema: v22

## Context

The existing product already has the right mutation boundary for Tasks and Events:
client-generated operation ids, durable offline replay, explicit conflicts, and an
Assistant that proposes typed actions rather than mutating canonical state directly.
A quick capture that is not yet a Task or Event needs persistence without inventing a
second task/planning model.

## Decision

Note is a small canonical account-owned entity, not an Obligation and not planner input.
It stores user-authored content, optional machine/speech transcript, lifecycle, timestamps
and optimistic version.

Original voice audio is owned by note_audio as a SQLite BLOB. The HTTP upload is raw
binary, never a large base64 JSON mutation. Audio, transcript and user-edited content are
separate fields so transcription or later editing cannot destroy the recording.

Text Note mutations reuse client_operations; replay therefore has the same exactly-once
boundary as Task/Event mutations. A deleted Note leaves a deleted_notes tombstone so a
late offline replay cannot resurrect it.

note_links records provenance from the capture to a confirmed Task, Event or Project.
Conversion does not delete the source Note. Planner inputs remain Tasks, Events and
canonical scheduling constraints; Notes never become hidden PlanBlocks.

The Today read model separately materializes every active Event intersecting the user's
local civil day. This display projection does not change which Events block planning:
SQLitePlanningStateSource.list_events() remains the planner source of canonical fixed events.

Beta feedback is account-scoped diagnostic data. The server records the message, server
revision and a strict allowlist of technical context. User task/note/calendar/transcript
content and credentials are never attached implicitly.

## Reliability and privacy

- account export includes Notes, links, original audio and feedback;
- full SQLite backup/restore is atomic over those tables;
- account deletion cascades them with the account;
- deleting a Note cascades audio and links;
- transcription failure is a Note state, not deletion of audio;
- account ids are server-bound; Note APIs never accept an account id from the client.

## Migration and rollback

v22 only adds tables/indexes and does not rewrite existing rows.

Rollback to v21 is allowed only before v22 contains user data. Verify all five v22 tables
are empty, then drop beta_feedback, deleted_notes, note_links, note_audio, notes and delete
schema migration row 22. Once any v22 data exists, rollback requires exporting or otherwise
preserving that data first; silently dropping it is forbidden.
