"""Recover disable targets through the versioned original-identity abort port."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Protocol

from speechnexus_backend.speech.domain.binding_disable_intent import DisableExecutionTarget
from speechnexus_backend.speech.domain.lease_release import ReleaseLease
from speechnexus_backend.speech.domain.leases import LeaseResult
from speechnexus_backend.speech.domain.runtime_intents import identifier, integer

_LOG = logging.getLogger(__name__)


class DisableStore(Protocol):
    def event_ids(self, after: str | None, limit: int) -> list[str]: ...
    def capture(
        self, event_id: str, wait_timeout_ms: int, limit: int, gateway_id: str, node_id: str
    ) -> None: ...
    def targets(
        self, event_id: str, after: str | None, limit: int, gateway_id: str, node_id: str
    ) -> list[DisableExecutionTarget]: ...
    def begin_abort(self, event_id: str, submit_intent_id: str) -> bool: ...
    def leases(
        self, event_id: str, gateway_id: str, node_id: str, after: str | None, limit: int
    ) -> list[str]: ...


class DisableLeaseSettlement(Protocol):
    def query(self, lease_id: str) -> LeaseResult: ...
    def release(self, request: ReleaseLease) -> LeaseResult: ...


class DisableGateway(Protocol):
    async def abort_idempotent(self, target: DisableExecutionTarget) -> object: ...
    async def query_disable_target(self, target: DisableExecutionTarget) -> object: ...


@dataclass(frozen=True, slots=True)
class DisablePassResult:
    attempted: int
    queried: int
    unavailable: int
    settled: int = 0
    pending: int = 0


class BindingDisableCoordinator:
    """Page durable work; no outcome is interpreted as released capacity.

    The caller must bind the gateway transport to this exact configured gateway
    and node. Repeated passes revisit all durable roots and targets after restart.
    """

    def __init__(
        self,
        store: DisableStore,
        gateway: DisableGateway,
        *,
        gateway_id: str,
        node_id: str,
        batch_size: int,
        wait_timeout_ms: int,
        settlement: DisableLeaseSettlement | None = None,
    ) -> None:
        identifier(gateway_id)
        identifier(node_id)
        integer(batch_size, minimum=1, maximum=128)
        integer(wait_timeout_ms, minimum=100, maximum=60000)
        self._store, self._gateway = store, gateway
        self._gateway_id, self._node_id = gateway_id, node_id
        self._batch_size, self._wait_timeout_ms = batch_size, wait_timeout_ms
        self._settlement = settlement
        self._event_after: str | None = None
        self._target_after: dict[str, str] = {}
        self._lease_after: dict[str, str] = {}

    async def run_pass(self) -> DisablePassResult:
        attempted = queried = unavailable = settled = pending = 0
        event_ids = await asyncio.to_thread(
            self._store.event_ids, self._event_after, self._batch_size
        )
        for event_id in event_ids:
            try:
                await asyncio.to_thread(
                    self._store.capture,
                    event_id,
                    self._wait_timeout_ms,
                    self._batch_size,
                    self._gateway_id,
                    self._node_id,
                )
                targets = await asyncio.to_thread(
                    self._store.targets,
                    event_id,
                    self._target_after.get(event_id),
                    self._batch_size,
                    self._gateway_id,
                    self._node_id,
                )
                for target in targets:
                    if (target.gateway_id, target.host_node_id) != (
                        self._gateway_id,
                        self._node_id,
                    ):
                        continue
                    first = await asyncio.to_thread(
                        self._store.begin_abort, event_id, target.submit_intent_id
                    )
                    try:
                        if not first:
                            queried += 1
                            await self._gateway.query_disable_target(target)
                        attempted += 1
                        await self._gateway.abort_idempotent(target)
                    except Exception:
                        # Gateway's original identity owns replay and uncertainty.
                        unavailable += 1
                if len(targets) == self._batch_size:
                    self._target_after[event_id] = targets[-1].submit_intent_id
                else:
                    self._target_after.pop(event_id, None)
                if self._settlement is not None:
                    lease_ids = await asyncio.to_thread(
                        self._store.leases,
                        event_id,
                        self._gateway_id,
                        self._node_id,
                        self._lease_after.get(event_id),
                        self._batch_size,
                    )
                    for lease_id in lease_ids:
                        try:
                            original = await asyncio.to_thread(self._settlement.query, lease_id)
                            if original.fact is None:
                                pending += 1
                                continue
                            if original.fact.lease_state == "released":
                                continue
                            result = await asyncio.to_thread(
                                self._settlement.release,
                                ReleaseLease(lease_id, original.fact.version),
                            )
                            if result.outcome == "unknown":
                                result = await asyncio.to_thread(self._settlement.query, lease_id)
                            if result.fact is not None and result.fact.lease_state == "released":
                                settled += 1
                            else:
                                pending += 1
                        except Exception:
                            pending += 1
                    if len(lease_ids) == self._batch_size:
                        self._lease_after[event_id] = lease_ids[-1]
                    else:
                        self._lease_after.pop(event_id, None)
            except Exception:
                # Keep the original facts pending, advance fairly, and retry on rotation.
                # Provider errors can contain connection details or private data.
                _LOG.warning("Disable root unavailable; original work will be retried")
                unavailable += 1
            self._event_after = event_id
        if len(event_ids) < self._batch_size:
            self._event_after = None
        return DisablePassResult(attempted, queried, unavailable, settled, pending)
