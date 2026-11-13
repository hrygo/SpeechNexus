# Profile publication and activation contract preflight

The later [P2a private intent journal](profile-lifecycle-intents.md) persists original
commands only. It adds no publisher, pointer or public outcome; the P1a authority
and remaining lifecycle boundaries described here continue to apply.

This A00/A02 unit originally supplied consumer contracts only. The product now
registers the publication, activation and retirement POSTs, the two public lists
and the fixed revision GET as well; the current implementation and qualification
boundary are described in [Profile drafts](profile-drafts.md). No trusted
validation-report provider, default pointer, historical Job change or engine
qualification is implemented here.

## Source and entrypoints

This preflight originally targeted `contracts/admin-openapi.yaml` `0.1.0.dev4`;
the current source version is declared in the file itself. The existing
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

The separate `profile_lifecycle` HTTP surface selects `publishProfileRevision`,
`activatePublishedProfileRevision` and `retireProfileRevision`. Existing
`profile-http` remains draft create/fixed-get only; packaging the surface does
not register it in the product's operational OpenAPI. There is no arbitrary
current/latest, Operation or query-by-key route in this unit. Retirement accepts
`mode=retire|disable` plus an optional bounded `reason`; it clears the family
current pointer when the target is current, records `retired_at_us` for `retire`
and the required reason for `disable`, and never rolls back automatically.
Generator versions remain
datamodel-code-generator 0.86.0, openapi-typescript 7.13.0 and static projection 1;
dependency locks are unchanged.

## Original intent and uncertain results

All three POST operations retain their existing path, request DTO, optional
Idempotency-Key and original-result replay semantics; publication and activation
also keep the current-pointer CAS. Publication still requires verification of
evidence for the fixed revision; activation still requires an existing published
revision; retirement requires a currently published revision and applies no CAS
because it only removes eligibility. A caller-supplied validation object or
digest cannot establish server authority or production quality.
Cross-field target identity, trusted evidence lookup and current eligibility remain
server-side work; structural parsing cannot prove them.

The contract requires an unreadable original result or uncertain commit to return 503
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

A publication or activation 200 response must be a published revision with
validation evidence and the canonical publication timestamp rules; draft, retired
and disabled responses are rejected. A retirement 200 response is the same fixed
revision carrying `retired` or `disabled` publication facts and the matching
`retired_at`/`disabled_reason`. Existing conflict/missing responses are retained. Ingress declarations
also cover Host/Origin refusal 403, method refusal 405 with required `Allow: POST`,
receive timeout 408, size 413, media 415 and capacity 429. Those describe this
HTTP attempt and do not resolve a prior unknown original-key result. Bounds are
deployment input, not new profile defaults. Retirement is served by the same
ingress, so `retireProfileRevision` declares the same set: `200`/`400`/`401`/
`403`/`404`/`405`/`408`/`409`/`413`/`415`/`422`/`429`/`503`. The paired binding
test reads the source contract and fails when any of them disappears, and the
runtime cases drive a real `GET` and an unconfigured writer through that exit so
the 405 `ProfileCreateMethodNotAllowed` and 503 `ProfileLifecycleUnknown` bodies
are validated instead of surfacing as an unvalidated 500.

## Verification and remaining implementation

Shared fixtures provide HTTP and DTO cases. The Python and TypeScript tests are
designed to validate the same positives/negatives without mutating inputs; Python
also checks the raw source independently. A test definition does not establish a
passing run for the current revision or environment.

Synthetic loopback consumer tests exercise each POST, with and without a key. The keyed
sequence is unknown POST → unavailable fixed GET → explicit original POST replay;
the server records byte-identical path/body/key on replay. The keyless sequence
sends one POST only. This responder returns contract fixtures and has no database,
publisher or trusted reports; its scope is transport/consumer behavior only.

Actual trusted report resolution, publication/activation CAS and persistent original
outcomes need their own narrow ports, storage, migration/role review and real
isolated database concurrency/fault evidence. Runtime registration, public published
registry, complete parameter resolution and real qualification follow separately.
No production grant, DDL, deployment, publication or pointer switch occurs here.
Current lifecycle requirements and acceptance are tracked in
[P-PROFILES](../prd.md#p-profiles) and [P-POLICY](../prd.md#p-policy); the A00/A02
work-package names in this document identify historical ownership, not current status.

## P1a: private intent and read-only evidence boundary

| Fact | Current source / owner | Consumer boundary |
| --- | --- | --- |
| Fixed profile definition | A02 `speech_profile_revisions` / 0009 | Real immutable draft reads exist; allocation head is not a published/current pointer |
| Submitted ProfileValidation | Existing admin request and scenario schema 1.0.0 / caller | A claim retained in the original intent, not trusted evidence |
| Trusted Profile-specific checks | Deployment-owned reviewed evidence document (`SPEECHNEXUS_PROFILE_PUBLICATION_EVIDENCE_FILE`) | Read at API startup; a missing, unreadable or invalid document fails startup naming the variable, and revisions it does not cover are a durable 422 `profile_not_eligible` |
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
Profile validation. The registered production verifier reads the deployment's
reviewed evidence document at startup and binds each publish to the exact
definition and validation bytes it reviewed; the deployment owns producing and
reviewing that document, and the repository ships no default entry. A
writer-enabled process that cannot read it fails startup naming
`SPEECHNEXUS_PROFILE_PUBLICATION_EVIDENCE_FILE` instead of reporting unavailable.

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
The P1 observation boundary itself has no database, migration, grants, new public
wire fields, route registration or lifecycle writes; the separate P2a journal
has its own storage and migration. Synthetic test providers exercise the consumer
boundary only. The deployment-owned reviewed evidence document is the registered
authority source for this boundary; its review content, the rest of A-CONFIG-01 and
any production quality claim remain open.
