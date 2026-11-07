# Immutable pronunciation revisions and atomic publication

This A02 component creates, reads and publishes immutable pronunciation
revisions. Publishing writes the revision, its append-only
`speech_publications` row and the selected `speech_publication_heads` row in one
database transaction; it never selects a runtime default, executes TTS or
establishes voice/engine qualification. The generated `pronunciation_sets`
surface serves `GET`/`POST /v1/nexus/pronunciation-sets`, which owns keyset
listing, draft creation and atomic publication through the one POST command.

The [fixed-revision read contract](pronunciation-read-contract.md) provides an
opt-in generated GET validator and an independently owned read-only API runtime,
distinguishing unavailable metadata from confirmed absence. The private writer
port's `create`/`recover` methods are not exposed as HTTP operations.

The canonical request is `PronunciationCreateRequest` in
`contracts/speech-openapi.yaml`; the response is `PronunciationRevision` in
`contracts/voice-resource.schema.json`. Both runtime entrypoints are generated
from `contracts/bindings.json`: the revision uses a reference to its actual
external source, not a copied wire definition. Python `speechnexus_contracts.parse`
and TypeScript `@speechnexus/contracts` expose the same two names. The collection
operations are bound by a separate `pronunciation_sets` entry in
`contracts/bindings.json`; the shared component and canonical-source references
are unchanged.

`publish` defaults to `true`. An omitted field and an explicit `true` both select
publication; `false` writes a private draft. `prepare_pronunciation_draft`
materializes the omitted default into the canonical intent, so a keyed replay can
never differ from its original request by an injected field. The port accepts
only a real boolean and rejects any other value before SQL.

## Atomic publication

On a publish request the adapter locks the command key and then the family row,
performs the allocation-head CAS, inserts the immutable revision and advances
`families.latest_revision`. In that same transaction it appends
`speech_publications(kind='pronunciation', object_id, revision, revision_id,
published_at_us)`, upserts the selected head, and lets the existing
`AFTER INSERT` trigger advance `speech_catalog_state`. Any failure rolls back the
command row, the revision, the family pointer, the ledger row and the head
together. A `503 pronunciation_unavailable` therefore never means "published but
missing a revision" or "revision marked published without a ledger row"; retrying
the same key starts from no persisted command.

`revision_id` is deterministic: `prv-` plus the SHA-256 of
`pronunciation-revision\0<id>\0<revision>`. Republication of the same revision
is not a new ledger row. Exactly one concurrent publisher wins the family CAS;
the loser receives `409 pronunciation_configuration_changed` and writes nothing.

The route selects the publication identity, not the draft writer, for
`publish: true`. A deployment with no publication identity answers
`422 pronunciation_publication_not_supported` before any SQL. The pre-existing
stable error code is retained; its meaning is now "this deployment has not
enabled the capability", not "this operation can never implement it". Sending
`publish: false` remains a draft there.

## Collection read and revision status

`list_sets(limit, after)` reads the family head joined to its immutable revision
and never parses `definition_json` per row. It computes `status` from an `EXISTS`
lookup in `speech_publications` for exactly `(id, head_revision)`: `published`
only when that row exists, otherwise `draft`. A draft appended above a published
revision therefore lists with the new revision and `draft`; the older published
revision keeps its own published fact and its fixed-revision GET still returns
`published`. `retired` remains reserved for a later retirement command and is not
produced here.

Ordering is ascending opaque id; `after=None` selects from the start, and the
adapter fetches `limit + 1` rows so `has_more` is proven by an extra row rather
than an estimate. `next_cursor` is the unpadded urlsafe-base64 form of the last
returned id. The route rejects a cursor whose decoded id does not re-encode to the
same bytes, so padding or alphabet tampering cannot silently shift the page.
`limit` is 1–100 (default 50); duplicate, unknown or out-of-range query
parameters, a GET body, ambiguous request ids and an over-limit create body are
`400 pronunciation_invalid_request`. An empty collection is a successful `200`
with `data: []`.

`POST` returns 201 when it creates the revision and 200 for a keyed replay of the
same canonical intent. It returns 409 for an idempotency conflict
(`pronunciation_idempotency_conflict`), a stale append
(`pronunciation_configuration_changed`) or a keyless uncertain outcome
(`pronunciation_outcome_unknown`). The retryable 503 has exactly three declared
causes: the port reports or raises `unavailable` (including a stored history it
cannot decode), a keyed submission has an unknown outcome (replaying that key
recovers the committed revision), or the route has neither a writer nor a
publisher configured. Every deterministic failure — a port result outside the
declared closed set, a `created` result without a draft, or a reply that fails its
own contract — answers 500 `pronunciation_contract_violation` with
`X-Should-Retry: false`, so an implementation defect is never dressed up as an
outage. Both operations accept the single trailing slash via Starlette's 307
redirect and enforce the shared Host/Origin entry boundary before the handler
runs.

## Identities, grants and startup verification

Three separate opt-in API identities are provisioned from declarations; startup
DDL never runs.

- **Reader** (`SPEECHNEXUS_PRONUNCIATION_READER_DATABASE_URL`) may only SELECT
  `speech_pronunciation_families`, `speech_pronunciation_revisions` and
  `speech_publications`. Startup rejects any write, table/column `REFERENCES`,
  table `TRIGGER` or privilege on any other relation. The publication SELECT is
  what makes `status` computable; it does not grant insertion.
- **Draft writer** (`SPEECHNEXUS_PRONUNCIATION_WRITER_DATABASE_URL`) keeps the
  narrow draft authority: SELECT/INSERT on the three A-owned tables, UPDATE on
  `families.latest_revision`, and UPDATE on only
  `commands.command_state/pronunciation_id/revision/error_code`. It holds no
  ledger INSERT and cannot publish.
- **Publisher** (`SPEECHNEXUS_PRONUNCIATION_PUBLISHER_DATABASE_URL`) is the
  minimum union needed for the atomic transaction: the draft writer's three-table
  reads/writes plus SELECT/INSERT on `speech_publications` and
  `speech_publication_heads`, UPDATE on only
  `heads.revision/revision_id/updated_at_us`, and SELECT plus UPDATE on only
  `catalog_state.revision/generated_at_us` for the ledger trigger. It never
  receives DELETE, table-level UPDATE, `REFERENCES`, `TRIGGER` or INSERT on the
  catalogue counter, and `speech_publications` stays INSERT-only.

Startup checks the actual role, not the DSN name, and compares column
privileges explicitly because `has_table_privilege` stays false under
column-level grants (#549). The reader and publisher each reject the shared
DELETE-bearing `publication_grants`; the publisher uses only
`pronunciation_publication_grants`. `SPEECHNEXUS_PRONUNCIATION_WRITE_CONCURRENCY`
(default 4), `SPEECHNEXUS_PRONUNCIATION_TRANSACTION_TIMEOUT_SECONDS` (default 3),
`SPEECHNEXUS_PRONUNCIATION_RECEIVE_MAX_BYTES` (default 32 MiB) and
`SPEECHNEXUS_PRONUNCIATION_RECEIVE_TIMEOUT_SECONDS` (default 30) bound both write
identities.

Upgrade order: apply the migration first, create the publisher role and its
grants, set the publisher URL, then restart the API. The same deployment may set
only the writer URL; publication then answers the stable 422 while drafts keep
working. A publisher-only deployment also serves explicit draft requests because
its grants are a superset of the draft writer's.

## Fixed intent and original identity

Prepare the full original request with `prepare_pronunciation_draft(request, key)`;
retain the resulting immutable command for `PostgresPronunciationDrafts(pool)`'s
`create` and `recover`. `get(id, revision)` reads one fixed revision;
`draft_to_wire(record)` produces a source-validated response whose `status` comes
from the record's publication fact. The pool must be explicitly supplied; the
component does not create roles, grant permissions or run migrations at startup.

RFC 8785 bytes retain entry order, duplicate graphemes, language omission and
Unicode material. No phonetic parser, normalization, deduplication or precedence
rule is invented for pinyin/IPA strings. Public semantic execution support needs
its own producer/consumer work. The 32 MiB private document bound covers the
source's 10,000 entries, including maximum string lengths and JSON escapes; the
256 KiB Profile codec is deliberately not reused to reject valid dictionaries.

A supplied `id` and `expected_revision` must be paired. An omitted ID stays
omitted in canonical intent. A new opaque ID is allocated **after** winning the
persistent command key; preparing/replaying the same original request never
introduces a fresh random ID into its digest. Keyless preparation creates a new
private identity; retaining that exact command supports recovery, while another
keyless call has no replay promise. Raw idempotency keys are not stored.

One short database transaction locks the command key, then one family. It checks
the original result before CAS, allocates/appends the revision, advances only the
allocation head, appends the publication facts when requested and fixes the
original result. An append rechecks its actual immutable parent. A null family
head is an internal lock anchor, not a published resource or default pointer. CAS
rejection is persisted: the same key cannot turn successful later when its
formerly future expected revision becomes current. Replaying revision 1 after
revision 2 was allocated still returns revision 1. The complete 19-digit revision
range is retained without integer-column or JavaScript-number narrowing;
exhaustion remains unavailable without allocation.

Each immutable revision also binds its command key. A unique key and composite
foreign key prevent a mutable command result from being redirected to another
identical auto-create intent's revision. Fixed reads recheck the definition digest
and canonical bytes; recovery also checks the original intent digest and canonical
bytes against the supplied command. Unreadable/corrupt history, acquisition
pressure, native query cancellation or uncertain COMMIT return private
`unknown/query_original`; none proves absence or permits a replacement identity.
Recovery is read-only, including when no committed command is yet visible.
Confirmed missing fixed reads return `None`; unavailable reads raise the generic
private unavailable exception. Database deadlines are cooperative statement/pool
limits, not a hard network-interruption guarantee. There is no automatic write
retry in the production adapter.

## Migration and evidence boundary

Append-only `0014_pronunciation_drafts` follows `0013_profile_lifecycle_intents`,
and `0045_pronunciation_set_summary` adds the derived `encoding` and `entry_count`
columns the list reads. `0045` backfills them from `definition_json`, refuses to
continue when a historical row is not representable, and then requires NOT NULL
plus the declared bounds; its downgrade drops only those recomputable columns.
It adds A-owned `speech_pronunciation_families`, `speech_pronunciation_revisions`
and `speech_pronunciation_commands`, outside the eighteen coordination ports.
Publication reuses the existing `speech_publications`,
`speech_publication_heads` and `speech_catalog_state` tables and trigger; no new
migration is required. The declared writer grants only SELECT/INSERT,
allocation-head UPDATE and pending-result columns; the publisher adds only the
atomic-publication columns. Neither can rewrite definition/original intent,
delete history, create tables or write Job/outbox/budget facts. Provisioning
declarations are applied only to synthetic isolated roles by these tests, not to
production.

Downgrade requires READ COMMITTED, obtains table locks in writer order before any
emptiness check and holds them through DDL. Any family, command or revision
history refuses downgrade. Two-connection tests cover writer-first commit and
migration-first exclusion. The test suite also checks that a failed upgrade
rolls back new DDL and retains previous history.

`tests/integration/test_pronunciation_sets_pg.py` exercises real PostgreSQL
publication success, forced rollback (a failing head trigger leaves no command,
revision, ledger or head), keyed replay (200, one ledger row), concurrent double
publish (exactly one 201 and one 409), list/status semantics and the narrow
reader/publisher role checks including DELETE-bearing and extra-column grants.
`tests/integration/test_pronunciation_drafts.py` covers the PG adapter and the
same SQL through a test-only InnoDB oracle, original-key concurrency, CAS,
rollback, real committed/reply-lost recovery, native query cancellation, minimum
permissions, maximum entry count, revision bounds and migration interleavings.
Actual Node consumes records produced by both databases. Synthetic media text,
shared SQL and connection termination do not prove real phonetic rendering,
physical multi-host failure, public HTTP or product/Gate qualification. Test
definitions alone do not establish a current passing run; execution results must
be reported for the environment and revision tested.
