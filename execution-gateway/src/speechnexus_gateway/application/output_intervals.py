"""Scope trusted metering facts to this gateway; never synthesize usage or engine work."""

from speechnexus_gateway.application.ingress import GatewayIngress
from speechnexus_gateway.application.observe_execution import check_gateway_scope
from speechnexus_gateway.domain.output_intervals import (
    AppendInterval,
    OutputIntervalConflictError,
    OutputIntervalRecord,
    OutputIntervalWrite,
)
from speechnexus_gateway.domain.ports import OutputIntervalLedger
from speechnexus_gateway.domain.submission import InvalidExecutionRequestError


class OutputIntervalService:
    def __init__(self, *, ingress: GatewayIngress, ledger: OutputIntervalLedger) -> None:
        self._ingress = ingress
        self._ledger = ledger

    def append_interval(self, interval: AppendInterval) -> OutputIntervalWrite:
        self._ingress.check_ownership()
        original = self._ledger.query_outcome(interval.submit_intent_id)
        if original is None:
            raise InvalidExecutionRequestError("unknown execution for output interval")
        check_gateway_scope(self._ingress, original.intent.identity)
        return self._ledger.append_interval(
            OutputIntervalRecord(interval, original.intent.identity)
        )

    def query_output_interval(
        self, interval_id: str, *, request_digest: str | None = None
    ) -> OutputIntervalRecord | None:
        self._ingress.check_ownership()
        record = self._ledger.query_output_interval(interval_id)
        if record is None:
            return None
        if (record.identity.gateway_id, record.identity.host_node_id) != (
            self._ingress.gateway_id,
            self._ingress.node_id,
        ):
            return None
        if request_digest is not None and request_digest != record.request_digest:
            raise OutputIntervalConflictError("original interval has another request digest")
        return record
