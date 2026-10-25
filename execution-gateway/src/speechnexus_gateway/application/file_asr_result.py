"""Collect only a complete native ASR run, then stage its bounded Transcription body."""

from __future__ import annotations

import json
from collections.abc import Callable

from speechnexus_gateway.application.file_asr import FileAsrTranscription
from speechnexus_gateway.domain.asr_results import (
    AsrResultError,
    AsrResultStore,
    PreparedAsrResult,
    StagedAsrResult,
    validate_asr_result_limit,
)
from speechnexus_gateway.domain.submission import SubmitRequest


async def stage_file_asr_result(
    request: SubmitRequest,
    transcription: FileAsrTranscription,
    *,
    store: AsrResultStore,
    max_result_bytes: int,
    check: Callable[[], None],
) -> StagedAsrResult:
    """Newline-join ordered, nonoverlapping model segments; never guess overlap removal.

    The caller has already validated/admitted the original command and owns the
    transcription context. ``check`` enforces its original deadline, binding and
    cancellation, including after storage returns.
    """
    validate_asr_result_limit(max_result_bytes)
    check()
    body = bytearray(b'{"text":"')
    count = 0
    previous_end = 0
    final_source_end = 0
    source_clock = None
    has_text = False
    policy_digest = None
    vad_policy_digest = None
    async for segment in transcription:
        check()
        count += 1
        if (
            segment.sequence != count
            or segment.binding_revision_id != request.identity.binding_revision_id
            or segment.config_revision != request.identity.policy_revision_id
            or segment.model_sample_span[0] < previous_end
            or segment.model_sample_span[0] >= segment.model_sample_span[1]
            or (source_clock is not None and segment.source_clock != source_clock)
            or (policy_digest is not None and segment.policy_digest != policy_digest)
            or (vad_policy_digest is not None and segment.vad_policy_digest != vad_policy_digest)
            or type(segment.text) is not str
            or (segment.no_speech and segment.text != "")
        ):
            raise AsrResultError("asr_result_invalid")
        source_clock = segment.source_clock
        policy_digest = segment.policy_digest
        vad_policy_digest = segment.vad_policy_digest
        previous_end = segment.model_sample_span[1]
        final_source_end = segment.source_sample_span[1]
        if segment.text:
            # Reject an oversized vendor string before escaping/encoding it.
            if len(segment.text) > max_result_bytes - len(body) - 2:
                raise AsrResultError("asr_result_limit_exceeded")
            try:
                encoded = json.dumps(segment.text, ensure_ascii=False)[1:-1].encode("utf-8")
            except UnicodeError:
                raise AsrResultError("asr_result_invalid") from None
            separator = b"\\n" if has_text else b""
            if len(body) + len(separator) + len(encoded) + 2 > max_result_bytes:
                raise AsrResultError("asr_result_limit_exceeded")
            body.extend(separator)
            body.extend(encoded)
            has_text = True
    check()
    summary = transcription.summary
    if (
        count == 0
        or summary is None
        or summary.clock != source_clock
        or summary.output_sample_rate_hz != 16000
        or summary.output_samples != previous_end
        or summary.source_samples != final_source_end
    ):
        raise AsrResultError("asr_result_incomplete")
    body.extend(b'"}')
    result = PreparedAsrResult(request.identity.workspace_id, request.request_digest, bytes(body))

    def cancelled() -> bool:
        check()
        return False

    receipt = await store.stage(result, cancelled=cancelled)
    check()
    if receipt.request_digest != result.request_digest or receipt.reference != result.reference(
        receipt.reference.storage_bucket
    ):
        raise AsrResultError("asr_result_identity_mismatch")
    return receipt
