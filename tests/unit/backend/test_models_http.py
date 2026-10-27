"""Model discovery returns the published catalogue or a stable error, never a guess."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, cast

import pytest
from httpx import ASGITransport, AsyncClient

from speechnexus_backend.app import create_app
from speechnexus_backend.settings import Settings
from speechnexus_backend.speech.application.model_catalog import (
    ModelCatalogueSnapshot,
    capability_model,
    model_document,
)
from speechnexus_backend.speech.domain.publications import (
    Publication,
    PublicationUnavailableError,
)
from speechnexus_backend.speech.infrastructure.model_catalog_postgres import PostgresModelCatalog
from tests.unit.backend.test_health import _settings

PUBLICATION = Publication("binding", "tts-quality", 2, "binding-rev-2", 1_700_000_000_000_000)
DEFINITION: dict[str, Any] = {
    "binding_id": "tts-quality",
    "logical_model": "qwen3-tts-customvoice",
    "tasks": ["tts_file"],
    "release_tier": "production",
}
MODEL = model_document(PUBLICATION, DEFINITION, owned_by="speechnexus")
FULL_DEFINITION: dict[str, Any] = {
    "schema_version": "1.0.0",
    "binding_id": "tts-quality",
    "revision": "2",
    "revision_id": "binding-rev-2",
    "logical_model": "qwen3-tts-customvoice",
    "tasks": ["tts_file"],
    "engine": {
        "kind": "vllm_omni",
        "image_digest": "sha256:" + "1" * 64,
        "runtime_version": "vllm-omni-2026.10",
    },
    "artifact_set": [
        {
            "role": "weights",
            "artifact_id": "model-qwen3-tts",
            "digest": "sha256:" + "2" * 64,
            "media_type": "application/x-safetensors",
        }
    ],
    "execution_recipe": {
        "recipe_revision": "1",
        "capacity_manifest_id": "cap-tts-quality-2",
        "stage_roles": ["tts_talker"],
    },
    "parameter_domains": {
        "tts": {
            "languages": ["zh"],
            "audio_formats": ["mp3", "wav", "pcm"],
            "sample_rates_hz": [24000],
            "text_commit_modes": ["explicit"],
            "audio_delivery_modes": ["complete"],
            "supports_instructions": True,
            "max_input_codepoints": 2000,
        }
    },
    "release_tier": "production",
    "publication": {
        "status": "draft",
        "published_at": None,
        "retired_at": None,
        "capacity_manifest_id": None,
        "qualification_report_id": None,
        "disabled_reason": None,
    },
}
CAPABILITY_MODEL = capability_model(PUBLICATION, FULL_DEFINITION, owned_by="speechnexus")


class Catalogue:
    def __init__(
        self,
        models: list[dict[str, Any]] | None = None,
        error: Exception | None = None,
        *,
        revision: int = 3,
        generated_at_us: int = PUBLICATION.published_at_us,
    ):
        self._models = models if models is not None else [MODEL]
        self._error = error
        self.revision = revision
        self.generated_at_us = generated_at_us

    def snapshot(self) -> ModelCatalogueSnapshot:
        if self._error is not None:
            raise self._error
        return ModelCatalogueSnapshot(
            models=tuple(self._models),
            capability_models=(CAPABILITY_MODEL,),
            catalog_revision=self.revision,
            generated_at_us=self.generated_at_us,
        )


class RowsDriver:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    def _run(
        self,
        operation: Callable[..., object],
        *,
        read_only: bool = False,
        repeatable_read: bool = False,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        assert read_only and repeatable_read
        return self.rows, {"catalog_revision": 2, "generated_at_us": PUBLICATION.published_at_us}


def _postgres_catalogue(definition: dict[str, Any]) -> PostgresModelCatalog:
    catalogue = PostgresModelCatalog(cast(Any, object()))
    row = {
        "kind": "binding",
        "object_id": "tts-quality",
        "revision": "2",
        "revision_id": "binding-rev-2",
        "published_at_us": PUBLICATION.published_at_us,
        "definition_json": json.dumps(definition),
    }
    cast(Any, catalogue)._driver = RowsDriver([row])
    return catalogue


def client(catalogue: Catalogue | None, settings: Settings | None = None) -> AsyncClient:
    app = create_app(settings=settings or _settings(), catalogue=catalogue)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_list_returns_published_models() -> None:
    async with client(Catalogue()) as http:
        response = await http.get("/v1/models")

    assert response.status_code == 200
    assert response.json() == {"object": "list", "data": [MODEL]}
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["etag"].startswith('"sha256:')


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


@pytest.mark.parametrize(
    "broken",
    [
        {**DEFINITION, "binding_id": "other-binding"},
        {**DEFINITION, "tasks": ["not_a_task"]},
        {**DEFINITION, "release_tier": "beta"},
    ],
)
async def test_poison_publication_is_503_never_500(broken: dict[str, Any]) -> None:
    catalogue = _postgres_catalogue(broken)
    async with client(cast(Any, catalogue)) as http:
        listed = await http.get("/v1/models")
        retrieved = await http.get("/v1/models/tts-quality")

    assert listed.status_code == retrieved.status_code == 503
    assert listed.status_code != 500
    assert listed.json()["error"]["code"] == "model_catalogue_unavailable"


async def test_models_conditional_get_hits_and_misses() -> None:
    catalogue = Catalogue()
    async with client(catalogue) as http:
        first = await http.get("/v1/models")
        matched = await http.get("/v1/models", headers={"If-None-Match": first.headers["etag"]})
        weak = await http.get("/v1/models", headers={"If-None-Match": "W/" + first.headers["etag"]})
        missed = await http.get(
            "/v1/models", headers={"If-None-Match": '"sha256:' + "0" * 64 + '"'}
        )

    assert matched.status_code == weak.status_code == 304
    assert matched.content == weak.content == b""
    assert matched.headers["etag"] == first.headers["etag"]
    assert missed.status_code == 200


async def test_models_etag_changes_with_catalog_and_policy_revisions() -> None:
    catalogue = Catalogue(revision=3)
    async with client(catalogue) as http:
        before = await http.get("/v1/models")
        catalogue.revision = 4
        after_catalog = await http.get("/v1/models")

    changed_policy = _settings().model_copy(update={"capability_policy_revision": 2})
    async with client(Catalogue(revision=4), changed_policy) as http:
        after_policy = await http.get("/v1/models")

    assert (
        len({before.headers["etag"], after_catalog.headers["etag"], after_policy.headers["etag"]})
        == 3
    )


async def test_capabilities_returns_the_contract_snapshot() -> None:
    async with client(Catalogue()) as http:
        response = await http.get(
            "/v1/nexus/capabilities", params={"workspace_id": "workspace-other"}
        )

    assert response.status_code == 200
    body = response.json()
    assert body["workspace_id"] == "workspace-other"
    assert body["catalog_revision"] == "3"
    assert body["policy_revision"] == "1"
    assert body["models"] == [CAPABILITY_MODEL]
    assert body["profiles"] == body["voices"] == []
    assert body["limits"] == {
        "max_audio_bytes": 4 * 1024 * 1024,
        "max_text_codepoints": 1_000_000,
        "max_segment_ms": 30_000,
        "max_realtime_session_ms": 600_000,
        "max_queue_wait_ms": 600_000,
    }
    assert response.headers["etag"].startswith('"sha256:')


async def test_capabilities_conditional_get_uses_the_same_validator() -> None:
    async with client(Catalogue()) as http:
        first = await http.get("/v1/nexus/capabilities")
        matched = await http.get(
            "/v1/nexus/capabilities", headers={"If-None-Match": first.headers["etag"]}
        )
        other_workspace = await http.get(
            "/v1/nexus/capabilities",
            params={"workspace_id": "workspace-other"},
            headers={"If-None-Match": first.headers["etag"]},
        )

    assert matched.status_code == 304
    assert matched.content == b""
    assert other_workspace.status_code == 200
    assert other_workspace.headers["etag"] != first.headers["etag"]


async def test_capabilities_etag_changes_with_catalog_revision() -> None:
    catalogue = Catalogue(revision=3)
    async with client(catalogue) as http:
        before = await http.get("/v1/nexus/capabilities")
        catalogue.revision = 4
        after = await http.get("/v1/nexus/capabilities")

    assert before.headers["etag"] != after.headers["etag"]
    assert before.json()["catalog_revision"] == "3"
    assert after.json()["catalog_revision"] == "4"


async def test_capabilities_etag_changes_with_policy_revision() -> None:
    async with client(Catalogue()) as http:
        before = await http.get("/v1/nexus/capabilities")

    changed_policy = _settings().model_copy(update={"capability_policy_revision": 2})
    async with client(Catalogue(), changed_policy) as http:
        after = await http.get("/v1/nexus/capabilities")

    assert before.headers["etag"] != after.headers["etag"]
    assert before.json()["policy_revision"] == "1"
    assert after.json()["policy_revision"] == "2"


async def test_capabilities_unconfigured_or_invalid_workspace_fail_closed() -> None:
    async with client(None) as http:
        unavailable = await http.get("/v1/nexus/capabilities")
    async with client(Catalogue()) as http:
        invalid = await http.get("/v1/nexus/capabilities", params={"workspace_id": "Invalid"})

    assert unavailable.status_code == 503
    assert unavailable.json()["error"]["code"] == "model_catalogue_unavailable"
    assert invalid.status_code == 404
    assert invalid.json()["error"]["code"] == "capability_workspace_not_found"


@pytest.mark.parametrize("path", ["/v1/models", "/v1/models/tts-quality"])
async def test_errors_carry_the_request_id(path: str) -> None:
    async with client(None) as http:
        response = await http.get(path)

    assert response.json()["error"]["request_id"]
