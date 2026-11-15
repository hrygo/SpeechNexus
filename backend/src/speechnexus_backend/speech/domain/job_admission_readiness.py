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

from speechnexus_backend.speech.domain.admission import (
    PUBLIC_ADMISSION_CODES,
    AdmissionPool,
    AdmissionQuota,
    AdmissionRuntime,
    deployment_refusals,
)
from speechnexus_backend.speech.domain.public_jobs import identifier
from speechnexus_backend.speech.domain.runtime_observations import (
    READINESS_EXPIRING,
    READINESS_VALID,
)

# The public Job codes a degraded report points at. The admission vocabulary is the
# single source for the ones admission owns; the workspace refusals are declared in
# ``interfaces/http/job_errors.py``. ``DEPLOYMENT_STATE_CODES`` there is the closed
# set of "the deployment refuses every create" answers, and
# ``tests/contract/test_job_admission_readiness.py`` pins that this enum's codes are
# exactly that set -- so a create and the report cannot answer different questions.
ENGINE_NOT_READY_CODE = PUBLIC_ADMISSION_CODES["readiness_expired"]
ENGINE_STATE_MISMATCH_CODE = PUBLIC_ADMISSION_CODES["reconciliation_required"]
ENGINE_RESOURCE_DISABLED_CODE = PUBLIC_ADMISSION_CODES["policy_or_resource_disabled"]
CAPACITY_EXHAUSTED_CODE = "job_capacity_exhausted"
WORKSPACE_NOT_CONFIGURED_CODE = "workspace_not_configured"
WORKSPACE_NOT_FOUND_CODE = "workspace_not_found"
WORKSPACE_DISABLED_CODE = "workspace_disabled"
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
    ENGINE_STATE_MISMATCH = "engine_state_mismatch"
    ENGINE_RESOURCE_DISABLED = "engine_resource_disabled"
    CAPACITY_EXHAUSTED = "capacity_exhausted"
    WORKSPACE_NOT_FOUND = "workspace_not_found"
    WORKSPACE_DISABLED = "workspace_disabled"
    WORKSPACE_NOT_CONFIGURED = "workspace_not_configured"
    STORAGE_UNAVAILABLE = "storage_unavailable"


# cause -> the one public code a create answers for it.
CAUSE_CODES: dict[AdmissionReadinessCause, str] = {
    AdmissionReadinessCause.ENGINE_RECEIPT_EXPIRED: ENGINE_NOT_READY_CODE,
    AdmissionReadinessCause.ENGINE_STATE_MISMATCH: ENGINE_STATE_MISMATCH_CODE,
    AdmissionReadinessCause.ENGINE_RESOURCE_DISABLED: ENGINE_RESOURCE_DISABLED_CODE,
    AdmissionReadinessCause.CAPACITY_EXHAUSTED: CAPACITY_EXHAUSTED_CODE,
    AdmissionReadinessCause.WORKSPACE_NOT_FOUND: WORKSPACE_NOT_FOUND_CODE,
    AdmissionReadinessCause.WORKSPACE_DISABLED: WORKSPACE_DISABLED_CODE,
    AdmissionReadinessCause.WORKSPACE_NOT_CONFIGURED: WORKSPACE_NOT_CONFIGURED_CODE,
    AdmissionReadinessCause.STORAGE_UNAVAILABLE: STORAGE_UNAVAILABLE_CODE,
}

# The internal refusal codes ``deployment_refusals`` can return, mapped to the class
# an operator acts on. This is a table over the admission vocabulary rather than a
# second verdict: the codes are produced by the create path's own helpers, and the
# mapping only names which operator action clears each one.
CAUSE_BY_REFUSAL_CODE: dict[str, AdmissionReadinessCause] = {
    "readiness_expired": AdmissionReadinessCause.ENGINE_RECEIPT_EXPIRED,
    "not_found": AdmissionReadinessCause.ENGINE_STATE_MISMATCH,
    "reconciliation_required": AdmissionReadinessCause.ENGINE_STATE_MISMATCH,
    "stale_instance_generation": AdmissionReadinessCause.ENGINE_STATE_MISMATCH,
    "policy_or_resource_disabled": AdmissionReadinessCause.ENGINE_RESOURCE_DISABLED,
    "unapproved_runtime_template": AdmissionReadinessCause.ENGINE_RESOURCE_DISABLED,
    "quota_exceeded": AdmissionReadinessCause.CAPACITY_EXHAUSTED,
    "capacity_exhausted": AdmissionReadinessCause.CAPACITY_EXHAUSTED,
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
class AdmissionReadinessFacts:
    """One read of the coordination store, as the admission decision reads it.

    ``runtimes`` and ``pools`` are the domain objects ``decide_admission`` consumes,
    so this report runs the same predicate instead of a second implementation.
    ``workspace_state`` is ``None`` when no ``speech_workspaces`` row exists, and
    ``quota`` is ``None`` when the workspace has no reservation row; both are
    deterministic configuration facts, not storage faults.

    ``undecodable`` names ready instances whose stored intent the admission codec
    refuses; the create path answers ``engine_state_mismatch`` for those, so they are
    carried as refusals rather than dropped.
    """

    workspace_id: str
    workspace_state: str | None
    quota: AdmissionQuota | None
    pools: tuple[AdmissionPool, ...]
    runtimes: tuple[AdmissionRuntime, ...]
    undecodable: tuple[str, ...]
    now_us: int

    def __post_init__(self) -> None:
        identifier(self.workspace_id)
        if type(self.now_us) is not int or self.now_us < 1:
            raise ValueError("Admission readiness requires one positive clock reading")
        if self.quota is not None and self.quota.workspace_id != self.workspace_id:
            raise ValueError("Quota facts must describe the reported workspace")
        for instance_id in self.undecodable:
            identifier(instance_id)


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
    quota: AdmissionQuota | None

    @property
    def admitting(self) -> bool:
        return self.status is AdmissionReadinessStatus.ADMITTING

    @property
    def usable_instances(self) -> int:
        return sum(1 for instance in self.instances if instance.receipt_usable)


def _instance_report(runtime: AdmissionRuntime, now_us: int) -> ReadinessInstanceReport:
    instance = runtime.instance
    return ReadinessInstanceReport(
        instance_id=instance.instance_id,
        instance_generation=instance.instance_generation,
        readiness_state=runtime.readiness.expiry_state(
            now_us, warning_us=DEFAULT_READINESS_WARNING_SECONDS * 1_000_000
        ),
        readiness_expires_at_us=runtime.readiness.expires_at_us,
        remaining_seconds=(runtime.readiness.expires_at_us - now_us) // 1_000_000,
        max_slots=instance.plan.max_slots,
        held_slots=instance.held_slots,
        quarantined_slots=instance.quarantined_slots,
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


def _workspace_causes(facts: AdmissionReadinessFacts) -> tuple[AdmissionReadinessCause, ...]:
    """The refusals that hold for every create in this workspace, from its own rows."""
    if facts.workspace_state is None:
        return (AdmissionReadinessCause.WORKSPACE_NOT_FOUND,)
    if facts.workspace_state != "active":
        return (AdmissionReadinessCause.WORKSPACE_DISABLED,)
    if facts.quota is None:
        return (AdmissionReadinessCause.WORKSPACE_NOT_CONFIGURED,)
    return ()


def _chain_refusals(
    facts: AdmissionReadinessFacts,
    runtime: AdmissionRuntime,
    quota: AdmissionQuota,
) -> tuple[str, ...]:
    """What a create pinned to this instance would be refused with, in its own order.

    ``deployment_refusals`` is the create path's own predicate, so this is a filter of
    the same tuple to one instance's chain rather than a parallel comparison.
    """
    return deployment_refusals(
        pools=tuple(
            pool
            for pool in facts.pools
            if pool.ownership.pool_id == runtime.instance.plan.binding.pool_id
        ),
        runtimes=(runtime,),
        quota=quota,
        now_us=facts.now_us,
    )


def _selectable(facts: AdmissionReadinessFacts, runtime: AdmissionRuntime) -> bool:
    """Whether a create could pin this instance at all."""
    if runtime.instance.instance_state != "ready":
        return False
    pool_id = runtime.instance.plan.binding.pool_id
    return any(
        pool.ownership.pool_id == pool_id and pool.pool_state == "active" for pool in facts.pools
    )


def _classify_chains(
    facts: AdmissionReadinessFacts,
    quota: AdmissionQuota,
) -> tuple[tuple[str, ...], tuple[tuple[str, ...], ...]]:
    """The refusals of the chain a create would pick, and every chain's refusals.

    A create is admitted when *any* ready instance's chain is clean, because the
    create path pins one instance per stage. The reported chain is the one with the
    fewest refusals (then the lowest codes), so a degradation is never hidden behind
    a worse-fitting instance while some other instance is genuinely unusable.
    """
    # Selection runs before admission: the create path refuses a non-active pool or a
    # non-ready instance with ``job_capacity_exhausted``
    # (``public_job_admission``'s instance selection), and admission's own pool rule
    # never sees those rows. Measured on an isolated database, so the report models
    # the same step instead of the rule that runs after it.
    chains = [
        _chain_refusals(facts, runtime, quota)
        for runtime in facts.runtimes
        if _selectable(facts, runtime)
    ]
    if chains:
        # A ready instance whose stored intent cannot be decoded is still selected by a
        # create and then refused by admission, so it counts only where selection
        # itself can succeed; with nothing selectable the create answers capacity.
        chains.extend(("reconciliation_required",) for _ in facts.undecodable)
    if not chains:
        # No ready engine at all: the create path answers the capacity refusal.
        return ("capacity_exhausted",), ()
    return min(chains, key=lambda chain: (len(chain), chain)), tuple(chains)


def _causes_from(codes: tuple[str, ...]) -> tuple[AdmissionReadinessCause, ...]:
    seen: set[AdmissionReadinessCause] = set()
    causes: list[AdmissionReadinessCause] = []
    for code in codes:
        cause = CAUSE_BY_REFUSAL_CODE[code]
        if cause not in seen:
            seen.add(cause)
            causes.append(cause)
    return tuple(sorted(causes))


def _detail(facts: AdmissionReadinessFacts, causes: tuple[AdmissionReadinessCause, ...]) -> str:
    first = causes[0]
    if first is AdmissionReadinessCause.WORKSPACE_NOT_FOUND:
        return (
            f"Workspace {facts.workspace_id} has no catalog row, so a create answers "
            f"{WORKSPACE_NOT_FOUND_CODE} before admission is consulted."
        )
    if first is AdmissionReadinessCause.WORKSPACE_DISABLED:
        return (
            f"Workspace {facts.workspace_id} is not active (state {facts.workspace_state!r}), "
            f"so a create answers {WORKSPACE_DISABLED_CODE}."
        )
    if first is AdmissionReadinessCause.WORKSPACE_NOT_CONFIGURED:
        return (
            "The workspace has no quota row, so admission has no reservation to charge and a "
            f"create answers {WORKSPACE_NOT_CONFIGURED_CODE}; registering an engine for this "
            "workspace writes one."
        )
    if first is AdmissionReadinessCause.CAPACITY_EXHAUSTED:
        quota = facts.quota
        if (
            quota is not None
            and quota.held_attempts + quota.quarantined_attempts >= quota.max_attempts
        ):
            return (
                "The workspace quota holds every attempt "
                f"(held {quota.held_attempts} + quarantined {quota.quarantined_attempts} of "
                f"{quota.max_attempts}); a create answers {CAPACITY_EXHAUSTED_CODE} until one is "
                "released."
            )
        return (
            "No ready engine instance can take another slot, so a create answers "
            f"{CAPACITY_EXHAUSTED_CODE} until an instance becomes ready or a slot is released; "
            "retrying alone does not clear the missing instance."
        )
    if first is AdmissionReadinessCause.ENGINE_RECEIPT_EXPIRED:
        return (
            "No ready engine instance holds a usable readiness receipt, so a create answers "
            f"{ENGINE_NOT_READY_CODE} until one is re-signed with `{RENEW_COMMAND}` (the "
            f"read-only report `{READINESS_REPORT_CODE}` prints the remaining window)."
        )
    if first is AdmissionReadinessCause.ENGINE_STATE_MISMATCH:
        return (
            "A ready engine instance's stored intent no longer matches admission's expectation, "
            f"so a create answers {ENGINE_STATE_MISMATCH_CODE}; reconcile or re-register it with "
            f"`{REGISTER_COMMAND}`."
        )
    return (
        "A ready engine instance, its pool or the approved recipe is not active, so a create "
        f"answers {ENGINE_RESOURCE_DISABLED_CODE} until it is re-enabled or re-approved."
    )


def classify_admission_readiness(facts: AdmissionReadinessFacts) -> AdmissionReadiness:
    """Classify one completed read with the create path's own predicate.

    ``deployment_refusals`` is the tuple ``decide_admission`` raises from, so the
    causes are a naming of its codes, never a second comparison. A clean chain for at
    least one ready instance means ``admitting``; otherwise the chain a create would
    pick names the class an operator has to act on.
    """
    instances = tuple(_instance_report(runtime, facts.now_us) for runtime in facts.runtimes)
    workspace_causes = _workspace_causes(facts)
    quota = facts.quota
    if workspace_causes or quota is None:
        # A workspace refusal holds for every create before admission is consulted.
        causes = workspace_causes
        admitting = False
    else:
        chosen, chains = _classify_chains(facts, quota)
        causes = _causes_from(chosen)
        # ``admitting`` requires one clean chain: a create pins one instance per stage.
        admitting = any(not chain for chain in chains)
        if admitting:
            causes = ()
    return AdmissionReadiness(
        status=(
            AdmissionReadinessStatus.ADMITTING if admitting else AdmissionReadinessStatus.DEGRADED
        ),
        causes=causes,
        workspace_id=facts.workspace_id,
        now_us=facts.now_us,
        detail=None if admitting else _detail(facts, causes),
        instances=instances,
        quota=facts.quota,
    )
