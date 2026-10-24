"""Generated from contracts/bindings.json; do not edit."""

from typing import Any, Literal, overload

from . import runtime_models as models
from .validation import validate_profile


@overload
def parse(
    name: Literal["RuntimeInstanceIntentRequest"], value: Any
) -> models.RuntimeInstanceIntentRequest: ...


@overload
def parse(
    name: Literal["RuntimeInstanceIntentResponse"], value: Any
) -> models.RuntimeInstanceIntentResponse: ...


@overload
def parse(name: Literal["NodeReconcileRequest"], value: Any) -> models.NodeReconcileRequest: ...


@overload
def parse(
    name: Literal["RuntimeControlErrorResponse"], value: Any
) -> models.RuntimeControlErrorResponse: ...


def parse(name: str, value: Any) -> Any:
    return validate_profile("runtime", name, value)
