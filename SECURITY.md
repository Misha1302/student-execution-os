# Security Policy

This project is pre-release and does not yet publish supported release versions.

## Reporting

Do not open a public issue for a vulnerability that could expose authentication tokens, exact private locations, calendars, educational data, private source content, or other sensitive user information.

Until a dedicated security contact/mechanism is published, report security issues privately to the repository owner through an available private GitHub channel.

## Security-sensitive architecture

The normative specification requires:

- account-scoped data ownership and server-side authorization;
- least-privilege connector/API scopes;
- no unrestricted long-lived secrets in LLM prompts/context;
- exact physical addresses/coordinates withheld from LLMs unless concretely required and authorized;
- connectors ingest evidence instead of directly mutating canonical local facts;
- auditability of automated/external mutations;
- raw private source content retained only when a defined purpose requires it;
- optimistic concurrency and idempotent mutation handling;
- token revocation/rotation and appropriate at-rest secret protection before production.

## Prompt-injection boundary

Imported, retrieved, or connector-provided content is **untrusted evidence/data**. It may be parsed into observations but must never:

- authorize a tool call;
- widen permissions;
- override server policy;
- substitute for authenticated user intent.

Extraction contexts that read untrusted content should not have mutation tools. Action/tool contexts must be separately authorized and validated against authenticated intent, scopes, versions, and idempotency policy.
