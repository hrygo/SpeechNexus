"""Model discovery returns the published catalogue or a stable error, never a guess."""

from __future__ import annotations

from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from speechnexus_backend.app import create_app
from speechnexus_backend.speech.application.model_catalog import model_document
from speechnexus_backend.speech.domain.publications import (
    Publication,
    PublicationUnavailableError,
)
from tests.unit.backend.test_health import _settings

PUBLICATION = Publication("binding", "tts-quality", 2, "binding-rev-2", 1_700_000_000_000_000)
DEFINITION: dict[str, Any] = {
    "binding_id": "tts-quality",
    "logical_model": "qwen3-tts-customvoice",
    "tasks": ["tts_file"],
    "release_tier": "production",
}
MODEL = model_document(PUBLICATION, DEFINITION, owned_by="speechnexus")


class Catalogue:
    def __init__(self, models: list[dict[str, Any]] | None = None, error: Exception | None = None):
        self._models = models if models is not None else [MODEL]
        self._error = error

    def list_models(self) -> list[dict[str, Any]]:
        if self._error is not None:
            raise self._error
        return self._models


def client(catalogue: Catalogue | None) -> AsyncClient:
    app = create_app(settings=_settings(), catalogue=catalogue)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_list_returns_published_models() -> None:
    async with client(Catalogue()) as http:
        response = await http.get("/v1/models")

    assert response.status_code == 200
    assert response.json() == {"object": "list", "data": [MODEL]}
    assert response.headers["cache-control"] == "no-store"


async def test_retrieve_returns_the_published_model() -> None:
    async with client(Catalogue()) as http:
        response = await http.get("/v1/models/tts-quality")

    assert response.status_code == 200
    assert response.json() == MODEL


async def test_unknown_model_is_not_found() -> None:
    async with client(Catalogue()) as http:
        response = await http.get("/v1/models/does-not-exist")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "model_not_found"


async def test_unconfigured_catalogue_is_unavailable() -> None:
    async with client(None) as http:
        listed = await http.get("/v1/models")
        retrieved = await http.get("/v1/models/tts-quality")

    assert listed.status_code == retrieved.status_code == 503
    assert listed.json()["error"]["code"] == "model_catalogue_unavailable"


async def test_catalogue_failure_is_unavailable_not_empty() -> None:
    async with client(Catalogue(error=PublicationUnavailableError())) as http:
        response = await http.get("/v1/models")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "model_catalogue_unavailable"


@pytest.mark.parametrize("path", ["/v1/models", "/v1/models/tts-quality"])
async def test_errors_carry_the_request_id(path: str) -> None:
    async with client(None) as http:
        response = await http.get(path)

    assert response.json()["error"]["request_id"]
