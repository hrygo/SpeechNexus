"""Generated from contracts/bindings.json; do not edit."""

from typing import Any, Literal, overload

from . import admin_models as models
from .validation import validate_profile


@overload
def parse(name: Literal["WorkspaceCreateRequest"], value: Any) -> models.WorkspaceCreateRequest: ...


@overload
def parse(name: Literal["WorkspacePatch"], value: Any) -> models.WorkspacePatch: ...


def parse(name: str, value: Any) -> Any:
    return validate_profile("admin", name, value)
