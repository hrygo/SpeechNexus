"""The catalogue reader identity is explicit, API-only and verified at startup."""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from speechnexus_backend.model_catalog_runtime import ModelCatalogRuntime
from tests.unit.backend.test_health import _settings


def test_requires_an_explicit_reader_identity() -> None:
    with pytest.raises(ValueError):
        ModelCatalogRuntime(_settings())


def test_requires_the_api_process_role() -> None:
    settings = _settings().model_copy(
        update={
            "model_catalog_reader_database_url": SecretStr("postgresql://unused@127.0.0.1/unused"),
            "process_role": "worker",
        }
    )
    with pytest.raises(ValueError):
        ModelCatalogRuntime(settings)


def test_unreachable_identity_is_rejected_and_closes() -> None:
    settings = _settings().model_copy(
        update={
            "model_catalog_reader_database_url": SecretStr(
                "postgresql://unused@127.0.0.1:1/unused?connect_timeout=1"
            )
        }
    )
    runtime = ModelCatalogRuntime(settings)
    with pytest.raises(ValueError):
        runtime.open()

    assert runtime.catalogue is None
    runtime.close()
