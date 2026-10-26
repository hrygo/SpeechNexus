# Fixed pronunciation revision read contract

The opt-in `pronunciation_read` HTTP surface selects only
`getPronunciationRevision` from `contracts/speech-openapi.yaml`. Python imports
`speechnexus_contracts.pronunciation_read_http`; TypeScript imports
`@speechnexus/contracts/pronunciation-read-http`. The generator follows the real
`voice-resource.schema.json` reference for `PronunciationRevision`.

The API composition now registers this fixed-revision GET using the existing
private PG draft reader. Without an explicit reader DSN it returns 503; it does
not borrow the asset, profile or binding writer identity. Publication, default
selection and TTS consumption remain separate components. The surface does not
expose POST, list or an invented command-key query.

## Reader deployment and lifetime

Set `SPEECHNEXUS_PRONUNCIATION_READER_DATABASE_URL` only on the API process and
select the existing speech schema with `SPEECHNEXUS_DATABASE_SCHEMA`. Provisioning
is external: the runtime does not run migrations, create roles or grant access.
The explicit identity needs schema USAGE and SELECT on
`speech_pronunciation_revisions`, with no other speech-table privileges or
column writes. Startup validates both SESSION_USER and CURRENT_USER and rejects
owners, owner membership (including NOINHERIT), schema CREATE, elevated role
attributes, missing SELECT and excess table/column privileges before exposing the
provider. This is a service database identity, not a user/tenant boundary.

`SPEECHNEXUS_PRONUNCIATION_READ_CONCURRENCY` defaults to 4 (range 1–4);
`SPEECHNEXUS_PRONUNCIATION_READ_TIMEOUT_SECONDS` defaults to 3 (range >0–30).
The API owns a separate pool, closes it on normal or failed startup, and can
reopen it on restart. Host/Origin rules reuse the explicit asset HTTP settings.
The route accepts GET with no body; framing, query/header ambiguity and bounded
receive failure are rejected. Reads consume one local permit; saturation returns
429. Cancellation retains that permit until the actual owned handler/DB thread
ends, even after repeated cancellation. PG statement/pool deadlines are bounded
cooperative limits, not hard network-interruption or engine-stop guarantees.

Reads validate stored definition/digest and project only the public DTO. They
compare returned id/revision with the request and preserve `draft`; an unavailable
or corrupt result is never converted into 404, published status or permission to
execute TTS. No migration, writer grant, historical Job or default pointer changes
are required. To disable this reader, unset its DSN and restart; retained revisions
remain untouched, and fixed reads return the declared unavailable response.

## Failure and recovery

| Status | Meaning and declared transport |
| --- | --- |
| 200 | Complete public revision representation, including its opaque string revision. |
| 400 | Invalid path, query, header or unexpected request body. |
| 401 | Existing shared unauthorized response. |
| 403 | Host or Origin rejected by the public entry boundary. |
| 404 | Confirmed missing fixed revision: `pronunciation_not_found`, `not_found_error`, `retryable: false`. Required `Cache-Control: no-store` and `X-Should-Retry: false`. |
| 405 | Unsupported method; required `Allow: GET`. |
| 429 | Existing capacity response, with the declared optional retry header. |
| 503 | Metadata cannot be read: `pronunciation_unavailable`, `service_unavailable_error`, `retryable: true`. Required `Cache-Control: no-store` and `X-Should-Retry: true`; optional `Retry-After`. |

Unreadable metadata does not prove absence. Neither 404 nor 503 contains an
invented `nexus` Operation. A caller may explicitly repeat the same fixed GET
after an unavailable result; the generated client never retries automatically,
changes revision or creates a replacement dictionary. A 404 body claiming
unavailability or allowing retry is rejected by both runtime validators.

`Retry-After` remains optional in runtime validation and in the TypeScript
response DTO. The locked compiler otherwise makes every referenced Header
Object required. The generation adapter reads presence from that compiler's
resolved source document and updates only response-header optional markers in
its TypeScript AST. Header value types, mandatory policy headers, source schemas
and runtime validators are preserved; no service is required to add a retry header.

Path revisions stay canonical decimal **strings**, including values above the
JavaScript safe integer bound. HTTP integer headers are decoded independently;
that does not authorize conversion of revision identifiers. Callers must compare
returned `id` and `revision` with the requested identity: per-message schema
validation does not establish that cross-message relationship.

## Compatibility and consumption order

The existing speech source version remains `0.1.0.dev0`. This adds selected
failure statuses and narrows the previously generic 404 to confirmed absence;
the original 200 definition, path parameters and pronunciation POST defaults
are preserved. Consumers with an older strict response-status table need the
new generated package before this route returns these added statuses. A
version string alone does not establish compatibility. The new opt-in client
does not alter existing broad source clients; API composition registers only the
one implemented fixed GET from that selected surface.

The validated TypeScript client requires an explicit deployment URL and
`maxJsonBytes` limit. That caller-selected response budget is not a dictionary
default or a replacement for the source entry limit. Validation inserts no
publication/default values, normalizes no phonetic text and infers no engine
qualification. Published/retired fixture representations only exercise the
already declared wire enum; they are not publication evidence.

## Evidence boundary

`tests/fixtures/contracts/pronunciation-read-http.json` supplies shared positive
and negative envelopes. Python checks the original source independently of the
generated validator; TypeScript consumes the same examples. Native loopback TCP
tests use a synthetic protocol server to exercise an explicit 503 then fixed GET
200 in both languages. These prove transport parsing and caller recovery shape,
not a production router, real database read, permission role, qualified engine
or a complete speech workflow. Separately,
`tests/integration/test_pronunciation_http.py` exercises the actual API/lifespan
and PG adapter under a SELECT-only role, independent login/effective role checks,
restart and startup failure, corrupt history, pool/SQL read pressure and actual
lock-wait cancellation. Python and generated TypeScript consume actual loopback
HTTP backed by those PG rows, including 503 then the same fixed GET and confirmed
404. Its seed is a synthetic private draft, not a public create, phonetic quality,
published dictionary, real speech engine or target-topology qualification.
