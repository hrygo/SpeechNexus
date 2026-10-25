# Binding default snapshot producer HTTP

`binding-defaults-producer.openapi.yaml` version `0.1.0.dev0` is a separate
capability profile for explicit parameter declarations and a
fixed snapshot read. Snapshot `1.0.0` and ModelBinding response bodies remain
unchanged. Legacy admin `0.1.0.dev3` and its implemented `binding` HTTP surface
remain unchanged: legacy mode does not admit the new input or advertise the snapshot GET.
An in-process [atomic storage port](model-binding-drafts.md#atomic-default-declarations)
implements new-revision persistence and original-key recovery. Explicit producer
mode now wires that port through real HTTP and a controlled four-table writer.

The producer request references each original field directly from the canonical
admin `ModelBindingCreateRequest` property. It retains that request's required
fields and closure, adding only optional `parameter_defaults`. No second set of
field types, bounds or defaults is maintained. The generated producer surface
enters the operational OpenAPI only in producer mode, where the corresponding
handlers are registered; the legacy fixed parent GET is also retained.

## Explicit writer mode and lifecycle

Set `SPEECHNEXUS_BINDING_DEFAULTS_WRITER_DATABASE_URL` for an API process only
after separately applying 0011 and provisioning the declared minimum four-table
role. It is mutually exclusive with `SPEECHNEXUS_BINDING_WRITER_DATABASE_URL`.
Blank/absent settings do not fall back to the bootstrap or general DB identity.
Legacy three-table mode and its input validation remain supported independently.
Compose passes both optional values through without filling either credential.

`BindingDefaultsRuntime` shares the controlled lifecycle implementation with the
legacy writer, using its own explicit allowed-table set. Before exposing a provider
it verifies SESSION_USER and CURRENT_USER, non-owner identity, exact table and
column privileges and absence of unrelated speech authority. Startup never grants
privileges or applies DDL; failure and shutdown close its pool and remove diagnostic
filters. Configuration conflicts fail before either writer starts.

Both fixed reads use strict no-body GET framing. Creation uses the same bounded
receive settings and keeps its private permit through an owned transaction even
when the HTTP caller cancels. Neither a permit nor cancellation changes a compute
lease or budget. The route validates the complete producer intent and checks the
returned parent/snapshot facts before confirming success. Snapshot GET checks the
fixed parent identity, revision ID, version and creation time.

## Explicit input on a new binding revision

The producer profile for `POST /v1/nexus/admin/model-bindings` accepts optional
`parameter_defaults`, using
`BindingDefaultParameters` from the existing snapshot source. The caller supplies
sparse canonical parameter values, never snapshot ID, parent revision identities,
creation time, publication evidence or an engine endpoint.

- Omission means no declaration is created; it does not infer engine defaults.
- `{}` explicitly declares no defaults and requires its own immutable snapshot.
- Null is invalid for the parameter map. Individual nullable fields retain their
  existing canonical domains; validation does not insert schema defaults.

All fields and field presence belong to the complete original-key intent. Same
key and intent replays the original binding and snapshot; changing a value or
changing omission to `{}` conflicts. A new successful command allocates a new
revision and at most one snapshot in one transaction. No later command attaches,
replaces or backfills defaults on that accepted or historical revision.

The producer must validate tasks/capability domains, parent identity and actual
explicit values before committing. Structurally valid but incompatible inputs
use the existing 422 response. The schema only validates field domains; it does
not prove that the binding supports those values. A synthetic declaration is not
an authoritative engine-default probe or publication/quality approval.

The 201 binding response shape is unchanged. After a known accepted original
command, its fixed identity addresses the snapshot GET. An unreadable creation
result or uncertain commit still uses 503 `binding_result_unknown`,
`retryable: false`: retain and explicitly replay the exact original POST/body/key.
Keyless recovery is not promised. Neither a candidate parent nor a snapshot GET
proves ownership of a prior original-key command; a new key is not recovery.

## Fixed snapshot read

`GET /v1/nexus/admin/model-bindings/{binding_id}/revisions/{revision}/parameter-defaults`
reads only that immutable parent. The producer verifies returned binding ID,
family revision and global revision ID against the authoritative parent. Schema
parsing cannot compare a response with a prior request or prove parent existence.

| Outcome | Contract |
| --- | --- |
| Known declaration | 200, complete `BindingParameterDefaultsSnapshot`; explicit empty parameters remain a valid declaration |
| Authoritatively absent declaration or parent | 404 `binding_defaults_not_found`, `not_found_error`, `retryable: false`; includes legacy revisions with no declaration |
| Parent/snapshot unreadable | 503 `binding_unavailable`, `retryable: true` for the same fixed GET; never absence or an empty declaration |
| Invalid path/header/query/read framing or body | 400 |
| Host/Origin refusal | 403 |
| Unsupported method | 405 with required `Allow: GET` |

The source also retains the existing shared-key 401 declaration. No latest/current
selection, query-by-key, mutation, Operation or new keyless recovery is added.
Read failure never authorizes reallocating a parent, replacing defaults or
releasing budgets. Publication/activation and the final merged parameter policy
remain separate registry/application acceptance.

## Consumable artifacts and evidence scope

The `binding_defaults_producer` profile exposes `BindingDefaultsCreateRequest`.
Its distinct `binding_defaults` HTTP surface selects `createModelBindingDraft`
and `getModelBindingParameterDefaults`. It does not change the legacy `binding`
surface's two operations or paths. Imports are:

- Python: `speechnexus_contracts.binding_defaults_producer.parse` and
  `speechnexus_contracts.binding_defaults_http`.
- TypeScript: `@speechnexus/contracts/binding-defaults-producer` and
  `@speechnexus/contracts/binding-defaults-http`.
- Shared HTTP cases: `tests/fixtures/contracts/binding-defaults-http.json`;
  direct DTO cases join `consumer-profiles.json`. Snapshot fields remain in
  `binding-parameter-defaults.schema.json` and their canonical shared domain source.

Generator versions remain datamodel-code-generator 0.86.0,
openapi-typescript 7.13.0 and static projection format revision 1. The HTTP bundler
now retains external OpenAPI parameter/header/response/security namespaces;
existing same-namespace name collisions still fail closed, including boolean
versus numeric constants. The external objects and request fields refer to their
canonical source. Existing operational paths and negative-input tests remain.

The runtime validators, generator, locks and source versions are unchanged. The
HTTP module selects the existing producer validator for POST/snapshot GET and the
existing legacy validator for the fixed parent GET; it maintains no wire copies.
The operational component namespaces remain disjoint and all references resolve.

Isolated PG tests exercise the actual four-table writer, lifecycle/role refusal,
old history, restart, replay/conflict, committed reply loss/read pressure, keyless
uncertainty, cancellation, corrupt/missing facts and real TCP Python/TypeScript
consumers. InnoDB storage behavior reuses the separate atomic-port evidence; it
does not represent a supported product DB. Neither synthetic fixture values nor
these API tests establish an engine's defaults, publication, quality or physical
cluster qualification. Published registry/parameter resolution remain separate.
Existing structural comparison modes retain their declared finite scope.
