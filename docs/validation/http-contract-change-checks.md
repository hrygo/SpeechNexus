# Versioned HTTP source structural checks

This A00 unit checks a defined portion of the existing versioned HTTP sources.
It does not create a backward compatibility promise for internal APIs, approve
an upgrade or establish product/Gate qualification. The original producer
handoff still includes the schema/types, positive/negative or sequence examples,
fake behavior and upgrade impact, followed by consumer verification.

| Source | Version and paths checked on 2026-11-05 | Compared surface |
| --- | --- | --- |
| admin-openapi.yaml | 0.1.0.dev5; 22 paths | All declared paths, including request/response Nexus fields |
| workbench-openapi.yaml | 0.1.0.dev2; 41 paths | All declared paths |
| internal/runtime-control.openapi.yaml | 0.1.0.dev1; 5 paths | All declared paths |
| internal/execution-control.openapi.yaml | 0.4.0.dev1; 9 paths | All declared paths |
| speech-openapi.yaml (explicit mode) | 0.1.0.dev2; 39 paths | All declared HTTP paths, including Nexus paths and fields |

Counts and versions are a dated snapshot; the CLI reads versions from
the actual documents instead of relying on this table. Unimplemented operations
in a source are compared as declarations, not advertised as running services.
The existing OpenAI gate keeps its five standard paths and Nexus exclusion.
The added explicit policy compares all paths and Nexus fields in these four
sources, including inside exclusive variants and repository references.
Those four remain the default selection. The additional speech mode selects only
the final row; its source and scope are reported separately.

## Run and consume the change inventory

```bash
# Export CONTRACT_BASE_REF with the verified baseline commit or ref first.
make contracts-http-changes
uv run python scripts/dev-env/check-http-contract-changes.py \
  --base "$CONTRACT_BASE_REF" --format json
```

The CLI resolves a branch/ref to one immutable commit before reading any source
or reference. Each invocation owns fresh document snapshots. Its text/JSON output
names the baseline, four source versions, findings and unverified scope. A missing
baseline/source, document that fails the required versioned OpenAPI shape, or a
missing, remote/out-of-contract or recursive reference encountered by comparison
cannot silently pass this check. This shape check is not complete OpenAPI document
validation: for example, a missing `info.title` can go unreported. Run the separate
source validation with `make contracts-check` as well. The comparison performs no
network reference loading, runtime API call, permission change or migration.

A source version change is visible in the inventory and never automatically
waives a finding. Version/upgrade descriptions and consumer acceptance remain
required. This tool has no skip/waiver switch. Breaking changes need an explicit
version/migration approach under the existing contract handoff process.

## What the check establishes

The existing directional comparison kernel is reused: reject known request
narrowing and response guarantee loss, including removed paths/operations,
new required parameters, removed request/response media or response statuses,
required response headers, object fields, supported types/enums/constants,
defaults (except the narrow permanent-placeholder notice below), formats,
bounds and boolean schemas. Supported conditional/composed
changes require explicit proof rather than being assumed safe. Unchanged
repository references are resolved against their own old/current document trees;
Nexus fields are included throughout this mode. Boolean reference targets retain
the sibling intersection: true leaves adjacent constraints effective, false stays
unsatisfiable, and annotation-only edits do not change acceptance.

Literal comparisons distinguish JSON booleans from numbers, including nested
`const`/`enum` values, defaults and composed constraints. Equal JSON numbers such
as `1` and `1.0` remain equivalent at value-comparison sites; the existing exact
subtree proof for response intersections stays conservative. Literal objects are
data: keys such as `description`, `$ref` or `properties.nexus` inside them are not
interpreted as schema annotations, references or extension exclusions. The
OpenAI policy still excludes schema properties named `nexus`, including when
checking overlapping constraints beside a reference.

The policy selection, not a mutable global path list, separates this mode from
the OpenAI standard gate. Original OpenAI regression cases continue to use its
existing defaults. The new regressions include all four path namespaces,
plain/exclusive Nexus changes, frozen external references, a branch moving during
comparison, version bumps that cannot hide changes, and invalid/unprovable inputs.

### Replacing the central placeholder 503

`#473` declares every unimplemented operation with the shared
`OperationNotImplemented` response. Its required `Cache-Control: no-store` and
`X-Should-Retry: false` headers describe "permanently unimplemented"; they are
not a runtime guarantee for an operation that ships. The directional comparison
would otherwise reject every promotion.

One structural exception is limited to a raw baseline response that is exactly
`$ref: "#/components/responses/OperationNotImplemented"` with no sibling keys, at
status `503`. The replacement must still:

- be an inline response object rather than a `$ref` to a shared component,
  because a shared 503 could carry guarantees declared for another operation;
- declare a required `Cache-Control` header whose schema allows only the
  `no-store` directive. Each `const`/`enum` value is split on commas, stripped
  and compared case-insensitively, and every non-empty token must be exactly
  `no-store`; each value in an `enum` list must carry it. `x-no-store`,
  `no-store-please`, `no-store, max-age=0` and `No-Store, public` name another
  directive or add a cacheable one and are rejected, as is a non-string value.
  A placeholder response reports an unavailable capability, so a replacement may
  not soften it into a publicly cacheable response. The header schema may be a
  repository `$ref`; an external, broken or value-less schema cannot be verified
  and is rejected;
- declare a required `X-Should-Retry` header whose schema allows only `"true"`:
  the value, not just the name, is the retry signal, and `false` still means
  "do not retry". An equivalent `enum: ["true"]` is accepted;
- keep an `application/json` body composed with `allOf` that references
  `#/components/schemas/ErrorResponse`; and
- declare no `operation_not_implemented` in the body's `error.code` `enum` or
  `const`: a replacement that keeps that literal still says the operation is
  permanently unimplemented.

The `error.code` check resolves repository `$ref`s, so a shared component
cannot hide the literal at the `allOf` branch, at `error`, or at
`error.properties.code`. An external or broken reference makes the body
unjudgeable and is rejected instead of assumed safe; `anyOf`/`oneOf`
alternatives are likewise refused because a union cannot be read as one conjunct.

Each violated rule is reported separately as `placeholder 503 replacement ...
<rule>`. An unchanged placeholder reference stays valid only while the shared
`OperationNotImplemented` component still declares a required `Cache-Control`
limited to `no-store`, a required `X-Should-Retry` limited to `"false"`, the
`allOf`/`ErrorResponse` body, and body constants that are exactly
`error.code: operation_not_implemented`, `error.type:
service_unavailable_error` and `retryable: false`. Conjuncts intersect, so the
shared `ErrorResponse` may allow every error type while the inline body narrows
it to the placeholder type; weakening any of the three constants is reported as
`placeholder 503 response ...` for every operation that still points at it.
All other responses keep the unchanged directional rules, including the existing
`const` and composition comparisons: a 503 that was not exactly the placeholder
reference (including a placeholder `$ref` with siblings) is compared normally. A
version bump remains no waiver. The positive and negative cases live in
`tests/contract/test_http_contract_changes.py`.

### Request defaults on a permanent placeholder

An operation whose baseline declares the permanent placeholder is never served by
this deployment, even when the baseline also lists 2xx responses, so no consumer
can depend on its request default. When the frozen baseline operation's `503` is exactly
`$ref: "#/components/responses/OperationNotImplemented"` with no sibling keys,
and that shared component's declared `error.code` is exactly the literal
`operation_not_implemented` (resolved through repository `$ref`s), a request-body
`default` change is recorded as a `notice` instead of an issue. The check stays
green; the notice is written to stderr, so `--format json` keeps stdout
parseable.

The exemption is deliberately narrower than the placeholder replacement rule:

- it covers only request-body schema nodes; parameters, response schemas and
  response headers keep the ordinary default comparison;
- it covers only `default` (value or presence). `type`, `enum`, `const`,
  `required`, `format`, bounds, composition and every other keyword are still
  compared normally, so a placeholder operation that also narrows `publish` to
  an enum or changes its type is still rejected;
- it is inferred from the baseline 503, never from `operationId`, a path or a
  hardcoded list. A new operation has no baseline and cannot qualify;
- a baseline operation whose 503 was already replaced by an inline retryable
  shape, or that declares any other 503, does not qualify: it may have shipped,
  so its request default may already be load-bearing.

Removing the placeholder proof, accepting any 503 as proof, or letting the
exemption swallow the rest of the schema comparison each turns a positive case
red in `tests/contract/test_http_contract_changes.py`.

## What remains outside this unit

This is an intentionally partial structural detector. Not every JSON Schema
vocabulary feature, added status/media, serialization setting or security scheme
change is classified. A zero result never proves full schema-language inclusion.
Standalone JSON protocols, WS/event timing, epoch/credit/gap, authorization meaning,
fencing/budget/stop effects, upgrade/migration and real client/server qualification
still require their original source change list, examples and consumers.
No source DTO, version, default, route, role, database or deployment changes in
this unit. The complete A00 package and cross-team Gates remain open.

PR CI runs this additional check in the existing Python job against the fetched
base. Existing protected check names, six database partitions, conditional Node
prerequisites and complete/disjoint report verification remain unchanged.

## Explicit complete speech HTTP selection

```bash
make contracts-speech-changes
uv run python scripts/dev-env/check-http-contract-changes.py \
  --base "$CONTRACT_BASE_REF" --surface speech --format json
```

This mode selects every HTTP path in `speech-openapi.yaml`, including the Nexus
resource surface and Nexus fields on standard paths. Its inventory reports the
source's real version; JSON output also includes the current path count. The default CLI and
`contracts-http-changes` retain the four-source selection and text/JSON output.
The separate OpenAI comparison retains its five standard paths and extension
exclusion; this extra structural check adds no formal OpenAI compatibility
promise.

The named real-source counterexample removes `/v1/nexus/assets`: the standard and
four-source comparators do not report that path removal, while explicit speech
selection rejects it. This demonstrates a comparator selection gap, not a claim
that generated drift, SDK compilation or the entire old CI would pass. A
referenced document also does not imply every path or standalone JSON root was
previously compared.

The same immutable baseline, source version inventory, directional request and
response checks and closed repository references are reused. A version bump is
not a waiver. Positive controls retain annotations and new paths. Missing sources,
versions or unprovable references cannot silently fall back to checking four
other documents. Existing source validation, examples and consumer tests remain
necessary. The general schema/serialization/status/media/security/WS and real
qualification limits above apply to this mode too. CI runs it as an explicit
step in the same Python job; no status name, partition or runtime permission changes.

### Narrow request proof for closed tagged objects

The full-field HTTP policy can prove one additional request-only case rather
than rejecting every changed `oneOf` tree. Both resolved parents must retain
`unevaluatedProperties: false`; their other constraints must be identical. The
parent permits only `oneOf`, that closure, an optional object type and unchanged
OpenAPI discriminator metadata. The proof derives actual required string
`const`/finite `enum` tags from each branch's object declarations and `allOf`
conjuncts. Tags must be nonempty, pairwise disjoint and unchanged, with every old
branch matched to exactly one new branch. Array order and schema/path names do
not establish identity; discriminator metadata alone supplies no proof.

Before this proof, both sides must certify a conservative dialect envelope over
the original OAS document and **all** repository documents loaded by its reference
resolver. Only a resource with `openapi: "3.1.0"` whose complete document envelope
passes the existing dependency's bundled OAS 3.1 meta-schema receives an OAS
default. This local structural check does not follow the document's references,
fetch dialects or establish full OpenAPI semantic validity. An ordinary `openapi`
annotation, even alongside `info` and `paths`, cannot certify a bare schema as an
OAS document. Resource identity is independent of filename and extension.
Such an OAS document defaults to the OAS 3.1 base dialect; explicit defaults
may select that canonical URI or the canonical JSON Schema Draft 2020-12 URI.
A referenced bare JSON Schema must explicitly declare one of those dialects;
it does not inherit the referring OAS document's default. Its ordinary `openapi`
and `jsonSchemaDialect` fields remain annotations. Every `$schema`
declaration in these original snapshots must also name one of those dialects.
Draft 7 ignores `unevaluatedProperties`, so its false literal cannot establish
closure. Unknown/missing dialect context cannot establish it either.

This envelope is intentionally conservative: any unsupported declaration in a
loaded document, including unused definitions or literal data, disables this
proof. It does not infer mixed-dialect resource scopes or erase ancestor/target
declarations when resolving a fragment. A supported `$ref` sibling cannot mask
an unsupported target. The existing structural rejection remains in force when
the envelope is not certified. These rules follow the dialect boundaries in
[OAS 3.1.0 §4.8.24.2.3](https://spec.openapis.org/oas/v3.1.0.html#specifying-schema-dialects).

Matched branches retain every existing property predicate, required name and
conjunct. Only optional property declarations for names absent from **all** old
conjuncts of that branch can be added. An old valid object cannot contain such a
name: the selected old branch never evaluates it, and the parent rejects
unevaluated names. The added declaration therefore never runs on an old valid
input. Its validity and evaluated property names remain unchanged, while the
required disjoint tags retain exactly one successful branch. This establishes
`Old ⊆ New` for this subset without treating zero structural findings as a
general schema inclusion proof.

At the affected object level, additional/pattern properties, patterns,
dependencies, conditionals, `not`, property counts, other evaluation keywords,
unknown keywords and unsupported composition fail closed, even when unchanged.
Branch-local `unevaluatedProperties` is unsupported. The one supported closure
is the unchanged false parent; open or schema-valued closures are rejected.
Existing field-value schemas (including scalar regex predicates) remain exact
opaque subtrees, and a new optional field's value schema is not evaluated on any
old valid input. No regex-language inclusion or arbitrary evaluation proof is
attempted. Unresolved/dynamic/recursive references cannot use this proof.

Responses, the original OpenAI policy, arbitrary branch widening/removal, tag
changes and other `oneOf` transformations retain their existing conservative
checks. Independent regressions retain the integer overlap witness, use a
different path/tag/field vocabulary, exercise rejected constraints and verify
old/new request acceptance with Draft 2020-12 validators and, for selected speech
requests, the generated Python parser.

## Separate realtime runtime selection

The [declared realtime constraint check](realtime-contract-change-checks.md)
compares the public event root, all declared members, PCM and the generated
transcription projection with a conservative constraint-identity policy. It
leaves the five checks above unchanged; its result does not extend HTTP/OpenAI
compatibility promises or establish event timing/real consumer qualification.
