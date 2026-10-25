# SpeechNexus boundary contracts

Team A owns this package. It contains generated JSON boundary types, a typed
speech HTTP client factory, and offline file-workflow validation for A/B/C.
It contains no business rules, database adapter, server route, or engine.
Do not import it from any domain package. Interfaces translate validated DTOs
into their own application/domain values.

The file boundary also exposes the existing public `Transcription` schema for
consumers of staged file-ASR JSON. This generates validation/types from the
speech source without changing its wire format. See
[transcript import and text export](../../docs/architecture/transcript-import-and-text-export.md).

Profile draft consumers can parse `ProfileRevisionCreateRequest` through the
`admin` namespace and the complete `ScenarioProfileRevision` through Python
`speechnexus_contracts.scenario_profiles` / TypeScript
`@speechnexus/contracts/scenario-profiles`. These come from the existing admin and
scenario-profile sources, including root conditionals. See the
[process-local port and qualification boundary](../../docs/architecture/profile-drafts.md).

The separate `profile-http` TypeScript / `profile_http` Python surface selects
only profile creation and fixed revision reads from the current admin source `0.1.0.dev3`.
It validates their request/error/replay envelopes without advertising profile
routes through the existing workspace client. The backend implements the bounded
profile draft routes separately; no validator call creates or publishes a profile.

Model binding consumers can now parse `ModelBindingCreateRequest` in `admin`,
`ModelBindingRevision` through Python `model_bindings` / TypeScript
`@speechnexus/contracts/model-bindings`, and the selected create/fixed GET envelopes
through `binding_http` / `@speechnexus/contracts/binding-http`. Admin source is
`0.1.0.dev3`; these entries do not implement routes or publish a model. See the
[ModelBinding draft contract handoff](../../docs/architecture/model-binding-drafts.md)
for original-key recovery, unknown results and the remaining producer work.

## Generate and verify

From the repository root, with locked Python and Node dependencies installed:

```sh
make contracts-generate
make contracts-drift
uv run pytest tests/contract/test_generated_bindings.py -q
pnpm --filter @speechnexus/contracts typecheck
pnpm --filter @speechnexus/contracts test
```

Inputs are `contracts/bindings.json`, the speech OpenAPI and its local reference
closure. The generator refuses references outside `contracts/` and remote URIs.
Python uses datamodel-code-generator 0.86.0 with the checked-in TypedDict
flattening template revision 1. TypeScript uses openapi-typescript 7.13.0.
Formatter versions are locked. Never edit `*.generated.ts`, `models.py`,
`parsers.py`, `file.schema.json`, or `source-inventory.json` by hand.

`source-inventory.json` records hashes and declared versions for every OpenAPI
and JSON Schema, including admin, workbench and internal protocols. Its diff
shows source changes; it is not a compatibility verdict. The existing OpenAI
compatibility gate continues to cover only the declared standard subset.

## Consumer handoff 0.1.0

Python consumers install the `speechnexus-contracts` wheel or add it as a uv
workspace dependency. Its schema bundle ships in the wheel and needs no source
checkout or network access:

```python
from speechnexus_contracts import parse

command = parse(
    "JobCreateRequest",
    {
        "kind": "asr",
        "model": "asr-quality",
        "input": {"audio_asset_id": "asset-demo"},
    },
)
```

TypeScript consumers add `@speechnexus/contracts: workspace:*`:

```ts
import { createSpeechClient, parse } from "@speechnexus/contracts";

const asset = parse("Asset", untrustedResponse);
const client = createSpeechClient(deploymentBaseUrl);
const response = await client.GET("/v1/nexus/assets/{asset_id}", {
  params: { path: { asset_id: asset.id } },
});
```

The caller chooses the deployment URL and authentication/fetch policy. The
client has no default remote endpoint and does not validate received JSON
automatically: call `parse` on untrusted data. Full speech OpenAPI static types
are available, but runtime entrypoints are limited to `bindings.json`: Jobs,
Job events, cancellation, Assets, Artifacts, Operations and public errors.
Python additionally exposes `validation.validate_coordination` for the first
internal `create_job` request and full `TransactionResult`. These use a separate
generated offline bundle; domain types remain independent of boundary DTOs.
The coordination bundle also validates owner acquire/renew/activate,
`claim_job`, `query_outcome`, `mark_execution_unknown` and
`record_execution_observation`, `redact_execution_command` and `append_interval` requests. The production
owner boundary implements the pool target, the claim boundary handles Job
current-authority queries, and the gateway implements its own submit snapshot.
An unclaimed Job row returns unknown, and an expired/later claim snapshot neither
proves that an earlier write rolled back nor permits redispatch.
Remaining query targets stay with their fact owners. The shared
`execution-recovery.json` corpus checks the new coordination entry against the
source schema, packaged Python validator and TypeScript bundle parser; this
does not add a public TypeScript coordination API.
The coordination bundle also exposes `create_runtime_intent` and
`observe_runtime`. A RuntimeSnapshot can now contain `runtime: null`, a frozen
request and coordinator timestamps while no controller observation exists;
consumers must handle this branch before adopting the new producer. It cannot
report successful lifecycle completion, and an observe result requires the
actual response branch. The controller HTTP response schema is unchanged.
Shared producer/consumer cases live in
`tests/fixtures/coordination/transaction-examples.json`; native runtime
create/query/observe transactions are implemented, while real controller
dispatch and qualification providers remain pending.
The trusted gateway observation boundary returns a complete validated
`TransactionResult`. It compares the full frozen identity and current version;
output/staging publication and verified stop remain separate writers. A stale
version requires a current-authority query, and an unknown commit never permits
blind observation retry or engine redispatch.
The native command-redaction boundary requires an explicitly injected trusted
deletion authorizer and returns the existing `FactSnapshot`, with the original
submit ID and canonical execution request digest. Its transaction verifies
cleanup and A's lease release, then erases only the body and stores an immutable
tombstone. Matching job/evidence replays preserve time/version; no default
authorizer or HTTP deletion route is supplied. Shared recovery examples cover
both boundary requests and their required result types.
The trusted native interval boundary appends immutable generated/delivered/played
facts and queries the original interval. Its `FactSnapshot` contains the interval
ID and digest of the complete canonical append request; there is no invented
interval version. Reads never write or dispatch, and an absent original remains
unknown. Shared Python/TypeScript examples cover complete results and recovery.
Logical-flow locking, overlap/ceiling decisions and cleanup eligibility are
application rules tested on PG/InnoDB; this package does not collect real audio
or settle usage.
Multipart `AssetUploadRequest.file` is a schema-level string placeholder;
actual upload transports must send binary media and need separate integration
tests. No service or browser integration is claimed here.

Python TypedDicts and TypeScript types cannot encode all JSON Schema rules.
Always validate wire data: the runtime validators preserve `if/then`, exclusive
unions, `unevaluatedProperties`, bounds, formats and nullable distinctions.
They do not add defaults, coerce values, drop fields, or include input content
in their error messages. Defaults must still be resolved by a shared canonical
command layer before digesting. Cross-record relationships, event ordering,
canonicalization, idempotency execution and cancellation cleanup need application
rules and integration evidence; this package does not implement them.

Both languages consume the same synthetic
[`file-workflow.json`](../../tests/fixtures/contracts/file-workflow.json).
Python additionally compares acceptance to the original unbundled OpenAPI.
B/C should run these commands at the merged integration SHA and record their
consumer evidence in [A00 #19](https://github.com/hrygo/SpeechNexus/issues/19).

## Changes

Change source contracts first; add positive, negative and necessary sequence
examples; regenerate and review source/type/inventory diffs together. Keep
annotations separate from wire changes in review. Breaking changes require an
ADR and coordinated consumer updates; this pre-release package does not imply
stable compatibility for every admin/workbench/internal contract. Extending
runtime entrypoints requires shared acceptance/rejection examples for each
new entry. Do not expand the OpenAI compatibility promise through this package.

## Explicit consumer profiles

Six opt-in JSON profiles extend the default file workflow. Each includes Python
TypedDicts, a typed parser, an offline schema bundle and generated TypeScript
types. HTTP profiles additionally expose basic clients; the two public realtime
profiles parse data without opening connections. Entry points are explicitly listed in `bindings.json`:

| Profile           | Runtime entrypoints                                                                                                      |
| ----------------- | ------------------------------------------------------------------------------------------------------------------------ |
| `admin`           | Workspace create/CAS request, Workspace/List response, shared ErrorResponse                                              |
| `workbench`       | Project create/CAS and Project/List; transcripts; status/errors; shared deletion; Meeting/create/seal and Minutes/create |
| `execution`       | Submit request/receipt/status, abort, barrier request/response, control errors                                           |
| `runtime`         | Runtime instance intent request/receipt, node reconciliation request                                                     |
| `realtime`        | Public session create/response and shared PCM descriptor; reuses speech TS types                                         |
| `realtime_events` | Original public event union, fourteen-member standard transcription selection, OpenAI PCM format                         |

```python
from speechnexus_contracts.execution import parse

request = parse("SubmitExecutionRequest", untrusted_json)
```

```ts
import { parse, createClient, ContractValidationError } from "@speechnexus/contracts/execution";

const request = parse("SubmitExecutionRequest", untrustedJson);
const client = createClient(deploymentBaseUrl);
```

Import the corresponding profile instead of the default package entry. These
are boundary DTOs, not domain values. Typed HTTP clients require an explicit
deployment URL, do not retry, and do not automatically validate responses.
The default and workbench parsers reject internal protocol names. A separate
profile is an import/API namespace, not an authorization mechanism.

The workbench JSON slice preserves `ProjectCreateRequest` and `ProjectPatch`,
then adds `Project`, `ProjectList`, `TranscriptCreateRequest`,
`TranscriptRevisionCreateRequest`, `Transcript`, `TranscriptRevision`,
`TranscriptView`, `TranscriptList`, `WorkbenchStatus`, `ErrorResponse`,
`DeletionPlanRequest`, `DeletionCreateRequest`, `DeletionPlan`, and `Deletion`.
The four deletion entrypoints use explicit `schema_refs` into the authoritative
`shared/deletion.schema.json`; their bundled names are aliases to resolved
definitions, not new wire schemas. Existing full OpenAPI TypeScript types are
reused. Project metadata revisions are integers; transcript and deletion
revisions are decimal strings. Parsers neither insert defaults nor convert them.

```python
from speechnexus_contracts.workbench import parse

command = parse("TranscriptCreateRequest", untrusted_json)
view = parse("TranscriptView", response_json)
```

```ts
import { createClient, parse } from "@speechnexus/contracts/workbench";

const client = createClient(deploymentBaseUrl);
const result = await client.POST("/v1/workbench/projects/{project_id}/transcripts", {
  params: { path: { project_id: projectId }, header: { "Idempotency-Key": originalKey } },
  body: parse("TranscriptCreateRequest", untrustedJson),
});
const view = result.data
  ? parse("TranscriptView", result.data)
  : parse("ErrorResponse", result.error);
```

These calls validate JSON bodies only. The opt-in HTTP profile below checks
transport parameters/status and validates client requests/responses.
Meeting/Minutes also have JSON and HTTP entrypoints; public realtime has the
separate parser profiles described below.
Schema validation does not establish a
cross-record transcript/revision match, deletion ownership of the selected
route, a fresh plan, actual cleanup, or permission to release budgets. C owns
those application/transport checks and its workbench migrations/routes; this
package does not register them in the speech backend.

### Workbench HTTP boundary

`workbench-http` derives its reviewed v1/v2 paths and operations from the source
OpenAPI: status, project create/read/update/list, transcript create/read/list and
revision create/read, deletion plan/create/read, meeting create/read/seal and
minutes create/read, render generation, saved-asset/book-export downloads and
export SSE. The existing C v1/v2 JSON selection remains available. The packaged
`workbench.openapi.json` is a closed contract for C's handlers and documentation;
loading it does not start a server or register a route. It retains the original
parameter/header requirements, per-status JSON/binary response schemas and public
errors. It does not change the default `workbench.createClient`.

```python
from speechnexus_contracts.workbench_http import validate_request, validate_response

validate_request("listProjects", {"query": {"limit": "50"}})
validate_response("listProjects", 200, {"headers": response_headers, "body": response_json})
```

The envelope accepts optional `path`, `query`, `headers` maps and a declared `body`
when the operation declares one; responses contain optional headers and the
declared body. Required parameters, body and statuses follow the actual route.
For example, deletion's Idempotency-Key is required and may be one character,
while project creation's optional key has the source component's eight-character
minimum. Undeclared path/query keys, duplicate case variants of headers and
unlisted response statuses are rejected. Extra HTTP headers remain allowed.

Integer query/header values accept a safe integer or its strict decimal HTTP
serialization. Header names are case insensitive. Validation uses a temporary
projection and returns the original envelope unchanged; it never changes the
JSON body, inserts the source's default limit, or converts revision strings.
The adapter must preserve/reject duplicate scalar query/header wire fields
before mapping them to a Python dictionary. This is shape validation, not authentication,
cross-resource ownership, CAS, deletion authorization or cleanup evidence.

```ts
import { createValidatedClient } from "@speechnexus/contracts/workbench-http";
import { parse } from "@speechnexus/contracts/workbench";

const client = createValidatedClient(deploymentBaseUrl, 8 * 1024 * 1024);
const result = await client.POST("/v1/workbench/projects/{project_id}/transcripts", {
  params: { path: { project_id: projectId }, header: { "Idempotency-Key": originalKey } },
  body: parse("TranscriptCreateRequest", untrustedJson),
});
```

The client checks the final URL, query, headers and serialized JSON before
fetching, then validates the actual response status, headers and JSON before
returning it. Its explicit byte limit applies to actual UTF-8 bytes in each
JSON request/response, independently of Content-Length; overflow/invalid UTF-8/JSON
cancels reading and returns a safe ContractValidationError. Callers choose a
bound that covers their supported payloads and retain their own concurrency and
timeouts. The client prohibits redirects and a different per-call deployment
origin/path, preserves the supplied body/key, and never retries a network error
or an unknown outcome. Declared errors keep their original status/body; this
validation does not grant permission to repeat a write. Consumers that add their
own middleware/fetch implementation must preserve these transport guarantees.

For `downloadSavedAsset` and `downloadBookExport`, 200/206 are native binary
rather than JSON. Python
`validate_response` accepts bytes, bytearray or memoryview; validation never
copies or decodes that payload. The TypeScript client requires `parseAs: "stream"`
on these operations before I/O:

```ts
const download = await client.GET("/v1/workbench/saved-assets/{asset_id}/content", {
  params: { path: { asset_id: savedAssetId }, header: { Range: "bytes=0-3" } },
  parseAs: "stream",
  signal: controller.signal,
});
```

Success returns the original ReadableStream, with no clone/read/buffering in
validation. Empty content can yield null/undefined as in openapi-fetch. The
caller owns a bounded reader, deadline and cancellation; the JSON limit does
not limit binary downloads. Workbench source `0.1.0.dev1` additionally declares
400/408/413/416/500/503 JSON failures on both download operations; saved assets keep
401/404 and book export also keeps 409/410. Strict `dev0` clients must regenerate
before the new producer is deployed. The source leaves Content-Range optional on
206 for existing consumers and adds the `bytes */size` shape on 416. Validation
does not check actual range selection or digest. The production C routes and Web
consumer add those behavioral checks; their limits and evidence scope are documented
in [committed content downloads](../../docs/architecture/workbench-content-downloads.md).
New binary constraints fail generation until implemented.

For `streamExportEvents`, use `parseAs: "stream"` as well. The middleware checks
the actual SSE status/media/headers without reading the body. Its 401/404/410
errors remain bounded JSON. The independently generated `ExportEvent` union is
available through `workbench.parse` and the HTTP `validateEvent` entrypoint.
Use the incremental helper to validate actual frames, not only metadata:

```ts
import { readEvents } from "@speechnexus/contracts/workbench-http";
const request = {
  path: { export_id: exportId },
  headers: { "Last-Event-ID": originalCursor },
};
const response = await client.GET("/v1/workbench/exports/{export_id}/events", {
  params: { path: request.path, header: request.headers },
  parseAs: "stream",
  signal: controller.signal,
});
if (response.data) {
  for await (const event of readEvents("streamExportEvents", response.data, request, 65536)) {
    // Consume event first; retain only a verified cursor for a later read.
    lastVerifiedCursor = event.id;
    if (shouldStopReading) break; // cancels this reader, not the export job
  }
}
```

Python is transport independent. Validate the original request before I/O;
callers own their HTTP client's fixed deployment, redirect policy, timeout,
actual media check and response context. Pass the same original request and
native byte iterator to the generated helper, and close both on early exit:

```python
from contextlib import closing
from speechnexus_contracts.workbench_http import iter_events

with closing(
    iter_events("streamExportEvents", response.iter_bytes(), request, max_event_bytes=65536)
) as events:
    for event in events:
        last_verified_cursor = event["id"]
        if should_stop_reading:
            break
```

The helper never buffers a complete stream. Its mandatory positive safe byte
budget covers one pending frame including metadata/comments; CR reserves a
possible following LF. UTF-8 chunks, LF/CRLF/CR, BOM, comments and multiline data
are supported. Payloads must satisfy the original event union and export id;
optional SSE id/event fields must match the payload, and ids must increase after
the original cursor. Decimal ids are compared without Number rounding. Invalid,
oversized or incomplete events raise a fixed error and do not advance the last
yielded cursor. No payload or transport exception details enter that error.
EOF does not establish export success; 410 and reconnection decisions belong
to the caller. The helpers do not retry, post cancellation or release resources.
Their ordered examples protect framing/identity, not the entire export business
state machine.

Both languages consume `workbench-http.json`, `workbench-binary-http.json` and
`workbench-sse.json`,
including every selected response status. The latter uses synthetic octet-array
test descriptors materialized as native bytes, never JSON download wire bodies.
Python also checks these cases against the original source operations;
the TS mock transport covers rejection before network I/O, byte bounds, cancellation,
unknown responses and no retries. Native loopback HTTP checks verify before-EOF binary/event consumption, reader
cancellation closing the socket, exact Range/cursor forwarding, explicit resume,
truncated-stream rejection, declared 410 and redirect refusal. Actual C handlers, their migrations and
target-network/browser qualification remain C's versioned integration work.

### Meeting and minutes projection

The JSON profile adds `Meeting`, `MeetingCreateRequest`, `MeetingSealRequest`,
`MinutesCreateRequest`, and `MinutesJob`. The HTTP profile selects the same five
source routes, including the `202` minutes-creation reply and declared failure
envelopes. This adds generated entries and shared examples without changing the
wire schemas or inventing a second model/default policy.

Meeting creation has exactly one source: `speech_session_id` or `transcript_id`.
Parsers preserve an omitted `save_audio` rather than inserting its server default.
Seal, source and template revisions are decimal strings. An omitted minutes
model is resolved by C against the deployment-approved configuration; the parser
and client never select or enable an LLM or accept an endpoint field.

`Meeting.failed` describes capture failure before sealing: it has an end time
and structured error, and no sealed revision or minutes reference. A failed
`MinutesJob` may contain partial text but no publishable Artifact. It does not
turn an already sealed meeting into capture failure: the meeting remains
`minutes_pending` with its sealed revision/minutes reference, and the linked job
carries its own structured error. Successful minutes require non-empty text or
an Artifact; non-failed jobs do not carry a non-null error.

C must verify the linked transcript/revision is sealed and owned by the project,
the minutes job belongs to the meeting and pins its exact sealed revision, and
the referenced job has succeeded before reporting `minutes_ready`. These are
cross-record/application checks; validating each DTO independently cannot prove
them. The shared failure/partial/success examples and client trace provide
versioned inputs for that integration, not evidence that C's handlers ran.

The public `MinutesJob` has no `usage` field. C's durable task and usage ledger
remain separate from this projection and must survive content cleanup as
required by their ownership rules. A public usage extension needs an explicit
source-contract change reviewed by A/C; internal task rows are rejected as DTOs.
C supplies its owner migration fragments, grants and root/HTTP integration
points; these generated packages neither reserve a migration revision nor
mount C's routes in the speech API.

### Public realtime parsers and standard transcription inputs

`speechnexus_contracts.realtime` / `@speechnexus/contracts/realtime` expose
`RealtimeSessionCreateRequest`, `RealtimeSession`, and `PcmDescriptor` from the
original speech schemas. TypeScript reuses `speech.generated.ts`; these are not
another set of types or defaults. Public PCM remains mono PCM16 little-endian
at 24 kHz. The B78 CPU VAD's internal 16 kHz input needs an explicit streaming
resampler in B's media path, not a change to the public descriptor.

`speechnexus_contracts.realtime_events` /
`@speechnexus/contracts/realtime-events` expose `RealtimeEvent`,
`TranscriptionEvent`, and `OpenAIPcmFormat`. The original OpenAPI puts WS events
in a `101` response extension that the HTTP type generator ignores. The event
profile therefore generates types directly from the closed original JSON
Schema using temporary compiler input; it does not create a second wire source.
`RealtimeEvent` keeps the complete declared union. `TranscriptionEvent` selects
the fourteen declared standard ASR definitions while retaining the root's
closed-field rules; nexus controls and unlisted OpenAI events are rejected by
that selection. Neither selection is a negotiated mode or a state machine.

```python
from speechnexus_contracts.realtime import parse as parse_session
from speechnexus_contracts.realtime_events import parse as parse_event

request = parse_session("RealtimeSessionCreateRequest", untrusted_json)
event = parse_event("TranscriptionEvent", decoded_frame)
```

```ts
import { parse as parseSession } from "@speechnexus/contracts/realtime";
import { parse as parseEvent } from "@speechnexus/contracts/realtime-events";

const request = parseSession("RealtimeSessionCreateRequest", untrustedJson);
const event = parseEvent("TranscriptionEvent", decodedFrame);
```

An omitted session expiry, PCM rate or model/default stays omitted. Internal
`runtime-control` commands remain in the distinct `runtime` namespace. These
public profiles expose no `createClient`, open no socket and approve no URL;
consumers wire parsed session DTOs through the existing explicitly configured
speech client and verify the configured WS origin before connecting. No public
REST/WS server, real ASR, TTS, duplex, credit/gap/epoch or cleanup authority is
enabled by importing a parser.

`consumer-profiles.json` adds shared syntax cases for every standard event,
session/PCM state and rejected private/unknown fields. Synthetic audio is stored
only as a bounded byte-count recipe and constructed inside tests, never as
encoded fixture audio. Whole-document imports drop source example annotations
while preserving their validation rules; root and narrowed event unions keep
`unevaluatedProperties: false`.

`transcription-sequences.json` supplies ten positive/negative semantic inputs
for C4/A05: manual commit, no speech, same-key replay, clear after acceptance,
empty commit, conflicting event id, changed/contradictory published final,
odd PCM alignment and unsafe append replay after disconnect. All their event
shapes deliberately pass schema validation. A00 tests validate only frame/recipe
consistency, not the `semantic_valid` expectations: C4/A05 must run them through
the actual reducer/producer before claiming sequence or engine qualification.
The consumer must also enforce negotiated JSON byte bounds, decoded PCM
alignment, stable-prefix evidence, idempotency and immutable published text.
Completed/failed fixes acoustic text only; it does not prove overall Operation
completion, auxiliary results, physical stop or budget release. Full nexus
credit/gap/epoch and terminal semantics remain later integration units.

Execution control `0.4.0.dev0` includes barrier and error parser entrypoints. Control
failures carry a constrained recovery hint: a lost commit acknowledgment is
`control_outcome_unknown` / `query_original` with `retryable=false`, while a
bounded unavailable transaction uses `retry_transaction`. Upgrade strict error
consumers together with this bundle; older error vocabularies cannot validate
the added codes. These control errors are not persisted engine execution errors.
An aborted execution may retain a null dispatch time only with controller/operator
whole-instance termination evidence. Submit replay must include `stop_evidence_kind`
in that branch. Read-only GET uses the existing status parser; the new
`control_execution_not_found` is fixed to `retryable=false/recovery=none`.
Old strict consumers reject these new state/error combinations and must upgrade
with the producer. The public speech contract is unchanged.

All profile outputs (including `*_models.py`, profile parsers and schema bundles)
are generated and checked by the same drift gate. Python static projection
revision 1 retains base field shapes when nested `allOf` refinements would cause
the pinned generator to replace required fields with optional partial objects.
It omits conditional refinements from static generation only. The packaged
runtime schema remains complete and authoritative in both languages. Consumers
must validate before using typed data; cross-record facts, digests, memory
inequalities, event sequences and actual transport behavior still need their
own application checks.

Both runtimes consume the synthetic `consumer-profiles.json` corpus, including
all nine effective command kinds and rejected cleanup/stop combinations.
Python compares each case with the original schema and TypeScript compile tests
cover parser results and client path restrictions. This is the first profile
slice; full admin/workbench runtime coverage, streaming protocols and actual
B/C consumer integration remain pending in A00.

## Asset HTTP transport

`uploadAsset(createSpeechClient(baseUrl), input, { idempotencyKey, requestId })`
replaces only `AssetUploadRequest.file`'s binary placeholder with a Blob/File.
All metadata uses the generated shape/runtime validator. The helper creates
actual multipart FormData, validates Asset/error replies and performs no retries.
It preserves response headers for original-identity recovery. It inserts no TTL
or workspace default. Set an explicit Idempotency-Key; an unknown reply never
means permission to create another upload.

```typescript
import { createSpeechClient, uploadAsset } from "@speechnexus/contracts";
const client = createSpeechClient("http://127.0.0.1:8000");
const result = await uploadAsset(
  client,
  { purpose: "text_input", file: new File(["synthetic"], "input.txt"), ttl_seconds: 3600 },
  { idempotencyKey: "asset-example-1" },
);
```

The server must be configured with a versioned private bucket, a non-owner DB
role and paired object credentials. Browser callers use an approved Origin and
the deployment's same-origin proxy. Initial content inspection supports actual
UTF-8 text; audio readiness requires B03.

`contracts/bindings.json.http_surfaces.assets` selects three implemented paths
for packaged `assets.openapi.json`. The generator closes external schemas and
retains parameters/responses/headers in their original component namespaces.
The running backend merges this complete projection with health schemas; it
never publishes the unimplemented speech paths in its OpenAPI. Schema default
annotations stay optional in TypeScript (`--default-non-nullable false`) and
never cause runtime default insertion. Generated files remain drift-protected.

## Voice catalogue read HTTP contract

The opt-in `voice-catalogue-http` entrypoint selects only `listVoices` and
`getVoiceRevision` from the existing speech source. Python imports
`speechnexus_contracts.voice_catalogue_http`; TypeScript imports
`@speechnexus/contracts/voice-catalogue-http`. Both validate complete public
revisions, including nested capabilities and lifecycle provenance; neither grants
publication or retirement methods. Shared positive/negative examples live in
`tests/fixtures/contracts/voice-catalogue-http.json`.

The running workbench adapter consumes this validator before mapping the C
projection and checks exact requested voice/revision identity separately.
Workbench source `0.1.0.dev2` adds `getVoiceRevisionV2` / `VoiceRevisionV2` for
the public 2000-character disable reason; v1 remains at its frozen 128-character
bound. Deploy the v2 C route before the updated formal Web consumer. See
[mapping, upgrade and evidence limits](../../docs/architecture/workbench-voice-catalogue.md).

## Workspace read HTTP contract

The opt-in `admin-http` surface selects `listWorkspaces` and `getWorkspace`
from `admin-openapi.yaml`. Python consumers import
`speechnexus_contracts.admin_http`; TypeScript consumers import
`@speechnexus/contracts/admin-http`. Both expose request and per-status response
validation. The TypeScript validated client requires an explicit deployment URL
and byte budget, validates replies, and never retries automatically.

The binding manifest can select `operations` within reviewed `paths`. An explicit
selection must contain unique, nonempty, existing operation IDs within those
paths; invalid selections fail generation. The generated OpenAPI, runtime parser
and validated client expose only those methods, even when the original path also
contains POST or PATCH. Existing surfaces without a selector retain their full
reviewed paths. The broader `admin` source types/client still describe the full
planned API; import `admin-http` for the restricted read surface.

Workspace JSON entrypoints preserve numeric `metadata_revision` and string
`policy_revision`; they reuse the shared public error definition. Omitted list
limits stay omitted during validation; the source contract declares default 50
and range 1–100. No parser inserts deployment defaults or creates a default
workspace. Both languages consume the same 39 transport examples in
`tests/fixtures/contracts/admin-http.json`, plus DTO examples in
`consumer-profiles.json`.

The consumer package itself does not register backend routes. The speech
backend now connects the two GETs to a bounded read-only PG port and the existing
asset runtime pool, and projects these selected methods in runtime OpenAPI.
Workspace writes remain separate A04 tasks. Schema checks and synthetic client
tests alone do not qualify the running service; actual non-owner PG/MinIO HTTP
coverage is documented in `docs/architecture/production-assets.md`.

Workbench status/configuration discovery and bound rendering use the generated
`WorkbenchStatusV2`, `RenderUnitV2`, `RenderUnitCreateRequestV2`,
`RenderUnitPatchV2` and `RenderUnitListV2` entrypoints with `/v2/workbench` HTTP
paths. V1 keeps its strict original schemas; the production boundary refuses v1
render creation with `503 workbench_unavailable` and performs no write.
`generateRenderUnit` / `generateRenderUnitV2` are validated contract declarations,
not registered production operations. See [ADR 0009](../../docs/decisions/0009-workbench-explicit-binding-http-version.md).
