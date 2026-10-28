# GB10 Qwen3-ASR capacity-manifest evidence bundle

This directory is the first repository copy of the two release-evidence documents required by
`speechnexus-publish-binding`:

- `capacity-manifest.json`
- `qualification-report.json`

It is an **experimental** bundle. It is not a production qualification, not a live publication, and
not proof of host capacity. Read the two operational documents before using it:

- [`docs/operations/capacity-manifest.md`](../../../../../docs/operations/capacity-manifest.md)
- [`docs/operations/qualification-report.md`](../../../../../docs/operations/qualification-report.md)

## Status

| Item | Value |
| --- | --- |
| Base revision | `9d1b9bbdc1041bfb7c33505a4788490c1db5ea45` (`main`) |
| Manifest | `capm-file-asr-qwen3-1p7b-gb10-20261027` |
| Report | `qrep-file-asr-qwen3-1p7b-gb10-20261027` |
| Proposed binding revision | `mbrev-file-asr-qwen3-1p7b-gb10-20261027` (not registered) |
| Release tier | `experimental` |
| Report status | `incomplete` (5 required: 2 passed, 3 skipped) |
| Measurement source | https://github.com/hrygo/SpeechNexus/issues/243#issuecomment-6453106316 |
| Measurement provenance | relayed primary measurement; original `DEL-103-evidence.md` was not supplied |
| Tool run | `scripts/dev-env/verify-release-evidence.py` with the query-path fixture |

## Files and digests

| File | Bytes | SHA-256 |
| --- | ---: | --- |
| `capacity-manifest.json` | 4579 | `sha256:269096a91dd4b4c09e6332bf490889c5e060be67510185c911218042e24ae149` |
| `qualification-report.json` | 12029 | `sha256:e4f3900bd171eb58c9ac91650f64f2e57b556737f3d715893debc8b210c9cd37` |
| `evidence.json` | 9497 | `sha256:082bd34a404c811d179c982df5f9fab1e6dc39f9e495a1b824da67708549d634` |
| `binding-definition.candidate.json` | 1299 | `sha256:fc717f921b3331f9234909db3f4e2bb29ba81f8a043f7469220b5cafbd6f63f6` |
| `../../../../../tests/fixtures/release-evidence/gb10-20261027-db-snapshot.json` | 5922 | `sha256:0f0991f5e5597cca8ce82a44490127872394f98cebc93bbdf67a483420555dac` |

`capacity-manifest.json` has `qualification.config_bundle_digest`:

```text
sha256:86626578d8937c895b27a346a9663dd45215bb9146f282d265f7a0c56de4d52e
```

That digest is RFC 8785 SHA-256 over:

```json
{"artifact_pin": <deploy/engines/file-asr-audio/artifact-pin.json>, "binding_definition": <binding-definition.candidate.json>}
```

The repository's deployed `binding-definition.json` is **not** modified and still lacks
`execution_recipe.capacity_manifest_id`. The candidate file is a proposal for the next immutable
revision; a real release must register that content as a new revision and recompute the digest.

## What was verified

- Both JSON documents validate against the frozen schemas.
- The three resource vectors aggregate exactly into `resource_envelope`.
- The report computes `required_scenarios_passed=false` and `overall_status=incomplete`.
- Manifest/report/evidence IDs, timestamps, topology and single stage/engine component agree.
- `artifact-pin.model` RFC 8785 digest equals the stage `model_revision_digest`.
- `config_bundle_digest`, `topology_digest`, the evidence file SHA, and all 14 scenario digests match.
- The supplied GitHub comment body (13,726 bytes) matches
  `sha256:42d1c033afe7c266d6450ef4afe39e4b79938c3c3670244b21e8c49edf010dfb`; the body itself is not
  copied here because it contains deployment paths and an internal role identifier.
- A query-path fixture exercises the DB consistency rules (revision, publication, evidence,
  instance vectors, host ledger, pool, workspace quota). The fixture is synthetic and is marked
  `source: fixture`.

## What was not verified

- No model download and no GPU run in this task; model weight bytes were not re-hashed.
- No live database publication; the fixture is not a publication receipt.
- Gateway E2E, artifact persistence, correlation, timeout, retry, cancellation, abort, quarantine,
  budget release, concurrency/P95, real noise, product microphone, telephone channel, dialect/accent,
  peak memory, CPU/PCM/scratch/bandwidth, and context-limit behavior are unverified.
- `resource_pools` has no numeric quota column; pool-level quota is `NA`, not proven.

## Reproduce

From the repository root, with the isolated toolchain:

```bash
UV_CACHE_DIR="$PWD/.uv-cache" \
UV_PYTHON=/opt/homebrew/bin/python3.14 \
uv run --no-project \
  --with 'jsonschema>=4.23,<5' \
  --with 'rfc8785>=0.1.4,<0.2' \
  python scripts/dev-env/verify-release-evidence.py \
  --manifest deploy/engines/file-asr-audio/evidence/gb10-20261027/capacity-manifest.json \
  --report deploy/engines/file-asr-audio/evidence/gb10-20261027/qualification-report.json \
  --evidence deploy/engines/file-asr-audio/evidence/gb10-20261027/evidence.json \
  --expect-manifest-id capm-file-asr-qwen3-1p7b-gb10-20261027 \
  --expect-report-id qrep-file-asr-qwen3-1p7b-gb10-20261027 \
  --artifact-pin deploy/engines/file-asr-audio/artifact-pin.json \
  --binding-definition deploy/engines/file-asr-audio/evidence/gb10-20261027/binding-definition.candidate.json \
  --database-snapshot tests/fixtures/release-evidence/gb10-20261027-db-snapshot.json \
  --require-published --require-digests --show-digests
```

To re-fetch the source body and verify it:

```bash
gh api repos/hrygo/SpeechNexus/issues/comments/6453106316 --jq .body > /tmp/gb10-comment.md
shasum -a 256 /tmp/gb10-comment.md
```

Expected: `42d1c033afe7c266d6450ef4afe39e4b79938c3c3670244b21e8c49edf010dfb` and 13,726 bytes.
Do not commit the fetched body: it contains deployment paths and an internal role identifier.

## Do not

- Do not use `local-test-capacity-1` or any `local-test-*` ID in a real release path.
- Do not treat this `experimental`/`incomplete` bundle as production evidence.
- Do not claim the 12 GiB memory figure is a measured peak; it is the experiment cgroup cap.
- Do not edit a published manifest/report in place; publish a new revision.
