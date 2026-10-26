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


@overload
def parse(
    name: Literal["RenderUnitGenerationRequest"], value: Any
) -> models.RenderUnitGenerationRequest: ...


@overload
def parse(
    name: Literal["RenderUnitGenerationAccepted"], value: Any
) -> models.RenderUnitGenerationAccepted: ...


@overload
def parse(
    name: Literal["TeleprompterUpdateRequest"], value: Any
) -> models.TeleprompterUpdateRequest: ...


@overload
def parse(name: Literal["TeleprompterState"], value: Any) -> models.TeleprompterState: ...


@overload
def parse(name: Literal["SavedAsset"], value: Any) -> models.SavedAsset: ...


@overload
def parse(
    name: Literal["SavedAssetCreateRequest"], value: Any
) -> models.SavedAssetCreateRequest: ...


@overload
def parse(name: Literal["Book"], value: Any) -> models.Book: ...


@overload
def parse(name: Literal["BookCreateRequest"], value: Any) -> models.BookCreateRequest: ...


@overload
def parse(name: Literal["Chapter"], value: Any) -> models.Chapter: ...


@overload
def parse(name: Literal["ChapterCreateRequest"], value: Any) -> models.ChapterCreateRequest: ...


@overload
def parse(name: Literal["RenderUnit"], value: Any) -> models.RenderUnit: ...


@overload
def parse(
    name: Literal["RenderUnitCreateRequest"], value: Any
) -> models.RenderUnitCreateRequest: ...


@overload
def parse(name: Literal["RenderUnitPatch"], value: Any) -> models.RenderUnitPatch: ...


@overload
def parse(name: Literal["AssistantTurn"], value: Any) -> models.AssistantTurn: ...


@overload
def parse(
    name: Literal["AssistantTurnCreateRequest"], value: Any
) -> models.AssistantTurnCreateRequest: ...


@overload
def parse(name: Literal["ExportArtifact"], value: Any) -> models.ExportArtifact: ...


@overload
def parse(name: Literal["ExportCreateRequest"], value: Any) -> models.ExportCreateRequest: ...


@overload
def parse(name: Literal["ExportJob"], value: Any) -> models.ExportJob: ...


@overload
def parse(name: Literal["BookList"], value: Any) -> models.BookList: ...


@overload
def parse(name: Literal["ChapterList"], value: Any) -> models.ChapterList: ...


@overload
def parse(name: Literal["RenderUnitList"], value: Any) -> models.RenderUnitList: ...


@overload
def parse(name: Literal["VoiceList"], value: Any) -> models.VoiceList: ...


@overload
def parse(name: Literal["VoiceSummary"], value: Any) -> models.VoiceSummary: ...


@overload
def parse(name: Literal["VoiceRevision"], value: Any) -> models.VoiceRevision: ...


@overload
def parse(name: Literal["VoiceRevisionV2"], value: Any) -> models.VoiceRevisionV2: ...


@overload
def parse(name: Literal["WorkbenchStatusV2"], value: Any) -> models.WorkbenchStatusV2: ...


@overload
def parse(name: Literal["RenderUnitV2"], value: Any) -> models.RenderUnitV2: ...


@overload
def parse(
    name: Literal["RenderUnitCreateRequestV2"], value: Any
) -> models.RenderUnitCreateRequestV2: ...


@overload
def parse(name: Literal["RenderUnitPatchV2"], value: Any) -> models.RenderUnitPatchV2: ...


@overload
def parse(name: Literal["RenderUnitListV2"], value: Any) -> models.RenderUnitListV2: ...


@overload
def parse(
    name: Literal["RenderUnitGenerationAcceptedV2"], value: Any
) -> models.RenderUnitGenerationAcceptedV2: ...


@overload
def parse(name: Literal["RenderGenerationV2"], value: Any) -> models.RenderGenerationV2: ...


@overload
def parse(
    name: Literal["RenderUnitGenerationListV2"], value: Any
) -> models.RenderUnitGenerationListV2: ...


@overload
def parse(name: Literal["ExportEvent"], value: Any) -> models.ExportEvent: ...


def parse(name: str, value: Any) -> Any:
    return validate_profile("workbench", name, value)
