# Profile publication and activation contract preflight

This A00/A02 unit supplies future consumer contracts, not a publisher. The product
still registers only the existing draft create/fixed-get profile routes. No
publication transaction, trusted validation-report provider, default pointer,
activation route, historical Job change or engine qualification is implemented here.

## Source and entrypoints

`contracts/admin-openapi.yaml` is now `0.1.0.dev4`. The existing
`ProfilePublicationRequest`, `ProfileActivationRequest` and canonical
`scenario-profile.schema.json` `1.0.0` remain the source for fields. The new
`ProfilePublishedRevision` response intersects that complete source with the
published state and a required reference to its existing validation definition.
It introduces no parallel parameter, evidence or lifecycle domain.

- Python DTOs: `speechnexus_contracts.admin.parse` with the three names above.
- Python future HTTP: `speechnexus_contracts.profile_lifecycle_http` exposes
  `contract`, `validate_request` and `validate_response`.
- TypeScript future HTTP: `@speechnexus/contracts/profile-lifecycle-http`, with
  `validateRequest`, `validateResponse` and `createValidatedClient`. Static types
  reuse `admin.generated`; the client requires an explicit JSON byte bound.

The separate `profile_lifecycle` HTTP surface selects only `publishProfileRevision`
and `activatePublishedProfileRevision`. Existing `profile-http` remains draft
create/fixed-get only; packaging a future surface does not register it in the
product's operational OpenAPI. There is no retirement, arbitrary current/latest,
Operation or query-by-key route in this unit. Generator versions remain
datamodel-code-generator 0.86.0, openapi-typescript 7.13.0 and static projection 1;
dependency locks are unchanged.

## Original intent and uncertain results

Both POST operations retain their existing path, request DTO, optional
Idempotency-Key, original-result replay and current-pointer CAS semantics.
Publication still requires verification of evidence for the fixed revision;
activation still requires an existing published revision. A caller-supplied
validation object or digest cannot establish server authority or production quality.
Cross-field target identity, trusted evidence lookup and current eligibility remain
server-side work; structural parsing cannot prove them.

An unreadable original result or uncertain commit returns 503
`profile_result_unknown`, `service_unavailable_error`, `retryable: false`, no
Operation recovery, no retry delay and no field-specific blame. The response
reuses the existing profile unknown-result error schema. It does not prove that
publication or pointer activation failed.

The caller may explicitly recover only with the complete original POST path,
body and original key. Original-result lookup precedes treating that request as a
new mutation or evaluating its CAS against a later current pointer. A changed
intent under that key remains a conflict. Clients do not automatically retry,
replace the key or invent an Operation. Keyless unknown attempts have no promised
automatic recovery. A fixed revision GET describes the resource, not the identity
or outcome of a publication/activation command; its existing 503 read-unavailable
response cannot be turned into 404 or permission to repeat a mutation under a new key.

Successful 200 responses must be published revisions with validation evidence and
the canonical publication timestamp rules; draft, retired and disabled responses
are rejected. Existing conflict/missing responses are retained. Ingress declarations
also cover Host/Origin refusal 403, method refusal 405 with required `Allow: POST`,
receive timeout 408, size 413, media 415 and capacity 429. Those describe this
HTTP attempt and do not resolve a prior unknown original-key result. Bounds are
deployment input, not new profile defaults.

## Verification and remaining implementation

Shared fixtures contain 82 HTTP cases and nine DTO cases. Python and TypeScript
validate the same positives/negatives without mutating inputs; Python independently
checks the raw source. Four initial source regressions first failed: two absent
ingress/unknown response maps and two success responses that accepted a draft.
All four pass after the source correction.

Synthetic loopback consumers exercise each POST, with and without a key. The keyed
sequence is unknown POST → unavailable fixed GET → explicit original POST replay;
the server records byte-identical path/body/key on replay. The keyless sequence
sends one POST only. This responder returns contract fixtures and has no database,
publisher or trusted reports; it proves transport/consumer behavior only.

Actual trusted report resolution, publication/activation CAS and persistent original
outcomes need their own narrow ports, storage, migration/role review and real
isolated database concurrency/fault evidence. Runtime registration, public published
registry, complete parameter resolution and real qualification follow separately.
No production grant, DDL, deployment, publication or pointer switch occurs here;
A00/A02 and cross-team Gates remain open.
