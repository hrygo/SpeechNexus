# SpeechNexus boundary contracts

Team A owns this package. It contains generated JSON boundary types, a typed
speech HTTP client factory, and offline file-workflow validation for A/B/C.
It contains no business rules, database adapter, server route, or engine.
Do not import it from any domain package. Interfaces translate validated DTOs
into their own application/domain values.

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
`claim_job`, `query_outcome` and `mark_execution_unknown` requests. The production
owner boundary implements the pool target, the claim boundary handles Job
current-authority queries, and the gateway implements its own submit snapshot.
An unclaimed Job row returns unknown, and an expired/later claim snapshot neither
proves that an earlier write rolled back nor permits redispatch.
Remaining query targets stay with their fact owners. The shared
`execution-recovery.json` corpus checks the new coordination entry against the
source schema, packaged Python validator and TypeScript bundle parser; this
does not add a public TypeScript coordination API.
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

Four opt-in profiles extend the default file workflow. Each includes Python
TypedDicts, a typed parser, an offline schema bundle, and TypeScript OpenAPI
types/client. Runtime entrypoints are explicitly listed in `bindings.json`:

| Profile     | Runtime entrypoints                                                  |
| ----------- | -------------------------------------------------------------------- |
| `admin`     | Workspace create and metadata CAS patch                              |
| `workbench` | Project create and metadata CAS patch                                |
| `execution` | Submit request/receipt/status, abort, barrier request/response, control errors |
| `runtime`   | Runtime instance intent request/receipt, node reconciliation request |

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
