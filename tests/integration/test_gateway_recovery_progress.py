"""PG/InnoDB scan progress and actual concurrent generated-usage writes.

No engine/GPU is contacted. PG uses the production gateway adapter and grants;
InnoDB executes the same SQL/domain rules through the established test oracle.
"""

from concurrent.futures import ThreadPoolExecutor
from threading import Event
from typing import Any

import pytest
from tests.fake.execution import fake_ingress
from tests.integration.test_coordination_foundation import Foundation
from tests.integration.test_coordination_foundation import foundation as foundation
from tests.integration.test_gateway_recovery import Ledger
from tests.integration.test_gateway_recovery import recovery_ledger as recovery_ledger
from tests.integration.test_gateway_recovery_sweep import (
    ScriptedTerminations,
    termination_evidence_for,
    uncertain_execution,
)
from tests.integration.test_gateway_stop_evidence import counters
from tests.integration.test_gateway_stop_evidence import stop_ledger as stop_ledger
from tests.integration.test_gateway_submit_ledger import seed_second_attempt
from tests.integration.test_production_jobs import database as database

from speechnexus_gateway.application.recover_executions import ExecutionRecoveryService
from speechnexus_gateway.domain.output_intervals import (
    AppendInterval,
    MeterKind,
    OutputIntervalRecord,
)
from speechnexus_gateway.domain.submission import ExecutionState

pytestmark = pytest.mark.integration


def test_unresolved_prefix_does_not_starve_the_next_database_row(
    foundation: Foundation, recovery_ledger: Ledger
) -> None:
    seed_second_attempt(foundation)
    submissions, first = uncertain_execution(recovery_ledger)
    _, second = uncertain_execution(
        recovery_ledger,
        submit_intent_id="intent-b",
        lease_id="lease-alt-stage-alt",
        stage_id="stage-alt",
        fencing_token=2,
    )
    original = recovery_ledger.get(first)
    before = counters(foundation)
    terminations = ScriptedTerminations()
    terminations.evidence[second] = termination_evidence_for(second)
    scanner = ExecutionRecoveryService(
        ingress=fake_ingress(node_id="host-test", gateway_id="gateway-test"),
        ledger=recovery_ledger,
        submissions=submissions,
        terminations=terminations,
    )

    assert scanner.sweep(limit=1).still_unknown == 1
    report = scanner.sweep(limit=1)

    assert (report.examined, report.closed, report.still_unknown) == (1, 1, 0)
    assert scanner.sweep(limit=1).examined == 0  # End page resets only local visit state.
    assert scanner.sweep(limit=1).still_unknown == 1
    restarted = ExecutionRecoveryService(
        ingress=fake_ingress(node_id="host-test", gateway_id="gateway-test"),
        ledger=recovery_ledger,
        submissions=submissions,
        terminations=terminations,
    )
    assert restarted.sweep(limit=1).still_unknown == 1
    assert recovery_ledger.get(first) == original
    assert counters(foundation) == before


def test_cursor_read_preserves_scope_order_and_all_durable_rows(
    foundation: Foundation, recovery_ledger: Ledger
) -> None:
    seed_second_attempt(foundation)
    _, first = uncertain_execution(recovery_ledger)
    _, second = uncertain_execution(
        recovery_ledger,
        submit_intent_id="intent-b",
        lease_id="lease-alt-stage-alt",
        stage_id="stage-alt",
        fencing_token=2,
    )
    before = foundation.database.query("SELECT * FROM submit_intents ORDER BY submit_intent_id")
    budgets = counters(foundation)
    page = recovery_ledger.list_quarantined(
        gateway_id="gateway-test",
        host_node_id="host-test",
        limit=1,
        after_submit_intent_id=first,
    )
    assert [row.identity.submit_intent_id for row in page] == [second]
    for gateway, node in [
        ("gateway-other", "host-test"),
        ("gateway-test", "host-other"),
    ]:
        assert (
            recovery_ledger.list_quarantined(
                gateway_id=gateway,
                host_node_id=node,
                limit=1,
                after_submit_intent_id=first,
            )
            == []
        )
    assert (
        foundation.database.query("SELECT * FROM submit_intents ORDER BY submit_intent_id")
        == before
    )
    assert counters(foundation) == budgets


def test_failed_source_keeps_original_fact_and_allows_the_next_native_row(
    foundation: Foundation, recovery_ledger: Ledger
) -> None:
    seed_second_attempt(foundation)
    submissions, first = uncertain_execution(recovery_ledger)
    _, second = uncertain_execution(
        recovery_ledger,
        submit_intent_id="intent-b",
        lease_id="lease-alt-stage-alt",
        stage_id="stage-alt",
        fencing_token=2,
    )
    original, before = recovery_ledger.get(first), counters(foundation)

    class FailingSource(ScriptedTerminations):
        def termination_evidence(self, intent: Any) -> Any:
            if intent.identity.submit_intent_id == first:
                raise RuntimeError("synthetic source failure")
            return super().termination_evidence(intent)

    source = FailingSource()
    source.evidence[second] = termination_evidence_for(second)
    scanner = ExecutionRecoveryService(
        ingress=fake_ingress(node_id="host-test", gateway_id="gateway-test"),
        ledger=recovery_ledger,
        submissions=submissions,
        terminations=source,
    )
    with pytest.raises(RuntimeError, match="synthetic source failure"):
        scanner.sweep(limit=1)
    assert scanner.sweep(limit=1).closed == 1
    assert recovery_ledger.get(first) == original
    assert counters(foundation) == before


@pytest.mark.parametrize("can_close", [False, True])
def test_concurrent_generated_append_does_not_double_count_or_lose_usage(
    foundation: Foundation, recovery_ledger: Ledger, can_close: bool
) -> None:
    seed_second_attempt(foundation)
    submissions, intent_id = uncertain_execution(recovery_ledger)
    original = recovery_ledger.get(intent_id)
    assert original is not None
    before = counters(foundation)
    enumerated, metered = Event(), Event()

    class PausedEnumeration:
        def __getattr__(self, name: str) -> Any:
            return getattr(recovery_ledger, name)

        def list_quarantined(self, **kwargs: Any) -> Any:
            candidates = recovery_ledger.list_quarantined(**kwargs)
            enumerated.set()
            assert metered.wait(5), "the actual concurrent append did not complete"
            return candidates

    terminations = ScriptedTerminations()
    if can_close:
        terminations.evidence[intent_id] = termination_evidence_for(intent_id)
    scanner = ExecutionRecoveryService(
        ingress=fake_ingress(node_id="host-test", gateway_id="gateway-test"),
        ledger=PausedEnumeration(),
        submissions=submissions,
        terminations=terminations,
    )
    with ThreadPoolExecutor(max_workers=1) as workers:
        scanning = workers.submit(scanner.sweep)
        try:
            assert enumerated.wait(5), "the scanner did not enumerate its snapshot"
            appended = recovery_ledger.append_interval(
                OutputIntervalRecord(
                    AppendInterval(
                        "interval-concurrent",
                        intent_id,
                        "tts-main",
                        24000,
                        MeterKind.GENERATED,
                        0,
                        100,
                    ),
                    original.identity,
                )
            )
            assert appended.intent.version == original.version + 1
            assert appended.intent.execution_state is ExecutionState.UNKNOWN_QUARANTINED
        finally:
            metered.set()
        report = scanning.result(timeout=5)

    assert (report.examined, report.observed, report.closed, report.still_unknown) == (
        1,
        0,
        int(can_close),
        int(not can_close),
    )
    current = recovery_ledger.get(intent_id)
    assert current is not None and current.generated_pcm_samples == 100
    assert current.version == original.version + 1 + int(can_close)
    assert current.execution_state is (
        ExecutionState.ABORTED if can_close else ExecutionState.UNKNOWN_QUARANTINED
    )
    assert counters(foundation) == before
    assert recovery_ledger.query_output_interval("interval-concurrent") is not None
