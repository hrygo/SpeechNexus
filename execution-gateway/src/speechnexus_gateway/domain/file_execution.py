"""File execution ports; acceptance and results retain the original intent authority."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from speechnexus_gateway.domain.asr_results import (
    AsrResultStore,
    StagedAsrResult,
    validate_asr_result_limit,
)
from speechnexus_gateway.domain.ports import (
    CommandEngineDispatchPort,
    CommittedExecutionStarter,
    EngineAbortPort,
    EngineObservationPort,
    ExecutionFactLedger,
    OriginalExecutionCommandSource,
)
from speechnexus_gateway.domain.submission import (
    MAX_ORIGINAL_COMMAND_BYTES,
    SubmissionIntent,
    SubmitRequest,
)


@dataclass(frozen=True, slots=True)
class FileExecutionLimits:
    """Local consumption bounds, independent of admission or scheduling budgets."""

    max_result_bytes: int
    max_command_bytes: int
    max_pending: int
    shutdown_seconds: float

    def __post_init__(self) -> None:
        validate_asr_result_limit(self.max_result_bytes)
        if (
            type(self.max_command_bytes) is not int
            or not 1 <= self.max_command_bytes <= MAX_ORIGINAL_COMMAND_BYTES
        ):
            raise ValueError("file work requires an explicit original-command byte budget")
        if type(self.max_pending) is not int or not 1 <= self.max_pending <= 64:
            raise ValueError("file work requires a bounded local preparation count")
        if type(self.shutdown_seconds) not in (int, float) or not 0 < self.shutdown_seconds <= 30:
            raise ValueError("file worker shutdown requires a bounded wait")


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
