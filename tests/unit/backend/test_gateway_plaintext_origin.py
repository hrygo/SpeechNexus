"""One rule decides whether a gateway origin may carry the component credential."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from speechnexus_backend.settings import Settings
from speechnexus_backend.speech.domain.gateway_origin import (
    ALLOW_PLAINTEXT_ORIGIN_KEY,
    plaintext_origin_opt_in,
    require_safe_gateway_origin,
)
from speechnexus_backend.tts_settlement_worker import TtsSettlementWorkerSettings
from tests.unit.backend.test_job_dispatch_worker import _settings as dispatch_settings

TTS_TOKEN = "synthetic-test-token"
API_GATEWAY_ORIGIN = "http://gateway:8010"


def tts_settings(origin: str, *, opt_in: str | None = None) -> TtsSettlementWorkerSettings:
    values: dict[str, Any] = {
        "SPEECHNEXUS_DATABASE_URL": "postgresql://synthetic@127.0.0.1/test",
        "SPEECHNEXUS_TTS_SETTLEMENT_DATABASE_URL": "postgresql://worker@127.0.0.1/test",
        "SPEECHNEXUS_DATABASE_SCHEMA": "speech",
        "SPEECHNEXUS_TTS_GATEWAY_ID": "gateway-test",
        "SPEECHNEXUS_TTS_HOST_NODE_ID": "host-test",
        "SPEECHNEXUS_EXECUTION_GATEWAY_BASE_URL": origin,
        "SPEECHNEXUS_EXECUTION_GATEWAY_COMPONENT_CREDENTIAL": TTS_TOKEN,
    }
    if opt_in is not None:
        values[ALLOW_PLAINTEXT_ORIGIN_KEY] = opt_in
    return TtsSettlementWorkerSettings.model_validate(values)


def api_settings(origin: str, *, opt_in: str | None = None) -> Settings:
    values: dict[str, Any] = {
        "database_url": "postgresql://synthetic@127.0.0.1/test",
        "object_storage_endpoint": "http://127.0.0.1:9000",
        "object_storage_healthcheck_url": "http://127.0.0.1:9000/minio/health/live",
        "object_storage_bucket": "synthetic",
        "execution_gateway_base_url": origin,
        "execution_gateway_component_credential": TTS_TOKEN,
        "execution_gateway_allow_plaintext_origin": opt_in or "",
    }
    return Settings.model_validate(values)


@pytest.mark.parametrize(
    ("origin", "opt_in"),
    [
        ("https://gateway.example", None),
        ("https://gateway.example:8443", None),
        ("http://localhost:8010", None),
        ("http://127.0.0.1:8010", None),
        ("http://[::1]:8010", None),
        (API_GATEWAY_ORIGIN, API_GATEWAY_ORIGIN),
        ("http://gateway:8010/", API_GATEWAY_ORIGIN),
        ("http://example.com:8010", "http://example.com:8010"),
    ],
)
def test_accepted_origins(origin: str, opt_in: str | None) -> None:
    assert require_safe_gateway_origin(origin, allow_plaintext_origin=opt_in, label="Test gateway")


def test_plain_http_outside_loopback_names_the_opt_in_key() -> None:
    with pytest.raises(ValueError) as refusal:
        require_safe_gateway_origin(
            API_GATEWAY_ORIGIN, allow_plaintext_origin=None, label="Test gateway"
        )
    message = str(refusal.value)
    assert ALLOW_PLAINTEXT_ORIGIN_KEY in message
    assert f"{ALLOW_PLAINTEXT_ORIGIN_KEY}={API_GATEWAY_ORIGIN}" in message


@pytest.mark.parametrize(
    ("origin", "opt_in"),
    [
        (API_GATEWAY_ORIGIN, "http://gateway:8011"),
        (API_GATEWAY_ORIGIN, "http://gateway"),
        ("http://gateway:8010", "http://Gateway:8010"),
        ("http://gateway:80", "http://gateway"),
        ("http://example.com:8010", "http://example.org:8010"),
    ],
)
def test_opt_in_matches_one_exact_origin(origin: str, opt_in: str) -> None:
    with pytest.raises(ValueError, match=ALLOW_PLAINTEXT_ORIGIN_KEY):
        require_safe_gateway_origin(origin, allow_plaintext_origin=opt_in, label="Test gateway")


@pytest.mark.parametrize(
    "value",
    [
        "http://gateway:8010/",
        "http://gateway:8010/anything",
        "http://gateway:8010?x=1",
        "http://gateway:8010#frag",
        "http://operator:secret@gateway:8010",
        "https://gateway:8010",
        "gateway:8010",
        "http://gateway:8010 ",
        "http://gateway:not-a-port",
        "http://",
    ],
)
def test_opt_in_itself_must_be_one_bare_http_origin(value: str) -> None:
    with pytest.raises(ValueError, match=ALLOW_PLAINTEXT_ORIGIN_KEY):
        plaintext_origin_opt_in(value)


def test_absent_opt_in_is_the_default() -> None:
    assert plaintext_origin_opt_in("") is None
    assert plaintext_origin_opt_in(None) is None


@pytest.mark.parametrize(
    "origin",
    [
        "http://gateway:8010/anything",
        "http://user:secret@gateway:8010",
        "http://gateway:8010?x=1",
        "http://gateway:8010#frag",
        "ftp://gateway:8010",
        "gateway:8010",
        "http://gateway:8010 ",
        "http://gateway:not-a-port",
    ],
)
def test_gateway_url_itself_must_be_a_bare_origin(origin: str) -> None:
    with pytest.raises(ValueError):
        require_safe_gateway_origin(origin, allow_plaintext_origin=origin, label="Test gateway")


def test_api_configured_for_the_reference_bridge_constructs_both_consumers() -> None:
    """The compose topology (api -> gateway service name) is a supported explicit choice."""
    settings = api_settings(API_GATEWAY_ORIGIN, opt_in=API_GATEWAY_ORIGIN)
    tts = tts_settings(
        str(settings.execution_gateway_base_url),
        opt_in=settings.execution_gateway_allow_plaintext_origin,
    )
    dispatch = dispatch_settings(
        gateway_base_url=str(settings.execution_gateway_base_url),
        allow_plaintext_origin=settings.execution_gateway_allow_plaintext_origin,
    )
    assert tts.allow_plaintext_origin == API_GATEWAY_ORIGIN
    assert str(dispatch.gateway_base_url) == API_GATEWAY_ORIGIN + "/"


def test_api_without_the_opt_in_cannot_construct_either_consumer() -> None:
    settings = api_settings(API_GATEWAY_ORIGIN)
    with pytest.raises(ValidationError, match=ALLOW_PLAINTEXT_ORIGIN_KEY):
        tts_settings(str(settings.execution_gateway_base_url))
    with pytest.raises(ValidationError, match=ALLOW_PLAINTEXT_ORIGIN_KEY):
        dispatch_settings(gateway_base_url=str(settings.execution_gateway_base_url))


def test_api_rejects_a_malformed_opt_in_even_without_a_consumer() -> None:
    with pytest.raises(ValidationError, match=ALLOW_PLAINTEXT_ORIGIN_KEY):
        api_settings(API_GATEWAY_ORIGIN, opt_in=API_GATEWAY_ORIGIN + "/")
