"""Explicit operator registration for one already-running engine instance.

The command provisions the relational facts admission reads (host, pool, quota,
instance, head, prepare intent, readiness receipt and outbox evidence) in one
transaction. It is an operator attestation backed by a live ``/health`` probe,
not a node-controller observation and not model/hardware qualification.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen

from speechnexus_backend.controller_head import (
    ControllerConsultationError,
    NodeInstanceHead,
    configured_endpoint,
    generation_disagreement,
    read_head,
)
from speechnexus_backend.speech.domain.capacity import PhysicalResources
from speechnexus_backend.speech.domain.runtime_intents import (
    ApprovedRuntimePlan,
    CreateRuntimeIntent,
    RuntimeBinding,
    RuntimeDeadline,
    RuntimeIntent,
    RuntimeRejectedError,
    identifier,
)
from speechnexus_backend.speech.domain.runtime_observations import (
    READINESS_EXPIRED,
    READINESS_EXPIRING,
    ObserveRuntimeIntent,
    VerifiedRuntimeReadiness,
)
from speechnexus_backend.speech.infrastructure.readiness_void_schema import (
    READINESS_VOID_INSERT_COLUMNS,
    READINESS_VOID_REASON_GENERATION_MISMATCH,
    READINESS_VOID_SOURCE_RENEWAL,
    READINESS_VOID_TABLE,
)
from speechnexus_backend.speech.infrastructure.runtime_codec import (
    decode_record,
    prepare_request,
)
from speechnexus_backend.speech.infrastructure.runtime_observation_codec import (
    prepare_observation,
    readiness_from_document,
    readiness_to_document,
    validate_stored_readiness,
)
from speechnexus_backend.speech.infrastructure.runtime_observation_sql import (
    observe_in_transaction,
)
from speechnexus_backend.speech.infrastructure.runtime_sql import (
    block_operator_attested_delivery,
    create_in_transaction,
)
from speechnexus_backend.speech.infrastructure.transcription_recipe import (
    TranscriptionRecipe,
    load_transcription_recipe,
)
from speechnexus_portable_sql.clock import ClockReader
from speechnexus_portable_sql.clock import now_us as now_us_for
from speechnexus_portable_sql.psycopg_driver import PortableConnection
from speechnexus_portable_sql.psycopg_driver import connect as portable_connect
from speechnexus_portable_sql.transaction import insert_if_absent, is_savepoint_control
from speechnexus_portable_sql.vendor import dict_row, psycopg

EXIT_INVALID = 2
EXIT_CONFLICT = 4
EXIT_UNAVAILABLE = 5
_SCHEMA = r"[a-z][a-z0-9_]{2,62}"
_MAX_PROBE_BYTES = 65_536
_DIMENSIONS = (
    "unified_physical_memory_bytes",
    "device_memory_bytes",
    "host_memory_bytes",
    "cpu_decode_millicores",
    "host_pcm_buffer_bytes",
    "scratch_disk_bytes",
)
# 列清单写字面量而不是运行时拼:受限方言不允许动态语句,而且拼出来的占位符混用 `?` 与 `%s`。
# `_DIMENSIONS` 与这里的一致性由 tests/unit/backend/test_engine_registration_statements.py 钉住。
HOST_INSERT = (
    "INSERT INTO host_nodes (host_node_id, memory_architecture, node_state, topology_digest, "
    "allocatable_unified_physical_memory_bytes, allocatable_device_memory_bytes, "
    "allocatable_host_memory_bytes, allocatable_cpu_decode_millicores, "
    "allocatable_host_pcm_buffer_bytes, allocatable_scratch_disk_bytes) "
    "VALUES (?, ?, 'active', ?, ?, ?, ?, ?, ?, ?)"
)
POOL_INSERT = (
    "INSERT INTO resource_pools "
    "(pool_id, service_class, pool_state, owner_id, fencing_token, "
    "owner_expires_at_us, owner_state) VALUES (?, ?, 'active', NULL, 0, 0, "
    "'reconciling_takeover')"
)
# 「插入即读」:配额行由这个工作区的第一次注册建立,之后每次注册都读同一行并锁住它。
# 语句里没有 `ON CONFLICT`:竞争下较晚的写者撞唯一索引,由 `insert_if_absent` 的保存点吸掉,
# 事务继续可用,随后的 `SELECT ... FOR UPDATE` 等在先到者提交后读到它那一行。
QUOTA_INSERT = (
    "INSERT INTO workspace_quotas "
    "(workspace_id, max_attempts, held_attempts, quarantined_attempts, policy_revision) "
    "VALUES (?, ?, 0, 0, ?)"
)

type Row = dict[str, Any]


class RegistrationError(Exception):
    """Stable operator-facing failure; the transaction is always rolled back."""

    def __init__(self, code: str, message: str, exit_code: int = EXIT_CONFLICT) -> None:
        self.code = code
        self.message = message
        self.exit_code = exit_code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class EngineProbe:
    url: str
    status: int
    content_type: str
    body_sha256: str
    body_bytes: int
    observed_at_us: int


@dataclass(frozen=True, slots=True)
class RegistrationSpec:
    """All immutable facts needed to register one instance generation."""

    model_id: str
    workspace_id: str
    policy_revision_id: str
    instance_id: str
    generation: int
    plan: ApprovedRuntimePlan
    host_capacity: PhysicalResources
    max_attempts: int


@dataclass(frozen=True, slots=True)
class CapacityHeadroom:
    """What a committed registration leaves for one more instance of the same shape."""

    remaining: PhysicalResources
    required: PhysicalResources


@dataclass(frozen=True, slots=True)
class RegistrationResult:
    outcome: str
    model_id: str
    instance_id: str
    instance_generation: int
    intent_id: str
    target_node_id: str
    pool_id: str
    gateway_id: str
    fencing_token: int
    owner_id: str
    workspace_id: str
    ready_at_us: int | None
    readiness_verified_at_us: int
    readiness_expires_at_us: int
    evidence_digest: str
    evidence_document: Row
    probe: EngineProbe
    rows: Row = field(repr=False)
    # How much of the receipt's window was left before this run, and whether this
    # run re-signed it. A renewal is only observable if the command reports the
    # state it found, so `readiness_state` is the fact an operator alerts on.
    readiness_state: str = "valid"
    readiness_renewed: bool = False
    # Set only when this host cannot hold one more instance of this shape, so the
    # command can warn at the first registration instead of failing at the second.
    capacity_headroom: CapacityHeadroom | None = None


def _vector(text: str) -> PhysicalResources:
    parts = [part.strip() for part in text.split(",")]
    if len(parts) != len(_DIMENSIONS):
        raise argparse.ArgumentTypeError(
            "capacity vectors require six comma-separated integers: "
            "unified,device,host,cpu_millicores,pcm_buffer,scratch"
        )
    try:
        values = [int(part) for part in parts]
    except ValueError as error:
        raise argparse.ArgumentTypeError("capacity vectors must be decimal integers") from error
    try:
        return PhysicalResources(*values)
    except ValueError as error:
        raise argparse.ArgumentTypeError("capacity vectors must be non-negative") from error


def _positive(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("value must be a positive integer") from error
    if parsed < 1:
        raise argparse.ArgumentTypeError("value must be a positive integer")
    return parsed


def _vector_text(vector: PhysicalResources) -> str:
    return ",".join(str(value) for value in vector.values)


def _needed_capacity(plan: ApprovedRuntimePlan) -> PhysicalResources:
    slots = plan.steady.add(plan.per_slot.scaled(plan.max_slots))
    return PhysicalResources(
        *(max(slot, peak) for slot, peak in zip(slots.values, plan.peak.values, strict=True))
    )


def _default_capacity(plan: ApprovedRuntimePlan) -> PhysicalResources:
    return _needed_capacity(plan)


def _probe_url(engine_url: str) -> str:
    parsed = urlsplit(engine_url)
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise RegistrationError(
            "invalid_engine_url",
            "engine URL must be an http(s) origin without credentials",
            EXIT_INVALID,
        )
    path = parsed.path or "/"
    if path == "/":
        path = "/health"
    return urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, ""))


def probe_engine(engine_url: str, *, timeout_seconds: float = 5.0) -> EngineProbe:
    """Probe the operator-supplied engine health endpoint; never retry silently."""
    if not 0 < timeout_seconds <= 30:
        raise RegistrationError(
            "invalid_health_timeout", "health timeout is out of range", EXIT_INVALID
        )
    url = _probe_url(engine_url)
    request = Request(url, headers={"Accept": "application/json, text/plain;q=0.9"})
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            status = int(response.status)
            body = response.read(_MAX_PROBE_BYTES + 1)
            content_type = response.headers.get("Content-Type", "")
    except HTTPError as error:
        raise RegistrationError(
            "engine_unhealthy",
            f"health endpoint returned HTTP {error.code}",
            EXIT_UNAVAILABLE,
        ) from None
    except URLError, TimeoutError, OSError:
        raise RegistrationError(
            "engine_unreachable",
            "health endpoint is unreachable from this operator host",
            EXIT_UNAVAILABLE,
        ) from None
    if status != 200:
        raise RegistrationError(
            "engine_unhealthy",
            f"health endpoint returned HTTP {status}",
            EXIT_UNAVAILABLE,
        )
    if len(body) > _MAX_PROBE_BYTES:
        raise RegistrationError(
            "engine_health_too_large",
            "health response exceeds the bounded probe size",
            EXIT_UNAVAILABLE,
        )
    return EngineProbe(
        url=url,
        status=status,
        content_type=content_type[:128],
        body_sha256="sha256:" + hashlib.sha256(body).hexdigest(),
        body_bytes=len(body),
        observed_at_us=time.time_ns() // 1_000,
    )


def prepared_observation_wire(
    *,
    plan: ApprovedRuntimePlan,
    instance_id: str,
    instance_generation: int,
    intent_id: str,
    fencing_token: int,
    observed_at_us: int,
) -> Row:
    """The succeeded prepare observation one ready generation is recorded with.

    One definition point for a fact two commands write: the registration command has
    just probed the engine, and the audited adoption command is recording that the same
    plan is serving under the node's generation. Both must produce a document the
    existing decoder and the future reader accept, and a second copy of this shape is
    exactly how one of them silently stops decoding (#1282's sibling risk).
    """
    return {
        "intent_id": intent_id,
        "target_node_id": plan.binding.target_node_id,
        "pool_id": plan.binding.pool_id,
        "gateway_id": plan.binding.gateway_id,
        "approved_template_id": plan.binding.approved_template_id,
        "binding_revision_id": plan.binding.binding_revision_id,
        "manifest_id": plan.binding.manifest_id,
        "config_digest": plan.binding.config_digest,
        "instance_id": instance_id,
        "instance_generation": instance_generation,
        "action": "prepare",
        "fencing_token": fencing_token,
        "intent_state": "succeeded",
        "observed_instance_state": "ready",
        "reserved_memory": {
            "architecture": plan.memory.architecture,
            "peak": _memory_wire(plan.memory.peak),
            "steady": _memory_wire(plan.memory.steady),
        },
        "observed_peak_memory": _memory_wire(plan.peak),
        "created_at": _iso(observed_at_us),
        "updated_at": _iso(observed_at_us),
    }


def probe_evidence_document(
    *,
    kind: str,
    instance_id: str,
    instance_generation: int,
    binding: RuntimeBinding,
    probe: EngineProbe,
    model_id: str | None,
) -> tuple[str, Row]:
    """The bounded evidence document one health probe is recorded as.

    One definition point for every command that attests an engine generation with a
    live probe: the registration command and the audited adoption command cite the
    same facts in the same shape, and the only thing that differs is ``kind`` (which
    is how a reader tells which command signed it). ``model_id`` is null for a repair
    that copies an already-registered plan: the ledger's ``engine_instances`` row does
    not carry a model identity, and inventing one from the recipe would put a claim in
    the evidence that the command never read.
    """
    document: Row = {
        "kind": kind,
        "model_id": model_id,
        "instance_id": instance_id,
        "instance_generation": instance_generation,
        "target_node_id": binding.target_node_id,
        "pool_id": binding.pool_id,
        "gateway_id": binding.gateway_id,
        "probe_url": probe.url,
        "http_status": probe.status,
        "content_type": probe.content_type,
        "body_sha256": probe.body_sha256,
        "body_bytes": probe.body_bytes,
        "observed_at_us": probe.observed_at_us,
    }
    text = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest(), document


def _evidence(spec: RegistrationSpec, probe: EngineProbe) -> tuple[str, Row]:
    return probe_evidence_document(
        kind="operator_engine_health_probe_v1",
        instance_id=spec.instance_id,
        instance_generation=spec.generation,
        binding=spec.plan.binding,
        probe=probe,
        model_id=spec.model_id,
    )


def _iso(us: int) -> str:
    seconds, micros = divmod(us, 1_000_000)
    return (
        datetime.fromtimestamp(seconds, UTC)
        .replace(microsecond=micros)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _intent_id(instance_id: str, generation: int) -> str:
    value = f"register-{instance_id}-g{generation}"
    if len(value) <= 128:
        return value
    digest = hashlib.sha256(value.encode()).hexdigest()[:48]
    return f"register-{digest}"


class _Cursor:
    def __init__(self, cursor: Any, deadline: float) -> None:
        self._cursor = cursor
        self._deadline = deadline

    @property
    def rowcount(self) -> int:
        return cast(int, self._cursor.rowcount)

    def execute(self, query: str, parameters: tuple[object, ...] = ()) -> object:
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise RegistrationError(
                "transaction_timeout",
                "registration transaction exceeded its bounded deadline",
                EXIT_UNAVAILABLE,
            )
        if is_savepoint_control(query):
            return self._cursor.execute(query, parameters)
        self._cursor.execute(
            "SELECT set_config('statement_timeout', ?, true)",
            (str(max(1, int(remaining * 1000))),),
        )
        return self._cursor.execute(query, parameters)

    def fetchall(self) -> list[Row]:
        return cast(list[Row], list(self._cursor.fetchall()))

    def fetchone(self) -> Row | None:
        rows = self.fetchall()
        return rows[0] if rows else None

    def clock(self) -> int:
        # 「现在」只从便携时钟读一次;语句里不再有品牌函数。
        return now_us_for(cast(ClockReader, self))


def _rows(cursor: _Cursor, query: str, parameters: tuple[object, ...] = ()) -> list[Row]:
    cursor.execute(query, parameters)
    return cursor.fetchall()


def _one(cursor: _Cursor, query: str, parameters: tuple[object, ...]) -> Row:
    rows = _rows(cursor, query, parameters)
    if len(rows) != 1:
        raise RegistrationError(
            "reconciliation_required",
            "expected exactly one coordination row",
        )
    return rows[0]


def _vector_from(row: Row, prefix: str) -> PhysicalResources:
    return PhysicalResources(*(int(row[prefix + name]) for name in _DIMENSIONS))


def _require_schema(cursor: _Cursor) -> None:
    row = _one(
        cursor,
        "SELECT to_regclass('engine_instances') AS engine, "
        "to_regclass('runtime_instance_heads') AS heads, "
        "to_regclass('runtime_instance_intents') AS intents",
        (),
    )
    if any(row[name] is None for name in ("engine", "heads", "intents")):
        raise RegistrationError(
            "schema_not_migrated",
            "coordination tables are missing; run speechnexus-job-migrate for this schema first",
            EXIT_UNAVAILABLE,
        )


def _ensure_host(cursor: _Cursor, spec: RegistrationSpec) -> Row:
    plan = spec.plan
    binding = plan.binding
    insert_if_absent(
        cursor,
        HOST_INSERT,
        (
            binding.target_node_id,
            plan.memory_architecture,
            plan.topology_digest,
            *spec.host_capacity.values,
        ),
    )
    row = _one(
        cursor,
        "SELECT * FROM host_nodes WHERE host_node_id = ? FOR UPDATE",
        (binding.target_node_id,),
    )
    if row["memory_architecture"] != plan.memory_architecture:
        raise RegistrationError(
            "host_architecture_mismatch",
            "existing host memory architecture does not match the runtime plan",
        )
    if row["topology_digest"] != plan.topology_digest:
        raise RegistrationError(
            "host_topology_mismatch",
            "existing host topology digest does not match the runtime plan",
        )
    if row["node_state"] == "quarantined":
        raise RegistrationError("host_quarantined", "existing host is quarantined")
    if row["node_state"] != "active":
        cursor.execute(
            "UPDATE host_nodes SET node_state = 'active', version = version + 1 "
            "WHERE host_node_id = ? AND version = ?",
            (binding.target_node_id, row["version"]),
        )
        if cursor.rowcount != 1:
            raise RegistrationError("version_conflict", "host version changed during activation")
        row["node_state"] = "active"
    return row


def _ensure_pool(
    cursor: _Cursor,
    spec: RegistrationSpec,
    *,
    owner_id: str,
    fencing_token: int | None,
    owner_ttl_us: int,
) -> Row:
    binding = spec.plan.binding
    insert_if_absent(
        cursor,
        POOL_INSERT,
        (binding.pool_id, spec.plan.service_class),
    )
    row = _one(
        cursor,
        "SELECT * FROM resource_pools WHERE pool_id = ? FOR UPDATE",
        (binding.pool_id,),
    )
    if row["service_class"] != spec.plan.service_class:
        raise RegistrationError(
            "pool_service_class_conflict",
            "existing pool service class does not match the runtime plan",
        )
    now = cursor.clock()
    current_token = int(row["fencing_token"])
    if row["owner_id"] is None:
        token = fencing_token if fencing_token is not None else max(1, current_token + 1)
    elif row["owner_id"] == owner_id:
        if current_token < 1:
            raise RegistrationError("stale_fencing_token", "existing owner has no fencing token")
        token = current_token
        if fencing_token is not None and fencing_token != token:
            raise RegistrationError(
                "fencing_token_conflict",
                f"pool is owned by fencing token {token}; refusing to reinterpret it",
            )
    elif int(row["owner_expires_at_us"]) <= now:
        token = fencing_token if fencing_token is not None else current_token + 1
        if token <= current_token:
            raise RegistrationError(
                "stale_fencing_token",
                f"takeover fencing token must exceed {current_token}",
            )
    else:
        raise RegistrationError(
            "pool_owner_conflict",
            "pool is owned by another live owner; stop that owner before registering",
        )
    expires = now + owner_ttl_us
    unchanged = (
        row["owner_id"] == owner_id
        and current_token == token
        and row["owner_state"] == "active"
        and row["pool_state"] == "active"
        and int(row["owner_expires_at_us"]) > now + owner_ttl_us // 2
    )
    if not unchanged:
        cursor.execute(
            "UPDATE resource_pools SET owner_id = ?, fencing_token = ?, "
            "owner_expires_at_us = ?, owner_state = 'active', pool_state = 'active', "
            "version = version + 1 WHERE pool_id = ? AND version = ?",
            (owner_id, token, expires, binding.pool_id, row["version"]),
        )
        if cursor.rowcount != 1:
            raise RegistrationError("version_conflict", "pool version changed during registration")
        row["owner_id"] = owner_id
        row["fencing_token"] = token
        row["owner_expires_at_us"] = expires
        row["owner_state"] = "active"
        row["pool_state"] = "active"
    return row


def _ensure_quota(cursor: _Cursor, spec: RegistrationSpec) -> Row:
    insert_if_absent(
        cursor,
        QUOTA_INSERT,
        (spec.workspace_id, spec.max_attempts, spec.policy_revision_id),
    )
    row = _one(
        cursor,
        "SELECT * FROM workspace_quotas WHERE workspace_id = ? FOR UPDATE",
        (spec.workspace_id,),
    )
    if row["policy_revision"] != spec.policy_revision_id:
        raise RegistrationError(
            "quota_policy_conflict",
            "existing workspace quota policy revision does not match the recipe",
        )
    if int(row["max_attempts"]) < spec.max_attempts:
        raise RegistrationError(
            "quota_capacity_conflict",
            f"existing workspace quota allows only {row['max_attempts']} attempts",
        )
    return row


def _require_capacity(cursor: _Cursor, spec: RegistrationSpec) -> None:
    row = _one(
        cursor,
        "SELECT * FROM host_nodes WHERE host_node_id = ? FOR UPDATE",
        (spec.plan.binding.target_node_id,),
    )
    capacity = _vector_from(row, "allocatable_")
    occupied = _vector_from(row, "held_").add(_vector_from(row, "quarantined_"))
    required = _needed_capacity(spec.plan)
    if not occupied.add(required).fits(capacity):
        raise RegistrationError(
            "host_capacity_insufficient",
            "existing host allocatable vector cannot hold the requested plan; "
            f"need at least {_vector_text(required)} beyond current holds "
            f"(allocatable {_vector_text(capacity)}, held+quarantined {_vector_text(occupied)}). "
            "This command cannot enlarge host_nodes.allocatable_* after the first registration "
            "on a node inserted it (insert-if-absent), and the runtime role cannot update "
            "those columns either. First check whether the held or quarantined part can be "
            "released; otherwise raise the allocatable vector in a reviewed change, or rebuild "
            "the data volume and pass an explicit --host-capacity when the first registration "
            "on this node runs",
        )


def _capacity_headroom(row: Row, spec: RegistrationSpec) -> CapacityHeadroom | None:
    """Return the gap one more same-shape instance would hit, or ``None`` when it fits.

    Only an undeclared capacity (still equal to this plan's default need) can be
    the one-shot declaration that this warning is about: once the operator supplied
    an explicit ``--host-capacity``, the run already states what the node must hold
    and must never be told to rebuild for a value it chose.

    ``row`` must be read after the registration wrote its reservation: the prepare
    step holds ``peak`` and the successful observation replaces it with ``steady``.
    The pre-write row still has the old held vector, so it would not include this
    registration's steady hold and would miss the shortfall.
    """
    capacity = _vector_from(row, "allocatable_")
    if capacity != _default_capacity(spec.plan):
        return None
    required = _needed_capacity(spec.plan)
    occupied = _vector_from(row, "held_").add(_vector_from(row, "quarantined_"))
    if occupied.add(required).fits(capacity):
        return None
    return CapacityHeadroom(capacity.subtract(occupied), required)


def plan_matches_row(row: Row, plan: ApprovedRuntimePlan) -> bool:
    """Whether an ``engine_instances`` row describes exactly this approved plan.

    One definition point for "the row and the plan agree": the registration command
    uses it to recognise an instance it already wrote, and the audited adoption
    command uses it to refuse copying a generation whose stored plan has drifted from
    its own row. A second copy would let one reader call a drifted row identical.
    """
    binding = plan.binding
    return (
        (
            row["host_node_id"],
            row["pool_id"],
            row["gateway_id"],
            row["binding_revision_id"],
            row["manifest_id"],
            row["approved_template_id"],
            row["management_mode"],
            row["loading_policy"],
            row["config_digest"],
            int(row["max_slots"]),
            int(row["realtime_reserved_slots"]),
        )
        == (
            binding.target_node_id,
            binding.pool_id,
            binding.gateway_id,
            binding.binding_revision_id,
            binding.manifest_id,
            binding.approved_template_id,
            "managed",
            plan.loading_policy,
            binding.config_digest,
            plan.max_slots,
            plan.realtime_reserved_slots,
        )
        and _vector_from(row, "peak_") == plan.peak
        and _vector_from(row, "steady_") == plan.steady
        and _vector_from(row, "per_slot_") == plan.per_slot
    )


def _existing_result(
    cursor: _Cursor,
    spec: RegistrationSpec,
    *,
    pool: Row,
    owner_id: str,
    evidence_digest: str,
    evidence_document: Row,
    probe: EngineProbe,
    readiness_ttl_us: int,
    renew_readiness: bool = False,
) -> RegistrationResult | None:
    heads = _rows(
        cursor,
        "SELECT * FROM runtime_instance_heads WHERE instance_id = ? FOR UPDATE",
        (spec.instance_id,),
    )
    if not heads:
        existing = _rows(
            cursor,
            "SELECT EXISTS (SELECT 1 FROM engine_instances WHERE instance_id = ?) AS present",
            (spec.instance_id,),
        )
        if existing and existing[0]["present"]:
            raise RegistrationError(
                "reconciliation_required",
                "engine rows exist without a runtime head",
            )
        return None
    head = heads[0]
    current = int(head["current_generation"])
    high_water = int(head["generation_high_water"])
    if spec.generation < current or (spec.generation <= high_water and spec.generation != current):
        # Both numbers matter to a stale caller: the current generation is the one a
        # renewal should have named, and the high water is why an intermediate number
        # is refused too.
        raise RegistrationError(
            "stale_instance_generation",
            f"generation {spec.generation} is not above high water {high_water}; "
            f"the current generation is {current}",
        )
    if spec.generation == current:
        instance = _one(
            cursor,
            "SELECT * FROM engine_instances WHERE instance_id = ? "
            "AND instance_generation = ? FOR UPDATE",
            (spec.instance_id, spec.generation),
        )
        if instance["instance_state"] != "ready":
            raise RegistrationError(
                "instance_not_ready",
                f"generation {spec.generation} is {instance['instance_state']}; reconcile it first",
            )
        if not plan_matches_row(instance, spec.plan):
            raise RegistrationError(
                "instance_config_conflict",
                "the ready instance has different binding, model plan or capacity facts",
            )
        if int(instance["last_fencing_token"]) != int(pool["fencing_token"]):
            raise RegistrationError(
                "stale_fencing_token",
                "the ready instance belongs to a superseded pool owner epoch",
            )
        prepare_id = head["current_prepare_intent_id"]
        if prepare_id is None:
            raise RegistrationError(
                "reconciliation_required",
                "the ready instance has no prepare intent",
            )
        intent = _one(
            cursor,
            "SELECT * FROM runtime_instance_intents WHERE target_node_id = ? "
            "AND intent_id = ? FOR UPDATE",
            (instance["host_node_id"], prepare_id),
        )
        if head["current_stop_intent_id"] is not None:
            raise RegistrationError(
                "instance_not_ready",
                "the current generation has an outstanding stop intent",
            )
        if (
            intent["intent_state"] != "succeeded"
            or intent["observation_document"] is None
            or intent["readiness_document"] is None
        ):
            raise RegistrationError(
                "reconciliation_required",
                "the ready instance lacks a successful observation and readiness receipt",
            )
        try:
            readiness = readiness_from_document(intent["readiness_document"])
        except ValueError:
            raise RegistrationError(
                "reconciliation_required",
                "the stored readiness receipt is invalid",
            ) from None
        now_us = cursor.clock()
        # A quarter of the declared TTL is reported as "expiring": enough of a
        # warning window for a periodic renewal to run before admission refuses.
        state = readiness.expiry_state(now_us, warning_us=readiness_ttl_us // 4)
        # An elapsed receipt is always re-signed. A periodic renewal asks for the
        # same transaction explicitly, so `--renew-readiness` forces a fresh
        # receipt instead of depending on how early the scheduler ran.
        renewed = readiness.expires_at_us <= now_us or renew_readiness
        if renewed:
            # Re-sign the identity that is actually stored, not the one this run
            # resolved: admission compares the receipt field by field against the
            # stored prepare request and plan, so a receipt built from today's
            # claim is unusable the moment the two differ -- a renewal would then
            # "succeed" while every create kept answering engine_state_mismatch
            # (#1246). Only the timestamps and the probe evidence are fresh.
            try:
                prepared_stored, stored_plan, _creator = decode_record(intent["request_document"])
            except ValueError:
                raise RegistrationError(
                    "reconciliation_required",
                    "the stored prepare intent no longer decodes, so its readiness "
                    "receipt cannot be re-signed; register a new generation",
                ) from None
            stored_request = prepared_stored.request
            if int(stored_request.fencing_token) != int(pool["fencing_token"]):
                raise RegistrationError(
                    "reconciliation_required",
                    "the stored prepare intent belongs to a superseded fencing epoch; "
                    "register a new generation instead of renewing this one",
                )
            refreshed = VerifiedRuntimeReadiness(
                stored_request.binding,
                stored_request.instance_id,
                stored_request.instance_generation,
                stored_request.fencing_token,
                stored_plan.topology_digest,
                stored_plan.qualification_digest,
                evidence_digest,
                now_us,
                now_us + readiness_ttl_us,
            )
            # The write is only worth making if admission can use what lands: the
            # check the next create will run, run here first.
            validate_stored_readiness(readiness_to_document(refreshed), stored_request, stored_plan)
            cursor.execute(
                "UPDATE runtime_instance_intents SET readiness_document = ?, "
                "updated_at_us = ?, version = version + 1 WHERE target_node_id = ? "
                "AND intent_id = ? AND version = ?",
                (
                    readiness_to_document(refreshed),
                    now_us,
                    instance["host_node_id"],
                    prepare_id,
                    intent["version"],
                ),
            )
            if cursor.rowcount != 1:
                raise RegistrationError(
                    "version_conflict",
                    "prepare intent changed while refreshing readiness",
                )
            cursor.execute(
                "UPDATE engine_instances SET ready_at_us = ?, version = version + 1 "
                "WHERE instance_id = ? AND instance_generation = ? AND version = ?",
                (now_us, spec.instance_id, spec.generation, instance["version"]),
            )
            if cursor.rowcount != 1:
                raise RegistrationError(
                    "version_conflict",
                    "instance changed while refreshing readiness",
                )
            readiness = refreshed
        return RegistrationResult(
            outcome="already_ready",
            model_id=spec.model_id,
            instance_id=spec.instance_id,
            instance_generation=spec.generation,
            intent_id=str(prepare_id),
            target_node_id=str(instance["host_node_id"]),
            pool_id=str(instance["pool_id"]),
            gateway_id=str(instance["gateway_id"]),
            fencing_token=int(instance["last_fencing_token"]),
            owner_id=owner_id,
            workspace_id=spec.workspace_id,
            ready_at_us=int(instance["ready_at_us"]) if instance["ready_at_us"] else None,
            readiness_verified_at_us=readiness.verified_at_us,
            readiness_expires_at_us=readiness.expires_at_us,
            evidence_digest=readiness.evidence_digest,
            evidence_document=evidence_document,
            probe=probe,
            rows=_snapshot(cursor, spec),
            readiness_state=state,
            readiness_renewed=renewed,
        )
    previous = _one(
        cursor,
        "SELECT * FROM engine_instances WHERE instance_id = ? AND instance_generation = ? "
        "FOR UPDATE",
        (spec.instance_id, current),
    )
    if previous["instance_state"] != "stopped" or previous["base_budget_kind"] != "released":
        raise RegistrationError(
            "previous_generation_active",
            "the current generation is not stopped and released; "
            "stop it before registering a new one",
        )
    return None


def _snapshot(cursor: _Cursor, spec: RegistrationSpec) -> Row:
    instance = _one(
        cursor,
        "SELECT * FROM engine_instances WHERE instance_id = ? AND instance_generation = ?",
        (spec.instance_id, spec.generation),
    )
    head = _one(
        cursor,
        "SELECT * FROM runtime_instance_heads WHERE instance_id = ?",
        (spec.instance_id,),
    )
    intent = _one(
        cursor,
        "SELECT target_node_id, intent_id, instance_id, instance_generation, action, "
        "intent_state, request_digest, version, "
        "observation_document IS NOT NULL AS has_observation, "
        "readiness_document IS NOT NULL AS has_readiness "
        "FROM runtime_instance_intents WHERE target_node_id = ? AND intent_id = ?",
        (instance["host_node_id"], head["current_prepare_intent_id"]),
    )
    pool = _one(
        cursor,
        "SELECT pool_id, service_class, pool_state, owner_id, fencing_token, "
        "owner_expires_at_us, owner_state, version FROM resource_pools WHERE pool_id = ?",
        (instance["pool_id"],),
    )
    quota = _one(
        cursor,
        "SELECT workspace_id, max_attempts, held_attempts, quarantined_attempts, "
        "policy_revision, version FROM workspace_quotas WHERE workspace_id = ?",
        (spec.workspace_id,),
    )
    return {
        "engine_instances": instance,
        "runtime_instance_heads": head,
        "runtime_instance_intents": intent,
        "resource_pools": pool,
        "workspace_quotas": quota,
    }


def _register_in_transaction(
    connection: PortableConnection,
    spec: RegistrationSpec,
    *,
    schema: str,
    probe: EngineProbe,
    evidence_digest: str,
    evidence_document: Row,
    owner_id: str,
    fencing_token: int | None,
    owner_ttl_us: int,
    readiness_ttl_us: int,
    intent_deadline_us: int,
    intent_id: str | None,
    timeout_seconds: float,
    renew_readiness: bool = False,
) -> RegistrationResult:
    deadline = time.monotonic() + timeout_seconds
    with connection.cursor() as raw:
        cursor = _Cursor(raw, deadline)
        cursor.execute("SELECT set_config('search_path', ?, true)", (schema + ",pg_catalog",))
        cursor.execute("SELECT set_config('TimeZone', 'UTC', true)")
        _require_schema(cursor)
        _ensure_host(cursor, spec)
        pool = _ensure_pool(
            cursor,
            spec,
            owner_id=owner_id,
            fencing_token=fencing_token,
            owner_ttl_us=owner_ttl_us,
        )
        _ensure_quota(cursor, spec)
        existing = _existing_result(
            cursor,
            spec,
            pool=pool,
            owner_id=owner_id,
            evidence_digest=evidence_digest,
            evidence_document=evidence_document,
            probe=probe,
            readiness_ttl_us=readiness_ttl_us,
            renew_readiness=renew_readiness,
        )
        if existing is not None:
            return existing
        now_us = cursor.clock()
        observed_at_us = min(probe.observed_at_us, now_us)
        token = int(pool["fencing_token"])
        request = RuntimeIntent(
            intent_id or _intent_id(spec.instance_id, spec.generation),
            spec.plan.binding,
            spec.instance_id,
            spec.generation,
            "prepare",
            token,
            spec.plan.memory,
            RuntimeDeadline(_iso(now_us + intent_deadline_us)),
        )
        prepared = prepare_request(request)
        heads = _rows(
            cursor,
            "SELECT version FROM runtime_instance_heads WHERE instance_id = ? FOR UPDATE",
            (spec.instance_id,),
        )
        expected_head = int(heads[0]["version"]) if heads else None
        _require_capacity(cursor, spec)
        created = create_in_transaction(
            cursor,
            CreateRuntimeIntent(request, expected_head, spec.plan, owner_id),
            prepared,
            clock=cursor.clock,
        )
        if created.outcome not in ("committed", "replayed"):
            raise RegistrationError(
                created.error_code or "runtime_create_rejected",
                "runtime create transaction did not commit",
            )
        observation_wire = prepared_observation_wire(
            plan=spec.plan,
            instance_id=spec.instance_id,
            instance_generation=spec.generation,
            intent_id=request.intent_id,
            fencing_token=token,
            observed_at_us=observed_at_us,
        )
        prepared_observation = prepare_observation(observation_wire)
        readiness = VerifiedRuntimeReadiness(
            spec.plan.binding,
            spec.instance_id,
            spec.generation,
            token,
            spec.plan.topology_digest,
            spec.plan.qualification_digest,
            evidence_digest,
            now_us,
            now_us + readiness_ttl_us,
        )
        observed = observe_in_transaction(
            cursor,
            ObserveRuntimeIntent(
                prepared_observation.observation,
                1,
                1,
                owner_id,
                prepared_observation.document,
                readiness,
            ),
            prepared_observation,
            clock=cursor.clock,
        )
        if observed.outcome not in ("committed", "replayed"):
            raise RegistrationError(
                observed.error_code or "runtime_observe_rejected",
                "runtime observation transaction did not commit",
            )
        # Operator attestation has no node-controller delivery to perform.
        block_operator_attested_delivery(cursor, prepared.event_id, now_us=cursor.clock())
        rows = _snapshot(cursor, spec)
        instance = rows["engine_instances"]
        # `_require_capacity` locked the host row; read it again after the writes.
        # The pre-write row does not contain the steady this registration now holds,
        # so using it would miss the shortfall this warning exists to report.
        host = _one(
            cursor,
            "SELECT * FROM host_nodes WHERE host_node_id = ? FOR UPDATE",
            (spec.plan.binding.target_node_id,),
        )
        return RegistrationResult(
            outcome="registered",
            model_id=spec.model_id,
            instance_id=spec.instance_id,
            instance_generation=spec.generation,
            intent_id=request.intent_id,
            target_node_id=str(spec.plan.binding.target_node_id),
            pool_id=str(spec.plan.binding.pool_id),
            gateway_id=str(spec.plan.binding.gateway_id),
            fencing_token=token,
            owner_id=owner_id,
            workspace_id=spec.workspace_id,
            ready_at_us=int(instance["ready_at_us"]) if instance["ready_at_us"] else None,
            readiness_verified_at_us=readiness.verified_at_us,
            readiness_expires_at_us=readiness.expires_at_us,
            evidence_digest=evidence_digest,
            evidence_document=evidence_document,
            probe=probe,
            rows=rows,
            capacity_headroom=_capacity_headroom(host, spec),
        )


def _memory_wire(vector: PhysicalResources) -> Row:
    return {
        "unified_memory_bytes": vector.unified_physical_memory_bytes,
        "device_memory_bytes": vector.device_memory_bytes,
        "host_memory_bytes": vector.host_memory_bytes,
    }


def register_engine(
    *,
    database_url: str,
    schema: str,
    spec: RegistrationSpec,
    engine_url: str,
    owner_id: str = "speechnexus-operator",
    fencing_token: int | None = None,
    owner_ttl_seconds: int = 86_400,
    readiness_ttl_seconds: int = 86_400,
    intent_deadline_seconds: int = 300,
    intent_id: str | None = None,
    health_timeout_seconds: float = 5.0,
    transaction_timeout_seconds: float = 10.0,
    connect_timeout_seconds: int = 5,
    probe: EngineProbe | None = None,
    renew_readiness: bool = False,
) -> RegistrationResult:
    """Register and activate one engine generation in one database transaction."""
    if not re.fullmatch(r"[a-z][a-z0-9_]{2,62}", schema):
        raise RegistrationError("invalid_schema", "schema name is invalid", EXIT_INVALID)
    identifier(owner_id)
    if spec.generation < 1:
        raise RegistrationError("invalid_generation", "generation must be positive", EXIT_INVALID)
    if not 0 < owner_ttl_seconds <= 31_536_000:
        raise RegistrationError("invalid_owner_ttl", "owner TTL is out of range", EXIT_INVALID)
    if not 0 < readiness_ttl_seconds <= 31_536_000:
        raise RegistrationError(
            "invalid_readiness_ttl", "readiness TTL is out of range", EXIT_INVALID
        )
    if not 1 <= intent_deadline_seconds <= 3_600:
        raise RegistrationError(
            "invalid_intent_deadline", "intent deadline is out of range", EXIT_INVALID
        )
    if intent_id is not None:
        identifier(intent_id)
    if not _needed_capacity(spec.plan).fits(spec.host_capacity):
        raise RegistrationError(
            "host_capacity_insufficient",
            "requested host capacity cannot hold the steady plan plus every slot",
            EXIT_INVALID,
        )
    chosen_probe = (
        probe
        if probe is not None
        else probe_engine(engine_url, timeout_seconds=health_timeout_seconds)
    )
    evidence_digest, evidence_document = _evidence(spec, chosen_probe)
    owner_ttl_us = owner_ttl_seconds * 1_000_000
    readiness_ttl_us = readiness_ttl_seconds * 1_000_000
    intent_deadline_us = intent_deadline_seconds * 1_000_000
    connection: PortableConnection | None = None
    try:
        connection = portable_connect(
            database_url,
            row_factory=dict_row,
            connect_timeout=connect_timeout_seconds,
        )
        result = _register_in_transaction(
            connection,
            spec,
            schema=schema,
            probe=chosen_probe,
            evidence_digest=evidence_digest,
            evidence_document=evidence_document,
            owner_id=owner_id,
            fencing_token=fencing_token,
            owner_ttl_us=owner_ttl_us,
            readiness_ttl_us=readiness_ttl_us,
            intent_deadline_us=intent_deadline_us,
            intent_id=intent_id,
            timeout_seconds=transaction_timeout_seconds,
            renew_readiness=renew_readiness,
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
        raise RegistrationError(
            error.code,
            "runtime transaction rejected the registration: " + error.code,
        ) from None
    except psycopg.errors.UndefinedTable:
        if connection is not None:
            connection.rollback()
        raise RegistrationError(
            "schema_not_migrated",
            "coordination tables are missing; run speechnexus-job-migrate for this schema first",
            EXIT_UNAVAILABLE,
        ) from None
    except psycopg.errors.UniqueViolation:
        if connection is not None:
            connection.rollback()
        raise RegistrationError(
            "concurrent_registration",
            "another registration committed the same identity; re-run to observe the ready row",
        ) from None
    except psycopg.Error:
        if connection is not None:
            connection.rollback()
        raise RegistrationError(
            "database_unavailable",
            "database transaction failed; no registration was committed",
            EXIT_UNAVAILABLE,
        ) from None
    finally:
        if connection is not None:
            connection.close()


def _spec_from_recipe(
    recipe: TranscriptionRecipe,
    *,
    generation: int,
    workspace_id: str,
    max_attempts: int,
    host_capacity: PhysicalResources | None,
    overrides: dict[str, str | None],
) -> RegistrationSpec:
    plan = recipe.approved.stages[0].runtime_plan
    binding = plan.binding
    checks = {
        "node": binding.target_node_id,
        "pool": binding.pool_id,
        "gateway": binding.gateway_id,
        "instance": recipe.instance_id,
        "model": recipe.model_id,
    }
    for name, expected in checks.items():
        supplied = overrides.get(name)
        if supplied is not None and supplied != expected:
            raise RegistrationError(
                f"{name}_override_conflict",
                f"--{name} does not match the private recipe ({expected})",
                EXIT_INVALID,
            )
    return RegistrationSpec(
        model_id=recipe.model_id,
        workspace_id=workspace_id,
        policy_revision_id=recipe.approved.policy_revision_id,
        instance_id=recipe.instance_id,
        generation=generation,
        plan=plan,
        host_capacity=host_capacity or _default_capacity(plan),
        max_attempts=max_attempts,
    )


def _spec_from_manual(args: argparse.Namespace) -> RegistrationSpec:
    required = (
        "model",
        "node",
        "pool",
        "gateway",
        "instance",
        "template_id",
        "binding_revision_id",
        "manifest_id",
        "config_digest",
        "topology_digest",
        "qualification_digest",
        "architecture",
        "service_class",
        "loading_policy",
        "max_slots",
        "policy_revision",
    )
    missing = [name for name in required if getattr(args, name, None) in (None, "")]
    for name in ("peak", "steady", "per_slot"):
        if getattr(args, name, None) is None:
            missing.append(name)
    if missing:
        flags = ", ".join("--" + name.replace("_", "-") for name in missing)
        raise RegistrationError(
            "missing_parameters",
            "without --recipe-file these parameters are required: " + flags,
            EXIT_INVALID,
        )
    plan = ApprovedRuntimePlan(
        RuntimeBinding(
            args.node,
            args.pool,
            args.gateway,
            args.template_id,
            args.binding_revision_id,
            args.manifest_id,
            args.config_digest,
        ),
        args.topology_digest,
        args.qualification_digest,
        args.architecture,
        args.service_class,
        args.loading_policy,
        args.max_slots,
        args.realtime_reserved_slots,
        args.peak,
        args.steady,
        args.per_slot,
    )
    return RegistrationSpec(
        model_id=args.model,
        workspace_id=args.workspace,
        policy_revision_id=args.policy_revision,
        instance_id=args.instance,
        generation=args.generation,
        plan=plan,
        host_capacity=args.host_capacity or _default_capacity(plan),
        max_attempts=args.max_attempts,
    )


def _spec_from_args(args: argparse.Namespace) -> RegistrationSpec:
    recipe_path = Path(args.recipe_file).expanduser() if args.recipe_file else None
    if recipe_path is not None:
        try:
            recipe = load_transcription_recipe(recipe_path)
        except ValueError as error:
            raise RegistrationError(
                "invalid_recipe",
                "private transcription recipe is unreadable or internally inconsistent",
                EXIT_INVALID,
            ) from error
        return _spec_from_recipe(
            recipe,
            generation=args.generation,
            workspace_id=args.workspace,
            max_attempts=args.max_attempts,
            host_capacity=args.host_capacity,
            overrides={
                "node": args.node,
                "pool": args.pool,
                "gateway": args.gateway,
                "instance": args.instance,
                "model": args.model,
            },
        )
    return _spec_from_manual(args)


def resolve_current_generation(
    database_url: str,
    schema: str,
    instance_id: str,
    *,
    connect_timeout_seconds: int = 5,
) -> int:
    """The generation the ledger says is current for this instance.

    A renewal has to describe the generation that is actually serving, and the only
    authority on that is ``runtime_instance_heads``. A manifest that restates the
    number drifts the moment the controller prepares a new generation: the renewal
    then either re-signs a receipt nobody serves or refuses every run, and the
    receipt quietly runs out (#1253). Reading it here keeps the deployment free of a
    constant that goes stale.

    An instance without a head has never been registered, so there is nothing to
    resolve: the first registration must say which generation it creates.
    """
    if not re.fullmatch(r"[a-z][a-z0-9_]{2,62}", schema):
        raise RegistrationError("invalid_schema", "schema name is invalid", EXIT_INVALID)
    identifier(instance_id)
    connection = portable_connect(
        database_url,
        row_factory=dict_row,
        connect_timeout=connect_timeout_seconds,
    )
    try:
        # The resolution must never depend on write rights: a read-only session makes
        # that a database-enforced property rather than a promise.
        connection.read_only = True
        with connection.cursor() as raw:
            cursor = _Cursor(raw, time.monotonic() + 5.0)
            cursor.execute("SELECT set_config('search_path', ?, true)", (schema + ",pg_catalog",))
            rows = _rows(
                cursor,
                "SELECT current_generation FROM runtime_instance_heads WHERE instance_id = ?",
                (instance_id,),
            )
    finally:
        connection.close()
    if not rows:
        raise RegistrationError(
            "generation_required",
            f"instance {instance_id} has no runtime head yet, so there is no current "
            "generation to renew; pass --generation for the first registration",
            EXIT_INVALID,
        )
    return int(rows[0]["current_generation"])


def _instance_hint(args: argparse.Namespace) -> str:
    """The instance a run names, from the flag or from the recipe it registers.

    ``--generation`` is resolved against the ledger by instance id, so the id has to
    be known before the spec exists. Recipe mode names the instance inside the
    private recipe, the same file the spec is built from.
    """
    if args.instance:
        return cast(str, args.instance)
    if args.recipe_file:
        try:
            recipe = load_transcription_recipe(Path(args.recipe_file).expanduser())
        except ValueError as error:
            raise RegistrationError(
                "invalid_recipe",
                "private transcription recipe is unreadable or internally inconsistent",
                EXIT_INVALID,
            ) from error
        return recipe.instance_id
    raise RegistrationError(
        "missing_instance",
        "--instance is required when --generation is omitted",
        EXIT_INVALID,
    )


def _settings(args: argparse.Namespace) -> tuple[str, str]:
    database_url = args.database_url or os.environ.get("SPEECHNEXUS_DATABASE_URL", "")
    if not database_url:
        raise RegistrationError(
            "missing_database_url",
            "set --database-url or SPEECHNEXUS_DATABASE_URL",
            EXIT_INVALID,
        )
    schema = args.schema or os.environ.get("SPEECHNEXUS_DATABASE_SCHEMA", "speech")
    if not re.fullmatch(r"[a-z][a-z0-9_]{2,62}", schema):
        raise RegistrationError("invalid_schema", "schema name is invalid", EXIT_INVALID)
    return database_url, schema


def _result_json(result: RegistrationResult) -> str:
    return json.dumps(
        {
            "outcome": result.outcome,
            "model": result.model_id,
            "instance_id": result.instance_id,
            "instance_generation": result.instance_generation,
            "intent_id": result.intent_id,
            "target_node_id": result.target_node_id,
            "pool_id": result.pool_id,
            "gateway_id": result.gateway_id,
            "fencing_token": result.fencing_token,
            "owner_id": result.owner_id,
            "workspace_id": result.workspace_id,
            "ready_at_us": result.ready_at_us,
            "readiness": {
                "verified_at_us": result.readiness_verified_at_us,
                "expires_at_us": result.readiness_expires_at_us,
                "evidence_digest": result.evidence_digest,
                # `state` is the window this run found before it acted; `renewed`
                # says whether the receipt it reports is a fresh re-signature.
                "state": result.readiness_state,
                "renewed": result.readiness_renewed,
            },
            "health_probe": {
                "url": result.probe.url,
                "status": result.probe.status,
                "content_type": result.probe.content_type,
                "body_sha256": result.probe.body_sha256,
                "body_bytes": result.probe.body_bytes,
                "observed_at_us": result.probe.observed_at_us,
            },
            "rows": result.rows,
        },
        indent=2,
        sort_keys=True,
        default=str,
    )


def _report(result: RegistrationResult) -> None:
    """Print operator warnings on stderr, then the unchanged JSON document on stdout."""
    if result.readiness_state in (READINESS_EXPIRED, READINESS_EXPIRING):
        # stderr keeps the machine-readable document on stdout unchanged.
        action = "re-signed by this run" if result.readiness_renewed else "NOT renewed"
        sys.stderr.write(
            f"warning: readiness {result.readiness_state} for "
            f"{result.instance_id} g{result.instance_generation} "
            f"(expires_at_us={result.readiness_expires_at_us}); {action}; "
            "admission refuses an elapsed receipt with 503 engine_not_ready\n"
        )
    if result.capacity_headroom is not None:
        sys.stderr.write(
            f"warning: host {result.target_node_id} cannot hold another instance of this "
            f"shape under the current holds: remaining "
            f"{_vector_text(result.capacity_headroom.remaining)}, one more requires "
            f"{_vector_text(result.capacity_headroom.required)}; release held or quarantined "
            "capacity first, otherwise raise allocatable_* in a reviewed change (the runtime "
            "role cannot), or rebuild the data volume and pass an explicit --host-capacity "
            "at the first registration\n"
        )
    print(_result_json(result))


def _require_the_node_prepares_this_generation(
    spec: RegistrationSpec,
    options: argparse.Namespace,
    *,
    database_url: str,
    schema: str,
) -> None:
    """Refuse to attest a generation the node has not prepared (#1282, #1286).

    The ledger and the controller count generations separately, and nothing made them
    move together: the live instance was registered at generation 1 while the node had
    prepared 11, so this command signed a receipt for a generation no pod served and
    every surface called it healthy. The node is asked before anything is written, and
    an unreachable node fails the run instead of skipping the check: an unverified
    attestation is exactly the green-but-wrong state this closes.

    The two answers are deliberately not the same answer (#1286):

    * a **determined** disagreement (the node names another generation) makes the
      receipts already signed for the ledger's generation unusable, so this run records
      an explicit void in ``runtime_readiness_voids`` before failing. Without that write
      the old receipt keeps admitting creates until it expires, which is the window the
      report-and-create disagreement lived in;
    * an **undetermined** answer (unreachable, timeout, refused, malformed) writes
      nothing. A wrong controller URL must not break production admission, and the
      report keeps telling the two apart by naming the controller's own error code.

    A void is never revoked: the next *successful* renewal signs a receipt whose
    ``verified_at_us`` is later than ``voided_at_us``, and the one rule in
    ``speech.domain.readiness_void`` stops applying the void to it.
    """
    endpoint = configured_endpoint(_process_environment())
    if endpoint is None:
        return
    node_id = spec.plan.binding.target_node_id
    try:
        head = read_head(
            endpoint,
            node_id=node_id,
            instance_id=spec.instance_id,
            timeout_seconds=options.health_timeout_seconds,
        )
    except ControllerConsultationError as error:
        # Undetermined, not inconsistent: report it and write no void.
        raise RegistrationError(error.code, error.message, EXIT_UNAVAILABLE) from None
    disagreement = generation_disagreement(spec.generation, head, node_id=node_id)
    if disagreement is not None:
        _void_readiness_for_mismatch(
            database_url=database_url,
            schema=schema,
            spec=spec,
            head=head,
            disagreement=disagreement,
            timeout_seconds=options.transaction_timeout_seconds,
        )
        raise RegistrationError("instance_generation_mismatch", disagreement, EXIT_CONFLICT)


def _void_readiness_for_mismatch(
    *,
    database_url: str,
    schema: str,
    spec: RegistrationSpec,
    head: NodeInstanceHead,
    disagreement: str,
    timeout_seconds: float,
) -> None:
    """Append one void row for a determined generation disagreement (#1286).

    It runs in its own short transaction, before the refusal is raised, because the
    refusal must carry the fact that the receipt is now unusable: a caller that sees
    only the error cannot tell "nothing happened" from "the old receipt was voided".
    The row is append-only and idempotent per (instance, ledger generation, node
    generation), so the 5-minute renewal loop leaves exactly one audit row per
    divergence instead of one every run.
    """
    assert head.current_generation is not None  # a disagreement always names one
    connection = portable_connect(
        database_url,
        row_factory=dict_row,
        connect_timeout=max(1, min(30, int(timeout_seconds) or 1)),
    )
    try:
        # The registration transaction pins ``search_path`` for its own statements, so
        # this short one has to do the same: an unqualified INSERT on a fresh connection
        # answers ``42P01 relation does not exist`` against the reference deployment.
        with connection.transaction(), connection.cursor() as raw:
            raw.execute(
                "SELECT set_config('search_path', ?, true)", (schema + ",pg_catalog",)
            )
            cursor = _Cursor(raw, time.monotonic() + max(1.0, timeout_seconds))
            # The database clock, so the void and the receipt that beat it later are
            # ordered by one clock rather than by this host's.
            insert_if_absent(
                cursor,
                "INSERT INTO " + READINESS_VOID_TABLE + " ("
                + ", ".join(READINESS_VOID_INSERT_COLUMNS)
                + ") VALUES ("
                + ", ".join("?" for _ in READINESS_VOID_INSERT_COLUMNS)
                + ")",
                (
                    spec.instance_id,
                    spec.generation,
                    int(head.current_generation),
                    READINESS_VOID_REASON_GENERATION_MISMATCH,
                    READINESS_VOID_SOURCE_RENEWAL,
                    cursor.clock(),
                    disagreement,
                ),
            )
        connection.commit()
    except Exception as error:
        connection.rollback()
        # A void that cannot be written must not be silently absent: the operator has to
        # see that the receipt is still usable before fixing the controller.
        raise RegistrationError(
            "readiness_void_failed",
            f"the node reports generation {head.current_generation} but the void of the "
            f"receipt for generation {spec.generation} could not be recorded: {error}",
            EXIT_UNAVAILABLE,
        ) from None
    finally:
        connection.close()


def _process_environment() -> Mapping[str, str]:
    return os.environ


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Register and activate one already-running engine instance for admission; "
            "this is an operator health attestation, not a node-controller observation"
        )
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="PostgreSQL DSN; defaults to SPEECHNEXUS_DATABASE_URL",
    )
    parser.add_argument(
        "--schema",
        default=None,
        help="Coordination schema; defaults to SPEECHNEXUS_DATABASE_SCHEMA or speech",
    )
    parser.add_argument(
        "--recipe-file",
        default=os.environ.get("SPEECHNEXUS_TRANSCRIPTION_RECIPE_FILE"),
        help="Owner-only private transcription recipe; defaults to the deployment variable",
    )
    parser.add_argument(
        "--engine-url",
        default=os.environ.get("SPEECHNEXUS_ENGINE_HEALTH_URL"),
        help="Engine origin or health URL; /health is appended to an origin",
    )
    parser.add_argument(
        "--generation",
        type=_positive,
        default=None,
        help=(
            "Generation to register. Omit it for a renewal: the ledger's current "
            "generation is the authority on which one is serving, and a restated "
            "number drifts as soon as a new generation is prepared"
        ),
    )
    parser.add_argument("--workspace", default="workspace-default")
    parser.add_argument("--max-attempts", type=_positive, default=8)
    parser.add_argument("--owner-id", default="speechnexus-operator")
    parser.add_argument("--fencing-token", type=_positive, default=None)
    parser.add_argument("--owner-ttl-seconds", type=_positive, default=86_400)
    parser.add_argument("--readiness-ttl-seconds", type=_positive, default=86_400)
    parser.add_argument("--intent-deadline-seconds", type=_positive, default=300)
    parser.add_argument("--intent-id", default=None)
    parser.add_argument(
        "--renew-readiness",
        action="store_true",
        help=(
            "Re-sign the readiness receipt of the already-ready instance with this run's "
            "health probe, even when the stored receipt has not expired yet. This is the "
            "renewal entry a periodic task calls; without it an elapsed receipt is still "
            "re-signed, but a still-valid one is left untouched"
        ),
    )
    parser.add_argument("--health-timeout-seconds", type=float, default=5.0)
    parser.add_argument("--transaction-timeout-seconds", type=float, default=10.0)
    # Manual mode mirrors the frozen ApprovedRuntimePlan and binding fields.
    parser.add_argument("--model", default=None)
    parser.add_argument("--node", default=None)
    parser.add_argument("--pool", default=None)
    parser.add_argument("--gateway", default=None)
    parser.add_argument("--instance", default=None)
    parser.add_argument("--template-id", default=None)
    parser.add_argument("--binding-revision-id", default=None)
    parser.add_argument("--manifest-id", default=None)
    parser.add_argument("--config-digest", default=None)
    parser.add_argument("--topology-digest", default=None)
    parser.add_argument("--qualification-digest", default=None)
    parser.add_argument(
        "--architecture",
        choices=("unified_memory", "discrete_device_memory"),
        default=None,
    )
    parser.add_argument(
        "--service-class",
        choices=("realtime", "interactive", "batch"),
        default=None,
    )
    parser.add_argument("--loading-policy", choices=("resident", "on_demand"), default=None)
    parser.add_argument("--max-slots", type=_positive, default=None)
    parser.add_argument("--realtime-reserved-slots", type=int, default=0)
    parser.add_argument("--peak", type=_vector, default=None)
    parser.add_argument("--steady", type=_vector, default=None)
    parser.add_argument("--per-slot", type=_vector, default=None)
    parser.add_argument(
        "--host-capacity",
        type=_vector,
        default=None,
        help=(
            "host allocatable vector for this node; defaults to this plan's own need "
            "(steady + per_slot*max_slots, at least peak per dimension). It is written only "
            "when the node row is first inserted, so pass a value sized for every instance "
            "you intend to run here"
        ),
    )
    parser.add_argument("--policy-revision", default=None)
    options = parser.parse_args(argv)
    try:
        database_url, schema = _settings(options)
        if not options.engine_url:
            raise RegistrationError(
                "missing_engine_url",
                "set --engine-url or SPEECHNEXUS_ENGINE_HEALTH_URL",
                EXIT_INVALID,
            )
        if options.generation is None:
            options.generation = resolve_current_generation(
                database_url, schema, _instance_hint(options)
            )
        spec = _spec_from_args(options)
        _require_the_node_prepares_this_generation(
            spec, options, database_url=database_url, schema=schema
        )
        result = register_engine(
            database_url=database_url,
            schema=schema,
            spec=spec,
            engine_url=options.engine_url,
            owner_id=options.owner_id,
            fencing_token=options.fencing_token,
            owner_ttl_seconds=options.owner_ttl_seconds,
            readiness_ttl_seconds=options.readiness_ttl_seconds,
            intent_deadline_seconds=options.intent_deadline_seconds,
            intent_id=options.intent_id,
            health_timeout_seconds=options.health_timeout_seconds,
            transaction_timeout_seconds=options.transaction_timeout_seconds,
            renew_readiness=options.renew_readiness,
        )
    except RegistrationError as error:
        parser.exit(error.exit_code, f"error: {error.code}: {error.message}\n")
    except (ValueError, TypeError) as error:
        parser.exit(EXIT_INVALID, f"error: invalid_request: {error}\n")
    _report(result)


if __name__ == "__main__":
    main()
