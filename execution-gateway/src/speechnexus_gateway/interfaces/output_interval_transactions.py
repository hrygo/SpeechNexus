"""Native trusted-meter boundary with complete append and original-interval query results."""

from typing import Any

from speechnexus_contracts import ContractValidationError
from speechnexus_contracts.validation import validate_coordination

from speechnexus_gateway.application.output_intervals import OutputIntervalService
from speechnexus_gateway.domain.output_intervals import AppendInterval, MeterKind
from speechnexus_gateway.domain.submission import SubmissionOutcomeUnknownError
from speechnexus_gateway.interfaces.execution_transactions import execution_transaction_result


class OutputIntervalBoundary:
    def __init__(self, service: OutputIntervalService) -> None:
        self._service = service

    def append_interval(self, request: Any) -> dict[str, Any]:
        validate_coordination("append_interval", request)

        def append() -> tuple[bool, int | None, dict[str, object]]:
            interval = AppendInterval(
                request["interval_id"],
                request["submit_intent_id"],
                request["output_stream_id"],
                int(request["sample_rate_hz"]),
                MeterKind(request["meter_kind"]),
                int(request["sample_start"]),
                int(request["sample_end"]),
            )
            result = self._service.append_interval(interval)
            return result.replayed, None, result.record.snapshot()

        return execution_transaction_result("append_interval", request["interval_id"], append)

    def query_outcome(self, request: Any) -> dict[str, Any]:
        validate_coordination("query_outcome", request)
        if request["target"]["kind"] != "interval":
            raise ContractValidationError("Interval boundary only queries original intervals")
        interval_id = request["target"]["interval_id"]

        def query() -> tuple[bool, int | None, dict[str, object]]:
            record = self._service.query_output_interval(
                interval_id, request_digest=request.get("request_digest")
            )
            if record is None:
                raise SubmissionOutcomeUnknownError("no original interval fact for this gateway")
            return True, None, record.snapshot()

        return execution_transaction_result("query_outcome", interval_id, query)
