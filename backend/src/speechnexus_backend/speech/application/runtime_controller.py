"""One transport call for an already committed, immutable runtime intent.

Persistent delivery, owner reconciliation and readiness qualification remain
separate use cases. Callers preserve occupancy on errors/cancellation and query
the original identity after an unanswered submit; this port never retries POST.
"""

from typing import Protocol

from speechnexus_backend.speech.domain.runtime_controller import ControllerObservation
from speechnexus_backend.speech.domain.runtime_intents import RuntimeIntent


class RuntimeController(Protocol):
    async def submit(self, original: RuntimeIntent) -> ControllerObservation: ...

    async def query(self, original: RuntimeIntent) -> ControllerObservation: ...
