"""Synthetic complete recipe/context; never actual qualification or business parsing."""

import hashlib
from dataclasses import replace

from speechnexus_backend.speech.domain.admission import (
    AdmissionIdentity,
    AdmissionPool,
    AdmissionQuota,
    AdmissionRequest,
    AdmissionRuntime,
    AdmitRecipe,
    ApprovedAdmissionRecipe,
    ApprovedStage,
    OriginalBusinessCommand,
    StageAllocation,
)
from speechnexus_backend.speech.domain.jobs import CreateJob
from speechnexus_backend.speech.domain.owners import OwnerAuthority
from tests.fake.runtime_intents import HOST, NOW, OWNER, PLAN, REQUEST
from tests.fake.runtime_observations import HEAD, INSTANCE, OBSERVATION, READINESS

DOCUMENT = '{"synthetic_effective_command":true}'
BUSINESS = OriginalBusinessCommand(
    DOCUMENT, "sha256:" + hashlib.sha256(DOCUMENT.encode()).hexdigest()
)
IDENTITY = AdmissionIdentity("operation-admit", "attempt-admit")
STAGE = StageAllocation(
    "stage-main",
    "lease-admit-main",
    "tts-realtime",
    "host-test",
    "gateway-test",
    REQUEST.instance_id,
    1,
    2,
    1,
)
JOB = CreateJob(
    "job-admit",
    IDENTITY.operation_id,
    IDENTITY.attempt_id,
    "event-admit",
    "sha256:" + "d" * 64,
    BUSINESS.request_digest,
)
ADMISSION = AdmissionRequest(
    IDENTITY,
    "workspace-test",
    "binding-test",
    "manifest-test",
    "sha256:" + "f" * 64,
    "policy-test",
    1,
    "realtime",
    (OwnerAuthority("tts-realtime", "scheduler-test", 7),),
    (STAGE,),
    NOW + 1000000,
    JOB,
)
APPROVED = ApprovedAdmissionRecipe(
    "binding-test",
    "manifest-test",
    ADMISSION.recipe_digest,
    "policy-test",
    "realtime",
    (ApprovedStage("stage-main", 1, PLAN),),
    NOW + 60000000,
    frozenset({"concurrent_attempts"}),
)
COMMAND = AdmitRecipe(ADMISSION, BUSINESS, APPROVED)
QUOTA = AdmissionQuota("workspace-test", 4, 0, 0, "policy-test", 1)
POOLS = (AdmissionPool(OWNER, "active", "realtime"),)
HOSTS = (replace(HOST, held=HOST.held.add(PLAN.steady)),)
RUNTIMES = (
    AdmissionRuntime(
        replace(
            INSTANCE,
            instance_state="ready",
            base_budget_kind="steady",
            version=2,
            ready_at_us=NOW - 5,
        ),
        HEAD,
        OBSERVATION,
        READINESS,
        0,
    ),
)
