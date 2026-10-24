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

## Explicit result surface

`CONTRACT_BASE_REF=origin/main make contracts-coordination-results` selects only
`$defs/TransactionResult` from the same source. The equivalent CLI is:

```bash
uv run python scripts/dev-env/check-coordination-request-changes.py \
  --base origin/main --surface result --format json
```

This surface compares **response** guarantees: new permitted outputs must retain
the old consumer's known constraints. For example, removing the required
`scope_id` makes a formerly invalid result valid; it is rejected as a lost
response guarantee. Adding enum values, widening types or removing constraints
is checked in this direction. A stronger existing-field guarantee or narrower
output enum is a positive structural control, not permission to change an
outcome's business behavior. Changes to conditionals or exclusive variants that
cannot be proven are rejected for explicit review.

The report explicitly names `TransactionResult`, `direction=response`, both
version origins and the still-unverified scope. The default CLI and
`contracts-coordination-requests` keep their existing 18-request selection and
output; the result is not silently compared as a request. Both commands reuse
the pinned baseline, fixed generator mapping, closed references and resolved
schema validation. Bumping source/binding versions never waives a finding.
The selected result's own `schema_version` property is included in the
comparison; the source/binding versions describe provenance and do not select
or authorize a new wire version.

This result check includes the selected definition's seven typed-value branches
and declared operation/outcome constraints, but does not certify their runtime
production or transaction semantics. The shared fixtures have 22 valid and two
invalid results. Python and the JS generic parser validate those same shapes;
this is not evidence of a dedicated exported client or full execution readiness.
Command envelopes and constraints on the mixed root, separate request
entrypoints, other JSON/event protocols, complete schema inclusion,
recovery/stop/budget facts and real qualification remain outside this command. It runs as a separate step in the
same PR Python job, with existing status names and six integration partitions.
