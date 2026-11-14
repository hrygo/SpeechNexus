"""One synthesis capability, one inline body bound.

`/v1/audio/speech` accepts one `input` per synthesis and the `tts` Job kind
performs exactly one synthesis per Job, so the two declared bounds must agree.
They must also agree with the domain and gateway limits that actually enforce
them; a wider contract only promises text the service refuses, and a narrower
one hides capability the service has.

The route checks below keep the admitted text intact and prove the over-bound
reply is a declared error rather than an undifferentiated 400.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
import yaml
from httpx import ASGITransport, AsyncClient

from speechnexus_backend.app import create_app
from speechnexus_backend.speech.application.public_jobs import JobAdmissionRejectedError
from speechnexus_backend.speech.application.speech_synthesis import SpeechUnavailableError
from speechnexus_backend.speech.domain.speech_synthesis import MAX_SPEECH_TEXT_CHARS
from speechnexus_gateway.domain.tts import MAX_TTS_TEXT_CHARS
from tests.support.contracts import validator
from tests.unit.backend.test_health import _settings

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "contracts" / "speech-openapi.yaml"
DOCUMENT = cast(dict[str, Any], validator("speech-openapi.yaml").schema)


def _schemas() -> dict[str, Any]:
    document = yaml.safe_load(SOURCE.read_text())
    return cast(dict[str, Any], document["components"]["schemas"])


def _async_inline_text(schemas: dict[str, Any]) -> dict[str, Any]:
    """The TtsJobInput branch that admits inline `text`, not `text_asset_id`."""

    branches = [
        branch
        for branch in schemas["TtsJobInput"]["oneOf"]
        if "text" in branch.get("properties", {})
    ]
    assert len(branches) == 1, "TtsJobInput must declare exactly one inline text branch"
    return cast(dict[str, Any], branches[0]["properties"]["text"])


def _synchronous_input(schemas: dict[str, Any]) -> dict[str, Any]:
    return cast(dict[str, Any], schemas["SpeechRequest"]["properties"]["input"])


def test_async_tts_body_bound_equals_the_synchronous_body_bound() -> None:
    schemas = _schemas()
    asynchronous = _async_inline_text(schemas)
    synchronous = _synchronous_input(schemas)

    assert asynchronous["type"] == synchronous["type"] == "string"
    assert asynchronous["minLength"] == synchronous["minLength"] == 1
    assert asynchronous["maxLength"] == synchronous["maxLength"], (
        "TtsJobInput.text and SpeechRequest.input describe one synthesis each; "
        "two different bounds make the Job contract promise text the synchronous "
        "route refuses"
    )


def test_declared_body_bounds_match_the_enforcing_limits() -> None:
    bounds = _async_inline_text(_schemas())["maxLength"]

    assert bounds == MAX_SPEECH_TEXT_CHARS, (
        "the Job contract must not promise more text than the domain admits"
    )
    assert bounds == MAX_TTS_TEXT_CHARS, (
        "the Job contract must not promise more text than the gateway submits"
    )


def _job_body(text: str) -> dict[str, Any]:
    return {
        "kind": "tts",
        "model": "tts-quality",
        "input": {"text": text},
        "voice": {"id": "voice-vivian", "revision": "4"},
        "client_reference": "synthetic-bound",
    }


class _RecordingJobs:
    """Records the admitted command and refuses at the declared admission boundary."""

    def __init__(self) -> None:
        self.submitted: list[dict[str, Any]] = []

    def submit(self, command: Any, *, request_id: str, idempotency_key: str | None) -> Any:
        self.submitted.append(cast(dict[str, Any], command))
        raise JobAdmissionRejectedError("job_unavailable")

    def artifacts(self, job: Any) -> tuple[()]:
        return ()


def _declared_response(operation: str, status: int) -> dict[str, Any]:
    for item in cast(dict[str, Any], DOCUMENT["paths"]).values():
        for specification in item.values():
            if isinstance(specification, dict) and specification.get("operationId") == operation:
                response: dict[str, Any] = specification["responses"][str(status)]
                while "$ref" in response:
                    response = DOCUMENT["components"]["responses"][
                        response["$ref"].rsplit("/", 1)[1]
                    ]
                return response
    raise AssertionError(f"{operation} does not declare {status}")


async def test_bound_text_reaches_admission_unchanged() -> None:
    jobs = _RecordingJobs()
    app = create_app(settings=_settings(), jobs=cast(Any, jobs))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/nexus/jobs",
            json=_job_body("啊" * MAX_SPEECH_TEXT_CHARS),
            headers={"Idempotency-Key": "synthetic-bound-04"},
        )

    assert response.status_code == 503, response.text
    assert len(jobs.submitted) == 1
    assert len(jobs.submitted[0]["input"]["text"]) == MAX_SPEECH_TEXT_CHARS


@pytest.mark.parametrize(
    "text",
    ["啊" * (MAX_SPEECH_TEXT_CHARS + 1), "啊" * 20_000, "a" * 200_000],
    ids=["cjk-4097", "cjk-20000", "ascii-200000"],
)
async def test_over_bound_text_is_rejected_with_the_declared_error(text: str) -> None:
    jobs = _RecordingJobs()
    app = create_app(settings=_settings(), jobs=cast(Any, jobs))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/v1/nexus/jobs",
            json=_job_body(text),
            headers={"Idempotency-Key": "synthetic-bound-05"},
        )

    assert response.status_code == 422, response.text
    # The reply is the declared 422 envelope, not an arbitrary framework error.
    source = validator("speech-openapi.yaml")
    declared = _declared_response("createJob", 422)["content"]["application/json"]["schema"]
    source.evolve(schema={**DOCUMENT, "allOf": [declared]}).validate(response.json())
    error = response.json()["error"]
    assert error["code"] == "unsupported_parameter"
    assert error["retryable"] is False
    # Rejection happens at the contract gate: no admission attempt, no Job row.
    assert jobs.submitted == []


def test_both_entries_document_one_limit_and_their_two_codes() -> None:
    """One limit, two entry points, two declared codes: the contract must say so.

    The synchronous route reports an over-bound body as 400 `invalid_request`
    with `param: input`; the asynchronous Job route reports the same condition
    as 422 `unsupported_parameter`. Neither code is free to change alone, so
    each entry has to name the shared limit and both codes, and each has to name
    the other entry. Deleting that correspondence would leave a consumer with
    two unrelated answers for one meaning.
    """

    schemas = _schemas()
    synchronous = cast(str, _synchronous_input(schemas).get("description", ""))
    asynchronous = cast(str, _async_inline_text(schemas).get("description", ""))

    for description, where in (
        (synchronous, "SpeechRequest.input"),
        (asynchronous, "TtsJobInput.text"),
    ):
        assert description, f"{where} must describe the one-synthesis text bound"
        assert "MAX_SPEECH_TEXT_CHARS" in description, (
            f"{where} must name the shared one-synthesis limit instead of declaring a bare number"
        )
        assert str(MAX_SPEECH_TEXT_CHARS) in description
        # Both codes are stated, because the same condition is reported twice.
        assert "400" in description and "invalid_request" in description
        assert "422" in description and "unsupported_parameter" in description

    # Each entry points at the other, so the mapping is reachable from either side.
    assert "/v1/nexus/jobs" in synchronous
    assert "TtsJobInput" in synchronous
    assert "/v1/audio/speech" in asynchronous


class _UnavailableSpeech:
    """Records that a synchronous request reached the backend, then refuses it."""

    def __init__(self) -> None:
        self.commands: list[Any] = []

    def synthesize(self, command: Any, *, request_id: str) -> Any:
        self.commands.append(command)
        raise SpeechUnavailableError

    def cancel(self, execution_id: str, reason: str) -> None:
        raise AssertionError("an unstarted synthesis cannot be cancelled")


async def test_the_same_over_bound_body_keeps_each_entry_point_code() -> None:
    """Measured behaviour behind the documented mapping, on both entries.

    The two replies stay deliberately different: the synchronous route reports
    the body it cannot accept as 400 `invalid_request`, the asynchronous Job
    route reports the shape its contract validator refuses as 422
    `unsupported_parameter`. What must not drift is the bound (both refuse at
    4097) or the declared envelope of each reply.
    """

    source = validator("speech-openapi.yaml")
    jobs = _RecordingJobs()
    speech = _UnavailableSpeech()
    app = create_app(settings=_settings(), jobs=cast(Any, jobs), speech=cast(Any, speech))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        synchronous = await client.post(
            "/v1/audio/speech",
            json={"model": "tts-quality", "input": "a" * (MAX_SPEECH_TEXT_CHARS + 1), "voice": "v"},
        )
        asynchronous = await client.post(
            "/v1/nexus/jobs",
            json=_job_body("a" * (MAX_SPEECH_TEXT_CHARS + 1)),
            headers={"Idempotency-Key": "synthetic-bound-706"},
        )

    # Synchronous: 400 invalid_request, naming the offending parameter.
    assert synchronous.status_code == 400, synchronous.text
    declared = _declared_response("createSpeech", 400)["content"]["application/json"]["schema"]
    source.evolve(schema={**DOCUMENT, "allOf": [declared]}).validate(synchronous.json())
    sync_error = synchronous.json()["error"]
    assert sync_error["code"] == "invalid_request"
    assert sync_error["param"] == "input"
    assert speech.commands == []

    # Asynchronous: 422 unsupported_parameter, still no admission attempt.
    assert asynchronous.status_code == 422, asynchronous.text
    declared_async = _declared_response("createJob", 422)["content"]["application/json"]["schema"]
    source.evolve(schema={**DOCUMENT, "allOf": [declared_async]}).validate(asynchronous.json())
    assert asynchronous.json()["error"]["code"] == "unsupported_parameter"
    assert jobs.submitted == []


def _capability_bound_table() -> list[str]:
    """The cross-entry table a consumer is told to look up in the errors section."""

    document = (ROOT / "docs" / "architecture" / "speech-api.md").read_text()
    start = document.index("载荷超过能力上界")
    end = document.index("前两行是同一个条件", start)
    return [line for line in document[start:end].splitlines() if line.startswith("|")]


def _rows_naming(rows: list[str], *tokens: str) -> list[str]:
    return [row for row in rows if all(token in row for token in tokens)]


def test_the_cross_entry_bound_table_states_each_entry_code() -> None:
    """The documented table must keep naming the code each measured entry returns.

    The synchronous and asynchronous one-synthesis refusals stay deliberately
    different, so the table is the single place a consumer can map them. If the
    table loses a row or a code, the mapping is no longer discoverable even
    though both contracts still declare it.
    """

    rows = _capability_bound_table()

    assert _rows_naming(rows, "POST /v1/audio/speech", "400", "`invalid_request`")
    assert _rows_naming(rows, "POST /v1/nexus/jobs", "422", "`unsupported_parameter`")
    assert _rows_naming(rows, "POST /v1/nexus/jobs", "413", "`job_request_too_large`")
    assert _rows_naming(rows, "POST /v1/nexus/assets", "413", "`asset_too_large`")
    assert _rows_naming(rows, "POST /v1/nexus/assets", "422", "`asset_text_too_long`")
    assert _rows_naming(rows, "render-units", "422", "`render_submission_rejected`")
