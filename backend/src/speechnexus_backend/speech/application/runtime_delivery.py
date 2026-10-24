"""Deliver committed originals outside storage transactions, then retain only ACKs."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from contextlib import suppress
from typing import Protocol

from speechnexus_backend.speech.application.runtime_controller import RuntimeController
from speechnexus_backend.speech.domain.runtime_controller import ControllerCallError
from speechnexus_backend.speech.domain.runtime_delivery import (
    DeliveryBatch,
    DeliveryClaim,
    DeliveryClaimRequest,
    DeliveryStart,
    DeliveryTicket,
    DeliveryUnavailableError,
    DeliveryWrite,
)
from speechnexus_backend.speech.domain.runtime_intents import integer


class RuntimeDeliveryTransactions(Protocol):
    def claim(self, request: DeliveryClaimRequest) -> DeliveryClaim: ...

    def start(self, ticket: DeliveryTicket) -> DeliveryStart:
        """Commit the before-wire marker and recheck current dispatch eligibility."""
        ...

    def acknowledge(self, ticket: DeliveryTicket, original_response: str) -> DeliveryWrite:
        """Validate the original controller receipt and atomically mark transport ACK."""
        ...

    def defer(self, ticket: DeliveryTicket, failure_code: str, delay_us: int) -> DeliveryWrite:
        """Retain the current phase; an unknown delivery never becomes unsent."""
        ...


class RuntimeDeliveryService:
    def __init__(
        self,
        transactions: RuntimeDeliveryTransactions,
        controllers: Mapping[str, RuntimeController],
        *,
        retry_delay_us: int,
    ) -> None:
        integer(retry_delay_us, minimum=100000, maximum=300000000)
        self._transactions = transactions
        self._controllers = dict(controllers)
        self._retry_delay = retry_delay_us

    async def run_once(
        self,
        claim: DeliveryClaimRequest,
        *,
        limit: int,
        should_stop: Callable[[], bool] | None = None,
    ) -> DeliveryBatch:
        integer(limit, minimum=1, maximum=32)
        if any(node not in self._controllers for node in claim.target_node_ids):
            raise ValueError("Claim targets must have explicit configured controllers")
        batch = DeliveryBatch()
        for _ in range(limit):
            if should_stop is not None and should_stop():
                break
            try:
                result = await asyncio.to_thread(self._transactions.claim, claim)
            except DeliveryUnavailableError:
                batch.unknown += 1
                break
            if result.outcome == "empty":
                break
            if result.outcome == "blocked":
                batch.blocked += 1
                continue
            ticket = result.ticket
            assert ticket is not None
            batch.claimed += 1
            submit = False
            if ticket.phase == "unsent":
                try:
                    started = await asyncio.to_thread(self._transactions.start, ticket)
                except DeliveryUnavailableError:
                    batch.unknown += 1
                    continue
                if started.outcome != "committed":
                    if started.outcome == "stale":
                        batch.stale += 1
                    else:
                        batch.unknown += 1
                    continue
                assert started.ticket is not None
                ticket, submit = started.ticket, started.submit_allowed
            controller = self._controllers[ticket.original.binding.target_node_id]
            try:
                observation = await (
                    controller.submit(ticket.original)
                    if submit
                    else controller.query(ticket.original)
                )
            except ControllerCallError as error:
                # Even NOT_FOUND or a local before-wire failure never changes an
                # already-started command back to unsent or releases occupancy.
                await self._defer(ticket, error.failure.value)
                batch.unknown += 1
                continue
            except asyncio.CancelledError:
                # Best effort only: a killed worker still leaves its durable
                # unknown marker and lease for a successor's original query.
                await asyncio.shield(self._defer(ticket, "cancelled"))
                raise
            try:
                acknowledged = await asyncio.to_thread(
                    self._transactions.acknowledge, ticket, observation.original_document
                )
            except DeliveryUnavailableError:
                batch.unknown += 1
                continue
            if acknowledged.outcome == "committed":
                batch.acknowledged += 1
            elif acknowledged.outcome == "stale":
                batch.stale += 1
            else:
                if acknowledged.outcome == "rejected":
                    await self._defer(ticket, "invalid_response")
                batch.unknown += 1
        return batch

    async def _defer(self, ticket: DeliveryTicket, code: str) -> None:
        # Unknown is already durable; expiry permits original-query recovery.
        with suppress(DeliveryUnavailableError):
            await asyncio.to_thread(self._transactions.defer, ticket, code, self._retry_delay)
