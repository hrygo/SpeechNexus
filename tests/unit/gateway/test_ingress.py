"""The committed-intent/pending-send race and the local gateway process lifetime."""

import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest

from speechnexus_gateway.application.ingress import GatewayIngress
from speechnexus_gateway.application.submit_execution import SubmitExecutionService
from speechnexus_gateway.domain.fencing import (
    BarrierRequest,
    BarrierSnapshot,
    IngressFence,
    IngressUnavailableError,
)
from speechnexus_gateway.domain.submission import (
    ExecutionCleanupPendingError,
    GatewayBarrierFencedError,
    InvalidExecutionRequestError,
    StaleFencingTokenError,
    SubmissionStoreError,
    SubmitRequest,
)
from speechnexus_gateway.infrastructure.process_ownership import ProcessIngressOwnership
from tests.fake.execution import InMemorySubmissionLedger, ScriptedEngine, fake_ingress
from tests.unit.gateway.test_submit_execution import COMMAND, NOW, identity_for

REQUEST = BarrierRequest("asr-batch", "scheduler-new", 8, False)


def test_seal_cannot_succeed_while_a_previously_persisted_send_can_still_resume() -> None:
    entered, resume = Event(), Event()
    ledger = InMemorySubmissionLedger(now=lambda: NOW)

    def pause(_: object) -> None:
        entered.set()
        assert resume.wait(5)

    engine = ScriptedEngine(before_dispatch=pause)
    ingress = fake_ingress(timeout_seconds=0.05)
    service = SubmitExecutionService(
        ledger=ledger, ingress=ingress, dispatch=engine, observation=engine
    )
    with ThreadPoolExecutor(max_workers=1) as workers:
        submitted = workers.submit(service.submit, SubmitRequest(identity_for(COMMAND), COMMAND))
        try:
            assert entered.wait(2)
            assert ledger.get("intent-1") is not None
            with pytest.raises(IngressUnavailableError):
                ingress.seal(REQUEST, ledger)
            assert not ledger._barriers
        finally:
            resume.set()
        submitted.result(5)
    proof = ingress.seal(REQUEST, ledger)
    assert proof.executing_intent_ids == ("intent-1",)
    assert proof.min_fencing_token == 8
    assert engine.dispatch_count == 1
    with pytest.raises(GatewayBarrierFencedError):
        service.submit(SubmitRequest(identity_for(COMMAND, submit_intent_id="intent-old"), COMMAND))
    assert engine.dispatch_count == 1
    # Existing identities stay queryable even after their token is sealed out.
    assert service.submit(SubmitRequest(identity_for(COMMAND), COMMAND)).idempotent_replay


def test_timeout_does_not_undo_the_local_floor() -> None:
    ingress = fake_ingress(timeout_seconds=0.02)
    ledger = InMemorySubmissionLedger(now=lambda: NOW)
    with ingress.dispatch(identity_for(COMMAND)), pytest.raises(IngressUnavailableError):
        ingress.seal(REQUEST, ledger)
    with pytest.raises(GatewayBarrierFencedError), ingress.dispatch(identity_for(COMMAND)):
        pytest.fail("stale entry reopened after a timeout")


def test_a_persistence_failure_does_not_undo_the_local_floor() -> None:
    class FailedLedger(InMemorySubmissionLedger):
        def seal_barrier(self, fence: IngressFence) -> BarrierSnapshot:
            raise SubmissionStoreError("synthetic store failure")

    ingress = fake_ingress()
    with pytest.raises(SubmissionStoreError):
        ingress.seal(REQUEST, FailedLedger())
    with pytest.raises(GatewayBarrierFencedError), ingress.dispatch(identity_for(COMMAND)):
        pytest.fail("stale entry reopened after persistence failure")


def test_barrier_minimum_cannot_move_backwards_and_abort_is_not_fabricated() -> None:
    ingress, ledger = fake_ingress(), InMemorySubmissionLedger(now=lambda: NOW)
    ingress.seal(REQUEST, ledger)
    with pytest.raises(StaleFencingTokenError):
        ingress.seal(BarrierRequest("asr-batch", "scheduler-new", 7, False), ledger)
    with pytest.raises(ExecutionCleanupPendingError):
        ingress.seal(BarrierRequest("asr-batch", "scheduler-new", 8, True), ledger)


@pytest.mark.parametrize(
    "fields",
    [
        {"gateway_id": "other-gateway"},
        {"host_node_id": "other-node"},
        {"pool_id": "unknown"},
    ],
)
def test_ingress_checks_its_physical_scope(fields: dict[str, object]) -> None:
    with (
        pytest.raises(InvalidExecutionRequestError),
        fake_ingress().dispatch(identity_for(COMMAND, **fields)),
    ):
        pytest.fail("foreign ingress accepted")


def test_a_second_process_cannot_acquire_the_live_gateway_identity(tmp_path: Path) -> None:
    owner = ProcessIngressOwnership(tmp_path, node_id="node-1", gateway_id="gateway-1")
    program = """
import sys
from pathlib import Path
from speechnexus_gateway.infrastructure.process_ownership import ProcessIngressOwnership
from speechnexus_gateway.domain.fencing import IngressUnavailableError
try:
    owner = ProcessIngressOwnership(Path(sys.argv[1]), node_id='node-1', gateway_id='gateway-1')
except IngressUnavailableError:
    sys.exit(3)
owner.close()
"""
    try:
        blocked = subprocess.run(
            [sys.executable, "-c", program, str(tmp_path)], timeout=5, check=False
        )
        assert blocked.returncode == 3
        with pytest.raises(IngressUnavailableError):
            ProcessIngressOwnership(tmp_path, node_id="node-1", gateway_id="gateway-1")
    finally:
        owner.close()
    restarted = subprocess.run(
        [sys.executable, "-c", program, str(tmp_path)], timeout=5, check=False
    )
    assert restarted.returncode == 0


def test_shutdown_keeps_ownership_until_the_last_send_exits(tmp_path: Path) -> None:
    owner = ProcessIngressOwnership(tmp_path, node_id="node-1", gateway_id="gateway-1")
    entered, resume, closing = Event(), Event(), Event()

    def hold() -> None:
        with owner.hold():
            entered.set()
            assert resume.wait(5)

    def close() -> None:
        closing.set()
        owner.close()

    with ThreadPoolExecutor(max_workers=2) as workers:
        sending = workers.submit(hold)
        assert entered.wait(2)
        shutdown = workers.submit(close)
        try:
            assert closing.wait(2)
            with pytest.raises(TimeoutError):
                shutdown.result(0.05)
            with pytest.raises(IngressUnavailableError):
                ProcessIngressOwnership(tmp_path, node_id="node-1", gateway_id="gateway-1")
        finally:
            resume.set()
        sending.result(5)
        shutdown.result(5)
    with pytest.raises(IngressUnavailableError), owner.hold():
        pytest.fail("closed owner reopened")
    owner.close()


def test_one_process_owner_cannot_create_two_independent_ingress_guards(tmp_path: Path) -> None:
    owner = ProcessIngressOwnership(tmp_path, node_id="node-1", gateway_id="gateway-1")
    try:
        GatewayIngress(owner)
        with pytest.raises(IngressUnavailableError):
            GatewayIngress(owner)
    finally:
        owner.close()


def test_maximum_length_node_and_gateway_identifiers_have_a_bounded_lock_path(
    tmp_path: Path,
) -> None:
    owner = ProcessIngressOwnership(tmp_path, node_id="n" * 128, gateway_id="g" * 128)
    owner.close()
    assert len(list(tmp_path.iterdir())) == 1
