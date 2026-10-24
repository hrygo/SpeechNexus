"""Generated from contracts/bindings.json; do not edit."""

from typing import Any, Literal, overload

from . import workbench_models as models
from .validation import validate_profile


@overload
def parse(name: Literal["ProjectCreateRequest"], value: Any) -> models.ProjectCreateRequest: ...


@overload
def parse(name: Literal["ProjectPatch"], value: Any) -> models.ProjectPatch: ...


@overload
def parse(name: Literal["Project"], value: Any) -> models.Project: ...


@overload
def parse(name: Literal["ProjectList"], value: Any) -> models.ProjectList: ...


@overload
def parse(
    name: Literal["TranscriptCreateRequest"], value: Any
) -> models.TranscriptCreateRequest: ...


@overload
def parse(
    name: Literal["TranscriptRevisionCreateRequest"], value: Any
) -> models.TranscriptRevisionCreateRequest: ...


@overload
def parse(name: Literal["Transcript"], value: Any) -> models.Transcript: ...


@overload
def parse(name: Literal["TranscriptRevision"], value: Any) -> models.TranscriptRevision: ...


@overload
def parse(name: Literal["TranscriptView"], value: Any) -> models.TranscriptView: ...


@overload
def parse(name: Literal["TranscriptList"], value: Any) -> models.TranscriptList: ...


@overload
def parse(name: Literal["WorkbenchStatus"], value: Any) -> models.WorkbenchStatus: ...


@overload
def parse(name: Literal["ErrorResponse"], value: Any) -> models.ErrorResponse: ...


@overload
def parse(name: Literal["DeletionPlanRequest"], value: Any) -> models.DeletionPlanRequest: ...


@overload
def parse(name: Literal["DeletionCreateRequest"], value: Any) -> models.DeletionCreateRequest: ...


@overload
def parse(name: Literal["DeletionPlan"], value: Any) -> models.DeletionPlan: ...


@overload
def parse(name: Literal["Deletion"], value: Any) -> models.Deletion: ...


@overload
def parse(name: Literal["Meeting"], value: Any) -> models.Meeting: ...


@overload
def parse(name: Literal["MeetingCreateRequest"], value: Any) -> models.MeetingCreateRequest: ...


@overload
def parse(name: Literal["MeetingSealRequest"], value: Any) -> models.MeetingSealRequest: ...


@overload
def parse(name: Literal["MinutesCreateRequest"], value: Any) -> models.MinutesCreateRequest: ...


@overload
def parse(name: Literal["MinutesJob"], value: Any) -> models.MinutesJob: ...


def parse(name: str, value: Any) -> Any:
    return validate_profile("workbench", name, value)
