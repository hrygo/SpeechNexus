# Immutable profile drafts

This A02 unit implements a process-local configuration port, not HTTP routes,
publication, activation, public profile listing, default selection, qualification
or historical Job changes. C consumes the public contracts; it does not write
these speech-owned tables. A published profile requires separate real validation
and lifecycle work.

The source is `createProfileRevision` in `contracts/admin-openapi.yaml` and
`contracts/scenario-profile.schema.json` (schema version 1.0.0). Python
`speechnexus_contracts.admin.parse("ProfileRevisionCreateRequest", value)` and
`speechnexus_contracts.scenario_profiles.parse("ScenarioProfileRevision", value)`
have matching TypeScript exports `@speechnexus/contracts/admin` and
`@speechnexus/contracts/scenario-profiles`. Both are generated from the sources
through `contracts/bindings.json`; generated TypedDict/static types do not replace
runtime validation. The existing pinned generators and static projection revision
are unchanged; the generator now supports a single typed entrypoint without an
invalid solitary overload. Shared positive/negative fixtures live in
`tests/fixtures/contracts/consumer-profiles.json`.

## Process-local use

`prepare_profile_draft(request, idempotency_key)` validates the source request and
the complete assembled draft, then enforces source-described cross-field producer
rules: override range membership/order, unique capability names and constraint
bounds, and ASR segment/preview ordering. Generic Python/TS schema parsers retain
the original schema acceptance behavior; producer comparisons are additional.

It returns a private immutable `CreateProfileDraft`. Retain that exact command
for `PostgresProfileDrafts(pool).create(command)` and `.recover(command)`;
`get(profile_id, revision)` reads a fixed revision. `draft_to_wire(record)`
projects the source-validated draft, with null publication timestamps and no
validation evidence. No public response exposes the private command identity.

Intent bytes use RFC 8785 canonical JSON, including profile ID, expected revision
and every explicitly supplied definition field. Object ordering and equivalent
JSON numbers do not change the digest; array ordering and omission versus null
remain explicit intent. No defaults are inserted. Prepared/stored documents are
bounded to 256 KiB; invalid/noncanonical/nonfinite/deep documents fail safely.
Revision strings retain the full contract range (1 through 9999999999999999999)
without BIGINT or JavaScript-number narrowing. Exhaustion leaves state unchanged
and unavailable; it does not wrap or invent a new public error code.

The optional Idempotency-Key uses the source's 8–128 character limit. Its SHA256
identity is scoped to this operation in the shared deployment, including conflicts
across families; the raw key is not stored. Without a supplied key, preparation
generates a fresh private identity. Retaining that command supports recovery of
the attempt; a subsequent keyless preparation is a new intent identity and has
no original-key replay promise.

## Atomic allocation and immutable history

Append-only migration `0009_profile_drafts` follows `0008_runtime_delivery` and
adds three A-owned configuration tables. These are outside the existing eighteen
coordination ports/table matrix:

| Table | Mutable projection | Immutable facts |
| --- | --- | --- |
| speech_profile_families | latest allocated revision | family identity |
| speech_profile_revisions | none | definition/canonical digest, request digest, creation time and fixed revision |
| speech_profile_commands | pending → created/rejected in the same transaction | key identity and full original canonical intent/digest |

The lock order is command key, then one family allocation row. A family with a
null allocation head is an internal lock anchor, not a published family or default
pointer. `expected_latest_revision=null` can allocate revision 1 only when no
revision has been allocated; subsequent creates require the exact latest revision.
One transaction writes the revision, advances that allocation head, and fixes the
command result. No engine, object-storage, network, Job or outbox work occurs.

The original-key result is checked before CAS, so replay of revision 1 still
returns revision 1 after another command creates revision 2. A mismatching intent
returns `idempotency_conflict` without rewriting the original command. CAS rejection
is durably recorded as `configuration_changed`; the same key cannot later become
successful when its expected revision happens to become current. No committed
pending command is produced by the port.

Commit uncertainty, pool pressure, cancellation, deadline expiry or corrupt
stored facts do not prove absence: create/recover return private outcome `unknown`
with `query_original`. Recovery is read-only and cannot allocate a revision or
even a missing command. Unavailable fixed reads raise a generic private exception,
distinct from a confirmed missing revision. The PG adapter has no automatic write
retry. The test-only InnoDB oracle retries only native 1213 confirmed transaction
rollback, with the same original command; it never retries a lost COMMIT reply
or turns a failed query into permission to write.

## Migration, permissions and evidence

`profile_draft_grants` is an explicit provisioning declaration. Application startup
does not grant roles or run DDL. Only fixture roles receive it in these tests:
SELECT/INSERT for the three tables and UPDATE of allocation/result columns. No
UPDATE of original intent/revision content, DELETE, DDL or existing Job/policy
permissions are added. Production role provisioning and HTTP composition remain
unimplemented deployment/integration work.

Migration failures roll back the new DDL and Alembic head while preserving prior
Job history. Repeat upgrade is stable. Automatic downgrade refuses to delete
configuration evidence; a code rollback must quiesce the new writers and retain
the new tables/head, then use reviewed forward recovery. Do not force a version
stamp or drop history to pretend downgrade succeeded.

`tests/integration/test_profile_drafts.py` exercises actual PG and identical SQL
on isolated InnoDB, with restricted non-owner roles: same-key lock waits,
different-key new-family CAS, stable conflicts, post-write rollback, lost real
commit reply, native pool/metadata-lock pressure, fixed historical reads,
corruption, revision exhaustion, and privilege refusal. PG alone verifies product
Alembic upgrade/rollback/refused downgrade. This is database evidence; InnoDB is a
portability oracle, not a product database or physical speech qualification.

## HTTP contract preflight

Admin source version `0.1.0.dev1` freezes the create/fixed-read boundary for the
next HTTP implementation. It does not register routes, open a writer pool, apply
permissions or expose a working service. The independent generated HTTP surface
selects only `createProfileRevision` and `getProfileRevisionForAdministration`.
The existing workspace HTTP surface still selects only workspace reads. TypeScript
reuses the full `admin.generated` types instead of maintaining another DTO copy.

| Request/result | Status and contract behavior |
| --- | --- |
| New or original-key replayed create | 201, complete source revision with publication status fixed to draft |
| Invalid envelope/schema/header or fixed-read path/query | 400; no revision allocation from invalid input |
| Source shared-service credential refusal | Existing 401 envelope |
| Create original-key conflict / allocation CAS rejection | 409, idempotency_conflict / configuration_changed, retryable=false |
| Structurally valid definition violating cross-field producer rules | 422 |
| Create original result unreadable or commit uncertain | 503, profile_result_unknown, retryable=false |
| Fixed revision authoritatively absent | Existing GET 404; POST has no 404 recovery result |
| Fixed revision metadata unreadable | 503, profile_unavailable, retryable=true for this same fixed GET |

An unknown create disables automatic retry. A caller retaining the original
Idempotency-Key can explicitly replay the exact original POST/body/key using the
already frozen idempotency semantics. Original-key lookup precedes allocation;
CAS rejection remains that key's result after the family advances. A new key or
changed intent cannot serve as recovery of the original command.

Without an original key, automatic recovery is not promised. Reading a candidate
revision cannot establish the original command identity. There is no new
Operation, query-by-key endpoint, recovery URI, implicit profile selection or
qualification projection. Create 503 and 409 errors prohibit non-null retry delay;
the profile-specific 409/503 bodies prohibit the generic Operation `nexus` field.
GET unavailability is separate from confirmed absence and from a create outcome.

Python consumers use `speechnexus_contracts.profile_http.contract`,
`validate_request` and `validate_response`. TypeScript consumers use
`@speechnexus/contracts/profile-http`, including `createValidatedClient` with an
explicit byte limit. Both parsers derive their constraints from the same source
operations. The client performs one transport request per explicit call; it does
not retry or construct an Operation query. Existing draft DTO parsing remains in
`admin` / `scenario_profiles`; no profile defaults are added.

`tests/fixtures/contracts/profile-http.json` supplies shared positive/negative
envelopes, including the full decimal revision, input/header/path failures,
draft-only create, valid historical read shapes, stable conflicts, unknown versus
unavailable errors, and refusal of fabricated Operation recovery. Python compares
these against original OpenAPI independently of the generated bundle. Python/TS
transport-consumer fixtures cover keyless uncertainty and explicit original-key
replay. They validate consumers and schemas; they do not implement HTTP persistence
or prove qualification. Actual bounded POST/fixed GET routing and PG/ingress tests
remain a separate integration PR based on this contract.
