# SpeechNexus contracts

Contracts in this directory are the source of truth for public and internal
producer/consumer protocols. Consumers must not
hand-maintain independent copies of defaults, units, enum domains, or stable
error codes.

## Current scope

`operations.openapi.yaml` defines the liveness and readiness protocol shared by
the speech API, execution gateway, and deployment probes. It contains no
speech business capability.

The P0 speech, realtime, workbench, scenario profile, voice resource, and
capacity manifest contracts are not yet frozen. Internal execution control,
execution streams, runtime lifecycle control, and binding/instance qualification
contracts are also pending; public and internal protocols will be organized in
separate directories. Their responsibilities are documented in the
[technical blueprint](../docs/architecture/technical-blueprint.md);
empty placeholders are deliberately
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
- Keep internal execution identities, instance generations, private endpoints,
  and runtime credentials out of public DTOs.
- Validate generated Python/TypeScript consumers against shared positive,
  negative, and event-sequence examples; schema syntax alone is insufficient.

Run `make contracts-check` after changing a contract. The validator checks
OpenAPI documents ending in `openapi.yaml` and JSON Schema files ending in
`.schema.json`.
