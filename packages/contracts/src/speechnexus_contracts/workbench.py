"""Generated from contracts/bindings.json; do not edit."""

from typing import Any, Literal, overload

from . import workbench_models as models
from .validation import validate_profile


@overload
def parse(name: Literal["ProjectCreateRequest"], value: Any) -> models.ProjectCreateRequest: ...


@overload
def parse(name: Literal["ProjectPatch"], value: Any) -> models.ProjectPatch: ...


def parse(name: str, value: Any) -> Any:
    return validate_profile("workbench", name, value)
