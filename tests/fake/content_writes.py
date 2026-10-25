"""A small content-write owner for application-only tests; PG owns concurrency proof."""

from typing import Any
from uuid import uuid4

from speechnexus_backend.workbench.application.content_writes import ContentWrite


class FakeContentWrites:
    def __init__(self, project_id: str = "project-synthetic") -> None:
        self.project_id = project_id
        self.states: dict[str, str] = {}

    async def reserve_asset(self, **kwargs: Any) -> ContentWrite:
        write = ContentWrite(uuid4().hex, kwargs["project_id"], kwargs["asset_id"], uuid4().hex)
        self.states[write.id] = "reserved"
        return write

    async def reserve_export(self, *, export_id: str, artifact_id: str) -> ContentWrite:
        write = ContentWrite(uuid4().hex, self.project_id, artifact_id, uuid4().hex)
        self.states[write.id] = "reserved"
        return write

    async def writing(
        self, write: ContentWrite, *, object_key: str, sha256: str, size: int
    ) -> None:
        self.states[write.id] = "writing"

    async def settle(self, write: ContentWrite) -> None:
        self.states[write.id] = "settled"

    async def unknown(self, write: ContentWrite) -> None:
        self.states[write.id] = "unknown"
