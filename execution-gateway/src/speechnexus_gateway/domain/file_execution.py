"""File execution ports; acceptance and results retain the original intent authority."""

from collections.abc import Callable
from typing import Protocol

from speechnexus_gateway.domain.asr_results import AsrResultStore, StagedAsrResult
from speechnexus_gateway.domain.ports import (
    CommandEngineDispatchPort,
    CommittedExecutionStarter,
    EngineAbortPort,
    EngineObservationPort,
    ExecutionFactLedger,
    OriginalExecutionCommandSource,
)
from speechnexus_gateway.domain.submission import SubmissionIntent, SubmitRequest


class FileAsrExecution(Protocol):
    def validate_request(self, request: SubmitRequest) -> SubmitRequest:
        """Freeze supported parameters without reading objects or starting inference."""
        ...

    async def stage_result(
        self,
        request: SubmitRequest,
        *,
        store: AsrResultStore,
        max_result_bytes: int,
        cancelled: Callable[[], bool],
    ) -> StagedAsrResult: ...


class FileExecutionLedger(ExecutionFactLedger, OriginalExecutionCommandSource, Protocol):
    def check_file_start(self, intent: SubmissionIntent) -> SubmissionIntent:
        """Recheck current authority; the read itself never grants execution ownership."""
        ...

    def list_unfinished(
        self, *, gateway_id: str, host_node_id: str, limit: int
    ) -> list[SubmissionIntent]:
        """Bounded startup read of submitting/executing/stopping, never a retry queue."""
        ...


class FileExecutionRuntime(
    CommandEngineDispatchPort,
    CommittedExecutionStarter,
    EngineAbortPort,
    EngineObservationPort,
    Protocol,
):
    @property
    def running(self) -> bool: ...

    @property
    def max_command_bytes(self) -> int: ...

    def open(self) -> None: ...

    def close(self) -> None:
        """Join all work before its database pool and ingress ownership are closed."""
        ...
