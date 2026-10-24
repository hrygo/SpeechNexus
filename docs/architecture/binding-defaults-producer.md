# Binding default snapshot producer contract

`binding-defaults-producer.openapi.yaml` version `0.1.0.dev0` is a separate
**contract-only capability profile** for explicit parameter declarations and a
fixed snapshot read. Snapshot `1.0.0` and ModelBinding response bodies remain
unchanged. Legacy admin `0.1.0.dev3` and its implemented `binding` HTTP surface
remain unchanged: they do not admit the new input or advertise the future GET.
Actual persistence and producer HTTP wiring require their own acceptance.

The producer request references each original field directly from the canonical
admin `ModelBindingCreateRequest` property. It retains that request's required
fields and closure, adding only optional `parameter_defaults`. No second set of
field types, bounds or defaults is maintained. The generated producer surface
is not included by the current application's operational OpenAPI composition.

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

## Consumable artifacts and remaining implementation

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

This unit changes no runtime validator, lock, storage migration, database role,
actual route or UI capability. Synthetic loopback tests qualify consumer behavior,
not a product writer. The existing product draft request remains the implemented
baseline. Atomic snapshot storage, complete intent recovery, parent matching and
actual new field/GET handling need real isolated database and HTTP tests before
this producer profile becomes ready. The five existing comparison modes retain
their declared source scope; source syntax/drift checks for this new profile do
not establish complete protocol compatibility or deployment qualification.
