"""Synthetic immutable runtime facts; never hardware/model qualification evidence."""

from speechnexus_backend.speech.domain.capacity import PhysicalResources
from speechnexus_backend.speech.domain.owners import PoolOwnership
from speechnexus_backend.speech.domain.runtime_intents import (
    ApprovedRuntimePlan,
    CreateRuntimeIntent,
    RuntimeBinding,
    RuntimeDeadline,
    RuntimeHost,
    RuntimeIntent,
    RuntimePrerequisites,
)

NOW = 1792756800000000
TOPOLOGY = "sha256:" + "a" * 64
CONFIG = "sha256:" + "b" * 64
QUALIFICATION = "sha256:" + "c" * 64
BINDING = RuntimeBinding(
    "host-test",
    "tts-realtime",
    "gateway-test",
    "template-test",
    "binding-test",
    "manifest-test",
    CONFIG,
)
PLAN = ApprovedRuntimePlan(
    BINDING,
    TOPOLOGY,
    QUALIFICATION,
    "unified_memory",
    "realtime",
    "on_demand",
    4,
    1,
    PhysicalResources(20, 0, 0, 200, 10, 50),
    PhysicalResources(10, 0, 0, 100, 5, 25),
    PhysicalResources(3, 0, 0, 20, 2, 5),
)
REQUEST = RuntimeIntent(
    "runtime-new",
    BINDING,
    "instance-new",
    1,
    "prepare",
    7,
    PLAN.memory,
    RuntimeDeadline("2250-01-01T00:00:00.123456789Z"),
)
COMMAND = CreateRuntimeIntent(REQUEST, None, PLAN, "scheduler-test")
HOST = RuntimeHost(
    "host-test",
    "unified_memory",
    TOPOLOGY,
    "active",
    1,
    PhysicalResources(100, 0, 0, 2000, 100, 1000),
    PhysicalResources(5, 0, 0, 10, 1, 2),
    PhysicalResources(7, 0, 0, 20, 2, 3),
)
OWNER = PoolOwnership("tts-realtime", "scheduler-test", 7, NOW + 60000000, "active", 3)
EMPTY = RuntimePrerequisites(True, True, True, False, False)
