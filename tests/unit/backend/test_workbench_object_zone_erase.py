"""The object sweep must not report a prefix clean while a late write survives it."""

from types import SimpleNamespace
from typing import Any, cast

from minio import Minio

from speechnexus_backend.workbench.infrastructure.minio_object_zone import (
    MAX_ERASE_PASSES,
    MinioObjectZone,
)


class FakeMinio:
    """A listing that can be repopulated between passes, like a concurrent late writer."""

    def __init__(self, *, late_after_first_listing: str | None = None, keep: bool = False) -> None:
        self.objects: list[str] = []
        self.listings = 0
        self.removed: list[str] = []
        self.late = late_after_first_listing
        self.keep = keep

    def list_objects(self, bucket: str, prefix: str, recursive: bool = True) -> Any:
        self.listings += 1
        if self.listings > 1 and self.keep:
            # A prefix that is repopulated forever: the sweep must stay bounded.
            self.objects.append(f"{prefix}late-{self.listings}")
        elif self.listings == 2 and self.late is not None:
            self.objects.append(self.late)
        return [SimpleNamespace(object_name=name) for name in self.objects]

    def remove_object(self, bucket: str, name: str) -> None:
        self.objects.remove(name)
        self.removed.append(name)


def zone_with(client: FakeMinio) -> MinioObjectZone:
    zone = MinioObjectZone(
        "http://127.0.0.1:59000", "synthetic-bucket", "access", "secret-key", prefix="saved"
    )
    # The sweep is exercised against a listing double; no network call is made.
    zone._client = cast("Minio", client)
    return zone


async def test_sweep_repeats_until_a_pass_finds_nothing_left() -> None:
    late = "saved/project-demo/late"
    client = FakeMinio(late_after_first_listing=late)
    client.objects.append("saved/project-demo/early")
    zone = zone_with(client)

    erased = await zone.erase_prefix(prefix="project-demo/")

    # One pass would have listed only the early object and left the late one behind.
    assert erased == 2
    assert client.objects == []
    assert client.listings == 3


async def test_sweep_stays_bounded_when_a_prefix_keeps_being_repopulated() -> None:
    client = FakeMinio(keep=True)
    client.objects.append("saved/project-demo/early")
    zone = zone_with(client)

    erased = await zone.erase_prefix(prefix="project-demo/")

    assert client.listings == MAX_ERASE_PASSES
    assert erased == MAX_ERASE_PASSES
    assert client.objects == []
