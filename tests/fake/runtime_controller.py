"""Synthetic controller payloads/streams; no runtime or readiness qualification."""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Any

import httpx

from speechnexus_backend.speech.domain.capacity import PhysicalResources
from speechnexus_backend.speech.domain.runtime_intents import RuntimeIntent
from speechnexus_backend.speech.infrastructure.runtime_codec import request_to_wire
from tests.fake.runtime_intents import COMMAND, HOST, PLAN, REQUEST

MIB = 1024**2


def _bytes(vector: PhysicalResources) -> PhysicalResources:
    return PhysicalResources(
        *(amount * (1 if index == 3 else MIB) for index, amount in enumerate(vector.values))
    )


# The native controller enforces a real 6 MiB minimum container limit. Keep
# the same synthetic capacity ratios, expressed in valid byte units.
TLS_PLAN = replace(
    PLAN, peak=_bytes(PLAN.peak), steady=_bytes(PLAN.steady), per_slot=_bytes(PLAN.per_slot)
)
TLS_REQUEST = replace(REQUEST, reserved_memory=TLS_PLAN.memory)
TLS_COMMAND = replace(COMMAND, request=TLS_REQUEST, approved_plan=TLS_PLAN)
TLS_HOST = replace(
    HOST,
    capacity=_bytes(HOST.capacity),
    held=_bytes(HOST.held),
    quarantined=_bytes(HOST.quarantined),
)


def observation(
    original: RuntimeIntent = REQUEST, *, ready: bool = False, replay: bool | None = False
) -> dict[str, Any]:
    value = request_to_wire(original)
    value.pop("deadline_at")
    value.update(
        intent_state="succeeded" if ready else "pending",
        observed_instance_state="ready" if ready else "warming_up",
        created_at="2026-10-24T00:00:00Z",
        updated_at="2026-10-24T00:00:01Z",
    )
    if original.action == "stop":
        value["observed_instance_state"] = "stopped" if ready else "stopping"
        if ready:
            value["stop_evidence"] = {
                "process_or_container_exited": True,
                "resources_released_confirmed": True,
                "verified_at": "2026-10-24T00:00:01Z",
            }
    if replay is not None:
        value["idempotent_replay"] = replay
    return value


class ResponseChunks(httpx.AsyncByteStream):
    def __init__(self, *chunks: bytes, delay: float = 0) -> None:
        self.chunks = chunks
        self.delay = delay
        self.closed = False
        self.yielded = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            if self.delay:
                await asyncio.sleep(self.delay)
            self.yielded += 1
            yield chunk

    async def aclose(self) -> None:
        self.closed = True
