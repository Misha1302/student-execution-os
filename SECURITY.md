# Security Policy

This project is pre-release and does not yet publish supported release versions.

## Reporting

Please do not open a public issue for a vulnerability that could expose authentication tokens, exact private locations, calendars, academic data, or other sensitive user information.

Until a dedicated security contact is published, report security issues privately to the repository owner through GitHub.

## Security-sensitive design areas

The architecture treats the following as security/privacy boundaries:

- scoped authentication and authorization for API clients and connectors;
- no unrestricted long-lived secret embedded in LLM prompts;
- exact physical addresses should not be exposed to LLMs unless necessary;
- connectors must not write directly to canonical storage;
- auditability of external and automated mutations;
- least-privilege connector permissions;
- private source content should not be retained unless needed.
