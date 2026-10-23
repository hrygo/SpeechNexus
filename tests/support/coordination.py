"""Test-only transaction prototype shared by real PG and MySQL qualification.

No server, worker, engine dispatch or production adapter imports this module.
The fixture supplies validated immutable recipes; these helpers prove database
atomicity/serialization, not model qualification or real stop observations.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, cast
from urllib.parse import unquote, urlparse

import psycopg
import pymysql
import rfc8785
from jsonschema.exceptions import ValidationError
from psycopg.rows import dict_row
from pymysql.cursors import DictCursor

from tests.support.contracts import validator

MAX_COUNTER = 2**63 - 1
DIMENSIONS: tuple[str, ...] = tuple(
    cast(
        dict[str, Any],
        validator("internal/coordination-transactions.schema.json", "PhysicalReservation").schema,
    )["properties"]
)
ACTIVE_EXECUTIONS = ("submitting", "executing", "stopping")
TERMINAL_EXECUTIONS = ("succeeded", "failed", "aborted")
# The Scheduler may lock a submit row but never reads its private request document.
# Keep this list aligned with SPEECH_NARROW_SELECT in scripts/dev-env/coordination-grants.py.
SPEECH_SUBMIT_READ_COLUMNS = (
    "submit_intent_id, lease_id, operation_id, attempt_id, execution_state, cleanup_state, version"
)
STOP_KINDS = (
    "engine_abort_ack",
    "engine_terminal_response",
    "instance_terminated_by_controller",
    "operator_verified_instance_terminated",
)
Row = dict[str, Any]


class RejectedError(Exception):
    """Stable transaction rejection; the enclosing transaction must roll back."""


def counter(value: int) -> int:
    if isinstance(value, bool) or not 0 <= value <= MAX_COUNTER:
        raise RejectedError("invalid_counter")
    return value


def digest(value: object) -> str:
    try:
        canonical = rfc8785.dumps(cast(Any, value))
    except rfc8785.IntegerDomainError as error:
        raise RejectedError("invalid_counter") from error
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def validate_request(operation: str, request: Row) -> None:
    try:
        validator("internal/coordination-transactions.schema.json", operation).validate(request)
    except ValidationError as error:
        code = (
            "invalid_counter"
            if error.validator in ("minimum", "maximum")
            else ("invalid_execution_request")
        )
        raise RejectedError(code) from error

    def versions(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if (
                    key.startswith("expected_")
                    and key.endswith("_version")
                    and isinstance(value, str)
                ):
                    counter(int(value))
                else:
                    versions(value)
        elif isinstance(node, list):
            for value in node:
                versions(value)

    versions(request)


@dataclass(frozen=True)
class Recipe:
    """Trusted, immutable manifest projection supplied by the test fixture."""

    binding_revision_id: str
    manifest_id: str
    stages: Mapping[str, str]  # stage_id -> required pool

    @property
    def digest(self) -> str:
        return digest(
            {
                "binding_revision_id": self.binding_revision_id,
                "manifest_id": self.manifest_id,
                "stages": dict(self.stages),
            }
        )


@dataclass(frozen=True)
class Database:
    dialect: Literal["postgresql", "mysql"]
    url: str
    namespace: str

    def connect(self) -> psycopg.Connection[Row] | pymysql.Connection[DictCursor]:
        if not re.fullmatch(r"sn_coord_[a-f0-9]{12}", self.namespace):
            raise ValueError("only isolated test namespaces are permitted")
        if self.dialect == "postgresql":
            connection = psycopg.connect(self.url, row_factory=dict_row)
            connection.execute(f'SET search_path TO "{self.namespace}", pg_catalog')
            connection.execute("SET TIME ZONE 'UTC'")
            connection.commit()
            connection.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
            return connection
        parsed = urlparse(self.url)
        mysql = pymysql.connect(
            host=parsed.hostname or "127.0.0.1",
            port=parsed.port or 3306,
            user=unquote(parsed.username or ""),
            password=unquote(parsed.password or ""),
            database=self.namespace,
            charset="utf8mb4",
            cursorclass=DictCursor,
            autocommit=False,
            connect_timeout=5,
        )
        with mysql.cursor() as cursor:
            cursor.execute("SET time_zone = '+00:00'")
            cursor.execute("SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED")
            cursor.execute("SET SESSION innodb_lock_wait_timeout = 5")
        return mysql

    @contextmanager
    def transaction(self) -> Iterator[Transaction]:
        connection = self.connect()
        try:
            yield Transaction(self.dialect, connection)
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()


class Transaction:
    def __init__(
        self,
        dialect: Literal["postgresql", "mysql"],
        connection: psycopg.Connection[Row] | pymysql.Connection[DictCursor],
    ) -> None:
        self.dialect = dialect
        self.connection = connection

    def execute(self, query: str, parameters: tuple[Any, ...] = ()) -> int:
        with self.connection.cursor() as cursor:
            cursor.execute(query, parameters or None)
            return cursor.rowcount

    def rows(self, query: str, parameters: tuple[Any, ...] = ()) -> list[Row]:
        with self.connection.cursor() as cursor:
            cursor.execute(query, parameters or None)
            return cast(list[Row], list(cursor.fetchall()))

    def one(self, query: str, parameters: tuple[Any, ...] = ()) -> Row:
        rows = self.rows(query, parameters)
        if len(rows) != 1:
            raise RejectedError("not_found")
        return rows[0]

    def now(self) -> int:
        query = (
            "SELECT CAST(EXTRACT(EPOCH FROM clock_timestamp()) * 1000000 AS BIGINT) AS now_us"
            if self.dialect == "postgresql"
            else "SELECT CAST(UNIX_TIMESTAMP(CURRENT_TIMESTAMP(6)) * 1000000 AS SIGNED) AS now_us"
        )
        return counter(int(self.one(query)["now_us"]))

    def lock_pool(self, pool_id: str) -> Row:
        return self.one("SELECT * FROM resource_pools WHERE pool_id = %s FOR UPDATE", (pool_id,))

    def authority(self, expected: Row, pool: Row, *, active: bool = True) -> None:
        if (
            pool["owner_id"] != expected["owner_id"]
            or pool["fencing_token"] != expected["fencing_token"]
            or pool["owner_expires_at_us"] <= self.now()
        ):
            raise RejectedError("stale_fencing_token")
        if active and (pool["owner_state"] != "active" or pool["pool_state"] != "active"):
            raise RejectedError("reconciliation_required")

    def acquire(self, request: Row) -> int:
        validate_request("acquire_owner", request)
        pool = self.lock_pool(request["pool_id"])
        if int(request["expected_version"]) != pool["version"]:
            raise RejectedError("version_conflict")
        now = self.now()  # after waiting for the authority lock
        if pool["owner_expires_at_us"] > now:
            raise RejectedError("owner_busy")
        token = counter(pool["fencing_token"] + 1)
        expiry = counter(now + request["ttl_ms"] * 1000)
        self.execute(
            """UPDATE resource_pools SET owner_id = %s, fencing_token = %s,
               owner_expires_at_us = %s, owner_state = 'reconciling_takeover',
               version = version + 1 WHERE pool_id = %s""",
            (request["owner_id"], token, expiry, request["pool_id"]),
        )
        return token

    def renew(self, request: Row) -> None:
        validate_request("renew_owner", request)
        authority = request["authority"]
        pool = self.lock_pool(authority["pool_id"])
        self.authority(authority, pool, active=False)
        if int(request["expected_version"]) != pool["version"]:
            raise RejectedError("version_conflict")
        expiry = counter(self.now() + request["ttl_ms"] * 1000)
        self.execute(
            """UPDATE resource_pools SET owner_expires_at_us = %s, version = version + 1
               WHERE pool_id = %s""",
            (expiry, authority["pool_id"]),
        )

    def seal(self, request: Row) -> None:
        """Persist a synthetic fixture observation, never perform real fencing."""
        validate_request("seal_barrier", request)
        authority = request["authority"]
        barrier = request["barrier"]
        gateway_id = barrier["gateway_id"]
        pool = self.lock_pool(authority["pool_id"])
        self.authority(authority, pool, active=False)
        if (
            barrier["pool_id"] != authority["pool_id"]
            or barrier["enforced_min_fencing_token"] != authority["fencing_token"]
        ):
            raise RejectedError("stale_fencing_token")
        previous = self.rows(
            """SELECT * FROM gateway_fencing_barriers
               WHERE gateway_id = %s AND pool_id = %s FOR UPDATE""",
            (gateway_id, authority["pool_id"]),
        )
        if previous:
            if previous[0]["min_fencing_token"] > barrier["enforced_min_fencing_token"]:
                raise RejectedError("stale_fencing_token")
            self.execute(
                """UPDATE gateway_fencing_barriers SET min_fencing_token = %s,
                   sealed_evidence_digest = %s, version = version + 1
                   WHERE gateway_id = %s AND pool_id = %s""",
                (
                    barrier["enforced_min_fencing_token"],
                    request["evidence_digest"],
                    gateway_id,
                    authority["pool_id"],
                ),
            )
        else:
            self.execute(
                "INSERT INTO gateway_fencing_barriers VALUES (%s, %s, %s, %s, 1)",
                (
                    gateway_id,
                    authority["pool_id"],
                    barrier["enforced_min_fencing_token"],
                    request["evidence_digest"],
                ),
            )

    def activate(self, request: Row) -> None:
        validate_request("activate_owner", request)
        authority = request["authority"]
        pool = self.lock_pool(authority["pool_id"])
        self.authority(authority, pool, active=False)
        if int(request["expected_version"]) != pool["version"]:
            raise RejectedError("version_conflict")
        missing = self.rows(
            """SELECT i.gateway_id FROM engine_instances i
               LEFT JOIN gateway_fencing_barriers b
               ON b.gateway_id = i.gateway_id AND b.pool_id = i.pool_id
               WHERE i.pool_id = %s AND i.instance_state <> 'stopped'
               AND (b.min_fencing_token IS NULL OR b.min_fencing_token < %s)""",
            (authority["pool_id"], authority["fencing_token"]),
        )
        unresolved = self.rows(
            """SELECT s.submit_intent_id FROM submit_intents s JOIN admission_leases l
               ON l.lease_id = s.lease_id WHERE l.pool_id = %s
               AND s.execution_state IN ('submitting', 'executing', 'stopping')""",
            (authority["pool_id"],),
        )
        if missing or unresolved:
            raise RejectedError("reconciliation_required")
        self.execute(
            """UPDATE resource_pools SET owner_state = 'active',
               version = version + 1 WHERE pool_id = %s""",
            (authority["pool_id"],),
        )

    def admit(
        self,
        request: Row,
        recipe: Recipe,
        *,
        after_first_lease: Callable[[], None] = lambda: None,
    ) -> str:
        """One recipe, quota, all host/instance budgets, leases and job/outbox."""
        validate_request("admit_recipe", request)
        request_hash = digest(request)
        stages: list[Row] = request["stages"]
        authorities = {item["pool_id"]: item for item in request["authorities"]}
        if (
            len({stage["stage_id"] for stage in stages}) != len(stages)
            or {s["stage_id"]: s["pool_id"] for s in stages} != recipe.stages
            or request["recipe_digest"] != recipe.digest
            or request["binding_revision_id"] != recipe.binding_revision_id
            or request["manifest_id"] != recipe.manifest_id
            or set(authorities) != {stage["pool_id"] for stage in stages}
            or len(authorities) != len(request["authorities"])
        ):
            raise RejectedError("recipe_mismatch")
        pools = {key: self.lock_pool(key) for key in sorted(authorities)}
        quota = self.one(
            "SELECT * FROM workspace_quotas WHERE workspace_id = %s FOR UPDATE",
            (request["workspace_id"],),
        )
        hosts = {
            key: self.one("SELECT * FROM host_nodes WHERE host_node_id = %s FOR UPDATE", (key,))
            for key in sorted({stage["host_node_id"] for stage in stages})
        }
        keys = sorted({(stage["instance_id"], stage["instance_generation"]) for stage in stages})
        heads = {
            key: self.one(
                "SELECT * FROM runtime_instance_heads WHERE instance_id = %s FOR UPDATE", (key,)
            )
            for key in sorted({key[0] for key in keys})
        }
        instances = {
            key: self.one(
                """SELECT * FROM engine_instances
                   WHERE instance_id = %s AND instance_generation = %s FOR UPDATE""",
                key,
            )
            for key in keys
        }
        identity = (request["identity"]["operation_id"], request["identity"]["attempt_id"])
        previous = self.rows(
            "SELECT * FROM admission_attempts WHERE operation_id = %s AND attempt_id = %s",
            identity,
        )
        if previous:
            if previous[0]["request_digest"] != request_hash:
                raise RejectedError("idempotency_conflict")
            return "replayed"  # return persisted identity even after lease/owner expiry
        for pool_id, expected in authorities.items():
            self.authority(expected, pools[pool_id])
            if pools[pool_id]["service_class"] != request["service_class"]:
                raise RejectedError("recipe_mismatch")
        if request["deadline_at_us"] <= self.now():
            raise RejectedError("lease_not_active")
        if int(request["expected_quota_version"]) != quota["version"]:
            raise RejectedError("version_conflict")
        if request["policy_revision_id"] != quota["policy_revision"]:
            raise RejectedError("policy_or_resource_disabled")
        if quota["held_attempts"] + quota["quarantined_attempts"] >= quota["max_attempts"]:
            raise RejectedError("quota_exceeded")
        for stage in stages:
            instance = instances[(stage["instance_id"], stage["instance_generation"])]
            host = hosts[stage["host_node_id"]]
            if (
                heads[stage["instance_id"]]["current_generation"] != stage["instance_generation"]
                or instance["host_node_id"] != stage["host_node_id"]
                or instance["pool_id"] != stage["pool_id"]
                or instance["gateway_id"] != stage["gateway_id"]
                or instance["binding_revision_id"] != recipe.binding_revision_id
                or instance["manifest_id"] != recipe.manifest_id
                or instance["instance_state"] != "ready"
                or host["node_state"] != "ready"
            ):
                raise RejectedError("instance_generation_mismatch")
            # Compare the original version once; repeated stages may share this instance.
            if int(stage["expected_instance_version"]) != instance["version"]:
                raise RejectedError("version_conflict")
        # Compute in Python before writing; no overflow-prone SQL multiplication.
        host_deltas = {key: dict.fromkeys(DIMENSIONS, 0) for key in hosts}
        instance_slots = dict.fromkeys(keys, 0)
        vectors: dict[str, dict[str, int]] = {}
        for stage in stages:
            key = (stage["instance_id"], stage["instance_generation"])
            instance = instances[key]
            slots = counter(stage["reserved_slots"])
            instance_slots[key] = counter(instance_slots[key] + slots)
            vector = {
                dimension: counter(instance[f"per_slot_{dimension}"] * slots)
                for dimension in DIMENSIONS
            }
            host = hosts[stage["host_node_id"]]
            if (
                host["memory_architecture"] == "unified_memory"
                and (vector["device_memory_bytes"] or vector["host_memory_bytes"])
            ) or (
                host["memory_architecture"] == "discrete_device_memory"
                and vector["unified_physical_memory_bytes"]
            ):
                raise RejectedError("recipe_mismatch")
            vectors[stage["stage_id"]] = vector
            for dimension in DIMENSIONS:
                host_deltas[stage["host_node_id"]][dimension] += vector[dimension]
        for key, slots in instance_slots.items():
            instance = instances[key]
            if (
                instance["held_slots"] + instance["quarantined_slots"] + slots
                > instance["max_slots"]
            ):
                raise RejectedError("capacity_exhausted")
            if request["service_class"] != "realtime":
                existing_batch = self.one(
                    """SELECT COALESCE(SUM(l.reserved_slots), 0) AS slots
                       FROM admission_leases l JOIN admission_attempts a
                       ON a.operation_id = l.operation_id AND a.attempt_id = l.attempt_id
                       WHERE l.instance_id = %s AND l.instance_generation = %s
                       AND l.lease_state <> 'released' AND a.service_class <> 'realtime'""",
                    key,
                )["slots"]
                if (
                    existing_batch + slots + instance["realtime_reserved_slots"]
                    > instance["max_slots"]
                ):
                    raise RejectedError("capacity_exhausted")
        for key, vector in host_deltas.items():
            host = hosts[key]
            for dimension in DIMENSIONS:
                counter(vector[dimension])
                if (
                    host[f"held_{dimension}"] + host[f"quarantined_{dimension}"] + vector[dimension]
                    > host[f"allocatable_{dimension}"]
                ):
                    raise RejectedError("capacity_exhausted")
        self.execute(
            """INSERT INTO admission_attempts
               (operation_id, attempt_id, workspace_id, binding_revision_id, manifest_id,
                recipe_digest, request_digest, required_stage_ids,
                policy_revision_id, service_class)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                *identity,
                request["workspace_id"],
                recipe.binding_revision_id,
                recipe.manifest_id,
                recipe.digest,
                request_hash,
                json.dumps(sorted(recipe.stages)),
                request["policy_revision_id"],
                request["service_class"],
            ),
        )
        self.execute(
            """UPDATE workspace_quotas SET held_attempts = held_attempts + 1,
               version = version + 1 WHERE workspace_id = %s""",
            (request["workspace_id"],),
        )
        for key, vector in host_deltas.items():
            assignments = ", ".join(f"held_{dim} = held_{dim} + %s" for dim in DIMENSIONS)
            self.execute(
                f"""UPDATE host_nodes SET {assignments},
                    version = version + 1 WHERE host_node_id = %s""",
                (*vector.values(), key),
            )
        for key, slots in instance_slots.items():
            token = authorities[instances[key]["pool_id"]]["fencing_token"]
            self.execute(
                """UPDATE engine_instances SET held_slots = held_slots + %s,
                   last_fencing_token = %s, version = version + 1
                   WHERE instance_id = %s AND instance_generation = %s""",
                (slots, max(instances[key]["last_fencing_token"], token), *key),
            )
        for index, stage in enumerate(stages):
            columns = ", ".join(f"reserved_{dimension}" for dimension in DIMENSIONS)
            placeholders = ", ".join(["%s"] * 20)
            self.execute(
                f"""INSERT INTO admission_leases
                    (lease_id, operation_id, attempt_id, stage_id, host_node_id,
                     instance_id, instance_generation, pool_id, gateway_id, fencing_token,
                     reserved_slots, deadline_at_us, lease_state, cleanup_state, {columns})
                    VALUES ({placeholders})""",
                (
                    stage["lease_id"],
                    *identity,
                    stage["stage_id"],
                    stage["host_node_id"],
                    stage["instance_id"],
                    stage["instance_generation"],
                    stage["pool_id"],
                    stage["gateway_id"],
                    authorities[stage["pool_id"]]["fencing_token"],
                    stage["reserved_slots"],
                    request["deadline_at_us"],
                    "reserved",
                    "pending",
                    *vectors[stage["stage_id"]].values(),
                ),
            )
            if index == 0:
                after_first_lease()
        self.create_job(
            {
                "job": request["job"],
                "identity": {"operation_id": identity[0], "attempt_id": identity[1]},
                "request_digest": request_hash,
            }
        )
        return "committed"

    def create_job(self, request: Row) -> str:
        """Job identity and its outbox event commit together; the business key replays."""
        validate_request("create_job", request)
        job = request["job"]
        identity = request["identity"]
        existing = self.rows(
            """SELECT job_id, request_digest FROM coordination_jobs
               WHERE job_id = %s OR (operation_id = %s AND attempt_id = %s)""",
            (job["job_id"], identity["operation_id"], identity["attempt_id"]),
        )
        if existing:
            if (
                existing[0]["job_id"] != job["job_id"]
                or existing[0]["request_digest"] != request["request_digest"]
            ):
                raise RejectedError("idempotency_conflict")
            return "replayed"
        self.execute(
            """INSERT INTO coordination_jobs (job_id, operation_id, attempt_id, request_digest)
               VALUES (%s, %s, %s, %s)""",
            (
                job["job_id"],
                identity["operation_id"],
                identity["attempt_id"],
                request["request_digest"],
            ),
        )
        self.execute(
            "INSERT INTO coordination_outbox (event_id, job_id, event_digest) VALUES (%s, %s, %s)",
            (job["event_id"], job["job_id"], job["event_digest"]),
        )
        return "committed"

    def claim(self, request: Row) -> int:
        validate_request("claim_job", request)
        # Ordinary conditional claim, independent of SKIP LOCKED and notifications.
        self.one(
            "SELECT job_id FROM coordination_jobs WHERE job_id = %s FOR UPDATE",
            (request["job_id"],),
        )
        now = self.now()
        changed = self.execute(
            """UPDATE coordination_jobs SET claim_owner_id = %s,
               claim_expires_at_us = %s, claim_version = claim_version + 1
               WHERE job_id = %s AND claim_version = %s
               AND claim_expires_at_us <= %s AND job_state = 'pending'""",
            (
                request["worker_id"],
                counter(now + request["ttl_ms"] * 1000),
                request["job_id"],
                request["expected_claim_version"],
                now,
            ),
        )
        if changed != 1:
            raise RejectedError("version_conflict")
        return counter(request["expected_claim_version"] + 1)

    def submit(self, request: Row) -> str:
        validate_request("create_submit", request)
        request_hash = digest(request)
        wire = request["execution_request"]
        if wire["command_digest"] != digest(wire["command"]):
            raise RejectedError("invalid_execution_request")
        pool = self.lock_pool(wire["pool_id"])
        head = self.one(
            "SELECT * FROM runtime_instance_heads WHERE instance_id = %s FOR UPDATE",
            (wire["instance_id"],),
        )
        instance = self.one(
            """SELECT * FROM engine_instances
               WHERE instance_id = %s AND instance_generation = %s FOR UPDATE""",
            (wire["instance_id"], wire["instance_generation"]),
        )
        attempt = self.one(
            """SELECT * FROM admission_attempts
               WHERE operation_id = %s AND attempt_id = %s FOR UPDATE""",
            (wire["operation_id"], wire["attempt_id"]),
        )
        lease = self.one(
            "SELECT * FROM admission_leases WHERE lease_id = %s FOR UPDATE", (wire["lease_id"],)
        )
        barrier = self.one(
            """SELECT * FROM gateway_fencing_barriers
               WHERE gateway_id = %s AND pool_id = %s FOR UPDATE""",
            (wire["gateway_id"], wire["pool_id"]),
        )
        previous = self.rows(
            "SELECT * FROM submit_intents WHERE lease_id = %s", (wire["lease_id"],)
        )
        if previous:
            if (
                previous[0]["request_digest"] != request_hash
                or previous[0]["submit_intent_id"] != wire["submit_intent_id"]
            ):
                raise RejectedError("submit_intent_conflict")
            return "replayed"  # never dispatch again on replay
        self.authority({"owner_id": pool["owner_id"], "fencing_token": wire["fencing_token"]}, pool)
        if (
            wire["fencing_token"] < barrier["min_fencing_token"]
            or wire["fencing_token"] != lease["fencing_token"]
            or wire["fencing_token"] < instance["last_fencing_token"]
        ):
            raise RejectedError("stale_fencing_token")
        for name in (
            "operation_id",
            "attempt_id",
            "stage_id",
            "pool_id",
            "host_node_id",
            "gateway_id",
            "instance_id",
            "instance_generation",
        ):
            if wire[name] != lease[name]:
                raise RejectedError("submit_intent_conflict")
        if (
            lease["lease_state"] not in ("reserved", "executing")
            or lease["cleanup_state"] != "pending"
            or lease["deadline_at_us"] <= self.now()
        ):
            raise RejectedError("lease_not_active")
        deadline = datetime.fromisoformat(wire["deadline_at"]) - datetime(1970, 1, 1, tzinfo=UTC)
        deadline_us = (
            deadline.days * 86_400_000_000 + deadline.seconds * 1_000_000 + deadline.microseconds
        )
        if deadline_us > lease["deadline_at_us"] or deadline_us <= self.now():
            raise RejectedError("lease_not_active")
        if (
            instance["instance_state"] != "ready"
            or head["current_generation"] != wire["instance_generation"]
        ):
            raise RejectedError("instance_draining")
        if any(
            attempt[key] != wire[key]
            for key in ("workspace_id", "binding_revision_id", "policy_revision_id")
        ):
            raise RejectedError("submit_intent_conflict")
        ceiling = wire["max_output_audio_ms"]
        samples = None if ceiling is None else counter(ceiling * wire["sample_rate_hz"] // 1000)
        recorded_at_us = self.now()
        self.execute(
            """INSERT INTO submit_intents
               (submit_intent_id, lease_id, operation_id, attempt_id,
                command_digest, request_digest, request_document, output_stream_id,
                sample_rate_hz, hard_output_ceiling_samples, voice_revision_id,
                deadline_at_us, recorded_at_us, updated_at_us)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                wire["submit_intent_id"],
                wire["lease_id"],
                wire["operation_id"],
                wire["attempt_id"],
                wire["command_digest"],
                request_hash,
                rfc8785.dumps(request).decode("utf-8"),
                request["output_stream_id"],
                wire["sample_rate_hz"],
                samples,
                wire.get("voice_revision_id"),
                deadline_us,
                recorded_at_us,
                recorded_at_us,
            ),
        )
        if request["output_stream_id"] is None:
            return "committed"
        stream_identity = (wire["operation_id"], wire["attempt_id"], request["output_stream_id"])
        streams = self.rows(
            """SELECT * FROM output_stream_heads WHERE operation_id = %s AND attempt_id = %s
               AND output_stream_id = %s FOR UPDATE""",
            stream_identity,
        )
        if streams:
            if streams[0]["sample_rate_hz"] != wire["sample_rate_hz"]:
                raise RejectedError("invalid_execution_request")
        else:
            self.execute(
                "INSERT INTO output_stream_heads VALUES (%s, %s, %s, %s, 1)",
                (*stream_identity, wire["sample_rate_hz"]),
            )
        return "committed"

    def mark_unknown(self, request: Row) -> str:
        """Gateway transaction; scheduler accounting is a separate transaction."""
        validate_request("mark_execution_unknown", request)
        if request["observed_at_us"] > self.now():
            raise RejectedError("invalid_execution_request")
        submit = self.one(
            "SELECT * FROM submit_intents WHERE submit_intent_id = %s FOR UPDATE",
            (request["submit_intent_id"],),
        )
        if submit["execution_state"] in TERMINAL_EXECUTIONS:
            raise RejectedError("submit_intent_conflict")
        if submit["execution_state"] == "unknown_quarantined":
            if submit["error_code"] != request["reason_code"]:
                raise RejectedError("idempotency_conflict")
            return "replayed"
        self.execute(
            """UPDATE submit_intents SET execution_state = 'unknown_quarantined',
               cleanup_state = 'quarantined', error_code = %s,
               updated_at_us = %s, version = version + 1 WHERE submit_intent_id = %s""",
            (request["reason_code"], self.now(), request["submit_intent_id"]),
        )
        return "committed"

    def quarantine(self, request: Row) -> str:
        """Keep all capacity; move held counters once and preserve terminal facts."""
        validate_request("quarantine_lease", request)
        lease_id = request["lease_id"]
        if request["observed_at_us"] > self.now():
            raise RejectedError("invalid_execution_request")
        hint = self.one("SELECT * FROM admission_leases WHERE lease_id = %s", (lease_id,))
        identity = (hint["operation_id"], hint["attempt_id"])
        attempt = self.one(
            "SELECT * FROM admission_attempts WHERE operation_id = %s AND attempt_id = %s",
            identity,
        )
        self.lock_pool(hint["pool_id"])
        self.one(
            "SELECT * FROM workspace_quotas WHERE workspace_id = %s FOR UPDATE",
            (attempt["workspace_id"],),
        )
        host = self.one(
            "SELECT * FROM host_nodes WHERE host_node_id = %s FOR UPDATE", (hint["host_node_id"],)
        )
        self.one(
            """SELECT * FROM engine_instances
               WHERE instance_id = %s AND instance_generation = %s FOR UPDATE""",
            (hint["instance_id"], hint["instance_generation"]),
        )
        attempt = self.one(
            """SELECT * FROM admission_attempts
               WHERE operation_id = %s AND attempt_id = %s FOR UPDATE""",
            identity,
        )
        lease = self.one(
            "SELECT * FROM admission_leases WHERE lease_id = %s FOR UPDATE", (lease_id,)
        )
        submits = self.rows(
            f"""SELECT {SPEECH_SUBMIT_READ_COLUMNS} FROM submit_intents
                WHERE lease_id = %s FOR UPDATE""",
            (lease_id,),
        )
        if lease["lease_state"] == "released":
            raise RejectedError("lease_not_active")
        if lease["lease_state"] == "quarantined":
            # The first observation and its reason stand; only that reason replays.
            if lease["quarantine_reason_code"] != request["reason_code"]:
                raise RejectedError("idempotency_conflict")
            return "replayed"
        if not any(item["execution_state"] == "unknown_quarantined" for item in submits) or any(
            item["execution_state"] in ACTIVE_EXECUTIONS for item in submits
        ):
            raise RejectedError("execution_cleanup_pending")
        assignments = ", ".join(
            f"held_{dim} = held_{dim} - %s, quarantined_{dim} = quarantined_{dim} + %s"
            for dim in DIMENSIONS
        )
        parameters = tuple(
            amount
            for dim in DIMENSIONS
            for amount in (lease[f"reserved_{dim}"], lease[f"reserved_{dim}"])
        )
        self.execute(
            f"UPDATE host_nodes SET {assignments}, version = version + 1 WHERE host_node_id = %s",
            (*parameters, lease["host_node_id"]),
        )
        above_threshold = any(
            (host[f"quarantined_{dim}"] + lease[f"reserved_{dim}"]) * 1_000_000
            > host[f"allocatable_{dim}"] * host["quarantine_threshold_ppm"]
            for dim in DIMENSIONS
        )
        if above_threshold:
            self.execute(
                "UPDATE host_nodes SET node_state = 'quarantined' WHERE host_node_id = %s",
                (lease["host_node_id"],),
            )
        self.execute(
            """UPDATE engine_instances SET held_slots = held_slots - %s,
               quarantined_slots = quarantined_slots + %s, version = version + 1
               WHERE instance_id = %s AND instance_generation = %s""",
            (
                lease["reserved_slots"],
                lease["reserved_slots"],
                lease["instance_id"],
                lease["instance_generation"],
            ),
        )
        self.execute(
            """UPDATE admission_leases SET lease_state = 'quarantined',
               cleanup_state = 'quarantined', quarantine_observed_at_us = %s,
               quarantine_reason_code = %s,
               version = version + 1 WHERE lease_id = %s""",
            (request["observed_at_us"], request["reason_code"], lease_id),
        )
        if attempt["quota_state"] == "held":
            self.execute(
                """UPDATE workspace_quotas SET held_attempts = held_attempts - 1,
                   quarantined_attempts = quarantined_attempts + 1, version = version + 1
                   WHERE workspace_id = %s""",
                (attempt["workspace_id"],),
            )
            self.execute(
                """UPDATE admission_attempts SET quota_state = 'quarantined',
                   version = version + 1 WHERE operation_id = %s AND attempt_id = %s""",
                identity,
            )
        return "committed"

    def stop(self, request: Row) -> str:
        validate_request("record_stop", request)
        evidence = request["evidence"]
        submit = self.one(
            "SELECT * FROM submit_intents WHERE submit_intent_id = %s FOR UPDATE",
            (evidence["submit_intent_id"],),
        )
        previous = self.rows(
            "SELECT * FROM execution_stop_evidence WHERE submit_intent_id = %s",
            (evidence["submit_intent_id"],),
        )
        if previous:
            if any(previous[0][key] != evidence[key] for key in evidence):
                raise RejectedError("idempotency_conflict")
            if submit["execution_state"] != request["terminal_state"]:
                raise RejectedError("submit_intent_conflict")
            return "replayed"
        state = request["terminal_state"]
        kind = evidence["evidence_kind"]
        if (
            kind not in STOP_KINDS
            or not evidence["execution_stopped"]
            or not evidence["resources_released"]
            or evidence["observed_at_us"] > self.now()
            or state not in ("succeeded", "failed", "aborted")
            or (state == "succeeded" and kind != "engine_terminal_response")
            or (state == "failed") != (request["error_code"] is not None)
            or submit["execution_state"] in ("succeeded", "failed", "aborted")
            or (submit["execution_state"] == "unknown_quarantined" and state != "aborted")
        ):
            raise RejectedError("execution_cleanup_pending")
        self.execute(
            """INSERT INTO execution_stop_evidence
               (evidence_id, submit_intent_id, evidence_kind, evidence_digest,
                observed_at_us, reason, execution_stopped, resources_released)
               VALUES (%s, %s, %s, %s, %s, %s, 1, 1)""",
            tuple(
                evidence[key]
                for key in (
                    "evidence_id",
                    "submit_intent_id",
                    "evidence_kind",
                    "evidence_digest",
                    "observed_at_us",
                    "reason",
                )
            ),
        )
        self.execute(
            """UPDATE submit_intents SET execution_state = %s, cleanup_state = 'complete',
               error_code = %s, terminated_at_us = %s,
               updated_at_us = %s, version = version + 1 WHERE submit_intent_id = %s""",
            (
                state,
                request["error_code"],
                evidence["observed_at_us"],
                self.now(),
                evidence["submit_intent_id"],
            ),
        )
        return "committed"

    def redact(self, request: Row) -> str:
        """Gateway cleanup after a trusted deletion coordinator has authorized this job."""
        validate_request("redact_execution_command", request)
        hint = self.one(
            "SELECT lease_id FROM submit_intents WHERE submit_intent_id = %s",
            (request["submit_intent_id"],),
        )
        lease = self.one(
            "SELECT * FROM admission_leases WHERE lease_id = %s FOR UPDATE", (hint["lease_id"],)
        )
        submit = self.one(
            "SELECT * FROM submit_intents WHERE submit_intent_id = %s FOR UPDATE",
            (request["submit_intent_id"],),
        )
        if submit["request_document"] is None:
            if (
                submit["command_deletion_job_id"] != request["deletion_job_id"]
                or submit["command_deletion_evidence_digest"] != request["deletion_evidence_digest"]
            ):
                raise RejectedError("idempotency_conflict")
            return "replayed"
        if (
            lease["lease_state"] != "released"
            or submit["execution_state"] not in ("succeeded", "failed", "aborted")
            or submit["cleanup_state"] not in ("complete", "not_required")
        ):
            raise RejectedError("execution_cleanup_pending")
        if submit["version"] != int(request["expected_version"]):
            raise RejectedError("version_conflict")
        now = self.now()
        self.execute(
            """UPDATE submit_intents SET request_document = NULL, command_redacted_at_us = %s,
               command_deletion_job_id = %s, command_deletion_evidence_digest = %s,
               updated_at_us = %s, version = version + 1 WHERE submit_intent_id = %s""",
            (
                now,
                request["deletion_job_id"],
                request["deletion_evidence_digest"],
                now,
                request["submit_intent_id"],
            ),
        )
        return "committed"

    def release(self, request: Row) -> str:
        # Locate immutable identity without locks; recheck after locking parents.
        validate_request("release_lease", request)
        lease_id = request["lease_id"]
        hint = self.one("SELECT * FROM admission_leases WHERE lease_id = %s", (lease_id,))
        attempt_hint = self.one(
            "SELECT * FROM admission_attempts WHERE operation_id = %s AND attempt_id = %s",
            (hint["operation_id"], hint["attempt_id"]),
        )
        self.lock_pool(hint["pool_id"])
        self.one(
            "SELECT * FROM workspace_quotas WHERE workspace_id = %s FOR UPDATE",
            (attempt_hint["workspace_id"],),
        )
        self.one(
            "SELECT * FROM host_nodes WHERE host_node_id = %s FOR UPDATE", (hint["host_node_id"],)
        )
        self.one(
            """SELECT * FROM engine_instances
               WHERE instance_id = %s AND instance_generation = %s FOR UPDATE""",
            (hint["instance_id"], hint["instance_generation"]),
        )
        attempt = self.one(
            """SELECT * FROM admission_attempts
               WHERE operation_id = %s AND attempt_id = %s FOR UPDATE""",
            (hint["operation_id"], hint["attempt_id"]),
        )
        lease = self.one(
            "SELECT * FROM admission_leases WHERE lease_id = %s FOR UPDATE", (lease_id,)
        )
        if lease["lease_state"] == "released":
            return "replayed"
        if lease["version"] != int(request["expected_version"]):
            raise RejectedError("version_conflict")
        submits = self.rows(
            f"""SELECT {SPEECH_SUBMIT_READ_COLUMNS} FROM submit_intents
                WHERE lease_id = %s FOR UPDATE""",
            (lease_id,),
        )
        if any(
            item["execution_state"] not in ("succeeded", "failed", "aborted")
            or item["cleanup_state"] != "complete"
            or not self.rows(
                "SELECT evidence_id FROM execution_stop_evidence WHERE submit_intent_id = %s",
                (item["submit_intent_id"],),
            )
            for item in submits
        ):
            raise RejectedError("execution_cleanup_pending")
        # A locked never-submitted lease cannot acquire a new submit while releasing.
        source = "quarantined" if lease["lease_state"] == "quarantined" else "held"
        assignments = ", ".join(f"{source}_{dim} = {source}_{dim} - %s" for dim in DIMENSIONS)
        self.execute(
            f"UPDATE host_nodes SET {assignments}, version = version + 1 WHERE host_node_id = %s",
            (*(lease[f"reserved_{dimension}"] for dimension in DIMENSIONS), lease["host_node_id"]),
        )
        self.execute(
            f"""UPDATE engine_instances SET {source}_slots = {source}_slots - %s,
                version = version + 1 WHERE instance_id = %s AND instance_generation = %s""",
            (lease["reserved_slots"], lease["instance_id"], lease["instance_generation"]),
        )
        self.execute(
            """UPDATE admission_leases SET lease_state = 'released', cleanup_state = 'complete',
               budget_released_at_us = %s, version = version + 1 WHERE lease_id = %s""",
            (self.now(), lease_id),
        )
        remaining = self.rows(
            """SELECT lease_id FROM admission_leases
               WHERE operation_id = %s AND attempt_id = %s AND lease_state <> 'released'""",
            (lease["operation_id"], lease["attempt_id"]),
        )
        if not remaining:
            quota_source = (
                "quarantined_attempts"
                if attempt["quota_state"] == "quarantined"
                else "held_attempts"
            )
            self.execute(
                f"""UPDATE workspace_quotas SET {quota_source} = {quota_source} - 1,
                    version = version + 1 WHERE workspace_id = %s""",
                (attempt_hint["workspace_id"],),
            )
            self.execute(
                """UPDATE admission_attempts SET quota_state = 'released', version = version + 1
                   WHERE operation_id = %s AND attempt_id = %s""",
                (lease["operation_id"], lease["attempt_id"]),
            )
        return "committed"

    def append(self, request: Row) -> str:
        validate_request("append_interval", request)
        submit = self.one(
            "SELECT * FROM submit_intents WHERE submit_intent_id = %s FOR UPDATE",
            (request["submit_intent_id"],),
        )
        if submit["output_stream_id"] is None:
            raise RejectedError("invalid_execution_request")
        stream_identity = (submit["operation_id"], submit["attempt_id"], submit["output_stream_id"])
        self.one(
            """SELECT * FROM output_stream_heads WHERE operation_id = %s AND attempt_id = %s
               AND output_stream_id = %s FOR UPDATE""",
            stream_identity,
        )
        if (
            request["output_stream_id"] != submit["output_stream_id"]
            or request["sample_rate_hz"] != submit["sample_rate_hz"]
        ):
            raise RejectedError("invalid_execution_request")
        previous = self.rows(
            "SELECT * FROM output_sample_intervals WHERE interval_id = %s",
            (request["interval_id"],),
        )
        if previous:
            keys = ("submit_intent_id", "meter_kind", "sample_start", "sample_end")
            if any(previous[0][key] != request[key] for key in keys):
                raise RejectedError("idempotency_conflict")
            return "replayed"
        if (
            request["output_stream_id"] != submit["output_stream_id"]
            or request["sample_rate_hz"] != submit["sample_rate_hz"]
            or request["meter_kind"] not in ("generated", "delivered", "played")
            or request["sample_start"] < 0
            or request["sample_end"] <= request["sample_start"]
        ):
            raise RejectedError("invalid_execution_request")
        if (
            submit["hard_output_ceiling_samples"] is not None
            and request["sample_end"] > submit["hard_output_ceiling_samples"]
        ):
            raise RejectedError("output_limit_exceeded")
        overlaps = self.rows(
            """SELECT interval_id FROM output_sample_intervals
               WHERE operation_id = %s AND attempt_id = %s AND output_stream_id = %s
               AND meter_kind = %s
               AND sample_start < %s AND sample_end > %s""",
            (
                *stream_identity,
                request["meter_kind"],
                request["sample_end"],
                request["sample_start"],
            ),
        )
        if overlaps:
            raise RejectedError("output_overlap")
        self.execute(
            """INSERT INTO output_sample_intervals
               (interval_id, submit_intent_id, meter_kind, sample_start, sample_end,
                operation_id, attempt_id, output_stream_id)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
            tuple(
                request[key]
                for key in (
                    "interval_id",
                    "submit_intent_id",
                    "meter_kind",
                    "sample_start",
                    "sample_end",
                )
            )
            + stream_identity,
        )
        if request["meter_kind"] == "generated":
            self.execute(
                """UPDATE submit_intents SET generated_pcm_samples = %s,
                   updated_at_us = %s, version = version + 1 WHERE submit_intent_id = %s""",
                (
                    max(submit["generated_pcm_samples"], request["sample_end"]),
                    self.now(),
                    request["submit_intent_id"],
                ),
            )
        return "committed"
