"""Synthetic controller/warmup facts; never real model or hardware qualification."""

from dataclasses import replace

from speechnexus_backend.speech.domain.runtime_intents import (
    RuntimeHead,
    RuntimeInstance,
    RuntimeRecord,
)
from speechnexus_backend.speech.domain.runtime_observations import (
    ObserveRuntimeIntent,
    RuntimeObservation,
    RuntimeObservationPrerequisites,
    VerifiedRuntimeReadiness,
)
from tests.fake.runtime_intents import COMMAND, NOW, PLAN, REQUEST
from tests.fake.runtime_intents import HOST as ORIGINAL_HOST

HOST = replace(ORIGINAL_HOST, held=ORIGINAL_HOST.held.add(PLAN.peak))
HEAD = RuntimeHead(REQUEST.instance_id, 1, 1, REQUEST.intent_id, None, 1)
INSTANCE = RuntimeInstance(REQUEST.instance_id, 1, PLAN, "preparing", "peak", 0, 0, 7, 1, None)
RECORD = RuntimeRecord(
    REQUEST, PLAN, COMMAND.owner_id, "sha256:" + "d" * 64, "pending", NOW - 1000, NOW - 1000, 1
)
OBSERVATION = RuntimeObservation(
    REQUEST.intent_id,
    REQUEST.binding,
    REQUEST.instance_id,
    1,
    "prepare",
    7,
    PLAN.memory,
    "succeeded",
    "ready",
    NOW - 500,
    NOW - 100,
    PLAN.memory.peak,
)
READINESS = VerifiedRuntimeReadiness(
    REQUEST.binding,
    REQUEST.instance_id,
    1,
    7,
    PLAN.topology_digest,
    PLAN.qualification_digest,
    "sha256:" + "e" * 64,
    NOW - 10,
    NOW + 60000000,
)
OBSERVE = ObserveRuntimeIntent(
    OBSERVATION, 1, 1, COMMAND.owner_id, "synthetic-domain-fact", READINESS
)
PREREQUISITES = RuntimeObservationPrerequisites(True, True, True)
