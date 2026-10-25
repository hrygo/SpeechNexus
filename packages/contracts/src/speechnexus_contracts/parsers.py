"""Generated from contracts/bindings.json; do not edit."""

from typing import Any, Literal, overload

from .models import (
    Artifact,
    Asset,
    AssetUploadRequest,
    ErrorResponse,
    Job,
    JobCancellation,
    JobCreateRequest,
    JobEvent,
    JobList,
    Operation,
    OperationCancellation,
    Transcription,
)


@overload
def parse(name: Literal["JobCreateRequest"], value: Any) -> JobCreateRequest: ...


@overload
def parse(name: Literal["Job"], value: Any) -> Job: ...


@overload
def parse(name: Literal["JobList"], value: Any) -> JobList: ...


@overload
def parse(name: Literal["JobEvent"], value: Any) -> JobEvent: ...


@overload
def parse(name: Literal["JobCancellation"], value: Any) -> JobCancellation: ...


@overload
def parse(name: Literal["AssetUploadRequest"], value: Any) -> AssetUploadRequest: ...


@overload
def parse(name: Literal["Asset"], value: Any) -> Asset: ...


@overload
def parse(name: Literal["Artifact"], value: Any) -> Artifact: ...


@overload
def parse(name: Literal["Transcription"], value: Any) -> Transcription: ...


@overload
def parse(name: Literal["Operation"], value: Any) -> Operation: ...


@overload
def parse(name: Literal["OperationCancellation"], value: Any) -> OperationCancellation: ...


@overload
def parse(name: Literal["ErrorResponse"], value: Any) -> ErrorResponse: ...


def parse(name: str, value: Any) -> Any:
    from .validation import validate

    return validate(name, value)
