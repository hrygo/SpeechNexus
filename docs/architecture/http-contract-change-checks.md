# Versioned HTTP source structural checks

This A00 unit checks a defined portion of the existing versioned HTTP sources.
It does not create a backward compatibility promise for internal APIs, approve
an upgrade or establish product/Gate qualification. The original producer
handoff still includes the schema/types, positive/negative or sequence examples,
fake behavior and upgrade impact, followed by consumer verification.

| Frozen source | Current source version | Compared surface |
| --- | --- | --- |
| admin-openapi.yaml | 0.1.0.dev2 | All 21 declared paths, complete request/response Nexus fields |
| workbench-openapi.yaml | 0.1.0.dev0 | All 34 declared paths |
| internal/runtime-control.openapi.yaml | 0.1.0.dev0 | All 3 declared paths |
| internal/execution-control.openapi.yaml | 0.4.0.dev0 | All 5 declared paths |

Counts and versions describe the current baseline; the CLI reads versions from
the actual documents instead of relying on this table. Unimplemented operations
in a source are compared as declarations, not advertised as running services.
The existing OpenAI gate keeps its five standard paths and Nexus exclusion.
The added explicit policy compares all paths and Nexus fields in these four
sources, including inside exclusive variants and repository references.

## Run and consume the change inventory

```bash
CONTRACT_BASE_REF=<frozen commit or ref> make contracts-http-changes
uv run python scripts/dev-env/check-http-contract-changes.py \
  --base <frozen commit or ref> --format json
```

The CLI resolves a branch/ref to one immutable commit before reading any source
or reference. Each invocation owns fresh document snapshots. Its text/JSON output
names the baseline, four source versions, findings and unverified scope. A missing
baseline/source, invalid document, missing pointer, remote/out-of-contract reference
or recursive reference cannot silently pass this check. It performs no network
reference loading, runtime API call, permission change or migration.

A source version change is visible in the inventory and never automatically
waives a finding. Version/upgrade descriptions and consumer acceptance remain
required. This tool has no skip/waiver switch. Breaking changes need an explicit
version/migration approach under the existing contract handoff process.

## What the check establishes

The existing directional comparison kernel is reused: reject known request
narrowing and response guarantee loss, including removed paths/operations,
new required parameters, removed request/response media or response statuses,
required response headers, object fields, supported types/enums/constants,
defaults, formats, bounds and boolean schemas. Supported conditional/composed
changes require explicit proof rather than being assumed safe. Unchanged
repository references are resolved against their own old/current document trees;
Nexus fields are included throughout this mode.

The policy selection, not a mutable global path list, separates this mode from
the OpenAI standard gate. Original OpenAI regression cases continue to use its
existing defaults. The new regressions include all four path namespaces,
plain/exclusive Nexus changes, frozen external references, a branch moving during
comparison, version bumps that cannot hide changes, and invalid/unprovable inputs.

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
