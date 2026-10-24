"""Generated from contracts/bindings.json; do not edit."""

from typing import Any, Literal, overload

from . import realtime_events_models as models
from .validation import validate_profile


@overload
def parse(name: Literal["RealtimeEvent"], value: Any) -> models.RealtimeEvent: ...


@overload
def parse(name: Literal["TranscriptionEvent"], value: Any) -> models.TranscriptionEvent: ...


@overload
def parse(name: Literal["OpenAIPcmFormat"], value: Any) -> models.OpenAIPcmFormat: ...


def parse(name: str, value: Any) -> Any:
    return validate_profile("realtime_events", name, value)
