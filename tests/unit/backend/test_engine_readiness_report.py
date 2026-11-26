"""The readiness report names valid, expiring and expired receipts without writing.

#828: an elapsed engine readiness receipt used to look exactly like a broken store
-- every public Job create answered ``503 job_unavailable`` ("Job storage is
unavailable") and nothing reported how much of the receipt window was left, so the
only recovery was an operator re-running the registration by hand.

``speechnexus-engine-readiness`` is the read-only half of the renewal path. These
cases pin the three facts an operator or monitor depends on:

* one classification per ready instance against the *database* clock admission
  reads, with the warning window as a reporting threshold rather than a rule;
* the exit code that makes an expired (or unusable) receipt alertable, and the
  ``--fail-on-expiring`` switch a supervisor can tighten to;
* the absence of any write: the command opens a read-only session and the report
  is a pure function of the rows and that clock.
"""

from __future__ import annotations

import inspect
import json
from typing import Any

import psycopg
import pytest

from speechnexus_backend import engine_readiness
from speechnexus_backend.engine_registration import EXIT_INVALID, EXIT_UNAVAILABLE
from speechnexus_backend.speech.domain.capacity import PhysicalResources
from speechnexus_backend.speech.domain.runtime_intents import ApprovedRuntimePlan, RuntimeBinding
from speechnexus_backend.speech.domain.runtime_observations import VerifiedRuntimeReadiness
from speechnexus_backend.speech.infrastructure.runtime_observation_codec import (
    readiness_to_document,
)

NOW = 1_000_000_000
DIGEST = "sha256:" + "a" * 64
DSN = "postgresql://operator@127.0.0.1:5432/synthetic"
ZERO = PhysicalResources(0, 0, 0, 0, 0, 0)


def _plan() -> ApprovedRuntimePlan:
    return ApprovedRuntimePlan(
        RuntimeBinding("node-1", "asr-batch", "gateway-1", "tpl-1", "bind-1", "manifest-1", DIGEST),
        DIGEST,
        DIGEST,
        "discrete_device_memory",
        "batch",
        "resident",
        2,
        0,
        ZERO,
        ZERO,
        ZERO,
    )


def _receipt(*, verified: int, expires: int) -> str:
    plan = _plan()
    return readiness_to_document(
        VerifiedRuntimeReadiness(
            plan.binding,
            "instance-1",
            1,
            1,
            plan.topology_digest,
            plan.qualification_digest,
            DIGEST,
            verified,
            expires,
        )
    )


def _row(*, document: str | None, now_us: int = NOW) -> dict[str, Any]:
    return {
        "instance_id": "instance-1",
        "instance_generation": 1,
        "host_node_id": "node-1",
        "current_prepare_intent_id": "register-instance-1-g1",
        "readiness_document": document,
        "database_now_us": now_us,
    }


def test_the_report_separates_valid_expiring_and_expired_against_one_clock() -> None:
    report = engine_readiness.readiness_report(
        [
            _row(document=_receipt(verified=NOW - 10, expires=NOW + 60_000_000_000)),
            _row(document=_receipt(verified=NOW - 10, expires=NOW + 3_600_000_000)),
            _row(document=_receipt(verified=NOW - 10, expires=NOW)),
        ],
        now_us=NOW,
        warn_within_us=21_600_000_000,
    )
    assert [entry["readiness_state"] for entry in report] == ["valid", "expiring", "expired"]
    assert report[0]["remaining_seconds"] == 60_000
    assert report[1]["remaining_seconds"] == 3_600
    assert report[2]["remaining_seconds"] == 0
    # Only a state with a recovery action carries one, and it names the entry point.
    assert report[0]["remediation"] is None
    assert "speechnexus-register-engine --renew-readiness" in report[1]["remediation"]
    assert "503 engine_not_ready" in report[2]["remediation"]


def test_a_ready_instance_without_a_usable_receipt_is_reported_rather_than_skipped() -> None:
    report = engine_readiness.readiness_report(
        [_row(document=None), _row(document="not-a-json-document")],
        now_us=NOW,
        warn_within_us=0,
    )
    assert [entry["readiness_state"] for entry in report] == ["missing", "invalid"]
    assert all(entry["expires_at_us"] is None for entry in report)
    assert all(
        "speechnexus-register-engine --renew-readiness" in entry["remediation"] for entry in report
    )


def test_main_reports_every_instance_then_a_summary_and_exits_zero_for_valid(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rows = [
        _row(document=_receipt(verified=NOW - 10, expires=NOW + 60_000_000_000)),
        _row(document=_receipt(verified=NOW - 10, expires=NOW + 3_600_000_000)),
    ]
    code = engine_readiness.main(["--database-url", DSN], env={}, connect=lambda *_, **__: rows)
    captured = capsys.readouterr()
    documents = [json.loads(line) for line in captured.out.splitlines() if line]
    assert code == 0
    assert len(documents) == 3
    assert documents[-1]["summary"] == {
        "ready_instances": 2,
        "valid": 1,
        "expiring": 1,
        "unobserved": 0,
        "expired": 0,
        "missing": 0,
        "invalid": 0,
        "unusable": 0,
    }
    # An expiring receipt is visible on stdout and is not an error by default.
    assert "expiring" in captured.out and captured.err == ""


def test_main_fails_on_an_expired_receipt_so_a_timer_or_probe_can_alert(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rows = [_row(document=_receipt(verified=NOW - 10, expires=NOW))]
    code = engine_readiness.main(["--database-url", DSN], env={}, connect=lambda *_, **__: rows)
    captured = capsys.readouterr()
    assert code == EXIT_UNAVAILABLE
    assert "error: readiness_expired: instance-1 g1" in captured.err


def test_main_can_treat_an_expiring_receipt_as_an_alert(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rows = [_row(document=_receipt(verified=NOW - 10, expires=NOW + 3_600_000_000))]
    code = engine_readiness.main(
        ["--database-url", DSN, "--fail-on-expiring"],
        env={},
        connect=lambda *_, **__: rows,
    )
    captured = capsys.readouterr()
    assert code == EXIT_UNAVAILABLE
    assert "error: readiness_expiring" in captured.err


def test_main_reports_an_empty_deployment_as_zero_instances(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = engine_readiness.main(["--database-url", DSN], env={}, connect=lambda *_, **__: [])
    documents = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line]
    assert code == 0
    assert documents == [
        {
            "summary": {
                "ready_instances": 0,
                "valid": 0,
                "expiring": 0,
                "unobserved": 0,
                "expired": 0,
                "missing": 0,
                "invalid": 0,
                "unusable": 0,
            }
        }
    ]


def test_main_keeps_a_database_failure_distinct_from_an_expired_receipt(
    capsys: pytest.CaptureFixture[str],
) -> None:
    def connect(*_: object, **__: object) -> list[dict[str, Any]]:
        raise psycopg.OperationalError("synthetic storage failure")

    code = engine_readiness.main(["--database-url", DSN], env={}, connect=connect)
    captured = capsys.readouterr()
    # The report could not read the facts; it must not claim every receipt expired.
    assert code == EXIT_UNAVAILABLE
    assert captured.out == ""
    assert "database_unavailable" in captured.err and "readiness_" not in captured.err


def test_main_requires_a_database_url_before_connecting(
    capsys: pytest.CaptureFixture[str],
) -> None:
    def connect(*_: object, **__: object) -> list[dict[str, Any]]:
        raise AssertionError("no connection without a DSN")

    with pytest.raises(SystemExit) as exited:
        engine_readiness.main([], env={}, connect=connect)
    assert exited.value.code == EXIT_INVALID
    assert "invalid_settings" in capsys.readouterr().err


def test_the_report_cannot_renew_a_receipt() -> None:
    # A receipt must come from a real health probe, so the report holds no write
    # authority: the session is read-only and the only statement it sends is a
    # SELECT over the three coordination tables.
    source = inspect.getsource(engine_readiness._queried_rows)
    assert "connection.read_only = True" in source
    assert "engine_instances" in engine_readiness._QUERY
    for verb in ("INSERT", "UPDATE", "DELETE", "TRUNCATE"):
        assert verb not in engine_readiness._QUERY


def test_a_receipt_nobody_re_signed_is_reported_as_unobserved_and_alerts() -> None:
    """A window with 23 h left is not a licence to promise capacity now."""
    later = NOW + 3_600 * 1_000_000
    stale = _receipt(verified=NOW, expires=NOW + 24 * 3_600 * 1_000_000)
    report = engine_readiness.readiness_report(
        [_row(document=stale)], now_us=later, warn_within_us=0
    )
    entry = report[0]

    assert entry["readiness_state"] == "unobserved"
    assert entry["remaining_seconds"] > 0
    assert entry["observed_seconds_ago"] == 3600
    assert "no health probe has re-signed the receipt" in entry["remediation"]
    assert engine_readiness._summary(report)["unusable"] == 1


def test_main_alerts_on_an_unobserved_receipt_without_being_asked(
    capsys: pytest.CaptureFixture[str],
) -> None:
    later = NOW + 3_600 * 1_000_000
    rows = [
        _row(document=_receipt(verified=NOW, expires=NOW + 24 * 3_600 * 1_000_000), now_us=later)
    ]
    code = engine_readiness.main(["--database-url", DSN], env={}, connect=lambda *_, **__: rows)
    captured = capsys.readouterr()

    assert code == EXIT_UNAVAILABLE
    assert "error: readiness_unobserved: instance-1 g1" in captured.err
    assert "observed_seconds_ago=3600" in captured.err
