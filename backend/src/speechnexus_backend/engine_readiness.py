"""Read-only readiness-window report for registered engine instances.

Admission refuses a create whose pinned instance has a readiness receipt outside
its declared window (``VerifiedRuntimeReadiness.require_valid``). The receipt is
written by ``speechnexus-register-engine`` with a TTL (default 24 h). Nothing used
to report how much of that window was left, so an elapsed receipt was
indistinguishable from a broken store: every create answered the generic
``503 job_unavailable`` until an operator happened to re-run the registration by
hand (#828).

This command is the read-only half of the renewal path. For every ``ready``
``engine_instances`` row it decodes the receipt of the current prepare intent and
prints one JSON line with:

* ``readiness_state``: ``valid``, ``expiring`` (inside the warning window),
  ``expired``, or ``missing``/``invalid`` when the row has no usable receipt;
* ``expires_at_us`` and ``remaining_seconds`` against the database clock, which is
  the same clock admission reads;
* ``remediation``: the operator command that re-signs the receipt.

A summary line follows, and the exit code is non-zero when any instance is
expired or has no usable receipt, so a periodic task or probes can alert on it.
The warning window is ``--warn-within-seconds`` (default 21600 s, i.e. a quarter
of the 24 h default TTL); it is a reporting threshold, never an admission rule.

The command never writes, never probes the engine and never derives a receipt. It
needs only ``SELECT`` on ``engine_instances``, ``runtime_instance_heads`` and
``runtime_instance_intents``; the renewal it points at needs the registration
identity, because a receipt must come from a real health probe.
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

from speechnexus_backend.engine_registration import EXIT_INVALID, EXIT_UNAVAILABLE
from speechnexus_backend.speech.domain.job_admission_readiness import (
    DEFAULT_READINESS_WARNING_SECONDS,
)
from speechnexus_backend.speech.domain.runtime_observations import (
    READINESS_EXPIRED,
    READINESS_EXPIRING,
    READINESS_VALID,
    VerifiedRuntimeReadiness,
)
from speechnexus_backend.speech.infrastructure.runtime_observation_codec import (
    readiness_from_document,
)

READINESS_MISSING = "missing"
"""The ready instance has no prepare intent or no stored receipt at all."""

READINESS_INVALID = "invalid"
"""The stored receipt no longer decodes, so admission cannot use it either."""

DEFAULT_WARNING_SECONDS = DEFAULT_READINESS_WARNING_SECONDS
"""A quarter of the 24 h default receipt TTL: one renewal interval of warning.

The same constant the admission report uses, so the console report and the polled
surface never classify ``expiring`` against two different thresholds.
"""

RENEW_COMMAND = "speechnexus-register-engine --renew-readiness"

type Row = dict[str, Any]


def _settings(env: Mapping[str, str]) -> tuple[str, str]:
    database_url = env.get("SPEECHNEXUS_DATABASE_URL", "").strip()
    if not database_url:
        raise ValueError("set --database-url or SPEECHNEXUS_DATABASE_URL")
    schema = env.get("SPEECHNEXUS_DATABASE_SCHEMA", "speech").strip()
    if not re.fullmatch(r"[a-z][a-z0-9_]{2,62}", schema):
        raise ValueError("schema name is invalid")
    return database_url, schema


def _decode(raw: str) -> VerifiedRuntimeReadiness | None:
    try:
        return readiness_from_document(raw)
    except ValueError:
        return None


def readiness_report(rows: Sequence[Row], *, now_us: int, warn_within_us: int) -> list[Row]:
    """Classify every ready instance from its stored receipt and one clock.

    ``rows`` are the joined instance/head/intent facts. The classification is a
    pure function of them plus ``now_us`` so the caller's query and the operator's
    reading cannot disagree about which window was checked.
    """
    report: list[Row] = []
    for row in rows:
        document = row.get("readiness_document")
        receipt = _decode(document) if isinstance(document, str) else None
        if receipt is None:
            state = READINESS_MISSING if document is None else READINESS_INVALID
            report.append(
                {
                    "instance_id": row["instance_id"],
                    "instance_generation": int(row["instance_generation"]),
                    "target_node_id": row.get("host_node_id"),
                    "readiness_state": state,
                    "expires_at_us": None,
                    "remaining_seconds": None,
                    "remediation": (
                        "the ready instance has no usable readiness receipt; "
                        f"re-register it with {RENEW_COMMAND}"
                    ),
                }
            )
            continue
        state = receipt.expiry_state(now_us, warning_us=warn_within_us)
        remaining_us = receipt.expires_at_us - now_us
        remediation: str | None = None
        if state == READINESS_EXPIRED:
            remediation = (
                "admission refuses this instance with 503 engine_not_ready; "
                f"run {RENEW_COMMAND} to re-sign the receipt"
            )
        elif state == READINESS_EXPIRING:
            remediation = f"run {RENEW_COMMAND} before expires_at_us"
        report.append(
            {
                "instance_id": row["instance_id"],
                "instance_generation": int(row["instance_generation"]),
                "target_node_id": row.get("host_node_id"),
                "readiness_state": state,
                "expires_at_us": receipt.expires_at_us,
                "remaining_seconds": remaining_us // 1_000_000,
                "remediation": remediation,
            }
        )
    return report


def _summary(report: Sequence[Row]) -> Row:
    counts = {
        READINESS_VALID: 0,
        READINESS_EXPIRING: 0,
        READINESS_EXPIRED: 0,
        READINESS_MISSING: 0,
        READINESS_INVALID: 0,
    }
    for entry in report:
        counts[cast(str, entry["readiness_state"])] += 1
    return {
        "ready_instances": len(report),
        **counts,
        # One number a monitor can threshold without enumerating states.
        "unusable": counts[READINESS_EXPIRED]
        + counts[READINESS_MISSING]
        + counts[READINESS_INVALID],
    }


_QUERY = (
    "SELECT e.instance_id, e.instance_generation, e.host_node_id, "
    "h.current_prepare_intent_id, i.readiness_document "
    "FROM engine_instances AS e "
    "JOIN runtime_instance_heads AS h ON h.instance_id = e.instance_id "
    "AND h.current_generation = e.instance_generation "
    "LEFT JOIN runtime_instance_intents AS i ON i.target_node_id = e.host_node_id "
    "AND i.intent_id = h.current_prepare_intent_id "
    "WHERE e.instance_state = 'ready' "
    "ORDER BY e.instance_id, e.instance_generation"
)


def _queried_rows(database_url: str, schema: str, *, connect_timeout_seconds: int) -> list[Row]:
    connection = psycopg.connect(
        database_url,
        row_factory=dict_row,
        connect_timeout=connect_timeout_seconds,
    )
    try:
        # The report must never depend on write rights, and a read-only session
        # makes that a database-enforced property rather than a promise.
        connection.read_only = True
        with connection.cursor() as cursor:
            cursor.execute("SELECT set_config('search_path', %s, true)", (schema + ",pg_catalog",))
            cursor.execute(
                "SELECT CAST(EXTRACT(EPOCH FROM clock_timestamp()) * 1000000 AS BIGINT) AS now_us"
            )
            now_us = int((cursor.fetchall())[0]["now_us"])
            cursor.execute(_QUERY)
            rows = [dict(row) for row in cursor.fetchall()]
        return [{**row, "database_now_us": now_us} for row in rows]
    finally:
        connection.close()


def main(
    argv: Sequence[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    connect: Callable[..., list[Row]] = _queried_rows,
    output: TextIO | None = None,
    error: TextIO | None = None,
) -> int:
    """Print the report, then return the exit code for a supervisor to alert on."""
    parser = argparse.ArgumentParser(
        description=(
            "Report the readiness window of every registered ready engine instance; "
            "a read-only check, never a renewal"
        )
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="PostgreSQL DSN; defaults to SPEECHNEXUS_DATABASE_URL",
    )
    parser.add_argument(
        "--schema",
        default=None,
        help="Coordination schema; defaults to SPEECHNEXUS_DATABASE_SCHEMA or speech",
    )
    parser.add_argument(
        "--warn-within-seconds",
        type=int,
        default=DEFAULT_WARNING_SECONDS,
        help=(
            "Report a still-valid receipt as expiring when less than this much of its "
            f"window is left (default {DEFAULT_WARNING_SECONDS} s)"
        ),
    )
    parser.add_argument(
        "--fail-on-expiring",
        action="store_true",
        help="Return the unavailable exit code for an expiring receipt as well as an expired one",
    )
    parser.add_argument("--connect-timeout-seconds", type=int, default=5)
    options = parser.parse_args(argv)
    source = os.environ if env is None else env
    stream = sys.stdout if output is None else output
    failures = sys.stderr if error is None else error
    if options.warn_within_seconds < 0:
        parser.exit(EXIT_INVALID, "error: invalid_warning: --warn-within-seconds must be >= 0\n")
    if not 0 < options.connect_timeout_seconds <= 60:
        parser.exit(
            EXIT_INVALID, "error: invalid_timeout: --connect-timeout-seconds is out of range\n"
        )
    overrides = {
        key: value
        for key, value in (
            ("SPEECHNEXUS_DATABASE_URL", options.database_url),
            ("SPEECHNEXUS_DATABASE_SCHEMA", options.schema),
        )
        if value
    }
    try:
        database_url, schema = _settings({**source, **overrides})
    except ValueError as invalid:
        parser.exit(EXIT_INVALID, f"error: invalid_settings: {invalid}\n")
    try:
        rows = connect(
            database_url, schema, connect_timeout_seconds=options.connect_timeout_seconds
        )
    except psycopg.Error as unavailable:
        # The report cannot read the facts; that is its own unavailable state, not
        # evidence that every instance expired.
        failures.write(f"error: database_unavailable: {type(unavailable).__name__}\n")
        return EXIT_UNAVAILABLE
    if not rows:
        print(json.dumps({"summary": _summary([])}, sort_keys=True), file=stream)
        return 0
    now_us = int(rows[0]["database_now_us"])
    report = readiness_report(
        rows,
        now_us=now_us,
        warn_within_us=options.warn_within_seconds * 1_000_000,
    )
    for entry in report:
        print(json.dumps(entry, sort_keys=True), file=stream)
    summary = _summary(report)
    print(json.dumps({"summary": summary}, sort_keys=True), file=stream)
    for entry in report:
        if entry["readiness_state"] in (READINESS_EXPIRED, READINESS_MISSING, READINESS_INVALID):
            failures.write(
                f"error: readiness_{entry['readiness_state']}: "
                f"{entry['instance_id']} g{entry['instance_generation']} "
                f"(expires_at_us={entry['expires_at_us']})\n"
            )
    if summary[READINESS_EXPIRED] or summary[READINESS_MISSING] or summary[READINESS_INVALID]:
        return EXIT_UNAVAILABLE
    if options.fail_on_expiring and summary[READINESS_EXPIRING]:
        failures.write(
            f"error: readiness_expiring: {summary[READINESS_EXPIRING]} instance(s) inside the "
            f"{options.warn_within_seconds} s warning window\n"
        )
        return EXIT_UNAVAILABLE
    return 0


if __name__ == "__main__":
    sys.exit(main())
