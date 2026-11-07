"""The composed file runtime routes only the command kind its lanes serve."""

from typing import cast

import pytest

from speechnexus_gateway.domain.file_execution import FileStagingOutcome
from speechnexus_gateway.domain.submission import (
    InvalidExecutionRequestError,
    SubmitRequest,
)
from speechnexus_gateway.domain.tts_results import TtsResultStore
from speechnexus_gateway.file_asr_runtime import AsrFileStage
from speechnexus_gateway.file_speech_runtime import _RoutedExecution, _Stores
from speechnexus_gateway.interfaces.file_tts_command import CommandFileTts
from speechnexus_gateway.interfaces.file_voice_clone_command import CommandFileVoiceClone
from speechnexus_gateway.interfaces.file_voice_design_command import CommandFileVoiceDesign
from tests.fake.execution_command import command_example, original_request


class Lane:
    def __init__(self) -> None:
        self.validated = 0
        self.staged = 0

    def validate_request(self, request: SubmitRequest) -> SubmitRequest:
        self.validated += 1
        return request

    async def stage_result(
        self,
        request: SubmitRequest,
        *,
        store: object,
        cancelled: object,
    ) -> FileStagingOutcome:
        self.staged += 1
        return cast(FileStagingOutcome, object())


def _request(index: int) -> SubmitRequest:
    return original_request(command_example(index))


def _design_only(design: Lane) -> _RoutedExecution:
    return _RoutedExecution(None, None, cast(CommandFileVoiceDesign, design))


def _clone_only(clone: Lane) -> _RoutedExecution:
    return _RoutedExecution(None, None, None, cast(CommandFileVoiceClone, clone))


async def test_a_voicedesign_command_reaches_only_the_design_lane() -> None:
    design, asr, tts = Lane(), Lane(), Lane()
    store = cast(TtsResultStore, object())
    routed = _RoutedExecution(
        cast(AsrFileStage, asr), cast(CommandFileTts, tts), cast(CommandFileVoiceDesign, design)
    )
    request = _request(2)

    assert routed.validate_request(request) is request
    await routed.stage_result(request, store=_Stores(None, None, store), cancelled=lambda: False)

    assert (design.validated, design.staged) == (1, 1)
    assert (asr.validated, asr.staged, tts.validated, tts.staged) == (0, 0, 0, 0)


@pytest.mark.parametrize("index", [0, 1])
def test_tts_and_asr_commands_are_refused_by_a_design_only_gateway(index: int) -> None:
    with pytest.raises(InvalidExecutionRequestError):
        _design_only(Lane()).validate_request(_request(index))


async def test_a_missing_design_store_is_refused_before_staging() -> None:
    design = Lane()

    with pytest.raises(InvalidExecutionRequestError):
        await _design_only(design).stage_result(
            _request(2), store=_Stores(None, None, None), cancelled=lambda: False
        )

    assert design.staged == 0


async def test_a_voiceclone_command_reaches_only_the_clone_lane() -> None:
    clone, design, asr, tts = Lane(), Lane(), Lane(), Lane()
    store = cast(TtsResultStore, object())
    routed = _RoutedExecution(
        cast(AsrFileStage, asr),
        cast(CommandFileTts, tts),
        cast(CommandFileVoiceDesign, design),
        cast(CommandFileVoiceClone, clone),
    )
    request = _request(3)

    assert routed.validate_request(request) is request
    await routed.stage_result(
        request, store=_Stores(None, None, None, store), cancelled=lambda: False
    )

    assert (clone.validated, clone.staged) == (1, 1)
    assert (asr.validated, asr.staged, tts.validated, tts.staged) == (0, 0, 0, 0)
    assert (design.validated, design.staged) == (0, 0)


@pytest.mark.parametrize("index", [0, 1, 2])
def test_other_kinds_are_refused_by_a_clone_only_gateway(index: int) -> None:
    with pytest.raises(InvalidExecutionRequestError):
        _clone_only(Lane()).validate_request(_request(index))


async def test_a_missing_clone_store_is_refused_before_staging() -> None:
    clone = Lane()

    with pytest.raises(InvalidExecutionRequestError):
        await _clone_only(clone).stage_result(
            _request(3), store=_Stores(None, None, None, None), cancelled=lambda: False
        )

    assert clone.staged == 0
