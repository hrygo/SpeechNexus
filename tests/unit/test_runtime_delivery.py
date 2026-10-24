"""Before-wire and cancellation windows preserve original identity and unknowns."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock, Mock

import pytest

from speechnexus_backend.speech.application.runtime_controller import RuntimeController
from speechnexus_backend.speech.application.runtime_delivery import (
    RuntimeDeliveryService,
    RuntimeDeliveryTransactions,
)
from speechnexus_backend.speech.domain.runtime_controller import (
    ControllerCallError,
    ControllerFailure,
    ControllerObservation,
)
from speechnexus_backend.speech.domain.runtime_delivery import (
    DeliveryClaim,
    DeliveryClaimRequest,
    DeliveryStart,
    DeliveryTicket,
    DeliveryUnavailableError,
    DeliveryWrite,
    DeliveryWriteOutcome,
)
from tests.fake.runtime_intents import NOW, REQUEST
from tests.fake.runtime_observations import OBSERVATION

CLAIM = DeliveryClaimRequest("delivery-test", ("host-test",), 30000000)
FRESH = DeliveryTicket(
    "runtime-event-test",
    "sha256:" + "a" * 64,
    CLAIM.holder_id,
    2,
    NOW + 30000000,
    "unsent",
    REQUEST,
)
STARTED = replace(FRESH, phase="unknown", version=3)
CANDIDATE = ControllerObservation(OBSERVATION, "synthetic unqualified transport candidate")


def setup(ticket: DeliveryTicket = FRESH) -> tuple[RuntimeDeliveryService, Mock, AsyncMock]:
    transactions = Mock(spec=RuntimeDeliveryTransactions)
    transactions.claim.return_value = DeliveryClaim("claimed", ticket)
    transactions.start.return_value = DeliveryStart("committed", STARTED, True)
    transactions.acknowledge.return_value = DeliveryWrite("committed")
    transactions.defer.return_value = DeliveryWrite("committed")
    controller = AsyncMock(spec=RuntimeController)
    controller.submit.return_value = CANDIDATE
    controller.query.return_value = CANDIDATE
    service = RuntimeDeliveryService(transactions, {"host-test": controller}, retry_delay_us=100000)
    return service, transactions, controller


async def test_fresh_submit_requires_acknowledged_marker_then_transport_ack() -> None:
    service, transactions, controller = setup()
    batch = await service.run_once(CLAIM, limit=1)
    transactions.start.assert_called_once_with(FRESH)
    controller.submit.assert_awaited_once_with(REQUEST)
    controller.query.assert_not_awaited()
    transactions.acknowledge.assert_called_once_with(STARTED, CANDIDATE.original_document)
    assert batch.acknowledged == batch.claimed == 1


async def test_unknown_queries_the_same_original_without_reopening_submission() -> None:
    service, transactions, controller = setup(STARTED)
    batch = await service.run_once(CLAIM, limit=1)
    transactions.start.assert_not_called()
    controller.submit.assert_not_awaited()
    controller.query.assert_awaited_once_with(REQUEST)
    assert batch.acknowledged == 1


@pytest.mark.parametrize("outcome", ["unknown", "stale", "rejected"])
async def test_no_http_without_a_committed_before_wire_marker(
    outcome: DeliveryWriteOutcome,
) -> None:
    service, transactions, controller = setup()
    transactions.start.return_value = DeliveryStart(outcome)
    batch = await service.run_once(CLAIM, limit=1)
    controller.submit.assert_not_awaited()
    controller.query.assert_not_awaited()
    transactions.acknowledge.assert_not_called()
    assert batch.stale + batch.unknown == 1


async def test_lost_start_commit_ack_never_sends_post() -> None:
    service, transactions, controller = setup()
    transactions.start.side_effect = DeliveryUnavailableError
    batch = await service.run_once(CLAIM, limit=1)
    assert batch.unknown == 1
    controller.submit.assert_not_awaited()
    controller.query.assert_not_awaited()


@pytest.mark.parametrize("may_have_run", [False, True])
async def test_even_local_call_failure_cannot_reopen_an_already_started_intent(
    may_have_run: bool,
) -> None:
    service, transactions, controller = setup()
    controller.submit.side_effect = ControllerCallError(
        ControllerFailure.UNAVAILABLE, request_may_have_run=may_have_run
    )
    first = await service.run_once(CLAIM, limit=1)
    assert first.unknown == 1
    transactions.defer.assert_called_once_with(STARTED, "unavailable", 100000)
    transactions.claim.return_value = DeliveryClaim("claimed", replace(STARTED, version=5))
    second = await service.run_once(CLAIM, limit=1)
    controller.submit.assert_awaited_once_with(REQUEST)
    controller.query.assert_awaited_once_with(REQUEST)
    assert second.acknowledged == 1


async def test_original_query_404_is_not_absence_or_a_successful_delivery() -> None:
    service, transactions, controller = setup(STARTED)
    controller.query.side_effect = ControllerCallError(
        ControllerFailure.NOT_FOUND, request_may_have_run=False
    )
    result = await service.run_once(CLAIM, limit=1)
    assert result.unknown == 1 and result.acknowledged == 0
    controller.submit.assert_not_awaited()
    transactions.acknowledge.assert_not_called()
    transactions.defer.assert_called_once_with(STARTED, "not_found", 100000)


async def test_http_cancellation_only_retains_unknown_and_propagates() -> None:
    service, transactions, controller = setup()
    controller.submit.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await service.run_once(CLAIM, limit=1)
    transactions.defer.assert_called_once_with(STARTED, "cancelled", 100000)
    transactions.acknowledge.assert_not_called()
    controller.query.assert_not_awaited()


async def test_lost_ack_storage_reply_is_unknown_without_a_second_http_call() -> None:
    service, transactions, controller = setup()
    transactions.acknowledge.side_effect = DeliveryUnavailableError
    result = await service.run_once(CLAIM, limit=1)
    assert result.unknown == 1 and result.acknowledged == 0
    controller.submit.assert_awaited_once()
    controller.query.assert_not_awaited()


async def test_storage_claim_unknown_and_empty_never_contact_a_node() -> None:
    service, transactions, controller = setup()
    transactions.claim.side_effect = DeliveryUnavailableError
    assert (await service.run_once(CLAIM, limit=2)).unknown == 1
    transactions.claim.side_effect = None
    transactions.claim.return_value = DeliveryClaim("empty")
    assert (await service.run_once(CLAIM, limit=2)).claimed == 0
    controller.submit.assert_not_awaited()
    controller.query.assert_not_awaited()


async def test_withdrawn_or_superseded_fresh_intent_only_queries_the_original() -> None:
    service, transactions, controller = setup()
    transactions.start.return_value = DeliveryStart("committed", STARTED, False)
    assert (await service.run_once(CLAIM, limit=1)).acknowledged == 1
    controller.submit.assert_not_awaited()
    controller.query.assert_awaited_once_with(REQUEST)


@pytest.mark.parametrize("limit", [0, 33, True])
async def test_unbounded_batch_or_missing_node_mapping_fails_before_storage(limit: int) -> None:
    service, transactions, _ = setup()
    with pytest.raises(ValueError):
        await service.run_once(CLAIM, limit=limit)
    with pytest.raises(ValueError):
        await service.run_once(replace(CLAIM, target_node_ids=("host-unknown",)), limit=1)
    transactions.claim.assert_not_called()


def test_committed_start_cannot_be_a_fresh_or_missing_ticket() -> None:
    with pytest.raises(ValueError):
        DeliveryStart("committed", FRESH, True)
    with pytest.raises(ValueError):
        DeliveryStart("committed")
    with pytest.raises(ValueError):
        DeliveryStart("unknown", None, True)
    assert REQUEST.deadline.text not in repr(FRESH)
