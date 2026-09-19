# Licensing decision

## Selected license: Apache License 2.0

The repository uses **Apache License 2.0**.

Apache-2.0 is permissive and includes an explicit patent grant/termination mechanism. That is a reasonable fit for a project that may later expose APIs, SDKs, clients, connectors, and extension points.

## Strongest simple alternative: MIT

MIT is shorter and simpler. If the project later prioritizes minimum license text/process over Apache-2.0's explicit patent terms, changing the license would require a deliberate copyright/licensing decision covering existing contributions.

## Repository handling

- `LICENSE` should contain the canonical Apache-2.0 license text rather than a project-modified copy of the license body.
- Project copyright notices may live in source-file headers/documentation when useful; they do not require modifying the Apache license text.
- A `NOTICE` file is not used merely as general licensing documentation. If future dependencies/contributions require attribution notices, add a deliberate NOTICE file containing only applicable notices.
- Third-party dependencies remain under their own licenses and required notices must be preserved.
- No Contributor License Agreement (CLA) or Developer Certificate of Origin (DCO) is required by this repository today.
- Future dual licensing/commercial exceptions require a separate explicit decision.

This is an engineering/project licensing decision record, not legal advice.
