"""Strict public projection of a committed saved asset; object keys stay internal."""

import speechnexus_contracts.workbench_models as wire
from speechnexus_contracts.workbench import parse

from speechnexus_backend.workbench.domain.saved_assets import SavedAsset
from speechnexus_backend.workbench.interfaces.version_views import _time


def saved_asset_payload(value: SavedAsset) -> wire.SavedAsset:
    if type(value) is not SavedAsset:
        raise ValueError("invalid workbench saved asset snapshot")
    return parse(
        "SavedAsset",
        {
            "object": "nexus.saved_asset",
            "id": value.id,
            "project_id": value.project_id,
            "display_name": value.copy.display_name,
            "media_type": value.copy.media_type,
            "bytes": value.copy.bytes,
            "sha256": value.copy.sha256,
            "source_artifact_id": value.copy.source_artifact_id,
            "source_receipt_id": value.copy.source_receipt_id,
            "created_at": _time(value.created_at),
        },
    )
