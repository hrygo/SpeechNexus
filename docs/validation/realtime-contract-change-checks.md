# Declared realtime runtime constraint checks

A00's explicit realtime check complements the OpenAI subset check. The latter
already checks 14 standard event definitions in request/response directions and
intentionally excludes Nexus extensions. It is unchanged by this unit.

```sh
CONTRACT_BASE_REF=<verified frozen commit or ref> make contracts-realtime-changes
uv run python scripts/dev-env/check-realtime-contract-changes.py \
  --base <verified frozen commit or ref> --format json
```

The source is `contracts/realtime-events.schema.json`; the runtime selection comes
from `contracts/bindings.json`, profile `realtime_events`. Currently this is the
33-member `RealtimeEvent` union (11 client, 22 server events), the 14-member
`TranscriptionEvent` projection and `OpenAIPcmFormat`. Counts describe the current
source, not hard-coded inventories. The report identifies both inventories and
binding versions. The source has no separate wire `schema_version` field; the
binding version is not presented as one.

## Conservative change fence

This check requires constraint identity after repository reference expansion,
with schema-position annotations (`title`, `description`, `examples`, `$comment`)
excluded. It does **not** implement general directional schema inclusion. A
constraint edit, including one that might be a safe widening/narrowing, produces
a finding requiring explicit upgrade and consumer proof. A binding version bump
never waives that finding; there is no skip or waiver option. A future intentional
change must be reviewed with its source/consumer evidence and an explicit update
to the comparison policy where needed, rather than silently treating it as safe.

The check compares the public event inventory, discriminator and declared
client/server base, root constraints (including `unevaluatedProperties`), each
public event's complete composition, PCM constraints/defaults and the binding's
projection membership. Projection validation follows the generator's root plus
selected `oneOf` members: it inherits the same root closure and event constraints.
It does not flatten `NexusOperationDone`'s five `allOf` branches or drop its
conditional terminal requirements. Union/projection member order is immaterial;
other composition/array reorderings can be conservatively rejected. JSON object
key order is immaterial, while boolean/number identity is preserved even inside
nested `const`, `enum` and default values. Literal JSON is opaque: a literal
`$ref` or `description` is not interpreted as a schema keyword. Numeric spelling
changes can be conservatively rejected; decimal parsing preserves precision.

A ref is resolved to one immutable Git commit before loading any baseline file.
Each invocation caches its own old/current source and reference snapshots. Source
and referenced JSON are parsed strictly and validated against Draft 2020-12.
Missing/remote/recursive/out-of-contract references, duplicate JSON keys,
non-JSON constants, invalid references/metadata, changed runtime mappings, and
unsupported nested resource/dynamic-reference scopes fail closed. The tool does
not fetch network references. Root resource IDs must match their canonical
repository paths. Unused definitions are outside the selected runtime constraints.

The named actual-source regressions remove Nexus `seq` or relax root closure.
Independent Draft 2020-12 validation demonstrates old rejection/new acceptance;
the existing standard comparator has no finding, while the new check rejects.
Another regression removes `SessionUpdate` from the runtime projection while
leaving the canonical event source unchanged. These prove selection gaps in that
comparator, **not** that generated drift, schema examples or the entire previous
CI would have passed those edits.

## Proof boundary and integration

This is a structural change detector for declared runtime entrypoints. An equal
constraint representation is not proof of arbitrary JSON Schema equivalence,
serialization/format behavior or consumer implementation correctness. It does
not establish WebSocket ordering, delivery, epoch/credit/gap, reconnect,
cancellation, terminal consistency, real producer/consumer qualification or
cross-team Gate acceptance. Existing positive/negative and sequence fixtures,
generated Python/TypeScript parsers, drift and actual service tests remain
necessary. Nonselected standalone JSON protocols still need separate coverage.

PR CI adds this explicit step to the existing Python contract/static job. The
five existing OpenAI, four-source HTTP, speech HTTP, coordination request and
coordination result checks remain in place. Protected status names, six database
partitions, browser preparation, report aggregation and execution budgets are
unchanged. This unit changes no canonical contract, generated artifact, lock,
route, migration, role, runtime permission or deployment. Rollback is removing
this check's script/tests and Make/CI wiring; no data migration is involved.
