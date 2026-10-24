---
title: ModelBinding immutable draft persistence unit
status: active
version: "0.1.0"
date: 2026-10-24
---

# ModelBinding draft persistence

Team A owns this A02 unit, Refs #19/#21. Integration base is
`22d50ee451a2b9e94038ba7d3f0e282e3e7a2b5a`, including B #123/#124. The #122 contract
preflight is already merged; do not repeat it or change its public wire fields.

Deliver only process-local prepare/create/recover/fixed-get and the private
PostgreSQL registry. HTTP wiring, publication/activation/default pointers,
qualification, model loading and effective parameter resolution stay separate.

1. Validate the existing request and complete draft source conditions; preserve
   explicit intent and omission. Allocate family revisions on the server, without
   Profile's client CAS or interpreting supersedes as a head guard.
2. Persist complete original intent and stable outcomes. Private document format
   v1 must preserve source-allowed integers, including values outside RFC8785's
   safe-integer domain, without changing a global interpreter guard or inventing
   a public hash protocol. Preserve boolean/number distinctions and immutable
   globally unique revision IDs across replay, read, restart and unknown recovery.
3. Append `0010_model_binding_drafts` after 0009. Three speech-owned tables cover
   families, immutable commands and immutable revisions. The unique original-key
   insert precedes the family allocation lock; the command/result link is unique.
   Only the family head is updatable. Provisioning declarations do not apply
   production grants, and application code executes no DDL.
4. Exercise native contention, failures after writes, genuine commit followed by
   a discarded reply, pressured/closed reads, cancellation/deadline, corruption,
   exhaustion and minimum-role refusal on isolated PG and InnoDB. InnoDB only
   executes the same SQL through a test oracle; it is not a product adapter.
5. Verify actual PG failed/repeated upgrades and preservation on refused
   downgrade. Retain earlier migration tests and exact ownership/role assertions.
   Keep migrations 0001–0009 byte-for-byte unchanged.
6. Run applicable gates and installed-package consumption; preserve all B #123
   artifacts, bindings, locks and the B dispatch section. Deliver a short PR with
   precise base/head and keep it open for management review. A02/Gates stay open.

Tests use only synthetic local data and uniquely scoped roles/namespaces. Existing
services, B hardware, C issues and production permissions are outside this unit.

## Local validation before serial integration

The implementation on base `402d537` passed 3234 nonintegration tests (81.01%
coverage), 659 coordination tests and 1098 integration tests; 22 existing skips
are reported separately. New binding storage/migration cases passed 40/40 on
actual isolated PG/InnoDB, with 50 boundary and existing Profile tests passing.
Ruff, mypy (459 source files), seven import boundaries, source/drift and existing
comparison gates passed. Same-source TypeScript consumers passed 906 tests.

Installed backend/contracts wheels outside the source tree performed actual PG
creation, original-key replay and recovery in a fresh independent process; the
TypeScript runtime parser consumed that persisted wire result. This is not HTTP
qualification. The initial wheel harness omitted the declared optional migration
tools; that failure is retained. The corrected harness installed the locked
operator tools, without changing package requirements or locks.

The local `make integration-test` startup wrapper failed before pytest because
its standard MinIO container could not bind the port occupied by this unit's
owned isolated MinIO. The failure remains recorded. Reusing verified owned
PG/MySQL/MinIO, the full `pytest tests/integration -q` run produced the result
above; the failed Make target is not counted as passed. No existing service was
replaced or restarted. Native CI must validate the final combination with main.
