"""Authoritative packaged wire schemas and private-to-public Asset mapping."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from importlib.resources import files
from typing import Any, cast

from jsonschema import Draft202012Validator, FormatChecker
from speechnexus_contracts import ContractValidationError, parse
from speechnexus_contracts.models import Asset

from speechnexus_backend.speech.domain.assets import AssetRecord


@lru_cache
def asset_http_contract() -> dict[str, Any]:
    return cast(
        dict[str, Any],
        json.loads(files("speechnexus_contracts").joinpath("assets.openapi.json").read_text()),
    )


@lru_cache
def _parameter(name: str) -> Draft202012Validator:
    if name not in ("AssetIdPath", "IdempotencyKey"):
        raise ValueError("Unknown Asset parameter")
    return Draft202012Validator(
        {
            "$ref": f"#/components/parameters/{name}/schema",
            "components": asset_http_contract()["components"],
        },
        format_checker=FormatChecker(),
    )


def validate_asset_parameter(name: str, value: str) -> None:
    if not _parameter(name).is_valid(value):
        raise ContractValidationError("Invalid Asset parameter")


def public_asset(asset: AssetRecord) -> Asset:
    def timestamp(microseconds: int) -> str:
        value = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(microseconds=microseconds)
        return value.isoformat(timespec="microseconds").replace("+00:00", "Z")

    wire: dict[str, Any] = {
        "id": asset.asset_id,
        "object": "nexus.asset",
        "workspace_id": asset.workspace_id,
        "purpose": asset.purpose,
        "status": asset.status,
        "media_type": asset.media.media_type,
        "bytes": asset.bytes,
        "sha256": asset.sha256,
        "created_at": timestamp(asset.created_at_us),
        "expires_at": timestamp(asset.expires_at_us),
    }
    for name in ("duration_ms", "sample_rate_hz", "channels"):
        value = getattr(asset.media, name)
        if value is not None:
            wire[name] = value
    return parse("Asset", wire)
