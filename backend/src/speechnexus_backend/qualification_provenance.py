"""Read-only provenance report for published builtin voice qualifications.

Every published builtin qualification was produced by exactly one of two
mechanisms: a node-controller observation with a controlled probe, or an
operator attestation for an externally managed engine. This command answers
"which one?" from the durable ledger alone and never from an inference about the
running topology, and prints the attestation behind an operator-sourced voice so
an operator can see that it is not controller-observed.

It writes nothing, probes nothing and needs only ``SELECT`` on
``speech_builtin_voice_qualifications``, ``speech_builtin_voice_approvals``,
``speech_builtin_voice_qualification_provenance`` and
``speech_builtin_voice_operator_attestations``. A qualification published before
migration ``0055`` has no provenance row; it is reported as
``legacy_pre_0055`` with ``source_kind='controller_observed'`` because the
operator path did not exist then.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from typing import Any, TextIO, cast

import psycopg
from psycopg.rows import dict_row

SOURCE_CONTROLLER = "controller_observed"
SOURCE_OPERATOR = "operator_attested"
SOURCE_UNVERIFIED = "unverified"
EVIDENCE_RECORDED = "provenance_row"
EVIDENCE_LEGACY = "legacy_pre_0055"
EVIDENCE_PROBE = "probe_authorization"
EVIDENCE_OPERATOR = "operator_attestation"
EVIDENCE_ABSENT = "absent"

ATTESTATION_LIVE = "live"
ATTESTATION_EXPIRED = "expired"
ATTESTATION_MISSING = "missing"

EXIT_UNAVAILABLE = 3
"""The read itself failed; the exit code never stands for a qualification verdict."""

type Row = dict[str, Any]

REPORT_SQL = (
    "SELECT q.voice_id, q.revision, q.revision_id, q.binding_revision_id, q.approval_id, "
    "p.source_kind AS recorded_source_kind, p.evidence_kind AS recorded_evidence_kind, "
    "p.attestation_id, p.probe_authorization_id AS recorded_probe_authorization_id, "
    "p.recorded_at, "
    "a.probe_authorization_id AS approval_probe_authorization_id, "
    "a.facts->>'instance_id' AS instance_id, a.facts->>'generation' AS instance_generation, "
    "a.facts->'attestation'->>'fencing_token' AS fencing_token, "
    "floor(extract(epoch FROM a.expires_at)*1000000)::bigint AS approval_expires_at_us, "
    "t.declared_by, t.declaration_digest, t.image_digest, t.template_digest, "
    "t.declared_lifecycle_digest, t.observed_at_us, t.expires_at_us, "
    "floor(extract(epoch FROM clock_timestamp())*1000000)::bigint AS now_us "
    "FROM speech_builtin_voice_qualifications q "
    "LEFT JOIN speech_builtin_voice_qualification_provenance p "
    "ON p.voice_id=q.voice_id AND p.revision=q.revision "
    "LEFT JOIN speech_builtin_voice_approvals a ON a.approval_id=q.approval_id "
    "LEFT JOIN speech_builtin_voice_operator_attestations t ON t.attestation_id=p.attestation_id "
    "ORDER BY q.voice_id, q.revision"
)


def _labelled_source(row: Row, *, provenance_recorded: bool) -> tuple[str, str, str | None]:
    """Return ``(source_kind, evidence_kind, note)`` from evidence, not absence.

    A recorded row is trusted only when its evidence names the row it claims. A row
    without provenance (published before migration ``0055``) is attributed to the
    controller only when the approval itself names a probe authorization -- the one
    column the attestation identity cannot write. Everything else is ``unverified``.
    """
    approval_probe = row.get("approval_probe_authorization_id")
    if provenance_recorded:
        recorded = cast(str, row["recorded_source_kind"])
        evidence = cast(str, row.get("recorded_evidence_kind") or EVIDENCE_ABSENT)
        attested = row.get("attestation_id")
        probe = row.get("recorded_probe_authorization_id")
        if recorded == SOURCE_CONTROLLER:
            if evidence == EVIDENCE_PROBE and probe is not None and probe == approval_probe:
                return SOURCE_CONTROLLER, EVIDENCE_PROBE, None
            return (
                SOURCE_UNVERIFIED,
                EVIDENCE_ABSENT,
                ("recorded as controller-observed without a matching probe authorization"),
            )
        if recorded == SOURCE_OPERATOR:
            if evidence == EVIDENCE_OPERATOR and attested is not None:
                return SOURCE_OPERATOR, EVIDENCE_OPERATOR, None
            return (
                SOURCE_UNVERIFIED,
                EVIDENCE_ABSENT,
                ("recorded as operator-attested without its attestation"),
            )
        return (
            SOURCE_UNVERIFIED,
            EVIDENCE_ABSENT,
            (
                "recorded as unverified: neither a probe authorization nor an "
                "attestation backs a source label"
            ),
        )
    if approval_probe is not None:
        return SOURCE_CONTROLLER, EVIDENCE_PROBE, None
    return (
        SOURCE_UNVERIFIED,
        EVIDENCE_ABSENT,
        ("published before 0055 and no probe authorization is recorded; source unknown"),
    )


def _entry(row: Row, *, now_us: int) -> Row:
    recorded = row.get("recorded_source_kind")
    provenance_recorded = recorded in (SOURCE_CONTROLLER, SOURCE_OPERATOR, SOURCE_UNVERIFIED)
    source_kind, evidence_kind, note = _labelled_source(
        row, provenance_recorded=provenance_recorded
    )
    expires_at_us = row.get("expires_at_us")
    if source_kind == SOURCE_OPERATOR and expires_at_us is None:
        state = ATTESTATION_MISSING
    elif source_kind == SOURCE_OPERATOR and cast(int, expires_at_us) <= now_us:
        state = ATTESTATION_EXPIRED
    else:
        state = ATTESTATION_LIVE
    entry: Row = {
        "voice_id": row["voice_id"],
        "revision": row["revision"],
        "revision_id": row["revision_id"],
        "binding_revision_id": row["binding_revision_id"],
        "source_kind": source_kind,
        "source_evidence": evidence_kind,
        "provenance_recorded": provenance_recorded,
        "evidence": EVIDENCE_RECORDED if provenance_recorded else EVIDENCE_LEGACY,
        "instance_id": row.get("instance_id"),
        "declared_instance_generation": row.get("instance_generation"),
        "approval_expires_at_us": row.get("approval_expires_at_us"),
        "probe_authorization_id": row.get("approval_probe_authorization_id"),
    }
    if note is not None:
        entry["note"] = note
    if source_kind == SOURCE_OPERATOR:
        entry.update(
            {
                "attestation_id": row.get("attestation_id"),
                "declared_by": row.get("declared_by"),
                "declaration_digest": row.get("declaration_digest"),
                "declared_fencing_token": row.get("fencing_token"),
                "attestation_state": state,
                "declared_observed_at_us": row.get("observed_at_us"),
                "declared_expires_at_us": expires_at_us,
                "image_digest": row.get("image_digest"),
                "template_digest": row.get("template_digest"),
                "declared_lifecycle_digest": row.get("declared_lifecycle_digest"),
                "note": (
                    "operator-attested external engine: not a controller observation; "
                    "declared_* values are the declaration's snapshot, not live runtime state"
                    if state == ATTESTATION_LIVE
                    else "attestation is outside its declared window; publish a new voice "
                    "revision and attest it (one attestation per voice revision)"
                ),
            }
        )
    return entry


def provenance_report(rows: Sequence[Row], *, now_us: int) -> list[Row]:
    """One entry per published builtin qualification, from one clock."""
    return [_entry(row, now_us=now_us) for row in rows]


def summarize(report: Sequence[Row]) -> Row:
    summary: Row = {
        "qualifications": len(report),
        SOURCE_CONTROLLER: 0,
        SOURCE_OPERATOR: 0,
        SOURCE_UNVERIFIED: 0,
        "provenance_recorded": 0,
        EVIDENCE_LEGACY: 0,
        ATTESTATION_LIVE: 0,
        ATTESTATION_EXPIRED: 0,
        ATTESTATION_MISSING: 0,
    }
    for entry in report:
        summary[cast(str, entry["source_kind"])] += 1
        summary["provenance_recorded"] += 1 if entry["provenance_recorded"] else 0
        summary[EVIDENCE_LEGACY] += 0 if entry["provenance_recorded"] else 1
        if entry["source_kind"] == SOURCE_OPERATOR:
            summary[cast(str, entry["attestation_state"])] += 1
    return summary


def _settings(
    env: Mapping[str, str], *, database_url: str | None, schema: str | None
) -> tuple[str, str]:
    """Command-line flags win; the environment is the deployment default."""
    url = (database_url or "").strip()
    if not url:
        url = env.get("SPEECHNEXUS_QUALIFICATION_READER_DATABASE_URL", "").strip()
    if not url:
        url = env.get("SPEECHNEXUS_DATABASE_URL", "").strip()
    if not url:
        raise ValueError("set --database-url or SPEECHNEXUS_QUALIFICATION_READER_DATABASE_URL")
    name = (schema or "").strip() or env.get("SPEECHNEXUS_DATABASE_SCHEMA", "speech").strip()
    if not re.fullmatch(r"[a-z][a-z0-9_]{2,62}", name):
        raise ValueError("schema name is invalid")
    return url, name


def main(
    argv: Sequence[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    connect: Callable[[str, str], Any] | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url")
    parser.add_argument("--schema")
    parser.add_argument("--fail-on-expired-attestation", action="store_true")
    options = parser.parse_args(argv)
    out, err = stdout or sys.stdout, stderr or sys.stderr
    try:
        database_url, schema = _settings(
            dict(os.environ if env is None else env),
            database_url=options.database_url,
            schema=options.schema,
        )
        opener = connect or _connect
        rows, now_us = opener(database_url, schema)
    except Exception:
        print("error: qualification_provenance_unavailable", file=err)
        return EXIT_UNAVAILABLE
    report = provenance_report(rows, now_us=now_us)
    for entry in report:
        print(json.dumps(entry, sort_keys=True), file=out)
    summary = summarize(report)
    print(json.dumps({"summary": summary}, sort_keys=True), file=out)
    if options.fail_on_expired_attestation and (
        summary[ATTESTATION_EXPIRED] or summary[ATTESTATION_MISSING]
    ):
        print(
            "error: operator_attestation_outside_window: "
            f"{summary[ATTESTATION_EXPIRED] + summary[ATTESTATION_MISSING]} voice(s)",
            file=err,
        )
        return EXIT_UNAVAILABLE
    return 0


def _connect(database_url: str, schema: str) -> tuple[list[Row], int]:
    with psycopg.connect(database_url, row_factory=dict_row) as connection:
        connection.execute("SET TRANSACTION READ ONLY")
        connection.execute(f'SET search_path TO "{schema}", pg_catalog')
        rows = connection.execute(REPORT_SQL).fetchall()
    if not rows:
        return [], 0
    return rows, int(rows[0]["now_us"])


if __name__ == "__main__":
    sys.exit(main())
