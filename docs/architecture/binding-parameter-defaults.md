# Binding parameter default snapshots

This A00/A02 unit defines a standalone `BindingParameterDefaultsSnapshot` format
and same-source Python/TypeScript validators. It supplies the missing declaration
format for the product's binding defaults → published profile → allowed overrides
merge order. An in-process atomic draft registry now stores explicit declarations;
the explicit producer HTTP mode exposes those fixed facts. Published retrieval
and parameter resolution remain separate implementation units. This format
creates no engine capability or qualification.

## Explicit declaration and fixed identity

The snapshot has its own immutable `snapshot_id`, the exact `binding_id`, decimal
`binding_revision`, globally unique `binding_revision_id`, explicit `parameters`
and `created_at`. The schema version is `1.0.0`. IDs are claims until checked
against the authoritative registry. A parser cannot establish that the parent
exists, the values fit the binding or a producer is trusted.

```json
{
  "schema_version": "1.0.0",
  "snapshot_id": "binding-defaults-synthetic-1",
  "binding_id": "asr-quality",
  "binding_revision": "1",
  "binding_revision_id": "binding-revision-synthetic-1",
  "parameters": { "asr": { "max_segment_ms": 15000 } },
  "created_at": "2026-10-24T00:00:00Z"
}
```

This is a synthetic declaration. Actual values must be supplied explicitly through
an authorized configuration producer. Do not derive defaults from capability
ranges, schema annotations, qualification IDs or an engine's undocumented behavior.
There are no publication, readiness, approval or engine-endpoint fields.

## One source for canonical parameter fields

`shared/profile-parameter-values.schema.json` owns the existing canonical ASR,
TTS and output field domains. Complete profiles retain their original required
fields, closed objects, nested-object requirements, ranges, enums, references and
default annotations. Existing LanguageTag/ResourceReference aliases and their
nullable wrappers remain reachable in complete profiles, pointing to the same
shared language/reference domains. Other fields reference the new common scalar
definitions. The snapshot uses sparse field groups from that source, without
maintaining another value set.

Omitted fields stay omitted. Explicit null remains null where the existing domain
permits it. `parameters: {}` explicitly declares no binding defaults; an absent,
unknown or unreadable snapshot cannot be replaced by that declaration. Validation
returns the input unchanged and never populates an annotation's default value.

Nested objects keep their existing shape: supplying a planner requires both
`max_chars` and `max_wait_ms`; supplying turn detection requires both its mode and
silence duration. Canonical vocabulary/pronunciation references include resource
ID and revision. REST override shorthands have their own wire shape and bounds;
this snapshot uses canonical profile parameter fields.

## Registry integration requirements

Before enabling a registry producer:

1. Validate the full parent identity and the explicit values against applicable
   canonical domains and the parent's capability/parameter domains.
2. Allocate and persist the parent revision and its default snapshot atomically.
   Each binding_revision_id has at most one authoritative snapshot.
   Values are part of that revision's immutable effective definition; do not add
   or replace defaults on an existing revision. Legacy revisions without a
   snapshot stay without one; changing defaults requires a new binding revision.
3. Replay the original accepted intent, fixed revision and snapshot. Later
   configuration changes must not re-resolve an existing key or historical Job.
4. Resolve only through the appropriate published registry and fixed snapshots.
   Apply the declared merge order, allowlists/ranges, final cross-field checks,
   hard deployment/workspace constraints and explicit exact-parameter behavior.

The schema checks field types and individual domains. It does not perform a
registry lookup, validate producer authority, negotiate segment budgets or prove
cross-resource/merged-parameter compatibility. Atomic draft storage and the
explicit producer HTTP mode implement the declaration path. Publication CAS/default
pointers and actual engine/capacity/quality evidence still require separate
implementation and acceptance. The standalone format defines no operation. The separate
[producer HTTP contract](binding-defaults-producer.md) declares explicit input and
fixed lookup. The [atomic draft adapter](model-binding-drafts.md#atomic-default-declarations)
implements persistence; the explicit producer HTTP mode now serves fixed lookup.

## Consumers and compatibility evidence

- Python: `speechnexus_contracts.binding_defaults.parse("BindingParameterDefaultsSnapshot", value)`.
- TypeScript: `@speechnexus/contracts/binding-defaults`, with the same entrypoint.
- Source: `contracts/binding-parameter-defaults.schema.json`; common positive and
  negative cases: `tests/fixtures/contracts/consumer-profiles.json`.

Existing ModelBinding request/response sources stay unchanged. Profile wire
fields keep their existing domains and requiredness. Tests compare fully resolved
ASR/TTS/output/profile parameter fingerprints with integration base `08c77de`,
then check Python required keys, TypeScript required/sparse assignments, independent
source validation and input preservation. Existing HTTP, SDK and generated-drift
checks remain applicable. These checks do not prove complete protocol or physical
engine qualification.

The generator versions remain datamodel-code-generator 0.86.0,
openapi-typescript 7.13.0 and static projection revision 1. B #127's
`gateway_recovery` source, entrypoint, examples and generated artifacts are retained.
