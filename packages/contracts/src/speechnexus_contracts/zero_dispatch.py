"""Generated from contracts/bindings.json; do not edit."""

from typing import Any, Literal, overload

from . import zero_dispatch_models as models
from .validation import validate_profile


@overload
def parse(name: Literal["OriginalExecutionKey"], value: Any) -> models.OriginalExecutionKey: ...


@overload
def parse(
    name: Literal["ReceiveCancellationRequest"], value: Any
) -> models.ReceiveCancellationRequest: ...


@overload
def parse(
    name: Literal["ReceiveCancellationResult"], value: Any
) -> models.ReceiveCancellationResult: ...


@overload
def parse(name: Literal["OriginalRecoveryQuery"], value: Any) -> models.OriginalRecoveryQuery: ...


@overload
def parse(
    name: Literal["OriginalRecoverySnapshot"], value: Any
) -> models.OriginalRecoverySnapshot: ...


@overload
def parse(
    name: Literal["ZeroDispatchControlError"], value: Any
) -> models.ZeroDispatchControlError: ...


def parse(name: str, value: Any) -> Any:
    return validate_profile("zero_dispatch", name, value)
