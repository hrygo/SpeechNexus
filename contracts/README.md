# SpeechNexus contracts

Contracts in this directory are the public source of truth. Consumers must not
hand-maintain independent copies of defaults, units, enum domains, or stable
error codes.

## Current scope

`operations.openapi.yaml` defines the liveness and readiness protocol shared by
the speech API, execution gateway, and deployment probes. It contains no
speech business capability.

The P0 speech, realtime, workbench, scenario profile, voice resource, and
capacity manifest contracts are not yet frozen. Their names and responsibilities
are documented in the implementation plan; empty placeholders are deliberately
not created because an empty file could be mistaken for a delivered contract.

## Rules

- Freeze producer and consumer schemas before implementing business routes.
- Use snake_case fields and reject unknown request fields.
- Keep OpenAI-compatible fields limited to the explicitly declared subset.
- Put SpeechNexus extensions under their documented `nexus` namespaces.
- Generate or validate language bindings from this directory; do not make a
  Python model or TypeScript interface an independent authority.
- Add positive and negative examples with every state or error code change.
- Record incompatible changes in an ADR and update consumers with the contract.

Run `make contracts-check` after changing a contract. The validator checks
OpenAPI documents ending in `openapi.yaml` and JSON Schema files ending in
`.schema.json`.
