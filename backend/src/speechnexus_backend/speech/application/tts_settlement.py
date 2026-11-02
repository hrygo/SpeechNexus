"""Recover TTS lease settlement using owner-verified execution and stop facts."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from typing import Protocol

from speechnexus_backend.speech.application.leases import LeaseService
from speechnexus_backend.speech.domain.lease_release import ReleaseLease
from speechnexus_backend.speech.domain.leases import QuarantineLease
from speechnexus_backend.speech.domain.tts_settlement import (
    AbortReason,
    SettlementRoot,
    TtsSettlementTarget,
)


class SettlementStore(Protocol):
    def due(self, gateway_id: str, node_id: str, limit: int) -> list[SettlementRoot]: ...
    def request_abort(self, lease_id: str, reason: AbortReason) -> AbortReason | None: ...
    def defer(self, lease_id: str, *, seconds: float) -> None: ...
    def mark_settled(self, lease_id: str) -> None: ...


class SettlementGateway(Protocol):
    async def query_tts_target(self, target: TtsSettlementTarget) -> object: ...
    async def abort_tts(
        self, target: TtsSettlementTarget, *, reason: AbortReason, wait_timeout_ms: int
    ) -> object: ...


@dataclass(frozen=True, slots=True)
class SettlementPass:
    checked: int
    released: int
    pending: int


class TtsSettlementWorker:
    """At-least-once scans; all final release decisions stay inside LeaseService."""

    def __init__(
        self,
        store: SettlementStore,
        gateway: SettlementGateway,
        leases: LeaseService,
        *,
        gateway_id: str,
        node_id: str,
        batch_size: int = 32,
        poll_seconds: float = 1,
    ) -> None:
        if not 1 <= batch_size <= 128 or not 0 < poll_seconds <= 300:
            raise ValueError("TTS settlement bounds are invalid")
        self._store, self._gateway, self._leases = store, gateway, leases
        self._gateway_id, self._node_id = gateway_id, node_id
        self._batch_size, self._poll_seconds = batch_size, poll_seconds

    async def run_once(self) -> SettlementPass:
        roots = await asyncio.to_thread(
            self._store.due, self._gateway_id, self._node_id, self._batch_size
        )
        released = pending = 0
        for root in roots:
            try:
                if (root.target.gateway_id, root.target.host_node_id) != (
                    self._gateway_id,
                    self._node_id,
                ):
                    raise ValueError("TTS settlement route mismatch")
                if await self._reconcile(root):
                    released += 1
                else:
                    pending += 1
                    await asyncio.to_thread(
                        self._store.defer, root.target.lease_id, seconds=self._poll_seconds
                    )
            except Exception:
                pending += 1
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(
                        self._store.defer, root.target.lease_id, seconds=self._poll_seconds
                    )
        return SettlementPass(len(roots), released, pending)

    async def _reconcile(self, root: SettlementRoot) -> bool:
        target = root.target
        current = await asyncio.to_thread(self._leases.query, target.lease_id)
        if current.fact is None:
            return False
        if current.fact.lease_state == "released":
            await asyncio.to_thread(self._store.mark_settled, target.lease_id)
            return True
        reason = root.abort_reason
        if reason is None and root.now_us >= root.deadline_at_us:
            reason = await asyncio.to_thread(
                self._store.request_abort, target.lease_id, "deadline_exceeded"
            )
        status: object = None
        # Missing, unavailable and unknown never prove stopped execution.
        with contextlib.suppress(Exception):
            status = await self._gateway.query_tts_target(target)
        if isinstance(status, dict) and status.get("execution_state") == "unknown_quarantined":
            await asyncio.to_thread(
                self._leases.quarantine,
                QuarantineLease(target.lease_id, "engine_transport_unknown", root.now_us),
            )
        terminal = isinstance(status, dict) and status.get("execution_state") in (
            "succeeded",
            "failed",
            "aborted",
        )
        if reason is not None or terminal:
            # Release verifies the original submission and stop evidence under its locks.
            current = await asyncio.to_thread(self._leases.query, target.lease_id)
            if current.fact is not None:
                result = await asyncio.to_thread(
                    self._leases.release, ReleaseLease(target.lease_id, current.fact.version)
                )
                if result.outcome == "unknown":
                    result = await asyncio.to_thread(self._leases.query, target.lease_id)
                if result.fact is not None and result.fact.lease_state == "released":
                    await asyncio.to_thread(self._store.mark_settled, target.lease_id)
                    return True
        if reason is not None:
            # Next scan replays the same persisted reason and identity.
            with contextlib.suppress(Exception):
                await self._gateway.abort_tts(target, reason=reason, wait_timeout_ms=1000)
        return False
