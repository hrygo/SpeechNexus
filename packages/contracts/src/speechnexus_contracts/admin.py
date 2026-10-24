"""Generated from contracts/bindings.json; do not edit."""

from typing import Any, Literal, overload

from . import admin_models as models
from .validation import validate_profile


@overload
def parse(name: Literal["WorkspaceCreateRequest"], value: Any) -> models.WorkspaceCreateRequest: ...


@overload
def parse(name: Literal["WorkspacePatch"], value: Any) -> models.WorkspacePatch: ...


@overload
def parse(name: Literal["Workspace"], value: Any) -> models.Workspace: ...


@overload
def parse(name: Literal["WorkspaceList"], value: Any) -> models.WorkspaceList: ...


@overload
def parse(name: Literal["ErrorResponse"], value: Any) -> models.ErrorResponse: ...


def parse(name: str, value: Any) -> Any:
    return validate_profile("admin", name, value)
