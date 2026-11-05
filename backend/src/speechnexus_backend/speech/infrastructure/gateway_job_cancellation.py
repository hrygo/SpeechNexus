"""Bounded gateway stop attempt for one accepted public Job cancellation."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from speechnexus_backend.settings import Settings
from speechnexus_backend.speech.application.job_cancellation import StopObservation
from speechnexus_backend.speech.domain.job_dispatch import (
    gateway_failure_code,
    submit_intent_identity,
)
from speechnexus_backend.speech.domain.public_jobs import (
    JobCancellationReceipt,
    PublicJob,
)
from speechnexus_backend.speech.infrastructure.gateway_execution_client import (
    GatewayConnectionError,
    GatewayExecutionClient,
    GatewayExecutionError,
    GatewayRejectedError,
    GatewayResponseError,
    GatewayServerError,
    GatewayTimeoutError,
)

_ABORT_WAIT_MS = 5_000
_ABORTABLE = frozenset({"submitting", "executing", "stopping"})
_STOP_EVIDENCE = frozenset(
    {
        "engine_abort_ack",
        "engine_terminal_response",
        "instance_terminated_by_controller",
        "operator_verified_instance_terminated",
    }
)


class GatewayJobCancellationStopper:
    """One lookup, at most one durable abort, no fallback execution."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def stop(self, job: PublicJob, receipt: JobCancellationReceipt) -> StopObservation:
        submit_intent_id = submit_intent_identity(job.job_id)
        try:
            async with GatewayExecutionClient.from_settings(self._settings) as client:
                try:
                    document = await client.query_document(submit_intent_id)
                except GatewayRejectedError as error:
                    if error.status_code == 404:
                        # Absence after a possible submit is not stop evidence.
                        return StopObservation("not_found")
                    return StopObservation("unconfirmed")
                except GatewayConnectionError, GatewayTimeoutError, GatewayServerError:
                    return StopObservation("unknown")
                except GatewayResponseError:
                    return StopObservation("unconfirmed")
                observation = self._observe(document, submit_intent_id, job, receipt)
                if observation is not None:
                    return observation
                state = document.get("execution_state")
                if state not in _ABORTABLE:
                    return StopObservation("unconfirmed")
                fencing_token = document.get("fencing_token")
                if type(fencing_token) is not int or fencing_token < 1:
                    return StopObservation("unconfirmed")
                try:
                    response = await client.abort_intent(
                        submit_intent_id,
                        operation_id=receipt.operation_id,
                        attempt_id=receipt.attempt_id,
                        fencing_token=fencing_token,
                        reason="client_cancelled",
                        wait_timeout_ms=_ABORT_WAIT_MS,
                    )
                except GatewayRejectedError, GatewayResponseError, ValueError:
                    return StopObservation("unconfirmed")
                except GatewayExecutionError:
                    return StopObservation("unknown")
                if _verified_stop(response):
                    return StopObservation("stopped")
                return StopObservation("unconfirmed")
        except ValueError:
            return StopObservation("unconfirmed")

    @staticmethod
    def _observe(
        document: dict[str, Any],
        submit_intent_id: str,
        job: PublicJob,
        receipt: JobCancellationReceipt,
    ) -> StopObservation | None:
        if (
            document.get("submit_intent_id") != submit_intent_id
            or document.get("operation_id") != receipt.operation_id
            or document.get("attempt_id") != receipt.attempt_id
            or (document.get("operation_id") != job.job_id)
        ):
            return StopObservation("unconfirmed")
        state = document.get("execution_state")
        if state == "aborted":
            return (
                StopObservation("stopped")
                if _verified_stop(document)
                else StopObservation("unconfirmed")
            )
        if state == "failed":
            return StopObservation("failed", gateway_failure_code(document))
        if state == "succeeded":
            # A completed execution is not a cancellation and this path cannot
            # publish its result; keep the Job cleanup-pending.
            return StopObservation("unconfirmed")
        return None


def _verified_stop(document: Mapping[str, Any]) -> bool:
    """Aborted is not enough: require complete cleanup and explicit stop evidence."""
    return (
        document.get("execution_state") == "aborted"
        and document.get("cleanup_state") == "complete"
        and document.get("stop_evidence_kind") in _STOP_EVIDENCE
    )


__all__ = ["GatewayJobCancellationStopper"]
