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
from speechnexus_backend.controller_head import ControllerConsultationError, NodeInstanceHead
from speechnexus_backend.engine_registration import EXIT_INVALID, EXIT_UNAVAILABLE
from speechnexus_backend.speech.domain.capacity import PhysicalResources
from speechnexus_backend.speech.domain.runtime_intents import (
    ApprovedRuntimePlan,
    RuntimeBinding,
    RuntimeDeadline,
    RuntimeIntent,
)
from speechnexus_backend.speech.domain.runtime_observations import VerifiedRuntimeReadiness
from speechnexus_backend.speech.infrastructure.runtime_codec import encode_record, prepare_request
from speechnexus_backend.speech.infrastructure.runtime_observation_codec import (
    readiness_to_document,
    validate_stored_readiness,
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


def _request(plan: ApprovedRuntimePlan, *, fencing_token: int = 1) -> RuntimeIntent:
    return RuntimeIntent(
        "register-instance-1-g1",
        plan.binding,
        "instance-1",
        1,
        "prepare",
        fencing_token,
        plan.memory,
        RuntimeDeadline("2250-01-01T00:00:00.123456789Z"),
    )


def _request_document(*, fencing_token: int = 1, plan: ApprovedRuntimePlan | None = None) -> str:
    resolved = plan or _plan()
    return encode_record(
        prepare_request(_request(resolved, fencing_token=fencing_token)),
        resolved,
        "register-engine",
    )


def _receipt(*, verified: int, expires: int, token: int = 1) -> str:
    plan = _plan()
    return readiness_to_document(
        VerifiedRuntimeReadiness(
            plan.binding,
            "instance-1",
            1,
            token,
            plan.topology_digest,
            plan.qualification_digest,
            DIGEST,
            verified,
            expires,
        )
    )


def _row(
    *, document: str | None, now_us: int = NOW, request_document: str | None = None
) -> dict[str, Any]:
    return {
        "instance_id": "instance-1",
        "instance_generation": 1,
        "host_node_id": "node-1",
        "current_prepare_intent_id": "register-instance-1-g1",
        "request_document": _request_document() if request_document is None else request_document,
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


def test_a_receipt_that_describes_another_plan_is_invalid_not_valid() -> None:
    """#1246: the window is not the whole judgement.

    A receipt signed for a different fencing epoch has a perfectly valid window, so
    classifying the window alone reported it ``valid`` while admission refused the
    instance as ``invalid``. The report must answer the question admission answers.
    """
    stale_epoch = _receipt(verified=NOW - 10, expires=NOW + 60_000_000_000, token=9)

    report = engine_readiness.readiness_report(
        [_row(document=stale_epoch)], now_us=NOW, warn_within_us=0
    )

    assert report[0]["readiness_state"] == "invalid"
    assert report[0]["expires_at_us"] is None
    assert report[0]["remaining_seconds"] is None
    assert "fencing_token" in report[0]["reason"]
    assert engine_readiness._summary(report)["unusable"] == 1


def test_the_report_and_admission_refuse_the_same_receipt_for_the_same_reason() -> None:
    """One judgement, two exits: the report's reason is the validator's reason."""
    plan = _plan()
    stale_epoch = _receipt(verified=NOW - 10, expires=NOW + 60_000_000_000, token=9)

    report = engine_readiness.readiness_report(
        [_row(document=stale_epoch)], now_us=NOW, warn_within_us=0
    )
    with pytest.raises(ValueError) as refused:
        validate_stored_readiness(stale_epoch, _request(plan), plan)

    assert report[0]["readiness_state"] == "invalid"
    assert "fencing_token" in report[0]["reason"]
    assert "fencing_token" in str(refused.value)


def test_a_ready_instance_whose_stored_plan_does_not_decode_is_invalid() -> None:
    report = engine_readiness.readiness_report(
        [
            _row(
                document=_receipt(verified=NOW - 10, expires=NOW + 60_000_000_000),
                request_document="{not a runtime record",
            )
        ],
        now_us=NOW,
        warn_within_us=0,
    )
    entry = report[0]

    assert entry["readiness_state"] == "invalid"
    assert "does not decode" in entry["reason"]
    assert engine_readiness._summary(report)["unusable"] == 1


def test_main_alerts_when_a_receipt_describes_another_plan(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rows = [_row(document=_receipt(verified=NOW - 10, expires=NOW + 60_000_000_000, token=9))]
    code = engine_readiness.main(["--database-url", DSN], env={}, connect=lambda *_, **__: rows)
    captured = capsys.readouterr()
    documents = [json.loads(line) for line in captured.out.splitlines() if line]

    assert code == EXIT_UNAVAILABLE
    assert documents[0]["readiness_state"] == "invalid"
    assert "fencing_token" in documents[0]["reason"]
    assert "error: readiness_invalid: instance-1 g1" in captured.err


def test_a_generation_the_node_never_prepared_stops_reading_valid(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1282: the receipt said gen 1, the node had prepared gen 11.

    Waiting for the receipt's own window to elapse would report a healthy engine for the
    rest of the day after the divergence is already known, so the node is asked and the
    row is downgraded to the state admission refuses for.
    """
    rows = [_row(document=_receipt(verified=NOW - 10, expires=NOW + 60_000_000_000))]
    monkeypatch.setattr(
        engine_readiness,
        "read_head",
        lambda *_, **__: NodeInstanceHead("instance-1", 11, 11),
    )
    code = engine_readiness.main(
        ["--database-url", DSN],
        env={
            "SPEECHNEXUS_CONTROLLER_API_URL": "http://127.0.0.1:8080",
            "SPEECHNEXUS_CONTROLLER_API_TOKEN": "token",
        },
        connect=lambda *_, **__: rows,
    )
    captured = capsys.readouterr()
    entry = json.loads(captured.out.splitlines()[0])

    assert code == EXIT_UNAVAILABLE
    assert entry["readiness_state"] == "invalid"
    assert entry["reason"] == "instance_generation_mismatch"
    assert entry["expires_at_us"] is None
    assert "generation 11" in entry["remediation"]
    assert "error: readiness_invalid: instance-1 g1" in captured.err


def test_an_agreeing_node_leaves_the_report_untouched(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = [_row(document=_receipt(verified=NOW - 10, expires=NOW + 60_000_000_000))]
    monkeypatch.setattr(
        engine_readiness,
        "read_head",
        lambda *_, **__: NodeInstanceHead("instance-1", 1, 1),
    )
    code = engine_readiness.main(
        ["--database-url", DSN],
        env={
            "SPEECHNEXUS_CONTROLLER_API_URL": "http://127.0.0.1:8080",
            "SPEECHNEXUS_CONTROLLER_API_TOKEN": "token",
        },
        connect=lambda *_, **__: rows,
    )
    captured = capsys.readouterr()
    assert code == 0
    assert json.loads(captured.out.splitlines()[0])["readiness_state"] == "valid"


def test_an_unreachable_controller_makes_the_report_alert_instead_of_guessing(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Unknown is not healthy: a row nobody could verify must not carry a window.
    rows = [_row(document=_receipt(verified=NOW - 10, expires=NOW + 60_000_000_000))]

    def unavailable(*_: object, **__: object) -> object:
        raise ControllerConsultationError("controller_unreachable", "the controller is unreachable")

    monkeypatch.setattr(engine_readiness, "read_head", unavailable)
    code = engine_readiness.main(
        ["--database-url", DSN],
        env={
            "SPEECHNEXUS_CONTROLLER_API_URL": "http://127.0.0.1:9",
            "SPEECHNEXUS_CONTROLLER_API_TOKEN": "token",
        },
        connect=lambda *_, **__: rows,
    )
    captured = capsys.readouterr()
    entry = json.loads(captured.out.splitlines()[0])

    assert code == EXIT_UNAVAILABLE
    assert (entry["readiness_state"], entry["reason"]) == ("invalid", "controller_unreachable")
    assert "SPEECHNEXUS_CONTROLLER_API_URL" in entry["remediation"]


def test_a_report_without_a_controller_keeps_the_previous_behaviour(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rows = [_row(document=_receipt(verified=NOW - 10, expires=NOW + 60_000_000_000))]
    code = engine_readiness.main(["--database-url", DSN], env={}, connect=lambda *_, **__: rows)
    captured = capsys.readouterr()
    assert code == 0
    assert json.loads(captured.out.splitlines()[0])["readiness_state"] == "valid"
