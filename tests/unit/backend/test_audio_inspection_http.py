"""The HTTP AssetInspector speaks the frozen internal protocol and nothing else.

Every private outcome is asserted through its *public* consequence: the error
code a caller sees, the HTTP status beside it, and the `retryable` flag. Fakes
are injected as an httpx transport, so no inspector process or socket is started
and the exact request the adapter builds is observable.
"""

from __future__ import annotations

import hashlib
import io
import json
import socket
import wave
from contextlib import redirect_stdout
from tempfile import TemporaryFile
from typing import Any, cast

import httpx
import pytest
from pydantic import SecretStr

from speechnexus_asset_inspector import child
from speechnexus_asset_inspector.protocol import HTTP_STATUS, failure_body
from speechnexus_backend.speech.domain.assets import (
    AssetError,
    AssetPolicy,
    AssetPurpose,
    Inspection,
    MediaFacts,
)
from speechnexus_backend.speech.infrastructure.audio_inspection_http import (
    HttpAudioAssetInspector,
)
from speechnexus_backend.speech.interfaces.http.asset_errors import asset_error
from tests.unit.backend.test_asset_inspector_wiring import _service

BODY = b"RIFF" + b"\x00" * 60 + b"WAVEfmt " + b"\x01" * 256
POLICY = AssetPolicy(60)
URL = "http://asset-inspector.invalid:8080"
TOKEN = SecretStr("inspection-token-0001")

# inspector status, its HTTP status, the public code, the public HTTP status and
# whether a new request may succeed later.
MAPPING = [
    ("bad_request", 400, "asset_inspection_unavailable", 503, False),
    ("unauthorized", 401, "asset_inspection_unavailable", 503, False),
    ("encoded_too_large", 413, "asset_inspection_unavailable", 503, False),
    ("unsupported_media", 415, "invalid_asset_audio", 422, False),
    ("invalid_media", 422, "invalid_asset_audio", 422, False),
    ("media_limit_exceeded", 422, "asset_audio_too_long", 422, False),
    ("busy", 429, "asset_inspection_busy", 503, True),
    ("unavailable", 503, "asset_inspection_busy", 503, True),
    ("deadline_exceeded", 504, "asset_inspection_busy", 503, True),
]


def receipt(body: bytes = BODY, **changes: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "schema_version": 1,
        "status": "complete",
        "recipe_version": "pcm-decode-1",
        "sha256": "sha256:" + hashlib.sha256(body).hexdigest(),
        "encoded_bytes": len(body),
        "media_type": "audio/wav",
        "sample_rate_hz": 16000,
        "channels": 1,
        "source_samples": 16000,
        "duration_ms": 1000,
    }
    value.update(changes)
    return value


def json_response(status: int, value: Any) -> httpx.Response:
    return httpx.Response(
        status,
        content=json.dumps(value).encode(),
        headers={"Content-Type": "application/json"},
    )


def failure(status: str, code: int) -> httpx.Response:
    return json_response(code, {"schema_version": 1, "status": status})


def inspector(
    handler: Any,
    **changes: Any,
) -> HttpAudioAssetInspector:
    limits: dict[str, Any] = {
        "timeout_seconds": 15,
        "wall_seconds": 10,
        "max_duration_ms": 120_000,
        "max_input_bytes": 25 * 1024 * 1024,
        "max_receipt_bytes": 4096,
    }
    limits.update(changes)
    return HttpAudioAssetInspector(
        URL,
        TOKEN,
        _transport=httpx.MockTransport(handler),
        **limits,
    )


def public(code: str) -> tuple[int, dict[str, Any], str]:
    """The caller's view of a rejection: status, error body and retry header."""
    response = asset_error(code, "req_demo")
    return (
        response.status_code,
        json.loads(bytes(response.body))["error"],
        response.headers["X-Should-Retry"],
    )


def test_success_sends_exactly_one_bounded_request_and_returns_local_facts() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return json_response(200, receipt())

    result = inspector(handler).inspect("asr_input", io.BytesIO(BODY), POLICY)

    assert result == Inspection(facts=MediaFacts("audio/wav", 1000, 16000, 1))
    assert len(seen) == 1
    request = seen[0]
    assert request.method == "POST"
    assert str(request.url) == URL + "/internal/v1/inspection/audio"
    assert request.content == BODY
    assert request.headers.get_list("content-length") == [str(len(BODY))]
    assert request.headers.get_list("authorization") == ["Bearer " + TOKEN.get_secret_value()]
    assert request.headers.get_list("content-type") == ["application/octet-stream"]
    # Budgets travel as headers; the wall clock is sent in milliseconds.
    assert request.headers.get_list("x-nexus-deadline-ms") == ["10000"]
    assert request.headers.get_list("x-nexus-max-duration-ms") == ["120000"]


@pytest.mark.parametrize("status,code,public_code,public_status,retryable", MAPPING)
def test_every_private_status_maps_to_its_frozen_public_code(
    status: str, code: int, public_code: str, public_status: int, retryable: bool
) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return failure(status, code)

    with pytest.raises(AssetError) as caught:
        inspector(handler).inspect("asr_input", io.BytesIO(BODY), POLICY)

    assert caught.value.code == public_code
    # A single attempt: no retry, no fallback, no second status probe.
    assert len(calls) == 1
    http_status, error, should_retry = public(caught.value.code)
    assert (http_status, error["code"], error["retryable"]) == (
        public_status,
        public_code,
        retryable,
    )
    # Transport replay stays suppressed even for the retryable code; only
    # `error.retryable` permits a new logical upload.
    assert should_retry == "false"


@pytest.mark.parametrize(
    "status,public_code,retryable",
    [
        ("bad_request", "asset_inspection_unavailable", False),
        ("unauthorized", "asset_inspection_unavailable", False),
        ("encoded_too_large", "asset_inspection_unavailable", False),
        ("unsupported_media", "invalid_asset_audio", False),
        ("invalid_media", "invalid_asset_audio", False),
        ("media_limit_exceeded", "asset_audio_too_long", False),
        ("busy", "asset_inspection_busy", True),
        ("unavailable", "asset_inspection_busy", True),
        ("deadline_exceeded", "asset_inspection_busy", True),
    ],
)
def test_the_wire_status_word_alone_selects_the_public_family(
    status: str, public_code: str, retryable: bool
) -> None:
    """The status word decides, not the HTTP code a proxy might rewrite."""

    def handler(request: httpx.Request) -> httpx.Response:
        return failure(status, 500)

    with pytest.raises(AssetError) as caught:
        inspector(handler).inspect("asr_input", io.BytesIO(BODY), POLICY)

    assert caught.value.code == public_code
    assert public(caught.value.code)[1]["retryable"] is retryable


@pytest.mark.parametrize(
    "error",
    [
        httpx.ConnectError("inspector is unreachable"),
        httpx.ConnectTimeout("inspector did not accept the connection"),
        httpx.ReadTimeout("inspector did not answer"),
        httpx.WriteTimeout("inspector did not accept the body"),
        httpx.PoolTimeout("no connection slot"),
        httpx.RemoteProtocolError("connection reset"),
    ],
)
def test_transport_failures_are_the_permanent_unavailable_outcome(error: Exception) -> None:
    """An unreachable inspector is a deployment fault, not capacity.

    No verdict was obtained, so there is nothing a content retry could fix. The
    dominant cause is a wrong endpoint or a process that is not running; calling
    it retryable would hide a dead configuration behind an infinite retry loop.
    """

    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise error

    with pytest.raises(AssetError) as caught:
        inspector(handler).inspect("asr_input", io.BytesIO(BODY), POLICY)

    assert caught.value.code == "asset_inspection_unavailable"
    # One attempt only: no retry, no fallback to the text inspector, and no
    # second request whose body might no longer be available.
    assert len(calls) == 1
    http_status, body, should_retry = public(caught.value.code)
    assert (http_status, body["retryable"], should_retry) == (503, False, "false")


def test_a_refused_connection_sends_nothing_and_is_not_retryable() -> None:
    """A real closed loopback port proves the request bytes never leave.

    The transport records only attempts, so the assertion is made twice: the
    adapter attempts exactly one request and the caller is told the deployment
    is unusable, which is what an inspector that is not running looks like.
    """
    attempts: list[httpx.Request] = []

    def refuse(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        raise httpx.ConnectError("connection refused", request=request)

    adapter = inspector(refuse)
    with pytest.raises(AssetError) as caught:
        adapter.inspect("voice_reference", io.BytesIO(BODY), POLICY)

    assert caught.value.code == "asset_inspection_unavailable"
    assert len(attempts) == 1
    http_status, body, should_retry = public(caught.value.code)
    assert (http_status, body["code"], body["param"], body["retryable"], should_retry) == (
        503,
        "asset_inspection_unavailable",
        None,
        False,
        "false",
    )


def test_an_unreachable_real_endpoint_is_not_reported_as_capacity() -> None:
    """The acceptance scenario: a port with no listener is a deployment fault.

    A local address keeps the check deterministic and off any external network;
    the connection is refused before a single request byte is written.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reserved:
        reserved.bind(("127.0.0.1", 0))
        port = reserved.getsockname()[1]
    # The socket is closed, so nothing listens on that port any more.
    unreachable = HttpAudioAssetInspector(
        f"http://127.0.0.1:{port}",
        TOKEN,
        timeout_seconds=15,
        wall_seconds=10,
        max_duration_ms=120_000,
        max_input_bytes=1024,
        max_receipt_bytes=4096,
    )
    try:
        with pytest.raises(AssetError) as caught:
            unreachable.inspect("asr_input", io.BytesIO(BODY), POLICY)
    finally:
        unreachable.close()

    assert caught.value.code == "asset_inspection_unavailable"
    http_status, body, should_retry = public(caught.value.code)
    assert (http_status, body["retryable"], should_retry) == (503, False, "false")


@pytest.mark.parametrize(
    "changes",
    [
        {"sha256": "sha256:" + "0" * 64},
        {"encoded_bytes": len(BODY) + 1},
        {"media_type": "audio/pcm"},
        {"duration_ms": 120_001},
        # sample clock and duration disagree: 16000 samples at 16 kHz is 1000 ms
        {"source_samples": 16000, "sample_rate_hz": 48000, "duration_ms": 1000},
        {"source_samples": 16000.0},
        {"sample_rate_hz": 0},
        {"channels": 0},
        {"encoded_bytes": float(len(BODY))},
        {"schema_version": True},
        {"schema_version": 1.0},
        {"media_type": "text/plain"},
        {"status": "partial"},
        {"recipe_version": "pcm-decode-2"},
        {"schema_version": 2},
    ],
)
def test_a_receipt_that_fails_revalidation_is_configuration_unavailability(
    changes: dict[str, Any],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return json_response(200, receipt(**changes))

    with pytest.raises(AssetError) as caught:
        inspector(handler).inspect("asr_input", io.BytesIO(BODY), POLICY)

    # The bytes may be perfectly valid: this is never a content rejection.
    assert caught.value.code == "asset_inspection_unavailable"
    assert public(caught.value.code)[1]["retryable"] is False


def test_an_extra_receipt_field_is_refused() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return json_response(200, receipt(transcript="must never be trusted"))

    with pytest.raises(AssetError) as caught:
        inspector(handler).inspect("asr_input", io.BytesIO(BODY), POLICY)

    assert caught.value.code == "asset_inspection_unavailable"


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"not json",
        b'{"schema_version": 1}',
        b'{"schema_version": "1", "status": "busy"}',
        b'{"schema_version": 1, "status": "mystery"}',
        b'{"schema_version": 1, "status": "busy", "detail": "slots"}',
        b'{"schema_version": 1, "status": "busy", "status": "busy"}',
        b'{"schema_version": NaN, "status": "busy"}',
    ],
)
def test_an_unparsable_or_non_closed_failure_envelope_is_unavailable(raw: bytes) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, content=raw)

    with pytest.raises(AssetError) as caught:
        inspector(handler).inspect("asr_input", io.BytesIO(BODY), POLICY)

    assert caught.value.code == "asset_inspection_unavailable"
    assert public(caught.value.code)[1]["retryable"] is False


def test_a_buffered_receipt_over_the_bound_is_refused_whole() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return json_response(200, receipt(padding="x" * 4096))

    with pytest.raises(AssetError) as caught:
        inspector(handler, max_receipt_bytes=256).inspect("asr_input", io.BytesIO(BODY), POLICY)

    assert caught.value.code == "asset_inspection_unavailable"


def test_a_streamed_receipt_over_the_bound_is_refused_before_reading_it_all() -> None:
    chunks = [b'{"schema_version":1,', b'"status":"busy"', b"}" * 512]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, content=iter(chunks))

    with pytest.raises(AssetError) as caught:
        inspector(handler, max_receipt_bytes=64).inspect("asr_input", io.BytesIO(BODY), POLICY)

    assert caught.value.code == "asset_inspection_unavailable"


def test_text_input_keeps_the_text_inspector_and_an_unserved_purpose_is_named() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise AssertionError("no inspection request expected")  # pragma: no cover

    adapter = inspector(handler)
    text = adapter.inspect("text_input", io.BytesIO("合成文本".encode()), POLICY)
    assert text.facts == MediaFacts("text/plain; charset=utf-8")

    # A purpose this adapter does not serve is named instead of sharing the
    # "no inspector assembled" refusal: the caller must be able to tell a
    # capability gap from a deployment that cannot inspect at all.
    with pytest.raises(AssetError) as caught:
        adapter.inspect(cast(AssetPurpose, "audio_preview"), io.BytesIO(BODY), POLICY)

    assert caught.value.code == "asset_inspection_purpose_unsupported"
    # Nothing was decoded: the purpose is refused before the body is read.
    assert calls == []
    http_status, body, should_retry = public(caught.value.code)
    assert (http_status, body["code"], body["param"], body["retryable"], should_retry) == (
        503,
        "asset_inspection_unavailable",
        "purpose",
        False,
        "false",
    )
    assert body["message"] == "Asset inspection does not support this asset purpose."


def test_a_reference_clip_is_inspected_with_its_own_duration_budget() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return json_response(200, receipt())

    adapter = inspector(handler)
    policy = AssetPolicy(60, max_reference_duration_ms=42_000)

    reference = adapter.inspect("voice_reference", io.BytesIO(BODY), policy)
    assert reference.facts == MediaFacts("audio/wav", 1000, 16000, 1)
    # A reference clip is not an ASR utterance: the policy ceiling travels as the
    # per-request budget instead of the adapter's 120 s decode budget.
    assert seen[0].headers.get_list("x-nexus-max-duration-ms") == ["42000"]

    assert adapter.inspect("asr_input", io.BytesIO(BODY), policy).facts is not None
    assert seen[1].headers.get_list("x-nexus-max-duration-ms") == ["120000"]


def test_a_reference_receipt_over_its_own_budget_is_unavailable() -> None:
    """The per-purpose ceiling is revalidated locally, not just requested.

    The ASR budget is wider than the reference ceiling here, so only the
    purpose-specific check can reject this receipt.
    """
    body = BODY

    def handler(request: httpx.Request) -> httpx.Response:
        # 976_000 samples at 16 kHz is exactly 61 000 ms, inside the ASR budget.
        return json_response(200, receipt(duration_ms=61_000, source_samples=976_000))

    adapter = inspector(handler, max_duration_ms=120_000)
    policy = AssetPolicy(60)

    with pytest.raises(AssetError) as caught:
        adapter.inspect("voice_reference", io.BytesIO(body), policy)
    assert caught.value.code == "asset_inspection_unavailable"

    # Control: the same receipt is acceptable for an ASR input.
    assert adapter.inspect("asr_input", io.BytesIO(body), policy).facts is not None


def test_the_composed_runtime_inspects_a_reference_with_its_configured_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The configured key reaches a real reference inspection, not just a policy."""
    from speechnexus_backend import asset_runtime as asset_runtime_module
    from speechnexus_backend.asset_runtime import AssetRuntime
    from tests.unit.backend.test_asset_inspector_wiring import _audio_settings

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return json_response(200, receipt())

    real = HttpAudioAssetInspector

    def build(url: str, token: SecretStr, **limits: Any) -> HttpAudioAssetInspector:
        return real(url, token, _transport=httpx.MockTransport(handler), **limits)

    monkeypatch.setattr(asset_runtime_module, "HttpAudioAssetInspector", build)
    runtime = AssetRuntime(_audio_settings(audio_inspection_max_reference_duration_ms=30_000))
    try:
        result = runtime.inspector.inspect(
            "voice_reference", io.BytesIO(BODY), runtime.service.policy
        )
    finally:
        runtime.close()

    assert result.facts == MediaFacts("audio/wav", 1000, 16000, 1)
    assert seen[0].headers.get_list("x-nexus-max-duration-ms") == ["30000"]


def test_an_input_over_the_local_ceiling_is_a_configuration_fault_not_content() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return json_response(200, receipt())

    with pytest.raises(AssetError) as caught:
        inspector(handler, max_input_bytes=64).inspect("asr_input", io.BytesIO(BODY), POLICY)

    # The two API-side ceilings disagree, so nothing is sent and the media is
    # never blamed for a deployment mismatch.
    assert caught.value.code == "asset_inspection_unavailable"
    assert calls == []


def test_cancellation_before_send_skips_the_request() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return json_response(200, receipt())

    adapter = HttpAudioAssetInspector(
        URL,
        TOKEN,
        timeout_seconds=15,
        wall_seconds=10,
        max_duration_ms=120_000,
        max_input_bytes=1024,
        max_receipt_bytes=4096,
        cancelled=lambda: True,
        _transport=httpx.MockTransport(handler),
    )

    assert adapter.inspect("asr_input", io.BytesIO(BODY), POLICY) == Inspection(available=False)
    assert calls == []


def test_a_closed_inspector_refuses_new_work_and_closes_idempotently() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - unused
        raise AssertionError("no inspection request expected")

    adapter = inspector(handler)
    assert adapter.close() is True
    assert adapter.close() is True

    with pytest.raises(AssetError) as caught:
        adapter.inspect("asr_input", io.BytesIO(BODY), POLICY)

    # The runtime is being dismantled; a restarted process can serve the request,
    # so this is temporary rather than a configuration refusal.
    assert caught.value.code == "asset_inspection_busy"


def test_a_rejection_leaves_the_prepared_body_rewound() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return failure("invalid_media", 422)

    body = io.BytesIO(BODY)
    with pytest.raises(AssetError):
        inspector(handler).inspect("asr_input", body, POLICY)

    # The caller still owns the body and may publish from it after a rejection.
    assert body.tell() == 0


def test_a_success_receipt_leaves_the_body_rewound_for_publication() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return json_response(200, receipt())

    body = io.BytesIO(BODY)
    assert inspector(handler).inspect("asr_input", body, POLICY).facts is not None
    assert body.tell() == 0


@pytest.mark.parametrize(
    "status,code,public_code",
    [
        # A permanent content verdict must not leave a rejected row behind.
        ("invalid_media", 422, "invalid_asset_audio"),
        ("unsupported_media", 415, "invalid_asset_audio"),
        ("media_limit_exceeded", 422, "asset_audio_too_long"),
        # Neither must an unavailable or busy inspector.
        ("busy", 429, "asset_inspection_busy"),
        ("unauthorized", 401, "asset_inspection_unavailable"),
    ],
)
def test_no_asset_row_or_object_write_survives_any_inspection_failure(
    status: str, code: int, public_code: str
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return failure(status, code)

    service, metadata, objects = _service(inspector(handler))
    with pytest.raises(AssetError) as caught:
        service.upload(io.BytesIO(BODY), workspace_id="workspace-default", purpose="asr_input")

    assert caught.value.code == public_code
    # Inspection precedes every transaction and object write, so a rejection or
    # an outage leaves the business tables and the store untouched.
    metadata.reserve.assert_not_called()
    objects.check_writes_available.assert_not_called()


def test_a_base_url_with_a_trailing_slash_still_addresses_the_fixed_path() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return json_response(200, receipt())

    adapter = HttpAudioAssetInspector(
        URL + "/",
        TOKEN,
        timeout_seconds=15,
        wall_seconds=10,
        max_duration_ms=120_000,
        max_input_bytes=1024,
        max_receipt_bytes=4096,
        _transport=httpx.MockTransport(handler),
    )
    adapter.inspect("asr_input", io.BytesIO(BODY), POLICY)

    assert str(seen[0].url) == URL + "/internal/v1/inspection/audio"


def test_cancellation_after_the_receipt_is_still_honoured() -> None:
    cancelled = {"value": False}

    def handler(request: httpx.Request) -> httpx.Response:
        cancelled["value"] = True
        return json_response(200, receipt())

    adapter = HttpAudioAssetInspector(
        URL,
        TOKEN,
        timeout_seconds=15,
        wall_seconds=10,
        max_duration_ms=120_000,
        max_input_bytes=1024,
        max_receipt_bytes=4096,
        cancelled=lambda: cancelled["value"],
        _transport=httpx.MockTransport(handler),
    )

    # A validated receipt still never becomes a published fact after cancellation.
    assert adapter.inspect("asr_input", io.BytesIO(BODY), POLICY) == Inspection(available=False)


def test_a_streamed_success_receipt_is_read_to_the_end() -> None:
    raw = json.dumps(receipt()).encode()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=iter([raw[:20], raw[20:]]))

    result = inspector(handler).inspect("asr_input", io.BytesIO(BODY), POLICY)

    assert result.facts == MediaFacts("audio/wav", 1000, 16000, 1)


def test_a_read_timeout_while_streaming_the_receipt_is_not_retryable() -> None:
    def chunks() -> Any:
        yield b'{"schema_version":1,'
        raise httpx.ReadTimeout("response stalled")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=chunks())

    with pytest.raises(AssetError) as caught:
        inspector(handler).inspect("asr_input", io.BytesIO(BODY), POLICY)

    # The status line arrived without the verdict: still a transport fault, so
    # the caller is not told to retry a deployment that never answered.
    assert caught.value.code == "asset_inspection_unavailable"


@pytest.mark.parametrize(
    "changes",
    [
        {"token": SecretStr("")},
        {"timeout_seconds": 0},
        {"wall_seconds": 0},
        {"max_duration_ms": 0},
        {"max_input_bytes": 0},
        {"max_receipt_bytes": 0},
    ],
)
def test_unusable_constructor_limits_are_refused(changes: dict[str, Any]) -> None:
    limits: dict[str, Any] = {
        "timeout_seconds": 15,
        "wall_seconds": 10,
        "max_duration_ms": 120_000,
        "max_input_bytes": 1024,
        "max_receipt_bytes": 4096,
    }
    token = changes.pop("token", TOKEN)
    limits.update(changes)
    with pytest.raises(ValueError):
        HttpAudioAssetInspector(
            URL,
            token,
            _transport=httpx.MockTransport(lambda request: httpx.Response(200)),
            **limits,
        )


# ---------------------------------------------------------------------------
# Content verdicts end to end through the surviving implementation.
#
# The retired in-process supervisor was the last place where real audio bytes,
# the real decoder and this adapter met. Those judgements have two owners now:
# the `asset-inspector` unit decodes and classifies the content (its own verdicts
# are asserted in `tests/unit/inspector/test_inspection_service.py`), and this
# adapter projects the wire verdict onto the frozen public codes. The handler
# below keeps the two joined: it runs the inspector's own decode child on the
# exact bytes the adapter sent and answers in the inspector's wire vocabulary,
# so a content verdict is produced by real decoding, not by a canned receipt.
# ---------------------------------------------------------------------------

_CHILD_MAX_BYTES = 1 << 20


def pcm_wav(samples: int = 4800, *, sample_rate_hz: int = 48000, channels: int = 2) -> bytes:
    """A structurally complete synthetic PCM WAV: 4800 frames are 100 ms."""
    output = io.BytesIO()
    with wave.open(output, "wb") as encoded:
        encoded.setnchannels(channels)
        encoded.setsampwidth(2)
        encoded.setframerate(sample_rate_hz)
        encoded.writeframes(b"\x01\x00\x02\x00" * samples)
    return output.getvalue()


def decoded(request: httpx.Request) -> httpx.Response:
    """Answer with the real decode child's verdict for the received bytes."""
    # The budget travels in the request header, exactly as the protocol defines it.
    budget_ms = int(request.headers["X-Nexus-Max-Duration-Ms"])
    with TemporaryFile() as source:
        source.write(request.content)
        source.flush()
        source.seek(0)
        printed = io.StringIO()
        with redirect_stdout(printed):
            child.main(
                [
                    "--input-fd",
                    str(source.fileno()),
                    "--max-encoded-bytes",
                    str(_CHILD_MAX_BYTES),
                    "--max-duration-ms",
                    str(budget_ms),
                    "--timeout-seconds",
                    "10",
                ]
            )
    document = json.loads(printed.getvalue())
    if document["status"] == "complete":
        return json_response(200, document)
    return httpx.Response(
        HTTP_STATUS[document["status"]],
        content=failure_body(document["status"]),
        headers={"Content-Type": "application/json"},
    )


def test_qualified_audio_becomes_the_decoded_facts_through_the_real_decoder() -> None:
    result = inspector(decoded).inspect("asr_input", io.BytesIO(pcm_wav()), POLICY)

    assert result.available and result.error_code is None
    assert result.facts == MediaFacts("audio/wav", 100, 48000, 2)


@pytest.mark.parametrize(
    "content",
    [
        # Not a container at all, and a valid container missing its tail.
        b"synthetic-private-corrupt-audio",
        pcm_wav()[:-64],
    ],
)
def test_malformed_audio_is_the_permanent_content_verdict(content: bytes) -> None:
    with pytest.raises(AssetError) as caught:
        inspector(decoded).inspect("asr_input", io.BytesIO(content), POLICY)

    assert caught.value.code == "invalid_asset_audio"
    status, body, should_retry = public(caught.value.code)
    assert (status, body["retryable"], should_retry) == (422, False, "false")


def test_audio_over_the_shared_duration_budget_is_the_permanent_too_long_verdict() -> None:
    # One key bounds both the request the adapter sends and the child's own
    # decode budget, so exceeding it is the caller's file, never a deployment
    # fault and never retryable.
    with pytest.raises(AssetError) as caught:
        inspector(decoded, max_duration_ms=50).inspect("asr_input", io.BytesIO(pcm_wav()), POLICY)

    assert caught.value.code == "asset_audio_too_long"
    status, body, should_retry = public(caught.value.code)
    assert (status, body["retryable"], should_retry) == (422, False, "false")


def test_a_qualified_reference_is_decoded_by_the_same_recipe() -> None:
    result = inspector(decoded).inspect("voice_reference", io.BytesIO(pcm_wav()), AssetPolicy(60))

    assert result.available and result.error_code is None
    assert result.facts == MediaFacts("audio/wav", 100, 48000, 2)


def test_a_malformed_reference_is_the_permanent_content_verdict() -> None:
    with pytest.raises(AssetError) as caught:
        inspector(decoded).inspect("voice_reference", io.BytesIO(pcm_wav()[:-64]), AssetPolicy(60))

    assert caught.value.code == "invalid_asset_audio"
    status, body, should_retry = public(caught.value.code)
    assert (status, body["retryable"], should_retry) == (422, False, "false")


def test_a_reference_over_the_clone_ceiling_is_the_permanent_too_long_verdict() -> None:
    # 1.5 s of valid reference audio against a 1 s ceiling: the longest clip a
    # clone accepts is what the policy bounds, so this is the caller's file.
    with pytest.raises(AssetError) as caught:
        inspector(decoded).inspect(
            "voice_reference",
            io.BytesIO(pcm_wav(samples=72_000)),
            AssetPolicy(60, max_reference_duration_ms=1_000),
        )

    assert caught.value.code == "asset_audio_too_long"
    status, body, should_retry = public(caught.value.code)
    assert (status, body["retryable"], should_retry) == (422, False, "false")
