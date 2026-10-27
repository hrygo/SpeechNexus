"""Dispatcher adapter over the single execution-gateway HTTP client."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Self

from speechnexus_backend.speech.domain.job_dispatch import (
    DispatchProtocolError,
    DispatchRejectedError,
    DispatchUnavailableError,
    ExecutionSubmission,
    GatewayReply,
    ResultReceipt,
    identifier,
)
from speechnexus_backend.speech.infrastructure.gateway_execution_client import (
    GatewayConnectionError,
    GatewayExecutionClient,
    GatewayRejectedError,
    GatewayResponseError,
    GatewayServerError,
    GatewayTimeoutError,
)


class ExecutionGatewayAdapter:
    """Translate dispatcher calls into ``GatewayExecutionClient`` operations.

    Transport and response decoding live only in ``GatewayExecutionClient``. This
    adapter translates its five failure classes into dispatcher semantics: unavailable
    transport/server outcomes stay unknown, malformed responses are protocol errors,
    and explicit semantic rejections remain terminal ``failed`` outcomes.
    """

    def __init__(self, client: GatewayExecutionClient) -> None:
        self._client = client

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    @staticmethod
    def _unavailable() -> DispatchUnavailableError:
        return DispatchUnavailableError("Gateway transport outcome is unknown")

    @staticmethod
    def _rejected(error: GatewayRejectedError) -> DispatchProtocolError:
        if error.gateway_code is None:
            return DispatchProtocolError("Gateway rejection has no contract-valid error code")
        return DispatchRejectedError(error.gateway_code)

    async def query(self, submit_intent_id: str) -> GatewayReply:
        identifier(submit_intent_id)
        try:
            document = await self._client.query_document(submit_intent_id)
        except GatewayRejectedError as error:
            if error.status_code == 404:
                return GatewayReply("not_found")
            if error.status_code in (408, 429):
                return GatewayReply("unknown", error_code="outcome_unknown")
            raise self._rejected(error) from None
        except GatewayConnectionError, GatewayTimeoutError, GatewayServerError:
            raise self._unavailable() from None
        except GatewayResponseError:
            raise DispatchProtocolError("Gateway response is not interpretable") from None
        return GatewayReply("status", document)

    async def submit(self, submission: ExecutionSubmission) -> GatewayReply:
        identifier(submission.submit_intent_id)
        try:
            document = await self._client.submit_document(submission.body)
        except GatewayRejectedError as error:
            if error.status_code in (408, 429):
                return GatewayReply("unknown", error_code="outcome_unknown")
            raise self._rejected(error) from None
        except GatewayConnectionError, GatewayTimeoutError, GatewayServerError:
            raise self._unavailable() from None
        except GatewayResponseError:
            raise DispatchProtocolError("Gateway response is not interpretable") from None
        except TypeError, ValueError:
            raise DispatchProtocolError("Execution submission is not strict JSON") from None
        return GatewayReply("status", document)

    async def result_receipt(self, submit_intent_id: str, *, audio: bool) -> ResultReceipt:
        identifier(submit_intent_id)
        try:
            document = await self._client.staging_result_document(submit_intent_id, audio=audio)
        except GatewayRejectedError as error:
            if error.status_code == 404:
                return ResultReceipt("not_found")
            if error.status_code in (408, 429):
                return ResultReceipt("unknown", error_code="outcome_unknown")
            raise self._rejected(error) from None
        except GatewayConnectionError, GatewayTimeoutError, GatewayServerError:
            raise self._unavailable() from None
        except GatewayResponseError:
            raise DispatchProtocolError("Gateway response is not interpretable") from None
        return self._receipt(submit_intent_id, document)

    @staticmethod
    def _receipt(submit_intent_id: str, document: Mapping[str, Any]) -> ResultReceipt:
        if document.get("submit_intent_id") != submit_intent_id:
            raise DispatchProtocolError("Gateway receipt belongs to another execution")
        staging = document.get("staging_result")
        if staging is None:
            return ResultReceipt("unknown", error_code="result_not_ready")
        if not isinstance(staging, dict):
            raise DispatchProtocolError("Gateway staging result is not an object")
        if staging.get("request_digest") != document.get("request_digest"):
            raise DispatchProtocolError("Gateway receipt disagrees with its request digest")
        return ResultReceipt("status", staging)


__all__ = ["ExecutionGatewayAdapter"]
