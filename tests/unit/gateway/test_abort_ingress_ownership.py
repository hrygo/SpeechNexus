"""Abort retains owner and serializes with sends without rejecting old tokens."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest

from speechnexus_gateway.application.ingress import GatewayIngress
from speechnexus_gateway.domain.fencing import BarrierRequest, IngressUnavailableError
from speechnexus_gateway.infrastructure.process_ownership import ProcessIngressOwnership
from tests.fake.execution import InMemorySubmissionLedger
from tests.unit.gateway.test_submit_execution import COMMAND, NOW, identity_for


def test_abort_owner_cannot_be_replaced_until_bounded_send_returns(tmp_path: Path) -> None:
    owner = ProcessIngressOwnership(tmp_path, node_id="node-1", gateway_id="gateway-1")
    ingress = GatewayIngress(owner, timeout_seconds=0.05)
    identity = identity_for(COMMAND)
    entered, resume = Event(), Event()

    def abort() -> None:
        with ingress.abort_dispatch(identity):
            entered.set()
            assert resume.wait(5)

    with ThreadPoolExecutor(max_workers=2) as workers:
        sending = workers.submit(abort)
        assert entered.wait(2)
        closing = workers.submit(owner.close)
        try:
            with pytest.raises(TimeoutError):
                closing.result(timeout=0.05)
            with pytest.raises(IngressUnavailableError):
                ProcessIngressOwnership(tmp_path, node_id="node-1", gateway_id="gateway-1")
        finally:
            resume.set()
        sending.result(5)
        closing.result(5)
    replacement = ProcessIngressOwnership(tmp_path, node_id="node-1", gateway_id="gateway-1")
    replacement.close()


def test_new_fencing_floor_does_not_forbid_abort_of_old_execution() -> None:
    from tests.fake.execution import fake_ingress

    ingress = fake_ingress(timeout_seconds=0.05)
    ingress.seal(
        BarrierRequest("asr-batch", "scheduler-new", 8, False),
        InMemorySubmissionLedger(now=lambda: NOW),
    )
    with (
        ingress.abort_dispatch(identity_for(COMMAND)),
        pytest.raises(IngressUnavailableError),
        ingress.dispatch(identity_for(COMMAND)),
    ):
        pytest.fail("submit was allowed through the held abort lane")
