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
