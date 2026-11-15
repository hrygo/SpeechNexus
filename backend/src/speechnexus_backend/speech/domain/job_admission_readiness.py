"""Observable projection of "would a public Job create be admitted right now?".

An elapsed engine readiness receipt makes every public Job create answer
``503 engine_not_ready`` while ``/readyz``, the container healthcheck and
``docker ps`` all stay green (#845). ``/readyz`` must keep meaning "this process
can serve traffic" (#828 review B8): the receipt refuses only creation, so failing
``/readyz`` would take the whole API out of a load balancer and answer a
"create-only" degradation with a total outage. The compensation therefore belongs
on its own surface, and this module is the classification behind it.

Only facts the admission transaction itself reads are classified: the stored
readiness receipts of the ready instances, the workspace quota counters and the
per-instance slot counters, all against one database clock. The report never
re-derives a comparison the admission decision owns -- ``expiry_state`` still
decides the readiness window, and the counters are compared against the same
columns ``decide_admission`` reads -- and it never changes what admission accepts.

The refusal classes are a closed enum because "unavailable" is exactly the
collapse this surface exists to remove: an elapsed receipt, exhausted capacity, an
unreadable store and an unprovisioned quota need four different operator actions
and answer four different public codes. ``storage_unavailable`` is exclusive: when
the store cannot be read there is no other fact to report, so reporting further
causes would be invention.

Boundaries (deliberate, and stated so a later change does not read them as
coverage): the report speaks for the deployment, not for one request body, so
request-shaped refusals (``recipe_not_admitted``, ``unsupported_parameter``, a
binding that is not published, an input artifact that is not ready) are not
classified; host-level resource headroom is not evaluated because the required
vector comes from the request's plan; and the readiness *window* is classified
while receipt *identity* is not -- a receipt naming another fencing token answers
``503 engine_state_mismatch`` from a create and is reported here as the instance's
window state, which is a recorded gap rather than a claim.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from speechnexus_backend.speech.domain.admission import PUBLIC_ADMISSION_CODES
from speechnexus_backend.speech.domain.public_jobs import identifier
from speechnexus_backend.speech.domain.runtime_observations import (
    READINESS_EXPIRED,
    READINESS_EXPIRING,
    READINESS_VALID,
    VerifiedRuntimeReadiness,
)

# The public Job codes a degraded report points at, taken from the one place each
# is already declared. ``tests/contract/test_job_admission_readiness.py`` pins that
# a real create answers exactly these, so the sentence an operator reads and the
# code a client receives cannot drift apart.
ENGINE_NOT_READY_CODE = PUBLIC_ADMISSION_CODES["readiness_expired"]
CAPACITY_EXHAUSTED_CODE = "job_capacity_exhausted"
WORKSPACE_NOT_CONFIGURED_CODE = "workspace_not_configured"
STORAGE_UNAVAILABLE_CODE = "job_unavailable"

# The read-only report this surface points an operator at, and the renewal that
# actually re-signs a receipt. Both are installed console scripts, so a copy of the
# command in a message is a pointer, never a second implementation.
READINESS_REPORT_CODE = "speechnexus-engine-readiness"
RENEW_COMMAND = "speechnexus-register-engine --renew-readiness"
REGISTER_COMMAND = "speechnexus-register-engine"


class AdmissionReadinessStatus(StrEnum):
    """Whether a create is admitted, as one value a poller can assert on."""

    ADMITTING = "admitting"
    DEGRADED = "degraded"


class AdmissionReadinessCause(StrEnum):
    """Why a create would not be admitted, one class per operator action."""

    ENGINE_RECEIPT_EXPIRED = "engine_receipt_expired"
    CAPACITY_EXHAUSTED = "capacity_exhausted"
    STORAGE_UNAVAILABLE = "storage_unavailable"
    WORKSPACE_NOT_PROVISIONED = "workspace_not_provisioned"


# The refusal codes a degraded report names, in the order the classes are listed.
CAUSE_CODES: dict[AdmissionReadinessCause, str] = {
    AdmissionReadinessCause.ENGINE_RECEIPT_EXPIRED: ENGINE_NOT_READY_CODE,
    AdmissionReadinessCause.CAPACITY_EXHAUSTED: CAPACITY_EXHAUSTED_CODE,
    AdmissionReadinessCause.STORAGE_UNAVAILABLE: STORAGE_UNAVAILABLE_CODE,
    AdmissionReadinessCause.WORKSPACE_NOT_PROVISIONED: WORKSPACE_NOT_CONFIGURED_CODE,
}

# A receipt inside its window is usable: ``expiring`` is a reporting threshold,
# never an admission rule, so it is not a cause.
_USABLE_RECEIPT_STATES = frozenset({READINESS_VALID, READINESS_EXPIRING})

DEFAULT_READINESS_WARNING_SECONDS = 21_600
"""A quarter of the 24 h default receipt TTL: one renewal interval of warning.

Shared with ``speechnexus-engine-readiness`` so the console report and this surface
classify ``expiring`` against the same threshold. It is a reporting window, never an
admission rule: an ``expiring`` receipt still admits work, so a deployment that
overrides the CLI flag cannot change a create's answer or this surface's verdict.
"""

READINESS_MISSING = "missing"
"""The ready instance has no stored receipt at all; admission cannot admit onto it."""

READINESS_INVALID = "invalid"
"""The stored receipt no longer decodes, so admission cannot read it either."""


@dataclass(frozen=True, slots=True)
class ReadyInstanceFacts:
    """One ready instance as the admission transaction would read it."""

    instance_id: str
    instance_generation: int
    readiness: VerifiedRuntimeReadiness | None
    has_readiness_document: bool
    max_slots: int
    held_slots: int
    quarantined_slots: int

    def __post_init__(self) -> None:
        identifier(self.instance_id)
        if type(self.instance_generation) is not int or self.instance_generation < 1:
            raise ValueError("A ready instance requires a positive generation")
        # A receipt is absent (``missing``) or present-but-undecodable
        # (``invalid``); ``has_readiness_document`` keeps those two apart. Slot
        # counters may exceed the bound while a release is quarantined, so they are
        # reported as read instead of being refused here.
        for value in (self.max_slots, self.held_slots, self.quarantined_slots):
            if type(value) is not int or value < 0:
                raise ValueError("Slot counters are non-negative integers")


@dataclass(frozen=True, slots=True)
class WorkspaceQuotaFacts:
    """The reservation counters one create would charge."""

    workspace_id: str
    max_attempts: int
    held_attempts: int
    quarantined_attempts: int

    def __post_init__(self) -> None:
        identifier(self.workspace_id)
        for value in (self.max_attempts, self.held_attempts, self.quarantined_attempts):
            if type(value) is not int or value < 0:
                raise ValueError("Quota counters are non-negative integers")

    @property
    def exhausted(self) -> bool:
        """No attempt can be held: ``decide_admission`` refuses with ``quota_exceeded``."""
        return self.held_attempts + self.quarantined_attempts >= self.max_attempts


@dataclass(frozen=True, slots=True)
class AdmissionReadinessFacts:
    """Everything one read of the coordination store answered.

    ``quota`` is ``None`` when the workspace has no quota row, which is a
    deterministic configuration refusal (``422 workspace_not_configured``), not a
    storage fault: the read succeeded and found nothing.
    """

    workspace_id: str
    now_us: int
    instances: tuple[ReadyInstanceFacts, ...]
    quota: WorkspaceQuotaFacts | None

    def __post_init__(self) -> None:
        identifier(self.workspace_id)
        if type(self.now_us) is not int or self.now_us < 1:
            raise ValueError("Admission readiness requires one positive clock reading")
        if self.quota is not None and self.quota.workspace_id != self.workspace_id:
            raise ValueError("Quota facts must describe the reported workspace")


@dataclass(frozen=True, slots=True)
class ReadinessInstanceReport:
    """The public per-instance projection: window state plus slot headroom."""

    instance_id: str
    instance_generation: int
    readiness_state: str
    readiness_expires_at_us: int | None
    remaining_seconds: int | None
    max_slots: int
    held_slots: int
    quarantined_slots: int

    @property
    def receipt_usable(self) -> bool:
        return self.readiness_state in _USABLE_RECEIPT_STATES

    @property
    def slots_exhausted(self) -> bool:
        """No single-slot request fits: admission refuses with ``capacity_exhausted``."""
        return self.held_slots + self.quarantined_slots >= self.max_slots


@dataclass(frozen=True, slots=True)
class AdmissionReadiness:
    """One immutable verdict plus the facts that produced it."""

    status: AdmissionReadinessStatus
    causes: tuple[AdmissionReadinessCause, ...]
    workspace_id: str
    now_us: int
    detail: str | None
    instances: tuple[ReadinessInstanceReport, ...]
    quota: WorkspaceQuotaFacts | None

    @property
    def admitting(self) -> bool:
        return self.status is AdmissionReadinessStatus.ADMITTING

    @property
    def usable_instances(self) -> int:
        return sum(1 for instance in self.instances if instance.receipt_usable)


def _instance_report(facts: ReadyInstanceFacts, now_us: int) -> ReadinessInstanceReport:
    receipt = facts.readiness
    if receipt is None:
        state = READINESS_MISSING if not facts.has_readiness_document else READINESS_INVALID
        return ReadinessInstanceReport(
            instance_id=facts.instance_id,
            instance_generation=facts.instance_generation,
            readiness_state=state,
            readiness_expires_at_us=None,
            remaining_seconds=None,
            max_slots=facts.max_slots,
            held_slots=facts.held_slots,
            quarantined_slots=facts.quarantined_slots,
        )
    return ReadinessInstanceReport(
        instance_id=facts.instance_id,
        instance_generation=facts.instance_generation,
        readiness_state=receipt.expiry_state(
            now_us, warning_us=DEFAULT_READINESS_WARNING_SECONDS * 1_000_000
        ),
        readiness_expires_at_us=receipt.expires_at_us,
        remaining_seconds=(receipt.expires_at_us - now_us) // 1_000_000,
        max_slots=facts.max_slots,
        held_slots=facts.held_slots,
        quarantined_slots=facts.quarantined_slots,
    )


def storage_unavailable_readiness(*, workspace_id: str, now_us: int) -> AdmissionReadiness:
    """The report when the store could not be read at all.

    ``storage_unavailable`` is the only cause: with no facts there is nothing else
    this surface is entitled to claim, and reporting ``admitting`` because nothing
    was observed would be the same lie as a green healthcheck over a broken surface.
    """
    identifier(workspace_id)
    return AdmissionReadiness(
        status=AdmissionReadinessStatus.DEGRADED,
        causes=(AdmissionReadinessCause.STORAGE_UNAVAILABLE,),
        workspace_id=workspace_id,
        now_us=now_us,
        detail=(
            "The coordination store could not be read, so admission cannot be evaluated; "
            f"a create answers with the storage refusal {STORAGE_UNAVAILABLE_CODE}."
        ),
        instances=(),
        quota=None,
    )


def _engine_detail(instances: tuple[ReadinessInstanceReport, ...]) -> str:
    if not instances:
        return (
            "No ready engine instance is registered, so a create has nothing to be admitted "
            f"onto; register one with `{REGISTER_COMMAND}`. Admission answers "
            f"{ENGINE_NOT_READY_CODE} while no usable receipt exists."
        )
    unusable = [instance for instance in instances if not instance.receipt_usable]
    elapsed = [instance for instance in unusable if instance.readiness_state == READINESS_EXPIRED]
    if len(elapsed) == len(unusable):
        return (
            f"{len(elapsed)} of {len(instances)} ready engine instance(s) have an elapsed "
            f"readiness receipt; a create is refused with {ENGINE_NOT_READY_CODE} until it is "
            f"re-signed with `{RENEW_COMMAND}` (the read-only report `{READINESS_REPORT_CODE}` "
            "prints the remaining window per instance)."
        )
    states = ", ".join(sorted({instance.readiness_state for instance in unusable}))
    return (
        f"{len(unusable)} of {len(instances)} ready engine instance(s) have no usable readiness "
        f"receipt ({states}); a create is refused until the instance is registered again with "
        f"`{RENEW_COMMAND}` or `{REGISTER_COMMAND}` when no receipt exists yet."
    )


def _capacity_detail(
    quota: WorkspaceQuotaFacts | None, usable: tuple[ReadinessInstanceReport, ...]
) -> str:
    if quota is not None and quota.exhausted:
        return (
            "The workspace quota holds every attempt "
            f"(held {quota.held_attempts} + quarantined {quota.quarantined_attempts} of "
            f"{quota.max_attempts}); a create is refused with {CAPACITY_EXHAUSTED_CODE} until a "
            "holding attempt is released."
        )
    bound = [instance for instance in usable if instance.slots_exhausted]
    if not bound:
        # Unreachable through ``classify_admission_readiness``: it only composes
        # this sentence after finding an exhausted arm.
        raise ValueError("Capacity detail requires an exhausted quota or slot arm")
    instance = bound[0]
    return (
        "Every engine instance with a usable readiness receipt is at its slot bound "
        f"(instance {instance.instance_id} g{instance.instance_generation}: held "
        f"{instance.held_slots} + quarantined {instance.quarantined_slots} of "
        f"{instance.max_slots}); a create is refused with {CAPACITY_EXHAUSTED_CODE} until a "
        "slot is released."
    )


def classify_admission_readiness(facts: AdmissionReadinessFacts) -> AdmissionReadiness:
    """Classify one completed read into the closed cause enum.

    The causes are evaluated independently and reported together, so a deployment
    that is both out of quota and past its receipt window names both instead of
    hiding one behind the other. A create answers whichever cause it evaluates
    first, which is why this surface names the classes rather than mirroring one
    request's precedence.
    """
    instances = tuple(_instance_report(instance, facts.now_us) for instance in facts.instances)
    usable = tuple(instance for instance in instances if instance.receipt_usable)
    causes: list[AdmissionReadinessCause] = []
    details: list[str] = []
    if facts.quota is None:
        causes.append(AdmissionReadinessCause.WORKSPACE_NOT_PROVISIONED)
        details.append(
            f"The workspace {facts.workspace_id} has no provisioned Job quota; a create is "
            f"refused with {WORKSPACE_NOT_CONFIGURED_CODE} until one is provisioned."
        )
    if not usable:
        causes.append(AdmissionReadinessCause.ENGINE_RECEIPT_EXPIRED)
        details.append(_engine_detail(instances))
    slot_exhausted = bool(usable) and all(instance.slots_exhausted for instance in usable)
    quota_exhausted = facts.quota is not None and facts.quota.exhausted
    if quota_exhausted or slot_exhausted:
        causes.append(AdmissionReadinessCause.CAPACITY_EXHAUSTED)
        details.append(_capacity_detail(facts.quota, usable))
    return AdmissionReadiness(
        status=AdmissionReadinessStatus.DEGRADED if causes else AdmissionReadinessStatus.ADMITTING,
        causes=tuple(causes),
        workspace_id=facts.workspace_id,
        now_us=facts.now_us,
        detail=" ".join(details) if details else None,
        instances=instances,
        quota=facts.quota,
    )
