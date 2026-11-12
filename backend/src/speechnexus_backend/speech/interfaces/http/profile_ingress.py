"""Bound profile receive buffers, while global configuration coordination stays in PG."""

from __future__ import annotations

import re
from typing import Any

from speechnexus_contracts.profile_http import contract
from starlette.types import ASGIApp

from speechnexus_backend.speech.interfaces.http.draft_ingress import DraftIngress
from speechnexus_backend.speech.interfaces.http.profile_errors import CREATE, READ, ingress_error

PREFIX = "/v1/nexus/admin/profiles"
CREATE_PATH = next(path for path, item in contract()["paths"].items() if "post" in item)
READ_PATH = next(path for path, item in contract()["paths"].items() if "get" in item)
READ_PATTERN = re.compile(
    re.escape(READ_PATH).replace(r"\{profile_id\}", r"[^/]+").replace(r"\{revision\}", r"[^/]+")
)


LIFECYCLE = {
    READ_PATH + "/publications": "publishProfileRevision",
    READ_PATH + "/activations": "activatePublishedProfileRevision",
}
PUBLIC = {
    "/v1/nexus/profiles": "listProfiles",
    "/v2/nexus/profiles": "listPublishedProfiles",
    "/v1/nexus/profiles/{profile_id}/revisions/{revision}": "getProfileRevision",
}


def _matches(template: str, path: str) -> bool:
    return (
        re.fullmatch(
            re.escape(template)
            .replace(r"\{profile_id\}", r"[^/]+")
            .replace(r"\{revision\}", r"[^/]+"),
            path,
        )
        is not None
    )


def public_target(path: str, method: str) -> tuple[str, str] | None:
    return next(
        ((operation, "GET") for template, operation in PUBLIC.items() if _matches(template, path)),
        None,
    )


def target(path: str, method: str) -> tuple[str, str] | None:
    if path == CREATE_PATH:
        return CREATE, "POST"
    if READ_PATTERN.fullmatch(path):
        return READ, "GET"
    return next(
        (
            (operation, "POST")
            for template, operation in LIFECYCLE.items()
            if _matches(template, path)
        ),
        None,
    )


class ProfileIngress(DraftIngress):
    def __init__(self, app: ASGIApp, /, **kwargs: Any) -> None:
        public = DraftIngress(
            app,
            prefix="/v1/nexus/profiles",
            target=public_target,
            error=ingress_error,
            create_operation=CREATE,
            read_operation="getProfileRevision",
            read_operations=("listProfiles",),
            **kwargs,
        )
        public_v2 = DraftIngress(
            public,
            prefix="/v2/nexus/profiles",
            target=public_target,
            error=ingress_error,
            create_operation=CREATE,
            read_operation="listPublishedProfiles",
            **kwargs,
        )
        super().__init__(
            public_v2,
            prefix=PREFIX,
            target=target,
            error=ingress_error,
            create_operation=CREATE,
            read_operation=READ,
            write_operations=("publishProfileRevision", "activatePublishedProfileRevision"),
            **kwargs,
        )
