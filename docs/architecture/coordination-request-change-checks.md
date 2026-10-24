# Coordination request structure checks

`make contracts-coordination-requests` checks known structural changes to the
18 frozen raw request entrypoints of
`contracts/internal/coordination-transactions.schema.json`. Set
`CONTRACT_BASE_REF` to the integration baseline commit or fetched branch.
For a machine-readable report:

```bash
uv run python scripts/dev-env/check-coordination-request-changes.py \
  --base origin/main --format json
```

These are the `validate_coordination(operation, request)` Python inputs and
the equivalent inputs consumed by the JS generic parser's shared fixtures.
The JS test assembles that parser directly; this check does not assert a
dedicated exported coordination client. A request narrowing is checked in the
request direction. `TransactionResult` is **not** compared in that direction.

## Baseline and version evidence

The command resolves the baseline to one immutable Git SHA before reading the
schema, bindings or transitive references. It requires the source and
`contracts/bindings.json` on both sides. The explicit source declaration
`x-speechnexus-schema-version` and the coordination binding version must agree;
the report labels both origins. The general generated source inventory records
null for this JSON source's version; null does not replace its explicit schema
declaration, and no new version is invented.

The 18 root operation identities, corresponding `$defs`, and frozen runtime
binding entries must remain available. Duplicate JSON keys, invalid source
schemas, missing metadata or inconsistent source/version bindings reject the
check. A version bump never waives a finding: changed request definitions and
their references are still compared. Changing an entrypoint inventory requires
an explicit generator/consumer and check-scope update, rather than silently
shrinking the selected set.
The frozen profile uses direct `$defs` entrypoints. New `schema_refs` or
`schema_unions` remapping is rejected until the checker and consumers explicitly
agree on the new targets; comparing the old namesake definitions would not
check the parser's actual input.

## What is checked

The command reuses the existing comparison kernel with full fields, including
`nexus`. It detects known changes to types, enums/constants, required fields,
properties, numeric/string/array bounds, additional properties, defaults,
formats and supported compositions in the selected definitions. Conditional or
exclusive composition changes that cannot be proven preserve acceptance are
rejected for explicit review. References must close within repository contracts;
missing, remote, escaping or recursive references cannot produce a pass.
Referenced baseline documents always come from the same pinned commit.
The resolved request schemas are validated too: identical malformed keywords
in a referenced document do not count as proof that the inputs are compatible.

Unchanged sources and annotations are positive controls. The shared synthetic
`create_job` fixture demonstrates a request that becomes invalid when its
digest is narrowed; transitive-reference changes remain visible across a
source/binding version bump. Existing OpenAI and four-source HTTP checks retain
their independent scopes and policies.

## What remains unverified

This is a known structural-change detector, not a complete JSON Schema inclusion
proof or a new compatibility promise. It checks raw request definitions, not
the mixed root `oneOf`, command envelopes, wire `schema_version`,
`TransactionResult` or unselected definitions. Root-envelope or result-only
changes may therefore pass this command; the output always names these limits.
Other JSON protocols, response guarantees, events/epoch, authorization,
transaction atomicity, unknown-outcome recovery, physical stop/budget facts,
consumer upgrades and deployment qualification still need their own evidence.

The existing PR Python job runs this check against its fetched base. Required
status names, all six integration partitions and their union check are
unchanged. Passing this command does not complete A00 or a cross-team Gate.
