"""Translate one gateway execution document into cancellation reconciliation.

The no-execution branch is the important new fact: a submit refused by the
durable fence leaves a terminal failed intent with cleanup not_required and no
dispatch. That proves there is nothing to stop, so the accepted cancellation can
settle only after this observation. An aborted execution still requires complete
cleanup and one of the contract's verified stop evidence kinds. A missing
execution is never treated as a stop.

The active-execution branch delegates at most one bounded stop attempt to an
injected callable. The acceptance path owns the immediate stop; this worker is
the retry path and composition can supply the existing stop service here.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Protocol

from speechnexus_backend.settings import Settings
from speechnexus_backend.speech.application.job_cancellation_reconciliation import (
    Confirmation,
)
from speechnexus_backend.speech.domain.job_dispatch import submit_intent_identity
from speechnexus_backend.speech.infrastructure.gateway_execution_client import (
    GatewayConnectionError,
    GatewayExecutionClient,
    GatewayRejectedError,
    GatewayResponseError,
    GatewayServerError,
    GatewayTimeoutError,
)

_ACTIVE = frozenset({"submitting", "executing", "stopping"})
_STOP_EVIDENCE = frozenset(
    {
        "engine_abort_ack",
        "engine_terminal_response",
        "instance_terminated_by_controller",
        "operator_verified_instance_terminated",
    }
)


class CancellationSettlementPort(Protocol):
    """Merge-time adapter over the public Job store.

    settle_cancelled must be version-guarded and must return True only when the
    Job is now durably cancelled (or was already terminal-cancelled), never when
    cleanup is pending.
    """

    def settle_cancelled(self, job_id: str) -> bool: ...


StopAttempt = Callable[[str, Mapping[str, Any]], Awaitable[bool]]


def not_started(document: Mapping[str, Any]) -> bool:
    """True only for the gateway's durable no-execution fact."""
    return (
        document.get("execution_state") == "failed"
        and document.get("error_code") == "gateway_barrier_fenced"
        and document.get("cleanup_state") == "not_required"
        and document.get("stop_evidence_kind") == "none"
        and document.get("dispatched_at") is None
        and document.get("engine_request_id") is None
    )


def verified_stop(document: Mapping[str, Any]) -> bool:
    """Aborted alone is not a stop; cleanup and evidence must agree."""
    return (
        document.get("execution_state") == "aborted"
        and document.get("cleanup_state") == "complete"
        and document.get("stop_evidence_kind") in _STOP_EVIDENCE
    )


class GatewayCancellationConfirmer:
    def __init__(
        self,
        settings: Settings,
        settlement: CancellationSettlementPort,
        *,
        stop_attempt: StopAttempt | None = None,
    ) -> None:
        self._settings, self._settlement = settings, settlement
        self._stop_attempt = stop_attempt

    async def confirm(self, job_id: str) -> Confirmation:
        submit_intent_id = submit_intent_identity(job_id)
        try:
            async with GatewayExecutionClient.from_settings(self._settings) as client:
                try:
                    document = await client.query_document(submit_intent_id)
                except GatewayRejectedError as error:
                    if error.status_code == 404:
                        # The dispatcher may not have attempted submit yet. A
                        # missing intent is not no-execution evidence; retry.
                        return Confirmation("pending")
                    return Confirmation("unavailable")
                except GatewayConnectionError, GatewayTimeoutError, GatewayServerError:
                    return Confirmation("unavailable")
                except GatewayResponseError:
                    return Confirmation("unavailable")
                if (
                    document.get("submit_intent_id") != submit_intent_id
                    or document.get("operation_id") != job_id
                ):
                    return Confirmation("unavailable")
                if not_started(document) or verified_stop(document):
                    return self._settle(job_id)
                outcome = _non_actionable(document)
                if outcome is not None:
                    return outcome
                if self._stop_attempt is None:
                    return Confirmation("pending")
                try:
                    stopped = await self._stop_attempt(job_id, document)
                except Exception:
                    return Confirmation("unavailable")
                if not stopped:
                    return Confirmation("pending")
                return self._settle(job_id)
        except ValueError:
            return Confirmation("unavailable")

    def _settle(self, job_id: str) -> Confirmation:
        try:
            return (
                Confirmation("settled")
                if self._settlement.settle_cancelled(job_id)
                else Confirmation("pending")
            )
        except Exception:
            return Confirmation("unavailable")


def _non_actionable(document: Mapping[str, Any]) -> Confirmation | None:
    """A result for states this worker cannot advance; None means try one stop."""
    state = document.get("execution_state")
    if state in _ACTIVE:
        return None
    if state == "succeeded":
        return Confirmation("terminal_unconfirmed")
    if state == "unknown_quarantined":
        return Confirmation("pending")
    if state in ("failed", "aborted"):
        # The verified forms were handled above; the rest is not stop evidence.
        return Confirmation("stop_unconfirmed")
    return Confirmation("unavailable")


__all__ = [
    "CancellationSettlementPort",
    "GatewayCancellationConfirmer",
    "StopAttempt",
    "not_started",
    "verified_stop",
]
