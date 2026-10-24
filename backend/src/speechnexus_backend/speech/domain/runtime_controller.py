"""Controller facts and unresolved calls, without transport or qualification claims."""

from dataclasses import dataclass, field
from enum import StrEnum

from speechnexus_backend.speech.domain.runtime_observations import RuntimeObservation


class ControllerFailure(StrEnum):
    INVALID_REQUEST = "invalid_request"
    INVALID_RESPONSE = "invalid_response"
    UNAVAILABLE = "unavailable"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    NOT_FOUND = "not_found"
    REJECTED = "rejected"
    BUSY = "busy"
    CLOSED = "closed"


class ControllerCallError(Exception):
    """Never a physical stop, absence, dispatch permission or budget release fact.

    A false request_may_have_run only describes this local call before sending;
    it says nothing about earlier calls or the original persistent intent.
    """

    def __init__(
        self,
        failure: ControllerFailure,
        *,
        request_may_have_run: bool,
        controller_code: str | None = None,
    ) -> None:
        self.failure = failure
        self.request_may_have_run = request_may_have_run
        self.controller_code = controller_code
        super().__init__(f"Runtime controller call: {failure.value}")


@dataclass(frozen=True, slots=True)
class ControllerObservation:
    observation: RuntimeObservation
    original_document: str = field(repr=False)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.original_document, str)
            or not 0 < len(self.original_document.encode()) <= 65536
        ):
            raise ValueError("Controller observation requires its bounded original document")
