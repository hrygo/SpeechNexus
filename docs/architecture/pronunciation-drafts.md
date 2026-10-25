# Immutable pronunciation drafts

This A02 component creates and reads private, immutable pronunciation draft
revisions. It does not register public routes, publish a dictionary, select a
default, execute TTS or establish voice/engine qualification. Public list/create/
fixed-revision HTTP and the rest of A02/A04 remain separate work.

The [fixed-revision read contract](pronunciation-read-contract.md) now provides
an opt-in generated GET validator and distinguishes unavailable metadata from
confirmed absence. It does not register a public route or change this private
writer's scope.

The canonical request is `PronunciationCreateRequest` in
`contracts/speech-openapi.yaml`; the response is `PronunciationRevision` in
`contracts/voice-resource.schema.json`. Both runtime entrypoints are generated
from `contracts/bindings.json`: the revision uses a reference to its actual
external source, not a copied wire definition. Python `speechnexus_contracts.parse`
and TypeScript `@speechnexus/contracts` expose the same two names. The existing
binding/generator versions and canonical sources are unchanged.

The generic parser preserves the source's published default: omitted `publish`
and explicit `true` remain valid and are not rewritten. The private
`prepare_pronunciation_draft` port selects only **explicit `publish: false`**.
Unsupported publication intent is rejected before SQL. This is a private draft
selection, not a change to the declared public POST semantics.

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
identical auto-create intent's revision. Definition/intent digests and canonical
bytes are rechecked on reads and recovery. Unreadable/corrupt history, acquisition
pressure, native query cancellation or uncertain COMMIT return private
`unknown/query_original`; none proves absence or permits a replacement identity.
Recovery is read-only, including when no committed command is yet visible.
Confirmed missing fixed reads return `None`; unavailable reads raise the generic
private unavailable exception. Database deadlines are cooperative statement/pool
limits, not a hard network-interruption guarantee. There is no automatic write
retry in the production adapter.

## Migration and evidence boundary

Append-only `0014_pronunciation_drafts` follows `0013_profile_lifecycle_intents`.
It adds A-owned `speech_pronunciation_families`, `speech_pronunciation_revisions`
and `speech_pronunciation_commands`, outside the eighteen coordination ports.
The declared writer grants only SELECT/INSERT, allocation-head UPDATE and
pending-result columns; it cannot rewrite definition/original intent, delete
history, create tables or write Job/outbox/budget facts. Provisioning declarations
are applied only to synthetic isolated roles by these tests, not to production.

Downgrade requires READ COMMITTED, obtains table locks in writer order before any
emptiness check and holds them through DDL. Any family, command or revision
history refuses downgrade. Two-connection tests cover writer-first commit and
migration-first exclusion. A disposable mutation experiment removes only that
lock in a test connection: the regression fails and observes a committed
synthetic record disappear under DROP. The unmodified migration preserves it.
Failed upgrade rolls back new DDL and retains previous history.

`tests/integration/test_pronunciation_drafts.py` exercises the real PG adapter and
same SQL through a test-only InnoDB oracle, original-key concurrency, CAS,
rollback, real committed/reply-lost recovery, native query cancellation, minimum
permissions, maximum entry count, revision bounds and migration interleavings.
Actual Node consumes records produced by both databases; shard prerequisites
select its locked dependencies. The oracle's only automatic retry is native
InnoDB 1213 confirmed rollback for writes, never uncertain COMMIT or reads.
Synthetic media text, shared SQL and connection termination do not prove real
phonetic rendering, physical multi-host failure, public HTTP or product/Gate
qualification. Historical failures and corrected TypeScript test import are
retained in the local receipt; passing scopes are reported separately.
