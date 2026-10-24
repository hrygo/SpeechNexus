"""Generated from contracts/bindings.json; do not edit."""

from typing import Any, Literal, overload

from . import realtime_models as models
from .validation import validate_profile


@overload
def parse(
    name: Literal["RealtimeSessionCreateRequest"], value: Any
) -> models.RealtimeSessionCreateRequest: ...


@overload
def parse(name: Literal["RealtimeSession"], value: Any) -> models.RealtimeSession: ...


@overload
def parse(name: Literal["PcmDescriptor"], value: Any) -> models.PcmDescriptor: ...


def parse(name: str, value: Any) -> Any:
    return validate_profile("realtime", name, value)
