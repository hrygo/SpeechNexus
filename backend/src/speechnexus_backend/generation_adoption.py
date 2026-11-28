"""Audited adoption of the node's generation into the coordination ledger.

The coordination ledger and the node controller each keep a generation counter and
nothing makes them move together. In the field the same instance read ``1`` in
``runtime_instance_heads`` and ``11`` on the node while the pod serving traffic was the
eleventh: the readiness receipt attested a generation no pod served, every renewal
failed with ``instance_generation_mismatch`` and admission was left with no usable
receipt (#1282). ``4f5cc3f7`` made the two commands *detect and refuse* that state.
This module is the convergence path an operator chooses instead of restarting the
engine: advance the ledger to the generation the node actually prepared, and leave an
audit trail saying why that was allowed.

**What this is not.** It is not a way around the registration path's refusal to write a
higher generation. ``speechnexus-register-engine`` requires the previous generation to be
``stopped``/``released`` because *the ledger* holds no evidence that it stopped, and an
operator attestation is not evidence. The adoption does not assert that fact either: it
asks the node, whose intent store is the record of what was actually prepared and
stopped, and cites the node's answer in ``runtime_generation_adoptions``. Where the node
says nothing, the command refuses unless the operator passes
``--accept-missing-stop-evidence``, and the audit row then records ``missing`` -- the
weaker claim it really is.

**One transaction, no partial repair.** The write set is: the adopted generation's
``engine_instances`` row (copied from the retired generation's immutable plan), a
``succeeded`` prepare intent with this run's health-probe evidence and a fresh readiness
receipt, the retired generation marked ``stopped``/``released``, the head advanced, the
pool's fencing epoch advanced to the node's token when it is ahead, and the audit row.
Any failure rolls all of it back: a half-adopted ledger would be worse than the
divergence it repairs, because the head would then point at a generation with no plan
to read.

**Why the host vector is not touched.** Exactly one physical engine is running before
and after this command; the adopted generation's plan is the retired generation's plan.
The ledger moves which *instance identity* holds that steady reservation, not how much
is held, so ``host_nodes.held_*`` must not move either. Reserve-then-settle (the
create path) would subtract the retired generation's steady and add the adopted one --
the same number -- while briefly claiming capacity the engine already has.

**Why the pool's fencing token moves.** The node's generation was prepared under a
fencing token the ledger never saw. Leaving the pool at the old token would make the
next renewal fail ``stale_fencing_token`` (the stored instance token and the pool token
must agree) and would let a later prepare reuse an epoch the node has already passed.
Adoption therefore advances ``resource_pools.fencing_token`` to the node's token, and
refuses when the node's token is *behind* the pool: that epoch is already spent.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, cast

from speechnexus_backend.controller_head import (
    ADOPTION_ALIGNED,
    ADOPTION_REFUSED,
    PREVIOUS_STOP_MISSING,
    ControllerConsultationError,
    GenerationAdoptionDecision,
    NodeInstanceHead,
    configured_endpoint,
    decide_generation_adoption,
    read_head,
)
from speechnexus_backend.engine_registration import (
    EXIT_CONFLICT,
    EXIT_INVALID,
    EXIT_UNAVAILABLE,
    EngineProbe,
    RegistrationError,
    _Cursor,
    _iso,
    plan_matches_row,
    prepared_observation_wire,
    probe_engine,
    probe_evidence_document,
)
from speechnexus_backend.speech.domain.runtime_intents import (
    ApprovedRuntimePlan,
    RuntimeDeadline,
    RuntimeIntent,
    RuntimeRejectedError,
    identifier,
)
from speechnexus_backend.speech.domain.runtime_observations import VerifiedRuntimeReadiness
from speechnexus_backend.speech.infrastructure.runtime_codec import (
    PreparedRuntimeRequest,
    decode_record,
    encode_record,
    prepare_request,
)
from speechnexus_backend.speech.infrastructure.runtime_observation_codec import (
    prepare_observation,
    readiness_to_document,
    validate_stored_readiness,
)
from speechnexus_backend.speech.infrastructure.runtime_sql import (
    _INSERT_ENGINE_INSTANCE,
    _instance,
    _record,
    block_operator_attested_delivery,
    queue_intent_dispatch,
)
from speechnexus_portable_sql.psycopg_driver import PortableConnection
from speechnexus_portable_sql.psycopg_driver import connect as portable_connect
from speechnexus_portable_sql.vendor import dict_row, psycopg

ADOPTION_KIND = "operator_generation_adoption_v1"
PROBE_KIND = "operator_generation_adoption_health_probe_v1"

#: Preconditions the ledger can still satisfy are reported separately from facts that
#: could not be established: the first means "do not change the ledger", the second
#: means "this run could not tell".
_EXIT_BY_CODE = {
    "invalid_database_url": EXIT_INVALID,
    "invalid_engine_url": EXIT_INVALID,
    "invalid_instance": EXIT_INVALID,
    "invalid_node": EXIT_INVALID,
    "invalid_operator": EXIT_INVALID,
    "invalid_schema": EXIT_INVALID,
    "invalid_readiness_ttl": EXIT_INVALID,
    "invalid_intent_deadline": EXIT_INVALID,
    "invalid_health_timeout": EXIT_INVALID,
    "invalid_transaction_timeout": EXIT_INVALID,
    "controller_not_configured": EXIT_INVALID,
    "invalid_controller_url": EXIT_INVALID,
    "instance_not_registered": EXIT_CONFLICT,
    "node_has_no_intent": EXIT_CONFLICT,
    "node_head_incomplete": EXIT_CONFLICT,
    "ledger_ahead_of_node": EXIT_CONFLICT,
    "previous_generation_stop_unconfirmed": EXIT_CONFLICT,
    "stale_fencing_token": EXIT_CONFLICT,
    "version_conflict": EXIT_CONFLICT,
    "instance_not_ready": EXIT_CONFLICT,
    "reconciliation_required": EXIT_CONFLICT,
    "audit_conflict": EXIT_CONFLICT,
    "invalid_controller_timeout": EXIT_INVALID,
    "controller_unreachable": EXIT_UNAVAILABLE,
    "controller_refused": EXIT_UNAVAILABLE,
    "controller_head_invalid": EXIT_UNAVAILABLE,
    "engine_unreachable": EXIT_UNAVAILABLE,
    "engine_unhealthy": EXIT_UNAVAILABLE,
    "schema_not_migrated": EXIT_UNAVAILABLE,
    "database_unavailable": EXIT_UNAVAILABLE,
    "transaction_timeout": EXIT_UNAVAILABLE,
}


class AdoptionCursor(Protocol):
    """What the adoption transaction needs from a cursor.

    Narrow on purpose: the same statements run on PostgreSQL (through the operator
    command) and on InnoDB (through the dual-database integration case), so the
    transaction cannot depend on a driver-specific connection object.
    """

    @property
    def rowcount(self) -> int: ...

    def execute(self, query: str, parameters: tuple[object, ...] = ()) -> object: ...

    def fetchall(self) -> list[Row]: ...


type Row = dict[str, Any]


def _select(cursor: AdoptionCursor, query: str, parameters: tuple[object, ...]) -> list[Row]:
    cursor.execute(query, parameters)
    return cursor.fetchall()


def _select_one(cursor: AdoptionCursor, query: str, parameters: tuple[object, ...]) -> Row:
    rows = _select(cursor, query, parameters)
    if len(rows) != 1:
        raise _fail("reconciliation_required", "expected exactly one coordination row")
    return rows[0]


_HEAD_READ = "SELECT * FROM runtime_instance_heads WHERE instance_id = ?"
_HEAD_LOCK = "SELECT * FROM runtime_instance_heads WHERE instance_id = ? FOR UPDATE"
_INSTANCE_LOCATE = (
    "SELECT pool_id, host_node_id FROM engine_instances "
    "WHERE instance_id = ? AND instance_generation = ?"
)
_INSTANCE_LOCK = (
    "SELECT * FROM engine_instances WHERE instance_id = ? AND instance_generation = ? FOR UPDATE"
)
_POOL_LOCK = "SELECT * FROM resource_pools WHERE pool_id = ? FOR UPDATE"
_INTENT_LOCK = (
    "SELECT * FROM runtime_instance_intents WHERE target_node_id = ? AND intent_id = ? FOR UPDATE"
)
#: The adopted generation becomes ready in the same transaction that records its plan.
#: The insert above writes the create path's ``preparing`` state and the canonical row
#: never sets the budget kind, so the settle half of a prepare is one conditional update.
_SETTLE_INSTANCE = (
    "UPDATE engine_instances SET instance_state = 'ready', base_budget_kind = 'steady', "
    "ready_at_us = ?, version = version + 1 WHERE instance_id = ? "
    "AND instance_generation = ? AND version = 1"
)
_INTENT_INSERT = (
    "INSERT INTO runtime_instance_intents (target_node_id, intent_id, instance_id, "
    "instance_generation, action, request_digest, request_document, intent_state, "
    "observation_document, readiness_document, recorded_at_us, updated_at_us) VALUES "
    "(?, ?, ?, ?, 'prepare', ?, ?, 'succeeded', ?, ?, ?, ?)"
)
_RETIRE_PREVIOUS = (
    "UPDATE engine_instances SET instance_state = 'stopped', base_budget_kind = 'released', "
    "stopped_at_us = ?, version = version + 1 WHERE instance_id = ? "
    "AND instance_generation = ? AND version = ?"
)
_ADVANCE_HEAD = (
    "UPDATE runtime_instance_heads SET current_generation = ?, generation_high_water = ?, "
    "current_prepare_intent_id = ?, current_stop_intent_id = NULL, version = version + 1 "
    "WHERE instance_id = ? AND version = ?"
)
_ADVANCE_POOL_EPOCH = (
    "UPDATE resource_pools SET fencing_token = ?, version = version + 1 "
    "WHERE pool_id = ? AND version = ?"
)
#: 只追加:列序与 ``GENERATION_ADOPTION_INSERT_COLUMNS`` 一致,由
#: ``tests/unit/backend/test_generation_adoption_statements.py`` 逐列钉住。
#: 表名/列名以字面量重复一次,由 ``test_generation_adoption_statements`` 对照
#: ``generation_adoption_schema`` 钉住;受限方言检查禁止 f-string 拼 SQL。
_AUDIT_INSERT = (
    "INSERT INTO runtime_generation_adoptions ("
    "instance_id, adopted_generation, ledger_generation, node_high_water, "
    "node_last_fencing_token, previous_stop_evidence, operator_id, adopted_at_us, document"
    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
)
_AUDIT_READ = (
    "SELECT * FROM runtime_generation_adoptions WHERE instance_id = ? AND adopted_generation = ?"
)


@dataclass(frozen=True, slots=True)
class AdoptionRequest:
    """Everything the transaction decides with, fixed before it takes any lock."""

    instance_id: str
    node_id: str
    operator_id: str
    node_head: NodeInstanceHead
    accept_missing_stop_evidence: bool = False
    readiness_ttl_seconds: int = 86_400
    intent_deadline_seconds: int = 300


@dataclass(frozen=True, slots=True)
class AdoptionResult:
    """The committed facts, or the aligned state that needed no write."""

    outcome: str
    instance_id: str
    ledger_generation: int
    adopted_generation: int | None
    previous_stop_evidence: str | None
    intent_id: str | None
    fencing_token: int | None
    pool_fencing_token_before: int
    pool_fencing_token_after: int
    adopted_at_us: int | None
    node_head: NodeInstanceHead
    reason: str
    evidence_digest: str | None = None
    evidence_document: Row = field(default_factory=dict, repr=False)
    probe: EngineProbe | None = None
    readiness_verified_at_us: int | None = None
    readiness_expires_at_us: int | None = None
    rows: Row = field(default_factory=dict, repr=False)


def adoption_intent_id(instance_id: str, generation: int) -> str:
    """The prepare intent an adoption records, named after what produced it."""
    value = f"adopt-{instance_id}-g{generation}"
    if len(value) <= 128:
        return value
    digest = hashlib.sha256(value.encode()).hexdigest()[:48]
    return f"adopt-{digest}"


def _fail(code: str, message: str) -> RegistrationError:
    return RegistrationError(code, message, _EXIT_BY_CODE.get(code, EXIT_CONFLICT))


def _changed(cursor: AdoptionCursor, query: str, parameters: tuple[object, ...]) -> None:
    cursor.execute(query, parameters)
    if cursor.rowcount != 1:
        raise _fail(
            "version_conflict",
            "the row this adoption tried to change moved underneath it; nothing was committed "
            "-- re-run to observe the current state",
        )


def _decision(ledger_generation: int, request: AdoptionRequest) -> GenerationAdoptionDecision:
    return decide_generation_adoption(
        ledger_generation,
        request.node_head,
        node_id=request.node_id,
        accept_missing_stop_evidence=request.accept_missing_stop_evidence,
    )


def _audit_document(
    *,
    request: AdoptionRequest,
    decision: GenerationAdoptionDecision,
    ledger: Row,
    previous: Row,
    plan: ApprovedRuntimePlan,
    intent_id: str,
    fencing_token: int,
    pool_before: int,
    adopted_at_us: int,
    evidence_digest: str,
    evidence_document: Row,
    probe: EngineProbe,
) -> str:
    """The immutable record of what this run was allowed to do and on what evidence."""
    document = {
        "kind": ADOPTION_KIND,
        "reason": decision.reason,
        "branch": decision.previous_stop_evidence,
        "accept_missing_stop_evidence": request.accept_missing_stop_evidence,
        "operator_id": request.operator_id,
        "node": {
            "node_id": request.node_id,
            "instance_id": request.node_head.instance_id,
            "current_generation": request.node_head.current_generation,
            "generation_high_water": request.node_head.generation_high_water,
            "last_fencing_token": request.node_head.last_fencing_token,
            "stopped_generations": list(request.node_head.stopped_generations),
            "voided_generations": list(request.node_head.voided_generations),
        },
        "ledger_before": {
            "current_generation": int(ledger["current_generation"]),
            "generation_high_water": int(ledger["generation_high_water"]),
            "head_version": int(ledger["version"]),
            "current_prepare_intent_id": ledger["current_prepare_intent_id"],
            "current_stop_intent_id": ledger["current_stop_intent_id"],
            "instance_state": previous["instance_state"],
            "base_budget_kind": previous["base_budget_kind"],
            "held_slots": int(previous["held_slots"]),
            "quarantined_slots": int(previous["quarantined_slots"]),
        },
        "pool_epoch": {"before": pool_before, "after": fencing_token},
        "intent_id": intent_id,
        "adopted_generation": decision.adopted_generation,
        "adopted_at_us": adopted_at_us,
        "plan_binding": {
            "target_node_id": plan.binding.target_node_id,
            "pool_id": plan.binding.pool_id,
            "gateway_id": plan.binding.gateway_id,
            "approved_template_id": plan.binding.approved_template_id,
            "binding_revision_id": plan.binding.binding_revision_id,
            "manifest_id": plan.binding.manifest_id,
            "config_digest": plan.binding.config_digest,
        },
        "health_probe": evidence_document,
        "evidence_digest": evidence_digest,
        "probe_observed_at_us": probe.observed_at_us,
    }
    return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def adopt_in_transaction(
    cursor: AdoptionCursor,
    request: AdoptionRequest,
    *,
    probe: Callable[[], EngineProbe],
    now_us: Callable[[], int],
) -> AdoptionResult:
    """Adopt the node's generation, or answer why the ledger must not change.

    Locks are taken in the order the create path uses (pool, head, current instance,
    its intents) so an adoption and a registration of the same instance cannot
    deadlock: both walk the same parents from outside in.
    """
    instance_id = request.instance_id
    heads = _select(cursor, _HEAD_READ, (instance_id,))
    if not heads:
        raise _fail(
            "instance_not_registered",
            f"instance {instance_id} has no runtime head, so there is no ledger generation to "
            "adopt; register the instance first",
        )
    if len(heads) > 1:
        raise _fail("reconciliation_required", "more than one head row for one instance")
    ledger = heads[0]
    ledger_generation = int(ledger["current_generation"])
    located = _select(cursor, _INSTANCE_LOCATE, (instance_id, ledger_generation))
    if len(located) != 1:
        raise _fail(
            "reconciliation_required",
            f"the ledger's generation {ledger_generation} has no engine_instances row; the "
            "ledger cannot be repaired by adoption until that is resolved",
        )
    pool_id = cast(str, located[0]["pool_id"])
    pool = _select_one(cursor, _POOL_LOCK, (pool_id,))
    head = _select_one(cursor, _HEAD_LOCK, (instance_id,))
    if int(head["current_generation"]) != ledger_generation or int(head["version"]) != int(
        ledger["version"]
    ):
        raise _fail(
            "version_conflict",
            "the ledger head moved while this adoption was planning; re-run it",
        )
    previous = _select_one(cursor, _INSTANCE_LOCK, (instance_id, ledger_generation))
    if head["current_stop_intent_id"] is not None:
        raise _fail(
            "instance_not_ready",
            f"the ledger's current generation {ledger_generation} has an outstanding stop "
            f"intent {head['current_stop_intent_id']}; finish or reconcile that stop before "
            "adopting a generation, or the two repairs will disagree",
        )
    prepare_intent_id = head["current_prepare_intent_id"]
    if not isinstance(prepare_intent_id, str) or not prepare_intent_id:
        raise _fail(
            "reconciliation_required",
            f"the ledger's current generation {ledger_generation} has no prepare intent, so "
            "there is no approved plan to copy and nothing the adopted generation could run",
        )
    record_row = _select_one(
        cursor, _INTENT_LOCK, (cast(str, previous["host_node_id"]), prepare_intent_id)
    )
    try:
        prepared_request, plan, _creator = decode_record(cast(str, record_row["request_document"]))
    except ValueError as error:
        raise _fail(
            "reconciliation_required",
            f"the current generation's prepare intent does not decode ({error}); the plan "
            "cannot be copied",
        ) from None
    if (
        prepared_request.request.instance_id != instance_id
        or prepared_request.request.instance_generation != ledger_generation
        or record_row["action"] != "prepare"
        or not plan_matches_row(previous, plan)
    ):
        raise _fail(
            "reconciliation_required",
            f"the ledger's generation {ledger_generation} row and the intent {prepare_intent_id} "
            "that names its plan disagree; copying either one would carry the disagreement "
            "into the adopted generation",
        )
    decision = _decision(ledger_generation, request)
    if decision.outcome == ADOPTION_REFUSED:
        raise _fail(
            cast(str, decision.code),
            cast(str, decision.reason),
        )
    if decision.outcome == ADOPTION_ALIGNED:
        # Nothing to write, so no probe either: an aligned run is the idempotent answer
        # to a second adoption (or to an adoption of an instance the ledger already
        # follows), not a licence to re-sign a receipt.
        return AdoptionResult(
            outcome=ADOPTION_ALIGNED,
            instance_id=instance_id,
            ledger_generation=ledger_generation,
            adopted_generation=None,
            previous_stop_evidence=None,
            intent_id=None,
            fencing_token=None,
            pool_fencing_token_before=int(pool["fencing_token"]),
            pool_fencing_token_after=int(pool["fencing_token"]),
            adopted_at_us=None,
            node_head=request.node_head,
            reason=decision.reason or "",
        )
    adopted = cast(int, decision.adopted_generation)
    fencing_token = cast(int, request.node_head.last_fencing_token)
    pool_token = int(pool["fencing_token"])
    if fencing_token < pool_token:
        raise _fail(
            "stale_fencing_token",
            f"node {request.node_id} serves generation {adopted} at fencing token "
            f"{fencing_token} but the pool's epoch is already {pool_token}; adopting a "
            "generation prepared in a spent epoch would put two writers in one epoch -- "
            "prepare a fresh generation on the node instead",
        )
    intent_id = adoption_intent_id(instance_id, adopted)
    identifier(intent_id)
    # The probe happens inside the transaction, after every lock and before the first
    # write: a probe that fails writes nothing, structurally, and the row locks are held
    # for no longer than one bounded HTTP call.
    health = probe()
    evidence_digest, evidence_document = probe_evidence_document(
        kind=PROBE_KIND,
        instance_id=instance_id,
        instance_generation=adopted,
        binding=plan.binding,
        probe=health,
        model_id=None,
    )
    now = now_us()
    observed_at = min(health.observed_at_us, now)
    adopted_at_us = now
    intent = RuntimeIntent(
        intent_id,
        plan.binding,
        instance_id,
        adopted,
        "prepare",
        fencing_token,
        plan.memory,
        RuntimeDeadline(_iso(now + request.intent_deadline_seconds * 1_000_000)),
    )
    dispatchable = prepare_request(intent)
    observation_wire = prepared_observation_wire(
        plan=plan,
        instance_id=instance_id,
        instance_generation=adopted,
        intent_id=intent_id,
        fencing_token=fencing_token,
        observed_at_us=observed_at,
    )
    prepared_observation = prepare_observation(observation_wire)
    readiness = VerifiedRuntimeReadiness(
        plan.binding,
        instance_id,
        adopted,
        fencing_token,
        plan.topology_digest,
        plan.qualification_digest,
        evidence_digest,
        now,
        now + request.readiness_ttl_seconds * 1_000_000,
    )
    readiness_document = readiness_to_document(readiness)
    validate_stored_readiness(readiness_document, intent, plan)
    _insert_instance_row(cursor, intent, plan, adopted)
    _settle_instance(cursor, instance_id=instance_id, adopted=adopted, adopted_at_us=adopted_at_us)
    _insert_adopted_intent(
        cursor,
        intent=intent,
        dispatchable=dispatchable,
        plan=plan,
        creator_id=request.operator_id,
        observation_document=prepared_observation.document,
        readiness_document=readiness_document,
        recorded_at_us=adopted_at_us,
    )
    _retire_previous(cursor, previous, ledger_generation, adopted_at_us)
    _advance_head(
        cursor,
        head=head,
        adopted=adopted,
        intent_id=intent_id,
        node_head=request.node_head,
        ledger_generation=ledger_generation,
    )
    pool_after = _advance_pool_epoch(
        cursor, pool=pool, fencing_token=fencing_token, adopted=adopted
    )
    document = _audit_document(
        request=request,
        decision=decision,
        ledger=ledger,
        previous=previous,
        plan=plan,
        intent_id=intent_id,
        fencing_token=fencing_token,
        pool_before=pool_token,
        adopted_at_us=adopted_at_us,
        evidence_digest=evidence_digest,
        evidence_document=evidence_document,
        probe=health,
    )
    _write_audit(
        cursor,
        request=request,
        adopted=adopted,
        ledger_generation=ledger_generation,
        node_head=request.node_head,
        evidence=decision.previous_stop_evidence,
        fencing_token=fencing_token,
        adopted_at_us=adopted_at_us,
        document=document,
    )
    # Read the three written surfaces back through the production readers. If the rows
    # this command wrote are not the rows admission and the readiness report read, the
    # whole transaction is rolled back rather than committing a repair nobody can use.
    rows = _verify_written(cursor, instance_id=instance_id, adopted=adopted, intent_id=intent_id)
    return AdoptionResult(
        outcome="adopted",
        instance_id=instance_id,
        ledger_generation=ledger_generation,
        adopted_generation=adopted,
        previous_stop_evidence=decision.previous_stop_evidence,
        intent_id=intent_id,
        fencing_token=fencing_token,
        pool_fencing_token_before=pool_token,
        pool_fencing_token_after=pool_after,
        adopted_at_us=adopted_at_us,
        node_head=request.node_head,
        reason=decision.reason or "",
        evidence_digest=evidence_digest,
        evidence_document=evidence_document,
        probe=health,
        readiness_verified_at_us=now,
        readiness_expires_at_us=now + request.readiness_ttl_seconds * 1_000_000,
        rows=rows,
    )


def _insert_instance_row(
    cursor: AdoptionCursor,
    intent: RuntimeIntent,
    plan: ApprovedRuntimePlan,
    adopted: int,
) -> None:
    """Copy the immutable plan into the adopted generation's row.

    The row is written through the create path's own statement, so a column added to
    ``engine_instances`` reaches this copy without a second edit here. The plan and
    the slot counters are the copied facts; the budget kind and readiness are settled
    by the update below, which is the second half of a prepare.
    """
    values: tuple[object, ...] = (
        intent.instance_id,
        adopted,
        plan.binding.target_node_id,
        plan.binding.pool_id,
        plan.binding.gateway_id,
        plan.binding.binding_revision_id,
        plan.binding.manifest_id,
        plan.binding.approved_template_id,
        "managed",
        plan.loading_policy,
        plan.binding.config_digest,
        "preparing",
        plan.max_slots,
        plan.realtime_reserved_slots,
        intent.fencing_token,
    )
    for vector in (plan.peak, plan.steady, plan.per_slot):
        values += vector.values
    cursor.execute(_INSERT_ENGINE_INSTANCE, values)


def _settle_instance(
    cursor: AdoptionCursor, *, instance_id: str, adopted: int, adopted_at_us: int
) -> None:
    """Second half of a prepare: the generation becomes ready and holds its steady.

    Written as a conditional update on the freshly inserted row (``version = 1``) so a
    concurrent writer that somehow reached the row is a conflict, not a silent
    overwrite. ``held_slots``/``quarantined_slots`` keep the insert's zero: a new
    generation holds no leases yet.
    """
    _changed(cursor, _SETTLE_INSTANCE, (adopted_at_us, instance_id, adopted))


def _insert_adopted_intent(
    cursor: AdoptionCursor,
    *,
    intent: RuntimeIntent,
    dispatchable: PreparedRuntimeRequest,
    plan: ApprovedRuntimePlan,
    creator_id: str,
    observation_document: str,
    readiness_document: str,
    recorded_at_us: int,
) -> None:
    """Record the adopted generation's prepare as already succeeded, with its receipt."""
    cursor.execute(
        _INTENT_INSERT,
        (
            intent.binding.target_node_id,
            intent.intent_id,
            intent.instance_id,
            intent.instance_generation,
            dispatchable.request_digest,
            encode_record(dispatchable, plan, creator_id),
            observation_document,
            readiness_document,
            recorded_at_us,
            recorded_at_us,
        ),
    )
    queue_intent_dispatch(cursor, dispatchable, intent, now_us=recorded_at_us)
    block_operator_attested_delivery(cursor, dispatchable.event_id, now_us=recorded_at_us)


def _retire_previous(
    cursor: AdoptionCursor, previous: Row, ledger_generation: int, now: int
) -> None:
    """Mark the ledger's generation stopped and its budget released.

    ``held_slots`` is deliberately not zeroed: admission leases opened against this
    generation still exist and still settle against this row, and the host vector did
    not move (the engine it counted is the same engine the adopted generation counts).
    """
    _changed(
        cursor,
        _RETIRE_PREVIOUS,
        (now, cast(str, previous["instance_id"]), ledger_generation, int(previous["version"])),
    )


def _advance_head(
    cursor: AdoptionCursor,
    *,
    head: Row,
    adopted: int,
    intent_id: str,
    node_head: NodeInstanceHead,
    ledger_generation: int,
) -> None:
    high_water = max(cast(int, node_head.generation_high_water), ledger_generation)
    _changed(
        cursor,
        _ADVANCE_HEAD,
        (adopted, high_water, intent_id, cast(str, head["instance_id"]), int(head["version"])),
    )


def _advance_pool_epoch(
    cursor: AdoptionCursor, *, pool: Row, fencing_token: int, adopted: int
) -> int:
    """Move the pool's epoch to the node's token when the node is ahead.

    Equal tokens need no write; a token behind the pool was refused before any write.
    """
    current = int(pool["fencing_token"])
    if fencing_token == current:
        return current
    _changed(
        cursor,
        _ADVANCE_POOL_EPOCH,
        (fencing_token, cast(str, pool["pool_id"]), int(pool["version"])),
    )
    return fencing_token


def _write_audit(
    cursor: AdoptionCursor,
    *,
    request: AdoptionRequest,
    adopted: int,
    ledger_generation: int,
    node_head: NodeInstanceHead,
    evidence: str | None,
    fencing_token: int,
    adopted_at_us: int,
    document: str,
) -> None:
    cursor.execute(
        _AUDIT_INSERT,
        (
            request.instance_id,
            adopted,
            ledger_generation,
            int(cast(int, node_head.generation_high_water)),
            fencing_token,
            evidence,
            request.operator_id,
            adopted_at_us,
            document,
        ),
    )
    rows = _select(cursor, _AUDIT_READ, (request.instance_id, adopted))
    if len(rows) != 1:
        raise _fail(
            "audit_conflict",
            "the adoption audit row was not written exactly once; nothing was committed",
        )


def _verify_written(
    cursor: AdoptionCursor, *, instance_id: str, adopted: int, intent_id: str
) -> Row:
    """Re-read the writes through the readers admission and the report use."""
    instance_row = _select_one(cursor, _INSTANCE_LOCK, (instance_id, adopted))
    intent_row = _select_one(
        cursor, _INTENT_LOCK, (cast(str, instance_row["host_node_id"]), intent_id)
    )
    head_row = _select_one(cursor, _HEAD_READ, (instance_id,))
    audit_row = _select_one(cursor, _AUDIT_READ, (instance_id, adopted))
    try:
        record = _record(cast(Any, intent_row))
        _instance(cast(Any, instance_row), record)
    except RuntimeRejectedError as error:
        raise _fail(
            "reconciliation_required",
            f"the adopted generation's rows do not read back as an approved plan ({error.code}); "
            "the repair was rolled back rather than committed in a form admission would refuse",
        ) from None
    if (
        record.intent_state != "succeeded"
        or record.readiness_document is None
        or record.observation_document is None
    ):
        raise _fail(
            "reconciliation_required",
            "the adopted generation's prepare is not recorded as a succeeded observation with "
            "a readiness receipt",
        )
    if int(head_row["current_generation"]) != adopted or intent_row["intent_id"] != intent_id:
        raise _fail(
            "reconciliation_required",
            "the head does not point at the adopted generation and its prepare intent",
        )
    return {
        "runtime_instance_heads": head_row,
        "engine_instances": instance_row,
        "runtime_instance_intents": intent_row,
        "runtime_generation_adoptions": audit_row,
    }


def adopt_generation(
    *,
    database_url: str,
    schema: str,
    instance_id: str,
    node_id: str,
    node_head: NodeInstanceHead,
    engine_url: str = "",
    operator_id: str = "speechnexus-operator",
    accept_missing_stop_evidence: bool = False,
    readiness_ttl_seconds: int = 86_400,
    intent_deadline_seconds: int = 300,
    health_timeout_seconds: float = 5.0,
    transaction_timeout_seconds: float = 10.0,
    connect_timeout_seconds: int = 5,
    probe: Callable[[], EngineProbe] | None = None,
) -> AdoptionResult:
    """Run one audited adoption in one database transaction."""
    if not re.fullmatch(r"[a-z][a-z0-9_]{2,62}", schema):
        raise _fail("invalid_schema", "schema name is invalid")
    try:
        identifier(instance_id)
        identifier(node_id)
        identifier(operator_id)
    except ValueError:
        raise _fail(
            "invalid_instance",
            "--instance, --node and --operator-id must be lowercase runtime identifiers",
        ) from None
    if not 0 < readiness_ttl_seconds <= 31_536_000:
        raise _fail("invalid_readiness_ttl", "readiness TTL is out of range")
    if not 1 <= intent_deadline_seconds <= 3_600:
        raise _fail("invalid_intent_deadline", "intent deadline is out of range")
    if not 0 < health_timeout_seconds <= 30:
        raise _fail("invalid_health_timeout", "health timeout is out of range")
    if not 0.5 <= transaction_timeout_seconds <= 120:
        raise _fail("invalid_transaction_timeout", "transaction timeout is out of range")
    chosen_probe: Callable[[], EngineProbe] = probe or (
        lambda: probe_engine(engine_url, timeout_seconds=health_timeout_seconds)
    )
    request = AdoptionRequest(
        instance_id=instance_id,
        node_id=node_id,
        operator_id=operator_id,
        node_head=node_head,
        accept_missing_stop_evidence=accept_missing_stop_evidence,
        readiness_ttl_seconds=readiness_ttl_seconds,
        intent_deadline_seconds=intent_deadline_seconds,
    )
    connection: PortableConnection | None = None
    try:
        connection = portable_connect(
            database_url,
            row_factory=dict_row,
            connect_timeout=connect_timeout_seconds,
        )
        with connection.cursor() as raw:
            cursor = _Cursor(raw, time.monotonic() + transaction_timeout_seconds)
            cursor.execute(
                "SELECT set_config('search_path', ?, true)",
                (schema + ",pg_catalog",),
            )
            cursor.execute("SELECT set_config('TimeZone', 'UTC', true)")
            _require_adoption_schema(cast(AdoptionCursor, cursor))
            result = adopt_in_transaction(
                cast(AdoptionCursor, cursor),
                request,
                probe=chosen_probe,
                now_us=cursor.clock,
            )
        connection.commit()
        return result
    except RegistrationError:
        if connection is not None:
            connection.rollback()
        raise
    except RuntimeRejectedError as error:
        if connection is not None:
            connection.rollback()
        raise _fail(
            error.code,
            f"the coordination transaction rejected the adoption ({error.code})",
        ) from None
    except psycopg.errors.UndefinedTable:
        if connection is not None:
            connection.rollback()
        raise _fail(
            "schema_not_migrated",
            "the coordination or adoption tables are missing; run speechnexus-job-migrate for "
            "this schema first (0071_generation_adoptions)",
        ) from None
    except psycopg.errors.UniqueViolation:
        if connection is not None:
            connection.rollback()
        raise _fail(
            "audit_conflict",
            "another adoption committed the same instance and generation; re-run to observe it",
        ) from None
    except psycopg.Error:
        if connection is not None:
            connection.rollback()
        raise _fail(
            "database_unavailable",
            "database transaction failed; no adoption was committed",
        ) from None
    finally:
        if connection is not None:
            connection.close()


def _require_adoption_schema(cursor: AdoptionCursor) -> None:
    cursor.execute("SELECT to_regclass('runtime_generation_adoptions') AS adoption")
    rows = cursor.fetchall()
    if not rows or rows[0]["adoption"] is None:
        raise _fail(
            "schema_not_migrated",
            "table runtime_generation_adoptions is missing; run speechnexus-job-migrate for "
            "this schema first (0071_generation_adoptions)",
        )


def _result_json(result: AdoptionResult) -> str:
    return json.dumps(
        {
            "outcome": result.outcome,
            "instance_id": result.instance_id,
            "ledger_generation": result.ledger_generation,
            "adopted_generation": result.adopted_generation,
            "previous_stop_evidence": result.previous_stop_evidence,
            "intent_id": result.intent_id,
            "fencing_token": result.fencing_token,
            "pool_fencing_token": {
                "before": result.pool_fencing_token_before,
                "after": result.pool_fencing_token_after,
            },
            "adopted_at_us": result.adopted_at_us,
            "reason": result.reason,
            "node_head": {
                "instance_id": result.node_head.instance_id,
                "current_generation": result.node_head.current_generation,
                "generation_high_water": result.node_head.generation_high_water,
                "last_fencing_token": result.node_head.last_fencing_token,
                "stopped_generations": list(result.node_head.stopped_generations),
                "voided_generations": list(result.node_head.voided_generations),
            },
            "readiness": {
                "verified_at_us": result.readiness_verified_at_us,
                "expires_at_us": result.readiness_expires_at_us,
                "evidence_digest": result.evidence_digest,
            },
            "health_probe": {
                "url": result.probe.url,
                "status": result.probe.status,
                "content_type": result.probe.content_type,
                "body_sha256": result.probe.body_sha256,
                "body_bytes": result.probe.body_bytes,
                "observed_at_us": result.probe.observed_at_us,
            }
            if result.probe is not None
            else None,
            "rows": result.rows,
        },
        indent=2,
        sort_keys=True,
        default=str,
    )


def _settings(env: Mapping[str, str], args: argparse.Namespace) -> tuple[str, str, str]:
    database_url = args.database_url or env.get("SPEECHNEXUS_DATABASE_URL", "")
    if not database_url:
        raise _fail("invalid_database_url", "set --database-url or SPEECHNEXUS_DATABASE_URL")
    schema = args.schema or env.get("SPEECHNEXUS_DATABASE_SCHEMA", "speech")
    engine_url = args.engine_url or env.get("SPEECHNEXUS_ENGINE_HEALTH_URL", "")
    if not args.engine_url and not engine_url:
        raise _fail(
            "invalid_engine_url",
            "set --engine-url or SPEECHNEXUS_ENGINE_HEALTH_URL: the adopted generation's receipt "
            "has to come from a real health probe",
        )
    return database_url.strip(), schema.strip(), engine_url.strip()


def _report(result: AdoptionResult) -> None:
    """Operator notes on stderr; the machine-readable document on stdout is unchanged."""
    if result.outcome == ADOPTION_ALIGNED:
        sys.stderr.write(
            f"note: {result.instance_id} is already at generation {result.ledger_generation} "
            "in the ledger and on the node; nothing was written\n"
        )
        return
    if result.previous_stop_evidence == PREVIOUS_STOP_MISSING:
        sys.stderr.write(
            f"warning: generation {result.ledger_generation} of {result.instance_id} was retired "
            "without node stop evidence (--accept-missing-stop-evidence); the audit row records "
            "previous_stop_evidence=missing\n"
        )
    sys.stderr.write(
        f"adopted generation {result.adopted_generation} for {result.instance_id}: the ledger now "
        f"follows the node at fencing token {result.fencing_token}; re-run "
        "speechnexus-register-engine --renew-readiness to re-sign the receipt\n"
    )


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="speechnexus-adopt-generation",
        description=(
            "Adopt the generation the node controller actually prepared into the coordination "
            "ledger, with an audit row. This does not restart the engine and does not bypass "
            "the registration path's stop-evidence requirement: it substitutes the node's own "
            "testimony for the evidence the ledger is missing."
        ),
    )
    parser.add_argument("--database-url", help="coordination database DSN (or env)")
    parser.add_argument("--schema", help="coordination schema (default: speech)")
    parser.add_argument("--engine-url", help="engine health origin or /health URL (or env)")
    parser.add_argument("--instance", required=True, help="instance identity to adopt")
    parser.add_argument("--node", required=True, help="node id that runs the instance")
    parser.add_argument(
        "--operator-id",
        default="speechnexus-operator",
        help="identity recorded as the auditor of this adoption",
    )
    parser.add_argument(
        "--accept-missing-stop-evidence",
        action="store_true",
        help=(
            "retire the ledger's generation even though the node reports no stop or voided "
            "prepare for it; the audit row records previous_stop_evidence=missing"
        ),
    )
    parser.add_argument("--readiness-ttl-seconds", type=int, default=86_400)
    parser.add_argument("--intent-deadline-seconds", type=int, default=300)
    parser.add_argument("--health-timeout-seconds", type=float, default=5.0)
    parser.add_argument("--transaction-timeout-seconds", type=float, default=10.0)
    parser.add_argument("--connect-timeout-seconds", type=int, default=5)
    parser.add_argument("--controller-timeout-seconds", type=float, default=5.0)
    args = parser.parse_args(argv)
    env = os.environ
    try:
        database_url, schema, engine_url = _settings(env, args)
        endpoint = configured_endpoint(env)
        if endpoint is None:
            raise _fail(
                "controller_not_configured",
                "adoption is only allowed on node testimony, and no controller endpoint is "
                "configured; set SPEECHNEXUS_CONTROLLER_API_URL and "
                "SPEECHNEXUS_CONTROLLER_API_TOKEN so the node can be asked which generation it "
                "prepared",
            )
        head = read_head(
            endpoint,
            node_id=args.node,
            instance_id=args.instance,
            timeout_seconds=args.controller_timeout_seconds,
        )
        if head.instance_id != args.instance:
            raise _fail(
                "controller_head_invalid",
                f"the controller answered for instance {head.instance_id}, not {args.instance}",
            )
        result = adopt_generation(
            database_url=database_url,
            schema=schema,
            instance_id=args.instance,
            node_id=args.node,
            node_head=head,
            engine_url=engine_url,
            operator_id=args.operator_id,
            accept_missing_stop_evidence=args.accept_missing_stop_evidence,
            readiness_ttl_seconds=args.readiness_ttl_seconds,
            intent_deadline_seconds=args.intent_deadline_seconds,
            health_timeout_seconds=args.health_timeout_seconds,
            transaction_timeout_seconds=args.transaction_timeout_seconds,
            connect_timeout_seconds=args.connect_timeout_seconds,
        )
    except RegistrationError as error:
        parser.exit(error.exit_code, f"error: {error.code}: {error.message}\n")
    except ControllerConsultationError as error:
        parser.exit(
            _EXIT_BY_CODE.get(error.code, EXIT_UNAVAILABLE),
            f"error: {error.code}: {error.message}\n",
        )
    except (ValueError, TypeError) as error:
        parser.exit(EXIT_INVALID, f"error: invalid_request: {error}\n")
    _report(result)
    sys.stdout.write(_result_json(result) + "\n")


if __name__ == "__main__":
    main()
