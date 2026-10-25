"""Strict public projection of accepted teleprompter state; hypotheses stay internal."""

import speechnexus_contracts.workbench_models as wire
from speechnexus_contracts.workbench import parse

from speechnexus_backend.workbench.domain.teleprompter import TeleprompterState
from speechnexus_backend.workbench.interfaces.version_views import _revision, _time


def teleprompter_payload(value: TeleprompterState) -> wire.TeleprompterState:
    if type(value) is not TeleprompterState:
        raise ValueError("invalid workbench teleprompter snapshot")
    return parse(
        "TeleprompterState",
        {
            "object": "nexus.teleprompter_state",
            "project_id": value.project_id,
            "script_revision": _revision(value.script_revision),
            "visible_position": value.visible_position,
            "manual_hold": value.manual_hold,
            "updated_at": _time(value.updated_at),
        },
    )
