"""ASR settlement debt is a bounded operations view, never a readiness verdict."""

from __future__ import annotations

from datetime import UTC, datetime

from speechnexus_backend.speech.application.readiness import (
    ProbeResult,
    ProbeStatus,
    ReadinessReport,
)
from speechnexus_backend.speech.infrastructure.asr_settlement_debt import summarize
from speechnexus_backend.speech.interfaces.http.diagnostics import diagnostics_body


def row(
    operation_id: str,
    *,
    state: str | None = "succeeded",
    cleanup: str | None = "complete",
    settled: int | None = None,
    gateway: str = "gateway-test",
    host: str = "host-test",
) -> dict[str, object]:
    return {
        "operation_id": operation_id,
        "lease_id": "lease-" + operation_id,
        "gateway_id": gateway,
        "host_node_id": host,
        "settled_at_us": settled,
        "execution_state": state,
        "cleanup_state": cleanup,
        "evidence_kind": "engine_terminal_response",
        "execution_stopped": 1,
        "resources_released": 1,
        "overdue_us": 5,
    }


def test_debt_categories_are_counted_and_sampled() -> None:
    debt = summarize(
        [
            row("operation-unknown", state="unknown_quarantined"),
            row("operation-cleanup", cleanup="pending"),
            row("operation-other-route", gateway="elsewhere"),
            row("operation-settled", settled=1),
        ],
        ("gateway-test", "host-test"),
    )
    assert (
        debt.unknown,
        debt.cleanup_pending,
        debt.route_mismatch,
        debt.settled_but_held,
        debt.redacted_held,
    ) == (1, 1, 1, 1, 0)
    assert debt.total == 4
    assert len(debt.samples) == 3
    assert "unknown=1" in debt.detail()


def test_no_route_means_no_route_mismatch() -> None:
    debt = summarize([row("operation-any", gateway="elsewhere", host="elsewhere")], None)
    assert (debt.route_mismatch, debt.total) == (0, 0)


def test_healthy_work_is_not_debt() -> None:
    debt = summarize([row("operation-live")], ("gateway-test", "host-test"))
    assert debt.total == 0
    assert (
        debt.detail()
        == "unknown=0 cleanup_pending=0 route_mismatch=0 settled_but_held=0 redacted_held=0"
    )


def test_diagnostics_append_extra_checks_without_changing_readiness() -> None:
    report = ReadinessReport(
        checks=(ProbeResult("database", ProbeStatus.OK, "ok"),),
    )
    body = diagnostics_body(
        report,
        datetime(2026, 11, 18, tzinfo=UTC),
        ({"name": "asr_settlement_debt", "status": "ok", "detail": "unknown=0"},),
    )
    assert body["status"] == "ok"
    checks = body["checks"]
    assert isinstance(checks, list)
    assert [check["name"] for check in checks if isinstance(check, dict)] == [
        "database",
        "asr_settlement_debt",
    ]


def test_succeeded_with_abort_evidence_is_cleanup_debt() -> None:
    debt = summarize(
        [
            {
                **row("operation-abort-proof"),
                "evidence_kind": "engine_abort_ack",
                "execution_stopped": 1,
                "resources_released": 1,
            }
        ],
        ("gateway-test", "host-test"),
    )
    assert (debt.cleanup_pending, debt.total) == (1, 1)
    assert "cleanup_pending:operation-abort-proof" in debt.detail()


def test_redacted_held_root_less_lease_is_counted() -> None:
    debt = summarize(
        [{**row("operation-redacted"), "source": "redacted"}],
        ("gateway-test", "host-test"),
    )
    assert (debt.redacted_held, debt.total) == (1, 1)
    assert "redacted_held:operation-redacted" in debt.detail()
