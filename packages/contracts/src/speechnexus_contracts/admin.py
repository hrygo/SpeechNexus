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


@overload
def parse(
    name: Literal["ProfileRevisionCreateRequest"], value: Any
) -> models.ProfileRevisionCreateRequest: ...


@overload
def parse(
    name: Literal["ModelBindingCreateRequest"], value: Any
) -> models.ModelBindingCreateRequest: ...


@overload
def parse(
    name: Literal["ProfilePublicationRequest"], value: Any
) -> models.ProfilePublicationRequest: ...


@overload
def parse(
    name: Literal["ProfileActivationRequest"], value: Any
) -> models.ProfileActivationRequest: ...


@overload
def parse(
    name: Literal["ProfilePublishedRevision"], value: Any
) -> models.ProfilePublishedRevision: ...


def parse(name: str, value: Any) -> Any:
    return validate_profile("admin", name, value)
