"""Collect only a complete native ASR run, then stage its bounded Transcription body."""

from __future__ import annotations

import json
from collections.abc import Callable

from speechnexus_gateway.application.file_asr import FileAsrTranscription
from speechnexus_gateway.domain.asr import AsrSegment, AsrWord
from speechnexus_gateway.domain.asr_results import (
    AsrResultError,
    AsrResultStore,
    PreparedAsrResult,
    StagedAsrResult,
    validate_asr_result_limit,
)
from speechnexus_gateway.domain.submission import SubmitRequest


def _source_ms(sample: int, sample_rate_hz: int) -> int:
    return (sample * 1000 + sample_rate_hz // 2) // sample_rate_hz


def _segment_payload(segment: AsrSegment, source_start_ms: int) -> tuple[int, int, bytes]:
    start_ms = source_start_ms + segment.start_ms
    end_ms = source_start_ms + segment.end_ms
    value: dict[str, object] = {
        "start_ms": start_ms,
        "end_ms": end_ms,
        "text": segment.text,
    }
    if segment.id is not None:
        value["id"] = segment.id
    if segment.tokens:
        value["tokens"] = list(segment.tokens)
    if segment.avg_logprob is not None:
        value["avg_logprob"] = segment.avg_logprob
    if segment.compression_ratio is not None:
        value["compression_ratio"] = segment.compression_ratio
    if segment.no_speech_prob is not None:
        value["no_speech_prob"] = segment.no_speech_prob
    if segment.seek is not None:
        value["seek"] = segment.seek
    if segment.temperature is not None:
        value["temperature"] = segment.temperature
    return (
        start_ms,
        end_ms,
        json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
    )


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
    speech_segments = 0
    language_missing = False
    languages: set[str] = set()
    evidence: list[bytes] = []
    evidence_bytes = 0
    segment_ends: list[int] = []
    words: list[bytes] = []
    word_bytes = 0
    word_ends: list[int] = []
    previous_segment_end = 0
    previous_word_start = 0
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
        if type(segment.duration_ms) is not int and segment.duration_ms is not None:
            raise AsrResultError("asr_result_invalid")
        if (
            (segment.language is not None and type(segment.language) is not str)
            or type(segment.segments) is not tuple
            or type(segment.words) is not tuple
            or any(not isinstance(item, AsrSegment) for item in segment.segments)
            or any(not isinstance(word, AsrWord) for word in segment.words)
        ):
            raise AsrResultError("asr_result_invalid")
        clip_ms = round((segment.model_sample_span[1] - segment.model_sample_span[0]) / 16)
        if segment.duration_ms is not None and (
            abs(segment.duration_ms - clip_ms) > 1000 or segment.duration_ms <= 0
        ):
            raise AsrResultError("asr_result_invalid")
        source_start_ms = _source_ms(
            segment.source_sample_span[0], segment.source_clock.sample_rate_hz
        )
        for item in segment.segments:
            if item.end_ms > clip_ms:
                raise AsrResultError("asr_result_invalid")
            start_ms, end_ms, encoded = _segment_payload(item, source_start_ms)
            if start_ms < previous_segment_end:
                raise AsrResultError("asr_result_invalid")
            previous_segment_end = end_ms
            segment_ends.append(end_ms)
            evidence.append(encoded)
            evidence_bytes += len(encoded) + 1
        for word in segment.words:
            if word.end_ms > clip_ms:
                raise AsrResultError("asr_result_invalid")
            start_ms = source_start_ms + word.start_ms
            end_ms = source_start_ms + word.end_ms
            if start_ms < previous_word_start:
                raise AsrResultError("asr_result_invalid")
            previous_word_start = start_ms
            word_ends.append(end_ms)
            encoded = json.dumps(
                {"word": word.word, "start_ms": start_ms, "end_ms": end_ms},
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            words.append(encoded)
            word_bytes += len(encoded) + 1
        if segment.request_id is not None:
            speech_segments += 1
            if segment.language is None:
                language_missing = True
            else:
                languages.add(segment.language)
        if len(body) + evidence_bytes + word_bytes + 2 > max_result_bytes:
            raise AsrResultError("asr_result_limit_exceeded")
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
    duration_ms = summary.duration_ms
    if not 0 < duration_ms <= 2**63 - 1:
        raise AsrResultError("asr_result_invalid")
    if any(end_ms > duration_ms for end_ms in segment_ends + word_ends):
        raise AsrResultError("asr_result_invalid")
    parts = [
        bytes(body),
        b'"',
        b',"duration_ms":',
        str(duration_ms).encode("ascii"),
    ]
    if speech_segments and not language_missing and len(languages) == 1:
        language = next(iter(languages))
        parts.extend([b',"language":', json.dumps(language).encode("ascii")])
    if evidence:
        parts.extend([b',"segments":[', b",".join(evidence), b"]"])
    if words:
        parts.extend([b',"words":[', b",".join(words), b"]"])
    parts.append(b"}")
    completed = b"".join(parts)
    if len(completed) > max_result_bytes:
        raise AsrResultError("asr_result_limit_exceeded")
    result = PreparedAsrResult(request.identity.workspace_id, request.request_digest, completed)

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
