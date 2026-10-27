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
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen

import psycopg
from psycopg.rows import dict_row

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
    ObserveRuntimeIntent,
    VerifiedRuntimeReadiness,
)
from speechnexus_backend.speech.infrastructure.runtime_codec import prepare_request
from speechnexus_backend.speech.infrastructure.runtime_observation_codec import (
    prepare_observation,
    readiness_from_document,
    readiness_to_document,
)
from speechnexus_backend.speech.infrastructure.runtime_observation_sql import (
    observe_in_transaction,
)
from speechnexus_backend.speech.infrastructure.runtime_sql import create_in_transaction
from speechnexus_backend.speech.infrastructure.transcription_recipe import (
    TranscriptionRecipe,
    load_transcription_recipe,
)

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


def _evidence(spec: RegistrationSpec, probe: EngineProbe) -> tuple[str, Row]:
    binding = spec.plan.binding
    document: Row = {
        "kind": "operator_engine_health_probe_v1",
        "model_id": spec.model_id,
        "instance_id": spec.instance_id,
        "instance_generation": spec.generation,
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
        self._cursor.execute(
            "SELECT set_config('statement_timeout', %s, true)",
            (str(max(1, int(remaining * 1000))),),
        )
        return self._cursor.execute(query, parameters)

    def fetchall(self) -> list[Row]:
        return cast(list[Row], list(self._cursor.fetchall()))

    def clock(self) -> int:
        self.execute(
            "SELECT CAST(EXTRACT(EPOCH FROM clock_timestamp()) * 1000000 AS BIGINT) AS now_us"
        )
        return int(self.fetchall()[0]["now_us"])


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
    names = ", ".join("allocatable_" + name for name in _DIMENSIONS)
    placeholders = ", ".join(["%s"] * len(_DIMENSIONS))
    cursor.execute(
        "INSERT INTO host_nodes (host_node_id, memory_architecture, node_state, "
        "topology_digest, "
        + names
        + ") VALUES (%s, %s, 'active', %s, "
        + placeholders
        + ") ON CONFLICT (host_node_id) DO NOTHING",
        (
            binding.target_node_id,
            plan.memory_architecture,
            plan.topology_digest,
            *spec.host_capacity.values,
        ),
    )
    row = _one(
        cursor,
        "SELECT * FROM host_nodes WHERE host_node_id = %s FOR UPDATE",
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
            "WHERE host_node_id = %s AND version = %s",
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
    cursor.execute(
        "INSERT INTO resource_pools "
        "(pool_id, service_class, pool_state, owner_id, fencing_token, "
        "owner_expires_at_us, owner_state) VALUES (%s, %s, 'active', NULL, 0, 0, "
        "'reconciling_takeover') ON CONFLICT (pool_id) DO NOTHING",
        (binding.pool_id, spec.plan.service_class),
    )
    row = _one(
        cursor,
        "SELECT * FROM resource_pools WHERE pool_id = %s FOR UPDATE",
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
            "UPDATE resource_pools SET owner_id = %s, fencing_token = %s, "
            "owner_expires_at_us = %s, owner_state = 'active', pool_state = 'active', "
            "version = version + 1 WHERE pool_id = %s AND version = %s",
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
    cursor.execute(
        "INSERT INTO workspace_quotas "
        "(workspace_id, max_attempts, held_attempts, quarantined_attempts, "
        "policy_revision) VALUES (%s, %s, 0, 0, %s) ON CONFLICT (workspace_id) DO NOTHING",
        (spec.workspace_id, spec.max_attempts, spec.policy_revision_id),
    )
    row = _one(
        cursor,
        "SELECT * FROM workspace_quotas WHERE workspace_id = %s FOR UPDATE",
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
        "SELECT * FROM host_nodes WHERE host_node_id = %s FOR UPDATE",
        (spec.plan.binding.target_node_id,),
    )
    capacity = _vector_from(row, "allocatable_")
    occupied = _vector_from(row, "held_").add(_vector_from(row, "quarantined_"))
    required = _needed_capacity(spec.plan)
    if not occupied.add(required).fits(capacity):
        raise RegistrationError(
            "host_capacity_insufficient",
            "existing host allocatable vector cannot hold the requested plan; "
            f"need at least {_vector_text(required)} beyond current holds",
        )


def _config_matches(row: Row, spec: RegistrationSpec) -> bool:
    plan, binding = spec.plan, spec.plan.binding
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
) -> RegistrationResult | None:
    heads = _rows(
        cursor,
        "SELECT * FROM runtime_instance_heads WHERE instance_id = %s FOR UPDATE",
        (spec.instance_id,),
    )
    if not heads:
        if _rows(
            cursor,
            "SELECT instance_id FROM engine_instances WHERE instance_id = %s LIMIT 1",
            (spec.instance_id,),
        ):
            raise RegistrationError(
                "reconciliation_required",
                "engine rows exist without a runtime head",
            )
        return None
    head = heads[0]
    current = int(head["current_generation"])
    high_water = int(head["generation_high_water"])
    if spec.generation < current or (spec.generation <= high_water and spec.generation != current):
        raise RegistrationError(
            "stale_instance_generation",
            f"generation {spec.generation} is not above high water {high_water}",
        )
    if spec.generation == current:
        instance = _one(
            cursor,
            "SELECT * FROM engine_instances WHERE instance_id = %s "
            "AND instance_generation = %s FOR UPDATE",
            (spec.instance_id, spec.generation),
        )
        if instance["instance_state"] != "ready":
            raise RegistrationError(
                "instance_not_ready",
                f"generation {spec.generation} is {instance['instance_state']}; reconcile it first",
            )
        if not _config_matches(instance, spec):
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
            "SELECT * FROM runtime_instance_intents WHERE target_node_id = %s "
            "AND intent_id = %s FOR UPDATE",
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
        if readiness.expires_at_us <= now_us:
            refreshed = VerifiedRuntimeReadiness(
                spec.plan.binding,
                spec.instance_id,
                spec.generation,
                int(pool["fencing_token"]),
                spec.plan.topology_digest,
                spec.plan.qualification_digest,
                evidence_digest,
                now_us,
                now_us + readiness_ttl_us,
            )
            cursor.execute(
                "UPDATE runtime_instance_intents SET readiness_document = %s, "
                "updated_at_us = %s, version = version + 1 WHERE target_node_id = %s "
                "AND intent_id = %s AND version = %s",
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
                "UPDATE engine_instances SET ready_at_us = %s, version = version + 1 "
                "WHERE instance_id = %s AND instance_generation = %s AND version = %s",
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
        )
    previous = _one(
        cursor,
        "SELECT * FROM engine_instances WHERE instance_id = %s AND instance_generation = %s "
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
        "SELECT * FROM engine_instances WHERE instance_id = %s AND instance_generation = %s",
        (spec.instance_id, spec.generation),
    )
    head = _one(
        cursor,
        "SELECT * FROM runtime_instance_heads WHERE instance_id = %s",
        (spec.instance_id,),
    )
    intent = _one(
        cursor,
        "SELECT target_node_id, intent_id, instance_id, instance_generation, action, "
        "intent_state, request_digest, version, "
        "observation_document IS NOT NULL AS has_observation, "
        "readiness_document IS NOT NULL AS has_readiness "
        "FROM runtime_instance_intents WHERE target_node_id = %s AND intent_id = %s",
        (instance["host_node_id"], head["current_prepare_intent_id"]),
    )
    pool = _one(
        cursor,
        "SELECT pool_id, service_class, pool_state, owner_id, fencing_token, "
        "owner_expires_at_us, owner_state, version FROM resource_pools WHERE pool_id = %s",
        (instance["pool_id"],),
    )
    quota = _one(
        cursor,
        "SELECT workspace_id, max_attempts, held_attempts, quarantined_attempts, "
        "policy_revision, version FROM workspace_quotas WHERE workspace_id = %s",
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
    connection: psycopg.Connection[Row],
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
) -> RegistrationResult:
    deadline = time.monotonic() + timeout_seconds
    with connection.cursor() as raw:
        cursor = _Cursor(raw, deadline)
        cursor.execute("SELECT set_config('search_path', %s, true)", (schema + ",pg_catalog",))
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
            "SELECT version FROM runtime_instance_heads WHERE instance_id = %s FOR UPDATE",
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
        observation_wire: Row = {
            "intent_id": request.intent_id,
            "target_node_id": spec.plan.binding.target_node_id,
            "pool_id": spec.plan.binding.pool_id,
            "gateway_id": spec.plan.binding.gateway_id,
            "approved_template_id": spec.plan.binding.approved_template_id,
            "binding_revision_id": spec.plan.binding.binding_revision_id,
            "manifest_id": spec.plan.binding.manifest_id,
            "config_digest": spec.plan.binding.config_digest,
            "instance_id": spec.instance_id,
            "instance_generation": spec.generation,
            "action": "prepare",
            "fencing_token": token,
            "intent_state": "succeeded",
            "observed_instance_state": "ready",
            "reserved_memory": {
                "architecture": spec.plan.memory.architecture,
                "peak": _memory_wire(spec.plan.memory.peak),
                "steady": _memory_wire(spec.plan.memory.steady),
            },
            "observed_peak_memory": _memory_wire(spec.plan.peak),
            "created_at": _iso(observed_at_us),
            "updated_at": _iso(observed_at_us),
        }
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
        cursor.execute(
            "UPDATE runtime_intent_deliveries SET delivery_phase = 'blocked', "
            "last_failure_code = 'operator_attested', updated_at_us = %s, "
            "version = version + 1 WHERE event_id = %s AND delivery_phase = 'unsent'",
            (cursor.clock(), prepared.event_id),
        )
        rows = _snapshot(cursor, spec)
        instance = rows["engine_instances"]
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
    connection: psycopg.Connection[Row] | None = None
    try:
        connection = psycopg.connect(
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
    parser.add_argument("--generation", required=True, type=_positive)
    parser.add_argument("--workspace", default="workspace-default")
    parser.add_argument("--max-attempts", type=_positive, default=8)
    parser.add_argument("--owner-id", default="speechnexus-operator")
    parser.add_argument("--fencing-token", type=_positive, default=None)
    parser.add_argument("--owner-ttl-seconds", type=_positive, default=86_400)
    parser.add_argument("--readiness-ttl-seconds", type=_positive, default=86_400)
    parser.add_argument("--intent-deadline-seconds", type=_positive, default=300)
    parser.add_argument("--intent-id", default=None)
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
    parser.add_argument("--host-capacity", type=_vector, default=None)
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
        spec = _spec_from_args(options)
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
        )
    except RegistrationError as error:
        parser.exit(error.exit_code, f"error: {error.code}: {error.message}\n")
    except (ValueError, TypeError) as error:
        parser.exit(EXIT_INVALID, f"error: invalid_request: {error}\n")
    print(_result_json(result))


if __name__ == "__main__":
    main()
