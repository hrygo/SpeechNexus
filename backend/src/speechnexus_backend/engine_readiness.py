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

from speechnexus_backend.controller_head import (
    CONTROLLER_URL_VARIABLE,
    ControllerConsultationError,
    ControllerEndpoint,
    NodeInstanceHead,
    configured_endpoint,
    generation_disagreement,
    read_head,
)
from speechnexus_backend.engine_registration import EXIT_INVALID, EXIT_UNAVAILABLE
from speechnexus_backend.speech.domain.readiness_verdict import (
    READINESS_INVALID as READINESS_INVALID_STATE,
)
from speechnexus_backend.speech.domain.readiness_verdict import (
    READINESS_MISSING as READINESS_MISSING_STATE,
)
from speechnexus_backend.speech.domain.readiness_verdict import ReadinessVerdict
from speechnexus_backend.speech.domain.runtime_intents import (
    ApprovedRuntimePlan,
    RuntimeIntent,
)
from speechnexus_backend.speech.domain.runtime_observations import (
    DEFAULT_READINESS_FRESHNESS_US,
    READINESS_EXPIRED,
    READINESS_EXPIRING,
    READINESS_UNOBSERVED,
    READINESS_VALID,
    VerifiedRuntimeReadiness,
)
from speechnexus_backend.speech.infrastructure.runtime_codec import decode_record
from speechnexus_backend.speech.infrastructure.runtime_observation_codec import (
    readiness_from_document,
    stored_readiness_verdict,
)
from speechnexus_portable_sql.clock import ClockReader
from speechnexus_portable_sql.clock import now_us as now_us_for
from speechnexus_portable_sql.psycopg_driver import connect as portable_connect
from speechnexus_portable_sql.vendor import dict_row, psycopg

# The two unusable states are part of the shared verdict vocabulary: the report
# must speak the same words the judgement does (#1246).
READINESS_MISSING = READINESS_MISSING_STATE
"""The ready instance has no prepare intent or no stored receipt at all."""

READINESS_INVALID = READINESS_INVALID_STATE
"""The stored receipt cannot be used, so admission cannot use it either."""

DEFAULT_WARNING_SECONDS = 21_600
"""A quarter of the 24 h default receipt TTL: one renewal interval of warning."""

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


def _stored_identity(
    row: Row,
) -> tuple[RuntimeIntent | None, ApprovedRuntimePlan | None, str | None]:
    """The immutable plan the receipt must describe, or why it cannot be read.

    The plan is read from the same intent row the receipt came from, so the report
    compares the receipt against exactly what admission compares it against. A row
    whose intent document does not decode is unusable for the same reason admission
    refuses it, and the decoder's own words are the reason the report prints.
    """
    document = row.get("request_document")
    if not isinstance(document, str):
        return None, None, "the prepare intent has no readable request document"
    try:
        prepared, plan, _creator = decode_record(document)
    except ValueError as error:
        return None, None, f"the stored runtime plan does not decode: {error}"
    return prepared.request, plan, None


def _decoded_receipt(document: object) -> VerifiedRuntimeReadiness | None:
    if not isinstance(document, str):
        return None
    try:
        return readiness_from_document(document)
    except ValueError:
        return None


def readiness_report(rows: Sequence[Row], *, now_us: int, warn_within_us: int) -> list[Row]:
    """Classify every ready instance from its stored receipt and one clock.

    ``rows`` are the joined instance/head/intent facts. The classification is a
    pure function of them plus ``now_us`` so the caller's query and the operator's
    reading cannot disagree about which window was checked.

    Identity is part of the judgement, not an extra: a receipt that describes a
    different plan admits nothing, so reporting it ``valid`` because its window has
    not elapsed told the operator the opposite of what admission would answer
    (#1246). The verdict is the one admission uses, and ``reason`` names the fact
    that decided it.
    """
    report: list[Row] = []
    for row in rows:
        document = row.get("readiness_document")
        request, plan, unreadable = _stored_identity(row)
        if unreadable is not None:
            # The plan the receipt must describe cannot be read, so no window can
            # make the receipt usable: admission refuses it for the same reason.
            verdict = ReadinessVerdict(READINESS_INVALID, None, None, unreadable)
        else:
            verdict = stored_readiness_verdict(
                document if isinstance(document, str) else None,
                request=request,
                plan=plan,
                now_us=now_us,
                warning_us=warn_within_us,
                freshness_us=DEFAULT_READINESS_FRESHNESS_US,
            )
        state = verdict.state
        reason = verdict.reason
        if verdict.expires_at_us is None or reason is not None:
            report.append(
                {
                    "instance_id": row["instance_id"],
                    "instance_generation": int(row["instance_generation"]),
                    "target_node_id": row.get("host_node_id"),
                    "readiness_state": state,
                    "expires_at_us": verdict.expires_at_us,
                    "remaining_seconds": None,
                    "reason": reason,
                    "remediation": (
                        "the ready instance has no usable readiness receipt; "
                        f"re-register it with {RENEW_COMMAND}"
                    ),
                }
            )
            continue
        receipt = _decoded_receipt(document)
        assert receipt is not None
        remaining_us = verdict.expires_at_us - now_us
        observed_us = now_us - receipt.verified_at_us
        remediation: str | None = None
        if state == READINESS_EXPIRED:
            remediation = (
                "admission refuses this instance with 503 engine_not_ready; "
                f"run {RENEW_COMMAND} to re-sign the receipt"
            )
        elif state == READINESS_UNOBSERVED:
            # Not "the engine is down": nobody has probed it inside the freshness
            # window, so the platform must stop promising capacity until it has.
            remediation = (
                "admission refuses this instance with 503 engine_not_ready; no health "
                f"probe has re-signed the receipt inside "
                f"{DEFAULT_READINESS_FRESHNESS_US // 1_000_000} s, so run {RENEW_COMMAND} "
                "and check the renewal task (a failed probe names the engine)"
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
                "observed_at_us": receipt.verified_at_us,
                "observed_seconds_ago": observed_us // 1_000_000,
                "remediation": remediation,
            }
        )
    return report


def with_node_generations(
    report: Sequence[Row],
    *,
    endpoint: ControllerEndpoint | None,
    read: Callable[..., NodeInstanceHead] | None = None,
    timeout_seconds: float = 5.0,
) -> list[Row]:
    """Downgrade every row whose generation the node never prepared (#1282).

    The report is the surface operators read, so a receipt signed for a generation the
    node does not serve must stop reading ``valid`` here even while its 24 h window has
    hours left: waiting for it to expire reports a healthy engine for a day after the
    divergence is already known. ``invalid`` is the state admission already refuses for,
    so the report and the create path keep answering the same thing.
    """
    if endpoint is None:
        return list(report)
    reader = read_head if read is None else read
    checked: list[Row] = []
    for entry in report:
        node_id = entry.get("target_node_id")
        if not isinstance(node_id, str) or not node_id:
            checked.append(entry)
            continue
        try:
            head = reader(
                endpoint,
                node_id=node_id,
                instance_id=str(entry["instance_id"]),
                timeout_seconds=timeout_seconds,
            )
        except ControllerConsultationError as error:
            # Unknown is not healthy: an unverifiable row is reported as unfinished
            # verification rather than carrying a window nobody checked.
            checked.append(
                {
                    **entry,
                    "readiness_state": READINESS_INVALID,
                    "expires_at_us": None,
                    "remaining_seconds": None,
                    "reason": error.code,
                    "remediation": (
                        f"the node controller could not be asked which generation it prepared "
                        f"({error.message}); fix {CONTROLLER_URL_VARIABLE} or the controller, "
                        "then re-run this report"
                    ),
                }
            )
            continue
        disagreement = generation_disagreement(
            int(entry["instance_generation"]), head, node_id=node_id
        )
        if disagreement is None:
            checked.append(entry)
            continue
        checked.append(
            {
                **entry,
                "readiness_state": READINESS_INVALID,
                "expires_at_us": None,
                "remaining_seconds": None,
                "reason": "instance_generation_mismatch",
                "remediation": disagreement,
            }
        )
    return checked


def _summary(report: Sequence[Row]) -> Row:
    counts = {
        READINESS_VALID: 0,
        READINESS_EXPIRING: 0,
        READINESS_UNOBSERVED: 0,
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
        + counts[READINESS_UNOBSERVED]
        + counts[READINESS_MISSING]
        + counts[READINESS_INVALID],
    }


_QUERY = (
    "SELECT e.instance_id, e.instance_generation, e.host_node_id, "
    "h.current_prepare_intent_id, i.request_document, i.readiness_document "
    "FROM engine_instances AS e "
    "JOIN runtime_instance_heads AS h ON h.instance_id = e.instance_id "
    "AND h.current_generation = e.instance_generation "
    "LEFT JOIN runtime_instance_intents AS i ON i.target_node_id = e.host_node_id "
    "AND i.intent_id = h.current_prepare_intent_id "
    "WHERE e.instance_state = 'ready' "
    "ORDER BY e.instance_id, e.instance_generation"
)


def _queried_rows(database_url: str, schema: str, *, connect_timeout_seconds: int) -> list[Row]:
    connection = portable_connect(
        database_url,
        row_factory=dict_row,
        connect_timeout=connect_timeout_seconds,
    )
    try:
        # The report must never depend on write rights, and a read-only session
        # makes that a database-enforced property rather than a promise.
        connection.read_only = True
        with connection.cursor() as cursor:
            cursor.execute("SELECT set_config('search_path', ?, true)", (schema + ",pg_catalog",))
            # 「现在」只从便携时钟读一次;语句由 profile 登记。
            now_us = now_us_for(cast(ClockReader, cursor))
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
    try:
        endpoint = configured_endpoint(source)
    except ControllerConsultationError as invalid:
        parser.exit(EXIT_INVALID, f"error: {invalid.code}: {invalid.message}\n")
    report = with_node_generations(report, endpoint=endpoint)
    for entry in report:
        print(json.dumps(entry, sort_keys=True), file=stream)
    summary = _summary(report)
    print(json.dumps({"summary": summary}, sort_keys=True), file=stream)
    for entry in report:
        if entry["readiness_state"] in (
            READINESS_EXPIRED,
            READINESS_UNOBSERVED,
            READINESS_MISSING,
            READINESS_INVALID,
        ):
            failures.write(
                f"error: readiness_{entry['readiness_state']}: "
                f"{entry['instance_id']} g{entry['instance_generation']} "
                f"(expires_at_us={entry['expires_at_us']}, "
                f"observed_seconds_ago={entry.get('observed_seconds_ago')})\n"
            )
    if (
        summary[READINESS_EXPIRED]
        or summary[READINESS_UNOBSERVED]
        or summary[READINESS_MISSING]
        or summary[READINESS_INVALID]
    ):
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
