# Immutable ModelBinding drafts

The PostgreSQL draft registry from #125 now has an opt-in HTTP POST, fixed
revision GET, retirement and explicit activation/rollback, using the A00/A02
contract preflight from #122. Publication through the operator command selects
the new head; default selection, qualification and model loading remain separate
work. The existing Profile draft provider remains a separate resource.

The authoritative sources are admin OpenAPI `0.1.0.dev4` and
`model-binding.schema.json` (`schema_version: "1.0.0"`). The selected operations
are `createModelBindingDraft`, `getModelBindingRevision`,
`retireModelBindingRevision` and `activatePublishedModelBindingRevision`. The full
admin TypeScript types already existed; the runtime entries use these same sources.

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
types (booleans differ from numbers). Nonstandard reference expansion separates OpenAPI object positions (path items,
operations, responses, request bodies, parameters and headers) from JSON Schema
positions; `const`, `enum`, defaults and examples remain literal JSON data. Literal
or dynamic references that prevent a closed-tree proof cannot use the shortcut.
This proof is limited to nonstandard responses; requests,
other unions, lost guarantees and the original OpenAI comparison policy retain
their existing checks. It is not general JSON Schema inclusion or runtime proof.

## Process-local registry

`prepare_binding_draft(request, idempotency_key)` validates the existing admin
request and the complete draft revision. Full-source conditions, including the
voice-design engine constraint, are checked without duplicating wire schemas.
The returned immutable private command retains complete intent and definition.
No caller-supplied revision ID, CAS field or publication claim is accepted.

`PostgresBindingDrafts(pool).create(command)` appends a revision; `recover(command)`
is read-only original-key recovery; `get(binding_id, revision)` reads that fixed
revision. `draft_to_wire(record)` projects the actual persisted revision ID and
microsecond creation time with publication `draft` and no evidence. Publication
and capacity/artifact reference eligibility are not established by storing a draft.

The optional key retains the source 8–128 character range. Its SHA256 identity is
scoped to this operation in the shared deployment, including across families;
the raw key is not stored. Keyless preparation creates a fresh private identity.
Retaining that private command permits server-side recovery, without adding a
public keyless recovery promise. No public response exposes the private key digest.

## Private canonical documents

Binding private document version 1 is deterministic UTF-8 JSON: object keys are
sorted by Unicode codepoint, arrays retain order, strings receive no Unicode
normalization, boolean values remain distinct from numbers, and finite integral
floats normalize to their exact integer value. Nonintegral finite floats use the
Python JSON representation. Omission stays distinct from explicit fields; no
schema defaults are inserted. Stored bytes must decode and re-encode identically.

Unlike the existing Profile RFC8785 codec, this private codec preserves arbitrary
source-allowed integer counts without float conversion, including beyond
`2^53-1` and the interpreter's default integer/string digit guard. Exact Decimal
conversions handle those integers locally; no global guard is changed. Documents
are bounded to 256 KiB and 64 nesting levels; oversized, nonfinite, invalid Unicode,
non-JSON or noncanonical stored values fail closed. This is a private versioned
storage/digest format, not a new public digest field or client algorithm contract.
Profile's RFC8785 behavior is retained; only pure revision/digest bound helpers are
shared, with its previous module exports preserved.

## Atomic allocation and minimum-role history

Append-only migration `0010_model_binding_drafts` follows 0009. It adds only these
A-owned configuration tables, outside the eighteen shared coordination ports:

| Table | Runtime writes | Authority |
| --- | --- | --- |
| speech_binding_commands | INSERT | Immutable original key, full intent, digest and document version |
| speech_binding_revisions | INSERT | Immutable definition, global revision ID and unique command/result link |
| speech_binding_families | INSERT and UPDATE latest_revision | Internal allocation head, without a publication/default pointer |

The unique command INSERT waits for an earlier same-key transaction. Existing
command/result facts are immutable, so reading them needs no UPDATE grant or
explicit command-row lock. A new command then locks the family row and appends
its next decimal revision. Independent commands both succeed after serialization;
there is no client CAS rejection. The same transaction inserts the original
journal, immutable result and allocation projection. No network, object storage,
engine, Job or outbox work occurs inside it.

`revision_id` is assigned by the registry and unique globally. A unique foreign-key
link binds exactly one revision to the original key, family, request digest and
document version. Runtime permissions cannot retarget a result or mutate original
intent/definition. Only the allocation head is updatable. Fixed reads also verify
the complete original intent, not just a recomputed definition checksum.

Replay lookup precedes allocation, so the original revision and global ID survive
later family revisions and process restart. Different intent under the original
key returns conflict without changing history. An unreadable result, missing
linked result, pool/query failure, cancellation, deadline or uncertain commit
remains unknown with query_original. Recovery never inserts even a missing
command; an orphaned committed journal is an operator-fixture corruption case,
not permission to allocate. Fixed-read unavailability is distinct from absence.

The PG adapter performs no automatic write retry. The test-only InnoDB oracle
retries native 1213 only when it proves complete rollback, retaining the same
command; it does not retry commit uncertainty or unreadable recovery.

`binding_draft_grants` only declares explicit provisioning: SELECT/INSERT on the
three tables and UPDATE of the family head. It grants no command/revision UPDATE,
DELETE, DDL or Job/coordination authority. Application startup performs no grants
or DDL. Downgrade refuses automatic history deletion; code rollback must preserve
the tables and use reviewed forward recovery. Production provisioning is separate.

`tests/integration/test_binding_drafts.py` uses restricted non-owner roles and the
same SQL on actual isolated PG/InnoDB, including observed key/family contention,
immutable global ID collisions, exact large integers and documents above 64 KiB,
real committed writes with discarded replies, pressured reads, query cancellation,
statement deadlines, closed pools, corruption and immutable original-result links.
PG separately verifies failed/repeated migrations and preserved older rows/ACLs.
Synthetic SQL fixtures do not qualify a physical cluster or speech engine.

## Publication evidence and admin projection

Migration `0021_publication_evidence` adds an independent
`speech_binding_publication_evidence` table with a foreign key to the publication
row it qualifies. Evidence is a publication-time fact, so it never rewrites the
immutable revision or `definition_json`. `publication_evidence_grants` gives the
publication writer `SELECT, INSERT`; `binding_admin_reader_grants` gives the
admin GET reader `SELECT` on binding history, publications and evidence; the
public catalogue reader is unchanged and cannot read evidence.

The admin GET uses its own read-only pool
(`SPEECHNEXUS_BINDING_ADMIN_READER_DATABASE_URL`). Without it the fixed GET fails
closed with `503 binding_unavailable` rather than reporting `draft` for a
published revision. A publication whose evidence is missing, whose
`revision_id` disagrees, or whose `capacity_manifest_id` differs from the
revision's `execution_recipe` is treated as unavailable, not as draft.

Historical publications without evidence are backfilled by an explicit,
default-free mapping (`speechnexus-backfill-binding-evidence`); production
preflight must reach `unresolved = 0` before this stage is a production
candidate. See the [backfill and projection guide](../operations/backfill-binding-publication-evidence.md).

While such a historical publication is still unbackfilled, public resolution can
select it from `speech_publications` even though the admin GET returns `503`.
That contradiction is a known transition state; it is not a second source of
truth and is expected to disappear after backfill.

## Published catalogue reader

Migration `0019_publication_reader_view` adds
`speech_published_binding_revisions`, a `security_barrier` view that joins only
publication rows to the revision identity they name. `model_catalog_reader_grants`
gives the public catalogue reader SELECT on `speech_publications`, this view,
`speech_catalog_state` and `speech_publication_retirements`, never on
`speech_binding_revisions`; unpublished definitions therefore stay unreadable
even with the reader credential. Startup rejects any inherited
predefined `pg_*` role, database CREATE or explicit TEMP grant, sequence use,
table/view or column SELECT beyond those four relations, write authority and
SECURITY DEFINER execution. An upgraded deployment must also revoke any old
`speech_binding_revisions` SELECT from the reader; startup fails closed while both
the draft table and the published view are readable. Database PUBLIC TEMP remains
a separate deployment audit as in the coordination baseline.

## Transactional catalogue revision (0022)

`catalog_revision` is a stored public-visibility version, not `COUNT(*)` over
`speech_publications`. Migration `0022_catalog_revision_state` creates the
single-row `speech_catalog_state`, seeds it to the old observable value
`COUNT(*) + 1`, and installs an `AFTER INSERT` trigger on both publications and
retirement tombstones. The trigger increments the state row and advances
`generated_at_us` with `GREATEST`, in the inserting transaction. PostgreSQL row
locking serializes concurrent writers, so two committed visible-set changes
cannot receive the same revision; a rolled-back publish or evidence write also
rolls its increment back. Evidence, lease and other unrelated writes do not
fire either trigger.

Publication rows remain immutable. `speech_publication_retirements` names the
exact `(kind, object_id, revision, revision_id)`, enforced by a composite foreign
key, and hides only that revision. Public catalogue and Job resolution do not
fall back to an older revision after a retirement. The write port takes a transaction-scoped advisory lock keyed by the
catalogue object before checking that the target is current, so a concurrent
newer publication cannot make the tombstone stale between the check and insert. Repeating the same retirement conflicts without advancing the
version again.

The public DTO shape is unchanged. A writer may read the state row and receives
`UPDATE` only on `revision, generated_at_us`; it cannot rewrite any other column. The public reader receives SELECT-only access to the state and
retirement tables. Downgrade refuses when tombstone history exists or when the
stored version cannot be reconstructed as the legacy count, rather than silently
moving historical ETags backwards.

## Selected head and lifecycle (0023)

Migration `0023_publication_lifecycle` adds the mutable
`speech_publication_heads` pointer, seeds it from each kind/object's newest
visible publication, and adds the append-only
`speech_publication_lifecycle_events` audit/idempotency table. Publications and
their revision documents remain immutable; a head names the exact
`(kind, object_id, revision, revision_id)` selected for new work.

The supported binding operations are now
`POST .../revisions/{revision}/retirements` and
`POST .../revisions/{revision}/activations`. Retirement inserts a permanent
tombstone and clears the head without deleting the publication, evidence,
revision or historical Jobs. Activation is the rollback path: it selects an
already-published, non-retired revision with an explicit
`expected_current_revision` compare-and-swap. `null` is accepted only after
retirement cleared the head. A retired revision cannot itself be reactivated;
roll back to another surviving published revision instead.

Both operations use one short transaction, a transaction-scoped advisory lock
keyed by the catalogue object and an idempotency-key advisory lock. The head and
`speech_catalog_state` advance in that same transaction: retirement advances via
the tombstone trigger, while head changes advance explicitly. A changed head is
therefore visible to the next catalogue/Job read and changes ETag; no read path
falls back to an older publication. In-flight Jobs keep the effective
configuration persisted at admission and never re-read the head.

Every successful first execution appends an event with the binding/revision,
result status, request ID, actor type/label, policy revision, intent digest,
optional idempotency digest and `change_summary_json` containing the operator
reason. Replaying the same key and complete intent returns the original event;
a changed intent under that key is `409 idempotency_conflict`. See the
[binding lifecycle operations guide](../operations/binding-lifecycle.md).

## Opt-in HTTP writer

Configure `SPEECHNEXUS_BINDING_WRITER_DATABASE_URL` explicitly for an API process
using a separate non-owner role with only `binding_draft_grants`. A blank or
absent value leaves the writer disabled and the selected endpoints unavailable;
it does not fall back to the general database identity. Startup verifies both
session and effective identities, table ownership, other speech-table authority
and exact mutable columns before exposing the provider. It never provisions
roles, grants or migrations. Failed startup and shutdown close the owned pool
and remove its diagnostic filter. Profile and Binding roles and pools stay separate.

Configure `SPEECHNEXUS_BINDING_LIFECYCLE_DATABASE_URL` with another explicit
non-owner role provisioned by `binding_lifecycle_grants`: append-only publication
and retirement rows, append-only lifecycle events, a mutable head, SELECT on the
binding revision/evidence history, and column-level UPDATE on the catalogue
version. Startup rejects owner/schema-creation authority, sequence access, other
speech-table grants and any broader mutable-column set. A blank value keeps the
lifecycle routes at a fail-closed `503` and never falls back to the draft writer.

Binding receive byte, concurrency, receive time and transaction time settings
are independent `SPEECHNEXUS_BINDING_*` configuration. Host/Origin uses the shared
explicit allowlist. Profile and Binding reuse the same bounded private receive
implementation; their routes, source validators and outcomes remain independent.
A complete request keeps its receive permit until its handler ends even if the
client cancels. This permit is not a global lease or compute budget.

Request JSON rejects duplicate keys and nonfinite values. Integer parsing and
response encoding preserve source-allowed large integers without changing the
interpreter guard or converting them to floats; Python actual-HTTP tests include
a 5001-digit integer. JavaScript clients retain their native numeric range; the
actual same-source TypeScript HTTP probe uses representable fixture values.
The response encoder has a 1 MiB bound including server metadata, independent
of the 256 KiB private document bound. No public default parameters are added.

Transaction deadlines are cooperative checks and SQL statement timeouts, not a
hard network interruption of COMMIT. A successful but delayed COMMIT may return
503 unknown; only the original complete intent/key can resolve it. Real PG tests
cover delay before/after commit, same-key concurrent different intents, original
writer rollback, lost acknowledgment, pressure, client cancellation and restart.
The served OpenAPI projects the four selected draft/lifecycle methods. Same-source
TypeScript runs against an actual loopback HTTP server with the minimum-role PG
writer; CI installs locked Node tools in every partition containing that probe.

## Remaining implementation

Publication of an already-stored revision now has an explicit operator command
(`speechnexus-publish-binding`, see the [operations guide](../operations/publish-binding-revision.md)).
It is not an HTTP route and does not implement the frozen
`publishModelBindingRevision` endpoint. The command requires both evidence
identities and writes `speech_publications` plus
`speech_binding_publication_evidence` in one transaction. The fixed GET projects
that stored state: `draft`, `published`, or `503 binding_unavailable` when a
publication has no matching evidence. Binding retirement, activation/rollback,
head selection and append-only audit events are implemented through the separate
lifecycle writer described above. Default parameters and full qualification are
not implemented here. Voice retirement still has only a frozen declaration and no
production writer/handler; voice activation has no declaration yet. Profile
lifecycle remains its separate intent/provider surface.

Model parameter domains still do not carry defaults. This unit neither adds that
field nor fabricates a published registry. Full effective parameter resolution,
resource qualification, real speech execution and A02/Gate acceptance remain open.
B #123/#124 source/artifacts and all existing consumer namespaces are preserved.

A standalone [default snapshot contract](binding-parameter-defaults.md) now
provides a source-defined declaration format for the remaining parameter-resolution
work. It does not attach defaults to existing revisions, add ModelBinding wire
fields, write registry data or establish a published/default configuration.

A separate [default producer profile](binding-defaults-producer.md) defines the
producer input and fixed snapshot lookup. It references the canonical fields above
and leaves this implemented `binding` surface and its negative-input checks
unchanged. The in-process atomic port below implements persistence; actual
producer HTTP support is available through the explicit four-table mode described
in that profile. Legacy three-table mode retains this original input/surface.

## Atomic default declarations

`BindingDefaultDrafts` / `PostgresBindingDefaults` persist one complete original
intent, parent revision and at most one default snapshot in the same short
transaction. The independent `prepare_binding_defaults_draft` validates the
frozen producer DTO, canonical parameter fields and declared parent task/domain
relationships. It neither infers values nor resolves external resource references
or engine qualification. The existing HTTP preparer continues rejecting the new
field. No new route or operational OpenAPI capability is enabled here.

Omission stores no snapshot; explicit `{}` stores one. Presence and values belong
to the original intent digest, so omission ↔ `{}` and changed values conflict under
the original key. Parameters stay outside the legacy ModelBinding response shape.
The legacy create/recover adapter rejects commands carrying declarations before
writing; callers must use the atomic port. Legacy parent GET remains readable.

Append-only `0011_binding_parameter_defaults` follows 0010. A nullable
`defaults_declared` command marker preserves old rows as NULL, while new atomic
commands record presence explicitly. `speech_binding_defaults` has unique parent
and global snapshot identities, private canonical JSON v1, parameter and record
digests, and a compound foreign key fixing the parent family/revision/global ID,
original key/request digest, document version and creation time. History is never
backfilled. The snapshot uses the accepted parent's creation time.

The new provisioning declaration `binding_defaults_grants` extends the original
three-table writer with SELECT/INSERT on the immutable snapshot table. It does
not execute grants, change the existing writer's lifecycle, or broaden startup
permissions. Runtime UPDATE/DELETE and coordination access remain denied. The
explicit producer HTTP mode validates its controlled four-table lifecycle;
the legacy three-table runtime still rejects snapshot privileges.

Creation and recovery return the original parent/snapshot identities. Recovery is
read-only and never allocates IDs. A lost or late committed reply, cancellation,
pool pressure or unreadable original result remains `unknown/query_original`.
Deadlines are cooperative bounds, not hard network interrupts. A declared but
missing/corrupt snapshot or inconsistent marker is unavailable, never a fabricated
empty declaration. Fixed reads return None only for an authoritative absent parent
or declaration. The record digest detects changed snapshot identity/metadata as
well as parameter corruption; it is not a security signature against a DB owner.

Migration rollback first locks commands → revisions → snapshots in ACCESS
EXCLUSIVE mode, matching writer order and holding the locks through both counts
and DDL. Downgrade requires READ COMMITTED; it rejects repeatable/serializable
snapshots that may predate a writer's commit while acquiring these locks.
An earlier writer must finish before the check; a later atomic writer
cannot succeed through the removal window. Rollback is allowed only when no new
snapshot or declared original command remains; a declared journal also blocks
rollback after child corruption.
Failure rolls back new DDL and preserves older facts/ACLs. Isolated PostgreSQL is
the product authority; the test-only InnoDB adapter executes the same transaction
SQL solely to probe portability. Tests cover real lock waits, original rollback,
commit reply loss/read pressure, cancellation, delayed committed replies, partial
rollback, parent FK, permission refusal and migration preservation.

The opt-in [producer HTTP mode](binding-defaults-producer.md) now consumes this
port. Published/default registry, merged parameter resolution and actual
engine/quality evidence remain separate acceptance. This port neither publishes
nor activates revisions and does not rewrite historical Jobs.
