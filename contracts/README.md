# SpeechNexus contracts

Contracts in this directory are the source of truth for public and internal
producer/consumer protocols. Consumers must not
hand-maintain independent copies of defaults, units, enum domains, or stable
error codes.

## Current scope

- `operations.openapi.yaml`: Liveness and readiness protocol shared by the
  speech API, execution gateway, and deployment probes. It contains no speech
  business capability.
- `capacity-manifest.schema.json`: P0-A hardware-neutral capacity, execution
  stage graph, loading peak, resident/incremental resource vector,
  cancellation/fencing capability, and qualification manifest bound to an
  immutable `ModelBindingRevision`.
- `internal/execution-control.openapi.yaml`: P0-A internal
  `ExecutionControlPort` protocol between the scheduler / workers / realtime
  gateway and `execution-gateway` (fenced two-phase submit intent barrier,
  execution status & output watermark query, verified abort vs quarantine,
  gateway takeover fencing barrier, and instance drain).
- `internal/runtime-control.openapi.yaml`: P0-A internal
  `RuntimeLifecycleControlPort` protocol between the scheduler and the
  node-scoped deployment controller (`node-controller`) for fenced prepare/stop
  intents and verified container/process stop evidence.

The remaining P0 public speech, realtime, workbench, scenario profile, and
voice resource contracts, as well as internal execution stream and
binding/instance qualification schemas, are still pending. Their
responsibilities are documented in the
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
