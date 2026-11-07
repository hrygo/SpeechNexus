# Immutable pronunciation drafts

This A02 component creates and reads immutable pronunciation draft revisions. The
private writer and public collection routes do not publish a dictionary, select a
default, execute TTS or establish voice/engine qualification. The generated
`pronunciation_sets` surface serves `GET`/`POST /v1/nexus/pronunciation-sets`,
which owns keyset listing and draft creation; publication, default selection and
TTS consumption remain separate work (#520).

The [fixed-revision read contract](pronunciation-read-contract.md) now provides
an opt-in generated GET validator and an independently owned read-only API
runtime, distinguishing unavailable metadata from confirmed absence. This does
not change this private writer's scope or expose its create/recover methods.

The canonical request is `PronunciationCreateRequest` in
`contracts/speech-openapi.yaml`; the response is `PronunciationRevision` in
`contracts/voice-resource.schema.json`. Both runtime entrypoints are generated
from `contracts/bindings.json`: the revision uses a reference to its actual
external source, not a copied wire definition. Python `speechnexus_contracts.parse`
and TypeScript `@speechnexus/contracts` expose the same two names. The collection
operations are bound by a separate `pronunciation_sets` entry in
`contracts/bindings.json`; the shared component and canonical-source references
are unchanged.

The contract declares `publish` with default `false` and draft semantics. The
public POST materializes an omitted field as `false`, rejects `publish: true`
with `422 pronunciation_publication_not_supported` before SQL, and always returns
`status: draft`. The private `prepare_pronunciation_draft` port accepts only an
**explicit `publish: false`**, so an unsupported publication intent cannot reach
the immutable writer.

## Collection read and draft create

`list_sets(limit, after)` reads the family head joined to its immutable revision
and never parses `definition_json` per row. Ordering is ascending opaque id;
`after=None` selects from the start, and the adapter fetches `limit + 1` rows so
`has_more` is proven by an extra row rather than an estimate. `next_cursor` is the
unpadded urlsafe-base64 form of the last returned id. The route rejects a cursor
whose decoded id does not re-encode to the same bytes, so padding or alphabet
tampering cannot silently shift the page. `limit` is 1–100 (default 50); duplicate,
unknown or out-of-range query parameters, a GET body, ambiguous request ids and an
over-limit create body are `400 pronunciation_invalid_request`. An empty
collection is a successful `200` with `data: []`.

`POST` returns 201 for a created or replayed command and 409 for an idempotency
conflict (`pronunciation_idempotency_conflict`), a stale append
(`pronunciation_configuration_changed`) or a keyless uncertain outcome
(`pronunciation_outcome_unknown`). Only a request carrying its original
`Idempotency-Key` maps an unknown outcome to the retryable 503, because replaying
that key recovers the committed revision; a keyless retry is a new intent and is
deliberately not retryable. Both operations accept the single trailing slash via
Starlette's 307 redirect and enforce the shared Host/Origin entry boundary before
the handler runs.

The writer identity is opt-in and API-only: set
`SPEECHNEXUS_PRONUNCIATION_WRITER_DATABASE_URL`, with
`SPEECHNEXUS_PRONUNCIATION_WRITE_CONCURRENCY` (default 4),
`SPEECHNEXUS_PRONUNCIATION_TRANSACTION_TIMEOUT_SECONDS` (default 3),
`SPEECHNEXUS_PRONUNCIATION_RECEIVE_MAX_BYTES` (default 32 MiB) and
`SPEECHNEXUS_PRONUNCIATION_RECEIVE_TIMEOUT_SECONDS` (default 30). Startup verifies
SELECT/INSERT on exactly the three A-owned tables, UPDATE on
`families.latest_revision`, UPDATE on only
`commands.command_state/pronunciation_id/revision/error_code`, and no table-level
UPDATE or privilege on any other speech table. The reader identity now needs
SELECT on both `speech_pronunciation_revisions` and
`speech_pronunciation_families`; any broader grant is rejected.

## Fixed intent and original identity

Prepare the full original request with `prepare_pronunciation_draft(request, key)`;
retain the resulting immutable command for `PostgresPronunciationDrafts(pool)`'s
`create` and `recover`. `get(id, revision)` reads one fixed revision, while
`draft_to_wire(record)` produces a source-validated draft without private command
keys or digests. The pool must be explicitly supplied; the component does not
create roles, grant permissions or run migrations at startup.

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
allocation head and fixes the original result. An append rechecks its actual
immutable parent. A null family head is an internal lock anchor, not a published
resource or default pointer. CAS rejection is persisted: the same key cannot turn
successful later when its formerly future expected revision becomes current.
Replaying revision 1 after revision 2 was allocated still returns revision 1.
The complete 19-digit revision range is retained without integer-column or
JavaScript-number narrowing; exhaustion remains unavailable without allocation.

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
The declared writer grants only SELECT/INSERT, allocation-head UPDATE and
pending-result columns; it cannot rewrite definition/original intent, delete
history, create tables or write Job/outbox/budget facts. Provisioning declarations
are applied only to synthetic isolated roles by these tests, not to production.

Downgrade requires READ COMMITTED, obtains table locks in writer order before any
emptiness check and holds them through DDL. Any family, command or revision
history refuses downgrade. Two-connection tests cover writer-first commit and
migration-first exclusion. The test suite also checks that a failed upgrade
rolls back new DDL and retains previous history.

`tests/integration/test_pronunciation_drafts.py` contains tests for the PG adapter and
same SQL through a test-only InnoDB oracle, original-key concurrency, CAS,
rollback, real committed/reply-lost recovery, native query cancellation, minimum
permissions, maximum entry count, revision bounds and migration interleavings.
Actual Node consumes records produced by both databases; shard prerequisites
select its locked dependencies. The oracle's only automatic retry is native
InnoDB 1213 confirmed rollback for writes, never uncertain COMMIT or reads.
Synthetic media text, shared SQL and connection termination do not prove real
phonetic rendering, physical multi-host failure, public HTTP or product/Gate
qualification. Test definitions alone do not establish a current passing run;
execution results must be reported for the environment and revision tested.
