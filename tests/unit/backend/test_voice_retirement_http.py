"""The voice retirement route keeps one readable command and one stable error surface."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import httpx
import pytest
from speechnexus_contracts import ContractValidationError
from speechnexus_contracts.voice_retirement_http import (
    validate_request,
    validate_response,
)

from speechnexus_backend.app import create_app
from speechnexus_backend.speech.domain.voice_lifecycle import (
    VoiceRetirementCommand,
    VoiceRetirementConflictError,
    VoiceRetirementReceipt,
    VoiceRetirementUnavailableError,
    VoiceRevisionNotFoundError,
)
from tests.unit.backend.test_health import _settings

ROOT = Path(__file__).resolve().parents[3]
PUBLISHED = next(
    example["value"]["body"]
    for example in json.loads(
        (ROOT / "tests/fixtures/contracts/voice-catalogue-http.json").read_text()
    )["examples"]
    if example["id"] == "published"
)
PATH = "/v1/nexus/voices/voice-demo-1/revisions/2/retirements"


class Catalogue:
    def __init__(self, body: dict[str, Any] | None = None) -> None:
        self.body = body or deepcopy(PUBLISHED)
        self.reads = 0

    def list_voices(self, **kwargs: object) -> dict[str, Any]:
        raise AssertionError("Retirement must not list the catalogue")

    def get_revision(self, voice_id: str, revision: str) -> dict[str, Any] | None:
        self.reads += 1
        return self.body


class Retirements:
    def __init__(
        self,
        *,
        error: Exception | None = None,
        replayed: bool = False,
    ) -> None:
        self.error, self.replayed = error, replayed
        self.commands: list[VoiceRetirementCommand] = []

    def retire(self, command: VoiceRetirementCommand) -> VoiceRetirementReceipt:
        self.commands.append(command)
        if self.error is not None:
            raise self.error
        return VoiceRetirementReceipt(
            voice_id=command.voice_id,
            revision=command.revision,
            revision_id=command.voice_id + "-" + command.revision,
            mode=command.mode,
            reason=command.reason,
            retired_at_us=1_793_001_600_000_000,
            replayed=self.replayed,
        )


def _retired(status: str = "retired", reason: str | None = None) -> dict[str, Any]:
    body: dict[str, Any] = deepcopy(PUBLISHED)
    body["status"] = status
    body["retired_at"] = "2026-11-06T00:00:00Z"
    body["disabled_reason"] = reason
    return body


async def _call(
    body: object,
    *,
    catalogue: Catalogue | None = None,
    retirements: Retirements | None = None,
    **kwargs: Any,
) -> httpx.Response:
    app = create_app(
        settings=_settings(),
        voices=catalogue if catalogue is not None else Catalogue(),
        voice_retirements=retirements if retirements is not None else Retirements(),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.post(
            PATH,
            json=body,
            headers={"X-Request-ID": "req-retire-490"},
            **kwargs,
        )


async def test_retire_returns_the_retired_revision_and_replays_the_same_body() -> None:
    catalogue = Catalogue(_retired())
    replays = Retirements(replayed=True)
    for response in (
        await _call({"mode": "retire"}, catalogue=catalogue, retirements=replays),
        await _call({"mode": "retire"}, catalogue=catalogue, retirements=replays),
    ):
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "no-store"
        assert response.json()["status"] == "retired"
        assert response.json()["disabled_reason"] is None
        validate_response("retireVoiceRevision", 200, {"body": response.json()})
    assert [command.voice_id for command in replays.commands] == ["voice-demo-1", "voice-demo-1"]
    assert catalogue.reads == 2


async def test_disable_is_rejected_before_any_write() -> None:
    catalogue = Catalogue(PUBLISHED)
    retirements = Retirements()
    response = await _call(
        {"mode": "disable", "reason": "emergency"},
        catalogue=catalogue,
        retirements=retirements,
    )
    assert response.status_code == 422, response.text
    assert response.headers["x-should-retry"] == "false"
    assert response.json()["error"]["code"] == "voice_disable_unsupported"
    assert retirements.commands == []
    assert catalogue.reads == 0
    validate_response("retireVoiceRevision", 422, {"body": response.json()})


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (VoiceRevisionNotFoundError(), 404, "voice_not_found"),
        (
            VoiceRetirementConflictError("voice_revision_not_current"),
            409,
            "voice_retirement_conflict",
        ),
        (VoiceRetirementUnavailableError(), 503, "voice_retirement_unavailable"),
    ],
)
async def test_store_failures_keep_distinct_statuses(
    error: Exception, status: int, code: str
) -> None:
    response = await _call({"mode": "retire"}, retirements=Retirements(error=error))
    assert response.status_code == status, response.text
    assert response.json()["error"]["code"] == code
    assert response.json()["error"]["retryable"] is (status == 503)
    assert response.headers["cache-control"] == "no-store"
    envelope: dict[str, Any] = {"body": response.json()}
    if status == 503:
        envelope["headers"] = {
            "Cache-Control": response.headers["cache-control"],
            "X-Should-Retry": response.headers["x-should-retry"],
        }
    validate_response("retireVoiceRevision", status, envelope)


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"mode": "retirex"},
        {"mode": "disable"},
        {"mode": "retire", "reason": ""},
        {"mode": "retire", "unknown": True},
    ],
)
async def test_invalid_bodies_never_reach_the_store(body: object) -> None:
    retirements = Retirements()
    response = await _call(body, retirements=retirements)
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "voice_invalid_request"
    assert retirements.commands == []


async def test_duplicate_json_keys_and_query_are_rejected() -> None:
    app = create_app(settings=_settings(), voices=Catalogue(), voice_retirements=Retirements())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        duplicate = await client.post(
            PATH,
            content=b'{"mode":"retire","mode":"retire"}',
            headers={"X-Request-ID": "req-retire-490"},
        )
        query = await client.post(
            PATH + "?force=1",
            json={"mode": "retire"},
            headers={"X-Request-ID": "req-retire-490"},
        )
    for response in (duplicate, query):
        assert response.status_code == 400, response.text
        assert response.json()["error"]["code"] == "voice_invalid_request"


async def test_unconfigured_retirement_is_retryable() -> None:
    app = create_app(settings=_settings())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            PATH, json={"mode": "retire"}, headers={"X-Request-ID": "req-retire-490"}
        )
    assert response.status_code == 503
    assert response.headers["x-should-retry"] == "true"
    assert response.json()["error"]["code"] == "voice_catalogue_unavailable"


def test_openapi_advertises_the_retirement_command() -> None:
    paths = create_app(settings=_settings()).openapi()["paths"]
    retirement = paths["/v1/nexus/voices/{voice_id}/revisions/{revision}/retirements"]
    assert set(retirement) == {"post"}
    assert retirement["post"]["operationId"] == "retireVoiceRevision"
    assert set(retirement["post"]["responses"]) >= {"200", "400", "404", "409", "422", "503"}
    assert set(paths["/v1/nexus/voices"]["get"]) >= {"operationId"}
    assert set(paths["/v1/nexus/voices/{voice_id}/revisions/{revision}"]) == {"get"}


def test_request_validation_accepts_both_declared_modes() -> None:
    for body in ({"mode": "retire"}, {"mode": "retire", "reason": "sunset"}):
        envelope = {
            "path": {"voice_id": "voice-demo-1", "revision": "2"},
            "headers": {"idempotency-key": "key-00000001"},
            "body": body,
        }
        assert validate_request("retireVoiceRevision", envelope) is envelope
    with pytest.raises(ContractValidationError):
        validate_request(
            "retireVoiceRevision",
            {
                "path": {"voice_id": "voice-demo-1", "revision": "2"},
                "body": {"mode": "disable"},
            },
        )
