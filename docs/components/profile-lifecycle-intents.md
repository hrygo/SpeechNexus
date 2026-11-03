# Private Profile lifecycle intent journal

This is A-CONFIG-01/P2a in [A02 #21](https://github.com/hrygo/SpeechNexus/issues/21).
It persists the complete original publish/activate intent prepared by the existing
[P1a boundary](profile-lifecycle-contract.md). It does not publish, activate, decide
eligibility, save a validation observation as authority, or return a public outcome.
No HTTP route, production verifier or runtime writer is registered.

## Scope and state

The existing `ProfileLifecycleCommand` remains the only private intent format.
`speech_profile_lifecycle_intents` stores its operation, target profile/revision,
canonical complete intent, key and request digests, and a database timestamp.
The intent includes the original CAS and validation claims without changing them.
Raw keys are not stored. Canonical byte comparison preserves JSON types and null.

The table is a separate A-owned namespace from draft creation commands. A private
key identifies one complete lifecycle intent, including its operation and target;
reuse with different intent returns a private conflict, never an overwritten row.
This process-local journal does not add an HTTP idempotency scope or recovery route.
Keyless preparation still allocates a fresh private key; retaining an internal
receipt does not promise a public keyless recovery protocol.

| Journal action | Private result | Public publication/activation meaning |
| --- | --- | --- |
| Record a new original intent | `recorded` with the exact intent and timestamp | None; even a missing target can have an intent record |
| Record/recover the same key and complete intent | `replayed` with the original timestamp/bytes | None; not a published revision or command completion |
| Read the same key with different complete intent | `conflict` | No public error is emitted by this component |
| Recovery sees no row, unreadable history, or unavailable storage | `unknown` | Does not establish absence of publication or permission to change identity |
| Insert/COMMIT may have succeeded but reply is lost | `unknown`; explicit recovery is read-only | Query only the original intent; no provider, CAS or replacement command |

There is deliberately no `published`, `activated`, `succeeded`, eligibility or
pointer field. A journal row has no foreign key claiming that a target exists or
is eligible. `record` reads the original before sampling a new timestamp or
attempting INSERT. An unreadable original aborts the attempt; it cannot be treated
as absence. Only confirmed absence permits inserting this private intent.
The original lookup precedes an INSERT permission check on replay.

The unique key arbitrates concurrent new records. A losing INSERT waits for the
actual first transaction: its commit returns the original/reports a conflict,
and its rollback permits the waiting writer to record its own intent. This is
not a publication lock or ownership claim. No mutable current/default state or
provider is consulted, and no Job/outbox, draft or budget is changed.

## Storage, ownership and migration

- Product adapter: `PostgresProfileLifecycleIntents`; application port:
  `ProfileLifecycleIntents`. The adapter reuses the existing bounded profile PG
  transaction driver, including unknown-on-pool/transport/commit failure behavior.
- SQL implementation: `profile_lifecycle_intents_sql.py`. Recovery uses a read-only
  transaction and compares the entire validated original, not only its digest.
  Damaged canonical JSON, digest, operation or target remains unknown, without repair.
- Append migration `0013_profile_lifecycle_intents` follows the integrated
  `0012_execution_results`. It adds one private table only. No historical rows,
  defaults, current pointer or observation are backfilled; shared 18-port ownership
  and shared A/B grants are unchanged.
- `profile_lifecycle_intent_grants` declares only schema USAGE and table
  SELECT/INSERT for an explicitly provisioned writer. It grants no UPDATE,
  DELETE, TRUNCATE, DDL, publication or cross-owner access. Migration/startup does
  not apply these grants to production roles. Existing profile HTTP roles do not
  gain this table automatically.
- Empty-only downgrade acquires `ACCESS EXCLUSIVE` before checking for rows and
  holds it through DROP under READ COMMITTED. Any history refuses downgrade.
  If a writer commits first, its row is retained; if downgrade acquires the lock
  first, the writer cannot report a successful record that is then erased.
  A nonempty deployment must preserve the table and revert application code
  without a destructive migration. No production upgrade/downgrade is executed here.

Migration integration remains serial. The exact table inventory, baseline
owner/gateway denial and current migration-head assertions are updated without
removing their strict checks. No new table enters the shared coordination grants.

## Verification

[Integration cases](../../tests/integration/test_profile_lifecycle_intents.py)
use the actual packaged migration/PG adapter and the same SQL/domain behavior
through the existing test-only InnoDB driver. Test receipts are synthetic intents,
not trusted quality or publication evidence. Named cases cover:

- Original bytes and time across rebuilt adapters, both operations, missing-target
  recording without publication effects, and distinct keyless preparations.
- Same key/different CAS, target, operation or validation claims, plus actual
  unique-key waits for identical/different intentions with both commit and rollback.
- Real COMMIT followed by injected acknowledgement loss on each engine; read-only
  recovery returns the original without a second row. This is not physical packet
  loss, process-crash or HA qualification.
- SELECT/INSERT denial, corrupted/deep JSON history, and denial of runtime
  UPDATE/DELETE/TRUNCATE; unreadable reads never start a new insert.
- Actual PG migration repeat/empty rollback and nonempty refusal, with two-connection
  writer-first and downgrade-first lock waits. Existing draft/Job history is retained.

The suite's absence of a provider is intentional: no call in this unit may resolve
or confer authority. PG remains the sole product database; InnoDB is a behavioral
oracle, with binary collation and TEXT capacity mappings, not a product adapter.

## Remaining P1/P2/P3/P4 work

P1's Profile-specific capability/tier/quality authority still requires registered,
traceable source rules. B's #142/#162 candidates are not registered facts. The
existing unavailable verifier remains the production behavior; this journal does
not change it or qualify no-Profile execution.

P2 still needs immutable terminal **publication command results**, published
revision facts and a distinct current-pointer CAS. Their transition must check
the original terminal result before consulting a provider or a changed pointer.
For a new transition, lock the original command and affected family/pointer,
recheck trusted applicable facts inside the transaction, and commit the published
fact, pointer and original response atomically. An earlier journal receipt or
P1a observation cannot authorize that transition. Lost replies must recover the
original terminal result, not infer it from this journal or a fixed revision GET.
The provider's concrete fact ownership and lock ordering must be agreed before
adding those tables or accepting a success path.

P3's real HTTP registration/published reads, P4's final parameter merge, historical
Job pinning, actual qualification and cross-team Gate remain separate unfinished
work. A02 is not closed by P2a; #155 and its consumed experiment remain untouched.
