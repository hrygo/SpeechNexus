# SpeechNexus contracts

Contracts in this directory are the source of truth for public and internal
producer/consumer protocols. Consumers must not hand-maintain independent copies
of defaults, units, enum domains, or stable error codes.

## Current scope

- `operations.openapi.yaml`: liveness and readiness protocol shared by the
  speech API, execution gateway, and deployment probes.
- `speech-openapi.yaml`: the public OpenAI-compatible Audio subset
  (`/v1/models`, `/v1/audio/transcriptions`, `/v1/audio/speech`), the
  transcription-only realtime compatibility mode, and the `nexus` speech
  execution surface: capabilities, profiles, jobs, operations, assets,
  artifacts, realtime sessions, voices, pronunciation sets, and voice
  design / clone / preview / validation / publication. A successful durable
  Job carries a kind-discriminated structured result tied to its committed
  artifacts and receipt; non-success states carry no result. Its
  `job.succeeded` event carries the same result shape and at least one
  committed artifact. Result artifact identifiers must resolve to committed
  entries of the same kind with the result receipt; committed artifacts have
  non-empty content. Realtime session projections require connection,
  terminal-time, structured failure, and cleanup facts to agree with the
  session status. A session's terminal operation references are unique and
  resolve to Operations owned by that session with matching terminal facts.
  Ready assets carry non-empty validated media facts; audio
  assets expose duration, sample rate, and channel count, while text assets
  cannot claim audio metadata.
- `workbench-openapi.yaml`: optional workbench application resources: projects,
  transcripts, meetings, assistant turns, teleprompter state, audiobooks,
  saved assets, export jobs, and export events. It does not claim OpenAI
  compatibility and does not write speech-base Job or Artifact state. A
  workbench export artifact pins the same immutable manifest revision as its
  owning export job and carries non-empty content. Export success / publication
  events must reference an artifact owned by the event's export job. Export
  progress cannot exceed its total; a succeeded export reports completed equal
  to total and the manifest-pinned artifact. Non-succeeded jobs never expose an
  artifact. Speech Artifact imports for transcripts, saved assets, and render
  units pin both source Artifact and receipt; a saved asset always retains the
  receipt, a ready render unit requires both source identities, and an explicit
  export unit subset cannot be empty.
  A failed meeting records a structured capture error and cannot claim a seal,
  minutes reference, or successful minutes transition; a failed MinutesJob
  remains visible through its meeting's `minutes_pending` state instead of
  reclassifying the whole meeting as a capture failure. A non-succeeded
  MinutesJob may expose explicitly partial text but never a publishable
  `artifact_id`.
- `admin-openapi.yaml`: deployment-scoped workspaces, model-binding
  and scenario-profile revision publication / activation / retirement,
  observed model instances, resource pools, policies,
  quarantine release, diagnostics, and anonymous / shared-credential audit
  events. Audit change summaries contain only unique RFC 6901 field pointers and
  an optional reason, never before/after values or request payloads. A
  binding-family summary distinguishes its latest immutable revision from the
  nullable current publication pointer, so a new draft remains visible before
  it is published. Model-binding publication uses separate target-revision and
  expected-current-revision compare-and-swap fields.
  Quarantine release persists and echoes the operator-observed evidence time,
  reason, and idempotent replay status. A ready resource pool has at least one
  contributing node and positive capacity; reserved slots do not exceed
  capacity, and active plus quarantined slots do not exceed capacity.
  Released submit-intent ids are sorted.
- Patch requests that require an expected revision must also carry at least one
  changed field. A compare-and-swap precondition alone cannot create a new
  metadata revision without a change.
- `shared/`: single-source definitions for opaque identifiers and digests
  (`identifiers.schema.json`), runtime and Operation states, the
  OpenAI-compatible public error envelope, revisioned references, language
  tags, capability and execution domains, public PCM / usage / realtime
  limits, retire/disable commands, and the redacted execution receipt shared by
  REST Operation and `nexus.operation.done` projections. ASR and TTS override
  fields are also shared, including audio / transcript formats, VAD,
  finalization, auxiliary, text-commit, audio-delivery, and operation-kind
  domains; transport-specific schemas may narrow a range but do not redefine
  the same option. The public error envelope accepts the official 503
  `service_unavailable_error` type in addition to the declared 4xx families.
  Public, admin, and internal contracts reference these definitions instead of
  copying domains.
- `realtime-events.schema.json`: the public WebSocket event union. The
  standard transcription subset keeps OpenAI event names and shapes; TTS,
  hypotheses, metadata, credits, gaps, and operation terminals use `nexus.*`.
- `scenario-profile.schema.json`: immutable profile revision, typed parameter
  domains, override allowlist and ranges, required capabilities, validation
  evidence, publication lifecycle, and execution-class constraints. Draft
  revisions omit validation evidence, are not publicly listed, and become
  immutable published revisions only through the admin publication endpoint.
  Rollback explicitly activates an already-published revision without rewriting
  it. Producer validation rejects a
  duplicate capability name, an empty/reversed capability constraint, a
  reversed override range, an override range for a non-allowlisted path, and
  `max_segment_ms < preview_interval_ms`.
- `voice-resource.schema.json`: voice draft, draft revision, validation,
  publication, immutable VoiceRevision, and pronunciation revision. Draft
  revisions bind generation, validation, and publication references to their
  lifecycle status, and failure details occur only in the failed state.
  Publication eligibility requires a passed required `reusable_tts` check;
  check names are unique per validation. A publication echoes the succeeded
  validation Job, and producer validation must atomically match its draft
  revision, validation, and published VoiceRevision binding. The immutable
  VoiceRevision source requires draft identity for designed / cloned voices,
  a reference-asset digest for clones, and no draft identity for built-ins.
  Public projections never contain backend handles, feature vectors, object
  keys, or paths.
- `model-binding.schema.json`: immutable logical model binding to engine,
  artifact set, execution recipe, parameter domains, capacity manifest, and
  qualification evidence, including a globally unique immutable `revision_id`
  used by capacity, qualification, instance, lease, intent, and receipt
  identities. It is separate from runtime instances.
- `model-instance.schema.json`: internal runtime instance identity, generation,
  configuration digest, readiness, observed facts, stop evidence, and
  quarantine state. `quarantine_reason` is present only for a quarantined
  instance and cannot be whitespace-only.
- `qualification-report.schema.json`: per-binding, per-topology qualification
  evidence, including the exact image and runtime identity for every execution
  stage. A production release tier requires all required scenarios to pass.
  `required_scenarios_passed` and `overall_status` are computed from required
  scenario results; producer validation additionally enforces one-to-one
  `test_suite.scenario_ids` coverage without duplicates, a recorded limitation
  for every non-passing optional scenario, expiry after qualification,
  matching binding / manifest / release identities, and one engine component
  per manifest stage with the declared engine kind.
- `capacity-manifest.schema.json`: hardware-neutral capacity, execution stage
  graph, loading peak, resource vector, cancellation/fencing capability, and
  qualification manifest bound to an immutable `ModelBindingRevision`. Every
  stage declares whether it emits PCM audio; an audio-emitting stage requires
  at least one declared audio output format, and execution submission carries
  the metering sample rate with the output-duration ceiling.
- `internal/execution-control.openapi.yaml`: internal
  `ExecutionControlPort` protocol between the scheduler / workers / realtime
  gateway and `execution-gateway` (fenced two-phase submit intent barrier,
  execution status and output watermark query, verified abort vs quarantine,
  gateway takeover fencing barrier, and instance drain). It requires a
  per-component bearer credential; this credential is not the public shared
  service key and public routes must reject it.
- `internal/execution-stream.schema.json`: realtime gateway to execution
  gateway control envelopes, input/output credits, backpressure, watermarks,
  terminal evidence, and the fixed binary PCM frame. `stream.opened` echoes the
  accepted submit intent, fencing token, and negotiated message limit; PCM
  descriptors accept 16, 24, and 48 kHz. Structured `stream.error` envelopes
  carry stable error codes and stream identity without becoming a terminal
  Operation result. A succeeded `stream.closed` requires a non-null receipt.
- `internal/runtime-control.openapi.yaml`: internal
  `RuntimeLifecycleControlPort` protocol between the scheduler and the
  node-scoped deployment controller (`node-controller`). Its per-node component
  credential is independent from scheduler, gateway, and public credentials.
  `(node_id, intent_id)` is a node-scoped idempotency key, so the same
  caller-generated `intent_id` can be reused independently on another node;
  replays echo the immutable intent identity and a changed request under the
  same node/key is a stable `409 intent_conflict`. A failed intent requires a
  non-empty `error_code`; a succeeded intent carries no error. Runtime memory
  reservations require peak to be at least steady in every architecture-specific
  counter. A reconciliation snapshot may report each
  `(instance_id, instance_generation)` once, and stop evidence is present only
  for a succeeded stop intent / stopped or absent observation.

MCP tools are a stateless projection of the public speech REST resources. They
must reuse the same canonical commands, idempotency summaries, and revision
pins; no separate MCP-only protocol semantics are introduced here.

The public speech, realtime, workbench, scenario profile, voice resource, and
internal binding / instance / qualification contracts are now present. Their
business routes and runtime behavior are **not** implemented yet. A contract
file proves a frozen protocol shape, not a running capability.

Durable Job / export SSE events use a strict payload variant per event type.
Unknown event data, mismatched type/status combinations, and unstructured
terminal errors are rejected; an extension must add an explicit versioned
variant rather than putting arbitrary keys in `data`.

An Operation result is terminal (`succeeded` / `failed` / `cancelled`) and is
reported separately from `cleanup_state`. In particular, cancellation with
unverified cleanup is `failed` plus `cleanup_state=quarantined`; `quarantined`
is not an Operation result status. A succeeded Operation requires a non-null
execution receipt on both the REST projection and `nexus.operation.done`. A
succeeded Job, Operation, and realtime terminal-operation projection require
`cleanup_state=not_required` or `complete`; `pending` and `quarantined` are
reserved for non-success states.

Verified terminal execution, controller termination, and independently
verified operator termination are cleanup evidence. They can close quarantined
submit intents only after a successful stop is established; a disconnect,
heartbeat loss, or record deletion is not evidence.

A failed submit intent may use `cleanup_state=not_required` only when
`engine_request_id` is explicitly null, no dispatch occurred, and no output or
staging fact exists. Dispatched executions must provide verified termination
evidence before capacity is released; a failed response without an error code
is invalid. A submit intent can be inserted only while its admission lease is
`reserved` or `executing` with `cleanup_state=pending`; the lease row is locked
so a concurrent release cannot race the insert. Active submit intents cannot
expose terminal stop evidence, and a terminal submit intent's error code and
stop evidence are immutable. An admission lease cannot become `released` or
`quarantined` while an attached submit intent is still active; `released`
additionally requires no unresolved `unknown_quarantined` execution. A stopped
engine generation cannot retain an unreleased admission lease or a non-terminal
submit intent. Once recorded, an admission lease's quarantine observation time
and release evidence are immutable; a gateway fencing barrier is permanently
sealed and its minimum accepted token cannot decrease.

## OpenAI compatibility

The compatibility promise is limited to the paths, fields, formats, events, and
error envelope declared in `speech-openapi.yaml` and
`realtime-events.schema.json`. See
[OpenAI compatibility](../docs/architecture/openai-compatibility.md) for the
supported SDK calls, non-goals, and the extension rules.

`tests/contract/test_p0_public_contracts.py` uses the repository-locked official
OpenAI Python SDK (`2.54.0`) with a mock transport for REST requests and the
SDK's real Realtime connection serialization path for `session.update` /
`input_audio_buffer.*`. `tests/contract/test_openai_js_sdk.mjs` runs the
repository-locked OpenAI JavaScript SDK (`7.43.0`) against the same REST subset
and verifies multipart, JSON, binary response, and rate-limit error shapes.
Captured requests, events, and parsed responses must match the declared
schemas. This is a contract-parity gate, not a live-server integration test.
The same assertions must run against HTTP and WebSocket routes once they are
implemented.

## Rules

- Freeze producer and consumer schemas before implementing business routes.
- Use snake_case fields and reject unknown request fields.
- Keep OpenAI-compatible fields limited to the explicitly declared subset.
- Put SpeechNexus enhancements under `/v1/nexus`, the optional `nexus` request
  object, or `nexus.*` events. Do not change the meaning of a standard field.
- Generate or validate language bindings from this directory; do not make a
  Python model or TypeScript interface an independent authority.
- Treat IDs as opaque at runtime. Code generation may brand distinct aliases,
  but the wire contract must not infer object type or authorization from an ID
  prefix.
- Add positive and negative examples with every state or error code change.
- Record incompatible changes in an ADR and update consumers with the contract.
- Keep internal execution identities, instance generations, private endpoints,
  credentials, backend voice handles, and runtime artifact paths out of public
  DTOs.
- Validate generated Python/TypeScript consumers against shared positive,
  negative, and event-sequence examples; schema syntax alone is insufficient.
- Resolve cross-file references against the containing contract file; do not
  rely on the current working directory.

## Validation

Run:

```bash
make contracts-check
make contracts-sdk-test
uv run pytest tests/contract
CONTRACT_BASE_REF=origin/main make contracts-compat
```

`make contracts-check` validates every `*openapi.yaml` and `*.schema.json` file
and resolves local file references relative to the containing document.
`tests/contract` validates examples, negative examples, OpenAI SDK parity, and
cross-protocol invariants. It also rejects duplicate inline schema definitions
and orphan local schema components, so shared domains cannot drift back into
independent copies and generators do not expose definitions the protocol never
uses.
`make contracts-compat` compares the OpenAI-standard paths in the public speech
OpenAPI and every component schema reachable from those paths, the declared
standard Realtime event definitions and their reachable definitions, and the
public `shared/` domains they reference with a Git baseline. It rejects removed
standard paths, operations, fields, response statuses, event types, enum values,
or newly required request/response fields. The optional `nexus` property and
everything reachable only through it are excluded from that walk; Nexus-only
paths, components, and events remain governed by their own positive / negative
examples rather than being misreported as OpenAI breakage. Pull requests run
this check against the target branch. Database coordination checks are
separate:

```bash
SPEECHNEXUS_TEST_DATABASE_URL=postgresql://speechnexus@127.0.0.1:55432/speechnexus_test \
  make integration-test
```

Those tests create an isolated schema, apply the coordination DDL, exercise
positive and negative PostgreSQL transactions, and drop the schema. CI runs
them against a disposable PostgreSQL 16 service.
