"""Contract-validated release-evidence identity reads against a fake store.

The fake records only what the publication evidence table records, so these
cases prove the response shape and the guarantee that a degraded field stays
absent rather than being synthesized.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import AnyHttpUrl, SecretStr
from speechnexus_contracts.admin_http import validate_response

from speechnexus_backend.app import create_app
from speechnexus_backend.settings import Settings
from speechnexus_backend.speech.application.binding_evidence import EvidenceKind
from speechnexus_backend.speech.application.readiness import ReadinessService
from speechnexus_backend.speech.domain.binding_drafts import BindingUnavailableError
from speechnexus_backend.speech.domain.binding_evidence import RecordedEvidenceIdentity
from speechnexus_backend.speech.interfaces.http.binding_evidence import (
    CAPACITY,
    QUALIFICATION,
    evidence_body,
    stamp,
)
from tests.fake.readiness import StaticProbe

CAPACITY_URL = "/v1/nexus/admin/capacity-manifests/capm-demo-1"
QUALIFICATION_URL = "/v1/nexus/admin/qualification-reports/qrep-demo-1"
DIGEST = "sha256:" + "a" * 64
RECORDED_AT_US = 1_700_000_000_123_456


def _settings() -> Settings:
    return Settings(
        service_name="speechnexus-api-test",
        database_url=SecretStr("postgresql://unused@127.0.0.1/unused"),
        object_storage_endpoint=AnyHttpUrl("http://127.0.0.1:9000"),
        object_storage_healthcheck_url=AnyHttpUrl("http://127.0.0.1:9000/health"),
        object_storage_bucket="test",
    )


@dataclass
class FakeEvidenceReader:
    identities: dict[tuple[EvidenceKind, str], RecordedEvidenceIdentity]
    failure: Exception | None = None
    seen: list[tuple[EvidenceKind, str]] | None = None

    def recorded_evidence(
        self, kind: EvidenceKind, identity: str
    ) -> RecordedEvidenceIdentity | None:
        if self.seen is not None:
            self.seen.append((kind, identity))
        if self.failure is not None:
            raise self.failure
        return self.identities.get((kind, identity))


@pytest.fixture
def reader() -> FakeEvidenceReader:
    return FakeEvidenceReader(
        {
            (EvidenceKind.CAPACITY_MANIFEST, "capm-demo-1"): RecordedEvidenceIdentity(
                "capm-demo-1", RECORDED_AT_US, DIGEST
            ),
            (EvidenceKind.QUALIFICATION_REPORT, "qrep-demo-1"): RecordedEvidenceIdentity(
                "qrep-demo-1", RECORDED_AT_US
            ),
        },
        seen=[],
    )


@pytest.fixture
def client(reader: FakeEvidenceReader) -> Iterator[AsyncClient]:
    app = create_app(
        settings=_settings(),
        binding_evidence=reader,
        readiness=ReadinessService([StaticProbe("fixture")], timeout_seconds=1),
    )
    client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    yield client


async def get(client: AsyncClient, url: str) -> httpx.Response:
    return await client.get(url, headers={"X-Request-ID": "request-evidence-1"})


async def test_capacity_manifest_returns_only_recorded_facts(
    client: AsyncClient, reader: FakeEvidenceReader
) -> None:
    response = await get(client, CAPACITY_URL)

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body == {
        "id": "capm-demo-1",
        "digest": DIGEST,
        "recorded_at": "2023-11-14T22:13:20.123456Z",
    }
    validate_response(CAPACITY, 200, {"body": body})
    assert reader.seen == [(EvidenceKind.CAPACITY_MANIFEST, "capm-demo-1")]


async def test_qualification_report_omits_a_digest_no_publication_recorded(
    client: AsyncClient,
) -> None:
    response = await get(client, QUALIFICATION_URL)

    assert response.status_code == 200
    body = response.json()
    assert body == {"id": "qrep-demo-1", "recorded_at": "2023-11-14T22:13:20.123456Z"}
    assert "digest" not in body
    validate_response(QUALIFICATION, 200, {"body": body})


async def test_unknown_identifier_is_proven_absence_not_a_placeholder(
    client: AsyncClient,
) -> None:
    response = await get(client, "/v1/nexus/admin/capacity-manifests/capm-absent-1")

    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "binding_evidence_not_found"
    assert body["error"]["retryable"] is False
    validate_response(CAPACITY, 404, {"body": body})


async def test_undeclared_query_parameter_is_rejected(client: AsyncClient) -> None:
    response = await get(client, CAPACITY_URL + "?workspace_id=workspace-1")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_binding_request"


async def test_malformed_identifier_returns_declared_400(client: AsyncClient) -> None:
    response = await get(client, "/v1/nexus/admin/qualification-reports/QREP_BAD")

    assert response.status_code == 400
    body = response.json()
    assert body["error"]["code"] == "invalid_binding_request"
    validate_response(QUALIFICATION, 400, {"body": body})


async def test_unreadable_store_is_503_and_never_absence() -> None:
    app = create_app(
        settings=_settings(),
        binding_evidence=UnavailableReader(),
        readiness=ReadinessService([StaticProbe("fixture")], timeout_seconds=1),
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as failure:
        response = await get(failure, CAPACITY_URL)

    assert response.status_code == 503
    body = response.json()
    assert body["error"]["code"] == "binding_unavailable"
    assert body["error"]["retryable"] is True
    assert response.headers["x-should-retry"] == "true"
    validate_response(CAPACITY, 503, {"body": body})


async def test_missing_reader_configuration_fails_closed() -> None:
    app = create_app(
        settings=_settings(),
        readiness=ReadinessService([StaticProbe("fixture")], timeout_seconds=1),
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await get(client, CAPACITY_URL)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "binding_unavailable"


def assert_no_degraded_fields(body: dict[str, Any], *, digest_expected: bool) -> None:
    """The narrowed fields must stay absent rather than carry a made-up value."""
    assert "source" not in body
    if digest_expected:
        assert body["digest"].startswith("sha256:")
    else:
        assert "digest" not in body


def test_the_absence_assertion_rejects_a_fabricated_locator() -> None:
    fabricated = {
        "id": "capm-demo-1",
        "recorded_at": "2023-11-14T22:13:20.123456Z",
        "source": "release-evidence://engine/bundle/capacity-manifest.json",
    }

    with pytest.raises(AssertionError, match="source"):
        assert_no_degraded_fields(fabricated, digest_expected=False)


def test_the_absence_assertion_rejects_a_fabricated_digest() -> None:
    fabricated = {
        "id": "qrep-demo-1",
        "recorded_at": "2023-11-14T22:13:20.123456Z",
        "digest": DIGEST,
    }

    with pytest.raises(AssertionError, match="digest"):
        assert_no_degraded_fields(fabricated, digest_expected=False)


def test_projected_body_omits_source_even_when_the_record_carries_a_digest() -> None:
    body = evidence_body(RecordedEvidenceIdentity("capm-demo-1", RECORDED_AT_US, DIGEST))
    assert_no_degraded_fields(body, digest_expected=True)
    assert body["recorded_at"] == stamp(RECORDED_AT_US)


class UnavailableReader:
    def recorded_evidence(
        self, kind: EvidenceKind, identity: str
    ) -> RecordedEvidenceIdentity | None:
        raise BindingUnavailableError
