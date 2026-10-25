# Profile publication and activation contract preflight

The later [P2a private intent journal](profile-lifecycle-intents.md) persists original
commands only. It adds no publisher, pointer or public outcome; the P1a authority
and remaining lifecycle boundaries described here continue to apply.

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

## P1a: private intent and read-only evidence boundary

| Fact | Current source / owner | Consumer boundary |
| --- | --- | --- |
| Fixed profile definition | A02 `speech_profile_revisions` / 0009 | Real immutable draft reads exist; allocation head is not a published/current pointer |
| Submitted ProfileValidation | Existing admin request and scenario schema 1.0.0 / caller | A claim retained in the original intent, not trusted evidence |
| Trusted Profile-specific checks | A02 producer with applicable B deployment facts | No production producer/adapter is registered yet; remain unavailable |
| Binding/capacity/software/topology/quality facts | Existing contracts and B's named evidence; A registry integration | Must be resolved and matched where applicable; cannot substitute a binding report for Profile validation |
| Published state/current pointer | Later A02 P2 transaction | Not implemented by this command or observation boundary |

The process-local boundary now prepares complete publication/activation intents:
`speech.interfaces.profile_lifecycle.prepare_profile_publication` and
`prepare_profile_activation`. They use the generated `profile_lifecycle_http`
request validator and freeze the operation, profile/revision and full body in a
bounded canonical document. Publication's expected revision and validation target
must match the path. No defaults, current/latest lookup, qualification, publication
timestamp or public Operation are manufactured. Input dictionaries remain unchanged.

The private command retains a hash of the supplied key, including complete intent
differences such as explicit null, CAS, validation metadata and target. Storage
must keep the operation context and compare the complete intent, not identify a
mutation by the key hash alone. A keyless preparation receives a fresh private
identity; another keyless preparation is a new command, with no public recovery
promise. Original body/key text is excluded from the command's representation.

`speech.application.profile_lifecycle` checks the private envelope and provides
the narrow `ProfilePublicationVerifier` port. A real verifier must resolve trusted
current Profile-specific evidence and check full schema/producer rules, capability
composition and applicable tier/quality facts. A binding's
`DeploymentQualificationReport` is a different fact and cannot stand in for this
Profile validation. No trusted production verifier is implemented or registered
by this unit; `UnavailableProfilePublicationVerifier` always reports unavailable.

`observe_publication_evidence` accepts only a structured observation bound to the
exact profile/revision, definition digest and complete intent digest. Empty,
boolean, wrong-target or wrong-digest results, failed reads and malformed private
facts remain unavailable. An explicit authoritative denial is distinct from an
unreadable result. Errors contain fixed text rather than provider/private input.
Activation cannot use this draft evidence path; missing published lifecycle facts
remain unavailable, not an authoritative ineligibility decision. Those facts
belong to the later writer.

The observation and its authority digest are neither a signed approval nor a
permission to commit. P2 still needs original-result lookup before provider/CAS,
transactional authority rechecking, persistent result recovery and pointer updates.
This unit has no database, migration, grants, new public wire fields, route
registration or lifecycle writes. Synthetic test providers exercise the consumer
boundary only; P1's real authority producer and the rest of A-CONFIG-01 remain open.
