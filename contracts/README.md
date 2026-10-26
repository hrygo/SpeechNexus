# SpeechNexus contracts

Contracts in this directory are the source of truth for public and internal
producer/consumer protocols. Consumers must not hand-maintain independent copies
of defaults, units, enum domains, or stable error codes.

The [coordination request structure check](../docs/architecture/coordination-request-change-checks.md)
compares the 18 frozen raw transaction request entrypoints with
`CONTRACT_BASE_REF=origin/main make contracts-coordination-requests`. Its report
separates schema and binding version evidence. Results, mixed root envelopes
and complete protocol compatibility remain outside that command's scope.
An explicit `contracts-coordination-results` target separately checks the
`TransactionResult` entrypoint in the response direction; it retains those
root/semantic limits and does not alter the default request surface.

## Current scope

- `internal/audio-inspection.schema.json`: private successful output from the
  isolated media worker, explicitly generated as `audio_inspection`. Consumers
  separately bind original input digest/length, exact source clock, configured
  limits and process completion. This is not a public Asset/stop/budget receipt.

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
  `artifact_id`. A succeeded assistant turn records both generated output text
  and the speech Job that renders it; a speech Job reference always has
  generated text and can never be detached from it.
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
  identities. It is separate from runtime instances. The selected admin draft
  creation/fixed revision HTTP validators and full runtime DTO entrypoint are
  described in [the draft contract handoff](../docs/architecture/model-binding-drafts.md);
  their existence does not implement the registry or publish/activate a model.
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
  service key and public routes must reject it. Version `0.4.0.dev0` separates
  retryable control-store unavailability from unknown commits and required
  reconciliation, with schema-enforced recovery hints. The authenticated
  [barrier and read-only query HTTP operations](../docs/architecture/execution-gateway.md)
  are implemented. An unknown execution without an observed dispatch can be aborted
  only with whole-instance termination evidence; submit replays in that branch must
  include `stop_evidence_kind`. The new 404 `control_execution_not_found` carries
  `retryable=false/recovery=none`; it does not prove absence of engine work.
  Strict consumers must upgrade together. The submit, abort and instance-drain HTTP routes are
  served; real engine dispatch and qualification remain pending.
- `internal/coordination-transactions.schema.json`: ADR 0006 application
  transaction ports for owner/fencing, complete-recipe admission, claim,
  execution evidence, quarantine/release, runtime lifecycle and interval
  accounting. Reuses existing internal/public domains; does not add HTTP routes.
  Includes typed results and unknown-outcome recovery. Exact atomic steps,
  lock order, replay identities and producer/consumer handoff are in the
  [application transaction design](../docs/architecture/coordination-transactions.md).
  The shared PG/InnoDB prototype is test-only. The first independent production
  `create_job`/outbox unit and read-only recovery are documented in
  [production jobs](../docs/architecture/production-jobs.md); remaining ports and
  their application integration are still pending.
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
  for a succeeded stop intent / stopped or absent observation. An unanswered
  node runtime is a retryable `503 runtime_controller_unavailable` that leaves
  the intent unresolved, and an unknown intent identity is
  `404 runtime_intent_not_found`.

  `GET getVerifiedInstanceStop` is a read-only instance-generation query. It
  returns an original successful stop only when its immutable request and full
  proof match the node's released local checkpoint. Missing or partial proof,
  including legacy success without a checkpoint, is 404; conflicting or
  ambiguous proof is 503. A fixed TLS gateway consumer validates the complete
  binding, same-pool fencing and execution time before recording each execution's
  stop. This query never advances recovery or returns scheduler capacity.
  See [trusted stop consumption](../docs/architecture/trusted-node-stop.md).

  `POST reconcileNodeInstances` accepts diagnostic assertions only. Its supplied
  state, stop evidence, timestamp and compatibility fencing token never update
  persisted intents/checkpoints, qualify readiness, fence resource-pool intents
  or release memory. `accepted_at` is node processing time; the quarantine list
  reports discrepancies without persisting quarantine. Consumers obtain
  authoritative observations by querying the originally accepted intent after
  node-local inspection/recovery. This semantic tightening preserves wire
  shapes; all serving nodes must upgrade before relying on it.

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
Host and instance capacity counters are transaction-end projections of the
authoritative instance base budgets and admission-lease states. A committed
transaction cannot leave reserved, incremental, or quarantined counters stale,
and capacity-accounting history cannot be deleted or truncated to bypass that
check. A new admission lease also requires an active pool, ready host node, and
ready instance generation; the row locks serialize that check with concurrent
draining or quarantine transitions.

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

## Workbench HTTP version migration

Strict v1 status/render responses retain their original fields. Approved Minutes
configuration and explicitly pinned render bindings use additive `/v2/workbench`
paths and generated `WorkbenchStatusV2` / `RenderUnitV2` types. V1 render creation
returns the existing `503 workbench_unavailable` without writing a unit; v2
requires the caller-selected binding. There is no automatic version fallback or
binding default. Both generation operations remain contract declarations without
production routes. See [ADR 0009](../docs/decisions/0009-workbench-explicit-binding-http-version.md)
for the operation mapping, consumer migration and rollback boundary.

## Generated consumer bindings

The first file-workflow consumer package is documented in
[`packages/contracts`](../packages/contracts/README.md). Generate it with
`make contracts-generate` and verify byte-for-byte reproducibility with
`make contracts-drift` after installing both locked toolchains. The
[ownership matrix](../docs/architecture/contract-ownership.md) assigns the 18
coordination ports and preserves typed unknown-outcome recovery. These artifacts
do not imply implemented business routes or completed consumer integration.

The explicit workbench JSON profile now includes projects, transcripts,
status/errors and the four shared deletion entrypoints. `schema_refs` in the
binding manifest selects external definitions without copying their wire
contracts. An opt-in `workbench-http` profile adds derived path/query/header,
per-status response/error validation and a client for seventeen selected
paths, including Meeting/create/seal, Minutes/create and `generateRenderUnit`.
The latter also exposes `RenderUnitGenerationRequest` and
`RenderUnitGenerationAccepted` through the Python/TypeScript runtime profile.
It preserves string revisions and the submitted key, and validates the declared
202/error responses. Its synthetic HTTP consumer checks transport only: it does
not implement a Workbench route, create a public speech Job or establish voice
publication.

`downloadSavedAsset` and `downloadBookExport` select their existing GET content paths: 200/206 are
`application/octet-stream`, while 401/404 retain their JSON error schema. Python
`validate_response` accepts native bytes/bytearray/memoryview without decoding
or copying content. TypeScript accepts Uint8Array or ReadableStream; actual
`createValidatedClient` calls must pass `parseAs: "stream"` before I/O. The
middleware validates metadata without reading, cloning or buffering a binary
response. The caller owns bounded reading and cancellation; `maxJsonBytes`
continues to bound JSON, including download errors, and is not a binary size
limit. Empty responses preserve openapi-fetch's null/undefined representation.

The source permits a missing Content-Range on 206 and declares no 416 here;
this projection preserves those choices. It does not validate actual range
selection, length/digest, media safety or a C production route. New constraints
on a binary schema fail generation until their validation is implemented.
`streamExportEvents` selects the existing SSE operation and its original
401/404/410 JSON errors. Its canonical `x-sse-event-schema` produces an HTTP
event entrypoint and the same six-member `ExportEvent` Python/TS runtime union.
Native response metadata validation does not consume the stream. Explicit
`iter_events` / `readEvents` helpers frame UTF-8 incrementally, enforce the
caller's per-event byte bound, validate each source payload and pin it to the
original export id and cursor. Provided SSE id/event fields must agree with the
payload; sequence comparison preserves decimal precision. CR reserves its
optional LF in the bound. Incomplete/invalid/oversized events are not yielded.

The consumer retains only the last verified event id, chooses subsequent reads
and handles 410 through the existing error/state protocol. There is no automatic
reconnect, retry or terminal-success inference from EOF. Reader cancellation
closes this transport; it is not export cancellation or a resource-release
receipt. Full export state-machine/C route qualification remains separate.
No source wire/default, database, lockfile or persistent state changes are made
by these transport entrypoints.
Separate public realtime profiles expose session/PCM DTOs and the original public event
union plus a fourteen-member transcription selection. They reuse canonical
sources without enabling routes or a socket. Shared trace inputs distinguish
shape validation from C4/A05 state-machine/real-engine acceptance; see the package
profile list and examples. Packaged HTTP contracts do not register C's routes.

The opt-in `admin-http` profile selects only `listWorkspaces` and `getWorkspace`
within the two reviewed workspace paths. `operations` narrows both generated
OpenAPI and the validated TypeScript client when source paths also declare
planned writes. Python/TypeScript runtime entrypoints include Workspace,
WorkspaceList and the canonical shared ErrorResponse. Standard 400/403/503
responses are declared on the source GET operations. These are contract
artifacts; the backend separately connects the same two GET operations to
read-only PostgreSQL and its configured runtime. Planned writes remain unregistered.

The independent future `profile-lifecycle-http` surface selects profile publication
and activation from admin `0.1.0.dev4`, with same-source request DTOs, published-only
success validation, bounded ingress responses and original-path/body/key recovery
after 503. Existing draft `profile-http` still selects create/fixed-get only.
These artifacts do not register a publisher, grant authority to caller-supplied
validation evidence or establish production qualification. See
[the contract preflight](../docs/architecture/profile-lifecycle-contract.md).

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
standard paths, operations, fields, response statuses, and event types, as well
as narrowed request domains and weakened response guarantees. This includes
new required request fields, removed required response fields, explicit
`const: null` changes, and newly named properties that violate the previous
`additionalProperties` domain. Changed `oneOf` constraints require explicit
compatibility proof: widening one branch can make previously accepted values
match multiple branches. Equivalent resolved references and annotation changes
remain accepted. The optional `nexus` property and
everything reachable only through it are excluded from that walk; Nexus-only
paths, components, and events remain governed by their own positive / negative
examples rather than being misreported as OpenAI breakage. Pull requests run
this check against the target branch. Database coordination checks are
separate:

```bash
SPEECHNEXUS_TEST_DATABASE_URL=postgresql://speechnexus@127.0.0.1:55432/speechnexus_test \
SPEECHNEXUS_TEST_MYSQL_URL=mysql://root:speechnexus_test@127.0.0.1:55434/ \
  make integration-test
```

The historical PG suite applies the preserved legacy fixture. The new shared
transaction suite applies the active simplified DDL to PG and MySQL/InnoDB.
`make integration-test` and `make coordination-test` are the required
cross-database gates: both demand `SPEECHNEXUS_TEST_DATABASE_URL` and
`SPEECHNEXUS_TEST_MYSQL_URL`, and the shared transaction suite fails rather than
skips when only one engine is configured, so a single-engine run cannot be
reported as dual-database evidence. Run the historical PG suite alone with
`uv run pytest -m integration tests/integration/test_cluster_coordination_schema.py -q`
when no MySQL instance is available. See
[integration setup and scope](../tests/integration/README.md).
CI provisions both disposable database services.

## Architecture repair revision (2026-10-23)

[ADR 0005](../docs/decisions/0005-complete-execution-and-data-lifecycle-contracts.md)
records the intentional internal/receipt changes and upgrade boundary.
`internal/execution-command.schema.json` freezes complete effective execution
commands and typed resource material; internal control 0.2.0.dev0 requires the
command and its canonical digest. Stream 1.1.0 adds explicitly negotiated text
input with append/commit/finish/ack and scalar-value credits. See
[execution command semantics](../docs/architecture/execution-command.md).

`shared/receipts.schema.json` revision 2.0.0 permits only public facts, typed
immutable references and allowlisted missing/unknown paths. It never returns
content or arbitrary vendor objects. Shared errors preserve the OpenAI envelope
and require `error.nexus.operation_id` for `operation_in_progress`; a
`response_not_replayable` additionally carries the original successful receipt.

`shared/deletion.schema.json` is shared by owner-scoped speech and workbench
plan/create/query endpoints. Version/digest-pinned dependencies and exact cascade
confirmation precede asynchronous cleanup. Independent saved copies remain
independent; stop uncertainty retains capacity and restoration replays the
persistent deletion ledger before exposing content. See
[deletion semantics](../docs/architecture/data-deletion.md).
These protocols have contract tests; business routes and cleanup services remain
unimplemented.

[ADR 0006](../docs/decisions/0006-cluster-coordination-and-storage-portability.md)
preserves these protocol semantics while moving cross-table coordination rules
into controlled application transactions. PostgreSQL remains the official
product target; the MySQL/InnoDB prototype validates key coordination behavior only.
The [refactoring plan](../docs/plans/2026-10-23-cluster-coordination-refactor.md)
does not constitute an implemented adapter or a new database support claim.

### Versioned HTTP structural change checks

`internal/gateway-recovery.schema.json` 1.0.0 defines private server-side activation of
the gateway's periodic stop recovery. Its `gateway_recovery` Python/TypeScript profile
uses the existing offline generator and shared positive/negative fixtures. Every field
is explicit; node credentials remain separate Secret settings. This adds no public HTTP
operation, database authority or engine qualification. See
[activation and lifecycle](../docs/architecture/execution-gateway.md#默认进程的自动恢复配置与生命周期).

`CONTRACT_BASE_REF=<frozen commit/ref> make contracts-http-changes` checks known
request/response structure changes across admin, workbench, runtime-control and
execution-control OpenAPI sources, including their repository references and
`nexus` fields. It reports both source versions and findings; a version bump does
not waive a finding. The existing `contracts-compat` OpenAI standard scope remains
unchanged. See [coverage and limits](../docs/architecture/http-contract-change-checks.md).
A zero result is not a proof of complete protocol compatibility or a replacement
for the producer's version/change list, examples, consumer review and migration.

`CONTRACT_BASE_REF=<frozen commit/ref> make contracts-speech-changes` explicitly
selects the complete speech HTTP source, including its Nexus paths and fields.
The equivalent CLI uses `--surface speech`. The default four-source output and
formal OpenAI standard policy are preserved. Full-source structural checks do
not establish broader OpenAI compatibility, complete schema inclusion or actual
route/engine qualification.

## Binding parameter default declaration

`binding-parameter-defaults.schema.json` defines the standalone, fixed-parent
`BindingParameterDefaultsSnapshot`. The `binding_defaults` Python/TS profile
validates explicit sparse canonical parameter fields without inserting values.
`shared/profile-parameter-values.schema.json` is their common field source with
complete profiles, which keep their original requiredness and domains. The
[format and lifecycle requirements](../docs/architecture/binding-parameter-defaults.md)
separate parsing from registry storage, published configuration and qualification.
That standalone format adds no HTTP operation and leaves ModelBinding response
fields unchanged; the separately selected producer mode is described below.

The separate `binding-defaults-producer.openapi.yaml` `0.1.0.dev0`
[producer profile](../docs/architecture/binding-defaults-producer.md) declares
optional `parameter_defaults` on new draft creation and a fixed snapshot GET.
It reuses the legacy admin request's canonical field schemas. The explicit
four-table producer writer now serves these operations, retains the fixed parent
GET, and selects this profile for the registered operational surface. The
independent three-table admin `0.1.0.dev3` / `binding` mode keeps its original input
and two operations. Generated artifacts validate boundaries; they do not prove
authority, publication or engine qualification. Snapshot `1.0.0` stays unchanged.
