"""Bridge the original intent port to an engine that consumes the frozen request."""

from collections.abc import Callable
from datetime import UTC, datetime

from speechnexus_gateway.domain.canonical import command_digest
from speechnexus_gateway.domain.ports import (
    CommandEngineDispatchPort,
    DispatchOutcome,
    OriginalExecutionCommandSource,
)
from speechnexus_gateway.domain.submission import (
    MAX_ORIGINAL_COMMAND_BYTES,
    DispatchResult,
    SubmissionIntent,
)


class OriginalCommandDispatch:
    """Used only by the original create-then-dispatch path, never by recovery queries.

    No default implementation is installed. The configured engine still owns
    generation/fencing, accepted identity and real lifecycle/stop facts.
    """

    def __init__(
        self,
        *,
        commands: OriginalExecutionCommandSource,
        engine: CommandEngineDispatchPort,
        max_document_bytes: int,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if (
            type(max_document_bytes) is not int
            or not 1 <= max_document_bytes <= MAX_ORIGINAL_COMMAND_BYTES
        ):
            raise ValueError("dispatch requires an explicit original-command byte budget")
        self._commands = commands
        self._engine = engine
        self._max_bytes = max_document_bytes
        self._now = now

    def dispatch(self, intent: SubmissionIntent) -> DispatchOutcome:
        intent.validate_new()  # An observation/recovery read cannot enter this path.
        try:
            request = self._commands.read_request(intent, max_document_bytes=self._max_bytes)
            if (
                request.identity != intent.identity
                or request.output_stream_id != intent.output_stream_id
                or request.request_digest != intent.request_digest
                or command_digest(request.command) != intent.identity.command_digest
            ):
                raise ValueError("the original execution request does not match")
        except Exception:
            # This invocation did not reach the engine. No error/input text is logged.
            return DispatchOutcome(DispatchResult.NOT_SENT, self._now())
        try:
            return self._engine.dispatch(request)
        except Exception:
            # An engine may have accepted before its adapter raised. Never turn that
            # failure into a clean rejection, or manufacture completion/stop evidence.
            return DispatchOutcome(DispatchResult.RESPONSE_LOST, self._now())
