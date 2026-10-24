# ModelBinding draft contract preflight

This is a contract and consumer handoff for A00/A02. It does not implement a
registry provider, database migration, HTTP route, publication, activation or
model loading. The existing Profile draft provider remains a separate resource.

The authoritative sources are admin OpenAPI `0.1.0.dev3` and
`model-binding.schema.json` (`schema_version: "1.0.0"`). The selected operations
are `createModelBindingDraft` and `getModelBindingRevision`. The full admin
TypeScript types already existed; the new runtime entries use these same sources.

## Creation and recovery

POST `/v1/nexus/admin/model-bindings` accepts the existing flattened
`ModelBindingCreateRequest`. The registry allocates the immutable revision and
globally unique `revision_id`. Separate successful commands append distinct
revisions. `supersedes_revision` is an optional positive revision string recording
a relationship, not a head CAS field; there is no `expected_latest_revision` input.
Source validation alone does not prove that a referenced revision exists or that
a recipe, artifact or capacity reference is consistent. Such composition rules
must be checked by the eventual producer and yield 422 when violated.

201 is constrained to a draft and cannot carry publication evidence. A production
target is still only a draft, without qualification or readiness. Original-key
lookup precedes allocation: the same optional Idempotency-Key and complete
canonical intent returns the original revision; a different intent under that key
returns 409 `idempotency_conflict`. This contract does not introduce a publication
CAS or pretend a mutable default pointer exists.

503 `binding_result_unknown` has `retryable: false`, a null/absent retry delay and
no Operation recovery field. A caller holding the original key and complete
intent may explicitly repeat that same POST to recover its original result. The
client does not automatically retry, replace a key or fabricate a query URI.
Without the original key, no automatic recovery is promised. A candidate revision
GET does not establish ownership of the original command. Ingress refusals
400/403/405/408/413/415/429 do not settle an earlier command's outcome.

GET `/v1/nexus/admin/model-bindings/{binding_id}/revisions/{revision}` selects only
the supplied identity; revisions remain decimal strings, including
`"9999999999999999999"`. Confirmed absence is 404. An unreadable result is 503
`binding_unavailable`, retryable only as that same fixed GET. GET supports existing
publication lifecycle projections; it never creates, publishes or activates one.

## Generated consumers

- Python: `speechnexus_contracts.admin.parse("ModelBindingCreateRequest", value)`,
  `speechnexus_contracts.model_bindings.parse("ModelBindingRevision", value)` and
  `speechnexus_contracts.binding_http.contract/validate_request/validate_response`.
- TypeScript: `@speechnexus/contracts/admin`,
  `@speechnexus/contracts/model-bindings` and
  `@speechnexus/contracts/binding-http`. `createValidatedClient` requires an
  explicit byte limit and exposes only the selected POST and fixed GET.
- Shared request/path/header/response/error examples:
  `tests/fixtures/contracts/binding-http.json`. Python compares each against the
  original OpenAPI independently; Python and TypeScript consume the same cases.
  `consumer-profiles.json` covers the DTO entrypoints and source binding examples.

The generator remains datamodel-code-generator 0.86.0 / openapi-typescript 7.13.0,
static projection revision 1. Package versions and lockfiles are unchanged. Run
`make contracts-generate` and `make contracts-drift`; do not edit generated files.

The finite HTTP structural gate recognizes this response refinement only when an
`allOf` branch equals the complete resolved baseline schema tree, preserving JSON
types (booleans differ from numbers). Nonstandard reference expansion visits schema
positions; `const`, `enum`, defaults and examples remain literal JSON data. Literal
or dynamic references that prevent a closed-tree proof cannot use the shortcut.
This proof is limited to nonstandard responses; requests,
other unions, lost guarantees and the original OpenAI comparison policy retain
their existing checks. It is not general JSON Schema inclusion or runtime proof.

## Remaining implementation

A separate unit must provide bounded immutable draft persistence, allocation,
original-intent storage, replay/conflict and unknown-result recovery, with real
isolated PostgreSQL tests. Fixed revision histories and independent commands must
survive concurrency and restart without duplicate side effects. Another unit will
wire the selected HTTP routes and controlled writer lifecycle after persistence.
No running service is claimed by the mocked consumer transports in this preflight.

Model parameter domains do not carry defaults. This preflight neither adds a
parameter-default field nor fabricates a published registry. Full effective
parameter resolution, resource qualification, publication/default-pointer CAS,
real speech execution and the A02/Gate acceptance remain open. B runtime proof
changes from #121 and all existing consumer namespaces are preserved.
