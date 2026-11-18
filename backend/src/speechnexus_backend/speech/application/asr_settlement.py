"""At-least-once settlement of synchronous ASR leases.

The worker chooses only *when* to ask for a release. Every authorization fact is
re-read by ``LeaseService.release`` under its own locks; a missing, unavailable
or non-terminal gateway answer keeps the capacity reserved.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from typing import Protocol

from speechnexus_backend.speech.application.leases import LeaseService
from speechnexus_backend.speech.domain.asr_settlement import AsrSettlementRoot
from speechnexus_backend.speech.domain.lease_release import ReleaseLease


class AsrSettlementStore(Protocol):
    def due(self, gateway_id: str, host_node_id: str, limit: int) -> list[AsrSettlementRoot]:
        """Return unsettled roots for this route; read only, no claim."""

    def intent(self, lease_id: str) -> str | None:
        """Return the lease's single original submit identity, or ``None``."""

    def defer(self, lease_id: str, *, seconds: float) -> None: ...

    def mark_settled(self, lease_id: str) -> None: ...


class AsrExecutionQuery(Protocol):
    async def query(self, submit_intent_id: str) -> object: ...


@dataclass(frozen=True, slots=True)
class AsrSettlementPass:
    scanned: int
    released: int
    pending: int


_TERMINAL = ("succeeded", "failed", "aborted")


class AsrSettlementWorker:
    def __init__(
        self,
        leases: LeaseService,
        store: AsrSettlementStore,
        gateway: AsrExecutionQuery,
        *,
        gateway_id: str,
        host_node_id: str,
        poll_seconds: float = 1,
        batch_size: int = 32,
    ) -> None:
        if not 0 < poll_seconds <= 300 or not 1 <= batch_size <= 128:
            raise ValueError("ASR settlement requires a bounded poll and batch")
        self._leases = leases
        self._store = store
        self._gateway = gateway
        self._gateway_id = gateway_id
        self._host_node_id = host_node_id
        self._poll = poll_seconds
        self._batch = batch_size

    async def run_once(self) -> AsrSettlementPass:
        roots = await asyncio.to_thread(
            self._store.due, self._gateway_id, self._host_node_id, self._batch
        )
        released = pending = 0
        for root in roots:
            try:
                if (root.gateway_id, root.host_node_id) != (self._gateway_id, self._host_node_id):
                    raise ValueError("ASR settlement route mismatch")
                if await self._reconcile(root):
                    released += 1
                else:
                    pending += 1
                    await asyncio.to_thread(self._store.defer, root.lease_id, seconds=self._poll)
            except Exception:
                pending += 1
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(self._store.defer, root.lease_id, seconds=self._poll)
        return AsrSettlementPass(len(roots), released, pending)

    async def _reconcile(self, root: AsrSettlementRoot) -> bool:
        current = await asyncio.to_thread(self._leases.query, root.lease_id)
        if current.fact is None:
            return False
        if current.fact.lease_state == "released":
            # The release already happened (worker replay or another operator).
            await asyncio.to_thread(self._store.mark_settled, root.lease_id)
            return True
        intent = await asyncio.to_thread(self._store.intent, root.lease_id)
        if intent is not None:
            status: object = None
            # Missing, unavailable and unknown never prove a terminal execution.
            with contextlib.suppress(Exception):
                status = await self._gateway.query(intent)
            state = status.get("execution_state") if isinstance(status, dict) else None
            if state not in _TERMINAL:
                return False
        # This also covers a lease with no submit intent: the lease lock blocks a
        # future create_submit, and release() itself decides the zero-intent case.
        current = await asyncio.to_thread(self._leases.query, root.lease_id)
        if current.fact is None:
            return False
        result = await asyncio.to_thread(
            self._leases.release, ReleaseLease(root.lease_id, current.fact.version)
        )
        if result.outcome == "unknown":
            result = await asyncio.to_thread(self._leases.query, root.lease_id)
        if result.fact is not None and result.fact.lease_state == "released":
            await asyncio.to_thread(self._store.mark_settled, root.lease_id)
            return True
        return False
