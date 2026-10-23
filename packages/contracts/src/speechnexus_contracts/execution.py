"""Generated from contracts/bindings.json; do not edit."""

from typing import Any, Literal, overload

from . import execution_models as models
from .validation import validate_profile


@overload
def parse(name: Literal["SubmitExecutionRequest"], value: Any) -> models.SubmitExecutionRequest: ...


@overload
def parse(
    name: Literal["SubmitExecutionResponse"], value: Any
) -> models.SubmitExecutionResponse: ...


@overload
def parse(
    name: Literal["ExecutionStatusResponse"], value: Any
) -> models.ExecutionStatusResponse: ...


@overload
def parse(name: Literal["AbortExecutionRequest"], value: Any) -> models.AbortExecutionRequest: ...


@overload
def parse(
    name: Literal["GatewayFencingBarrierRequest"], value: Any
) -> models.GatewayFencingBarrierRequest: ...


@overload
def parse(
    name: Literal["GatewayFencingBarrierResponse"], value: Any
) -> models.GatewayFencingBarrierResponse: ...


@overload
def parse(
    name: Literal["ExecutionControlErrorResponse"], value: Any
) -> models.ExecutionControlErrorResponse: ...


def parse(name: str, value: Any) -> Any:
    return validate_profile("execution", name, value)
