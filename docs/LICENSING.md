# Licensing decision

## Selected license: Apache License 2.0

The repository uses **Apache License 2.0**.

### Why Apache-2.0 fits this project

The project is expected to contain a server, public API, LLM/tool adapters, connectors, routing providers, notification integrations, and potentially SDKs. Apache-2.0 is permissive like MIT, but additionally provides:

- an explicit copyright license;
- an explicit patent grant from contributors;
- patent-termination protection;
- defined handling of NOTICE attribution;
- clear rules for modified files and redistribution.

Those properties are useful for infrastructure and ecosystem-oriented software where third parties may implement connectors, clients, or extensions.

## Strongest simple alternative: MIT

MIT is shorter and easier to read. It is an excellent choice when maximum simplicity is more important than explicit patent language.

For this project, the simplicity advantage does not outweigh Apache-2.0's explicit patent terms, so Apache-2.0 is the default.

## What this decision does not imply

- No Contributor License Agreement (CLA) is required by this repository today.
- No additional source-available or non-commercial restriction is added.
- No trademark grant is implied.
- Third-party dependencies remain under their own licenses.
- If the project later becomes dual-licensed or adopts a commercial exception model, that requires a separate explicit decision.

This document is an engineering/project decision record, not legal advice.
