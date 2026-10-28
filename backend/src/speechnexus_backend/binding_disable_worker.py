"""Explicit, independently configured disable coordinator worker."""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Literal, Self
from urllib.parse import urlsplit

import psycopg
from psycopg.conninfo import conninfo_to_dict
from psycopg_pool import PoolClosed, PoolTimeout, TooManyRequests
from pydantic import AnyHttpUrl, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from speechnexus_backend.speech.application.binding_disable_coordinator import (
    BindingDisableCoordinator,
)
from speechnexus_backend.speech.application.leases import LeaseService
from speechnexus_backend.speech.domain.runtime_intents import identifier
from speechnexus_backend.speech.infrastructure.binding_disable_coordinator_postgres import (
    PostgresDisableCoordinatorStore,
)
from speechnexus_backend.speech.infrastructure.binding_disable_targets_schema import (
    ABORT_INTENT_TABLE,
    TARGET_TABLE,
)
from speechnexus_backend.speech.infrastructure.coordination_grants import DIMENSIONS
from speechnexus_backend.speech.infrastructure.gateway_execution_client import (
    GatewayExecutionClient,
)
from speechnexus_backend.speech.infrastructure.leases_postgres import PostgresLeaseTransactions
from speechnexus_backend.speech.infrastructure.postgres_pool import SpeechPool, speech_pool

_LOG = logging.getLogger(__name__)
_SOURCE_COLUMNS = {
    "speech_binding_disable_intents": {"event_id"},
    "speech_publication_lifecycle_events": {"event_id", "revision_id", "action"},
    "admission_attempts": {
        "operation_id",
        "attempt_id",
        "workspace_id",
        "binding_revision_id",
        "policy_revision_id",
    },
    "admission_leases": {
        "operation_id",
        "attempt_id",
        "lease_id",
        "stage_id",
        "pool_id",
        "gateway_id",
        "host_node_id",
        "instance_id",
        "instance_generation",
        "fencing_token",
        "lease_state",
    },
    "submit_intents": {
        "submit_intent_id",
        "operation_id",
        "attempt_id",
        "lease_id",
        "voice_revision_id",
        "command_digest",
        "execution_state",
    },
}
_TARGETS = {TARGET_TABLE, ABORT_INTENT_TABLE}
_SETTLEMENT_FULL_READ = {
    "resource_pools",
    "workspace_quotas",
    "host_nodes",
    "engine_instances",
    "admission_attempts",
    "admission_leases",
}
_SETTLEMENT_READ = {
    "submit_intents": {
        "submit_intent_id",
        "lease_id",
        "operation_id",
        "attempt_id",
        "execution_state",
        "cleanup_state",
        "version",
    },
    "execution_stop_evidence": {
        "evidence_id",
        "submit_intent_id",
        "evidence_kind",
        "evidence_digest",
        "observed_at_us",
        "execution_stopped",
        "resources_released",
    },
}
_SETTLEMENT_UPDATE = {
    "resource_pools": {"version"},
    "workspace_quotas": {"held_attempts", "quarantined_attempts", "version"},
    "host_nodes": {"version"}
    | {f"{prefix}_{dimension}" for prefix in ("held", "quarantined") for dimension in DIMENSIONS},
    "engine_instances": {"held_slots", "quarantined_slots", "version"},
    "admission_attempts": {"quota_state", "version"},
    "admission_leases": {"lease_state", "cleanup_state", "budget_released_at_us", "version"},
    "submit_intents": {"version"},
}


class _PoolDiagnostics(logging.Filter):
    def __init__(self, name: str) -> None:
        super().__init__()
        self._name = name

    def filter(self, record: logging.LogRecord) -> bool:
        if self._name in record.getMessage():
            record.msg, record.args = "Disable coordinator database unavailable", ()
            record.exc_info = record.exc_text = record.stack_info = None
        return True


class BindingDisableWorkerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore", hide_input_in_errors=True)
    process_role: Literal["binding-disable-worker"] = Field(
        validation_alias="SPEECHNEXUS_PROCESS_ROLE"
    )
    database_url: SecretStr = Field(validation_alias="SPEECHNEXUS_DISABLE_DATABASE_URL")
    settlement_database_url: SecretStr = Field(
        validation_alias="SPEECHNEXUS_DISABLE_SETTLEMENT_DATABASE_URL"
    )
    database_schema: str = Field(
        validation_alias="SPEECHNEXUS_DATABASE_SCHEMA", pattern=r"^[a-z][a-z0-9_]{2,62}$"
    )
    gateway_id: str = Field(validation_alias="SPEECHNEXUS_DISABLE_GATEWAY_ID")
    node_id: str = Field(validation_alias="SPEECHNEXUS_DISABLE_NODE_ID")
    gateway_url: AnyHttpUrl = Field(validation_alias="SPEECHNEXUS_EXECUTION_GATEWAY_BASE_URL")
    credential: SecretStr = Field(
        validation_alias="SPEECHNEXUS_EXECUTION_GATEWAY_COMPONENT_CREDENTIAL", min_length=16
    )
    gateway_timeout_seconds: float = Field(
        validation_alias="SPEECHNEXUS_DISABLE_GATEWAY_TIMEOUT_SECONDS",
        gt=0,
        le=60,
        allow_inf_nan=False,
    )
    storage_timeout_seconds: float = Field(
        validation_alias="SPEECHNEXUS_DISABLE_STORAGE_TIMEOUT_SECONDS",
        gt=0,
        le=30,
        allow_inf_nan=False,
    )
    poll_seconds: float = Field(
        validation_alias="SPEECHNEXUS_DISABLE_POLL_SECONDS", ge=0.1, le=300, allow_inf_nan=False
    )
    batch_size: int = Field(validation_alias="SPEECHNEXUS_DISABLE_BATCH_SIZE", ge=1, le=128)
    wait_timeout_ms: int = Field(
        validation_alias="SPEECHNEXUS_DISABLE_WAIT_TIMEOUT_MS", ge=100, le=60000
    )

    @model_validator(mode="after")
    def check_authority(self) -> Self:
        identifier(self.gateway_id)
        identifier(self.node_id)
        for url in (self.database_url, self.settlement_database_url):
            try:
                info = conninfo_to_dict(url.get_secret_value())
            except Exception:
                raise ValueError(
                    "Disable worker requires an explicit PostgreSQL connection"
                ) from None
            if not all(info.get(key) for key in ("user", "dbname")) or not (
                info.get("host") or info.get("hostaddr")
            ):
                raise ValueError("Disable worker requires explicit host, database and principal")
        parsed = urlsplit(str(self.gateway_url))
        if (
            not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Disable gateway must be an origin")
        if parsed.scheme == "http" and parsed.hostname not in ("localhost", "127.0.0.1", "::1"):
            raise ValueError("Disable gateway requires TLS except on loopback")
        token = self.credential.get_secret_value()
        if len(token) > 4096 or any(not 33 <= ord(char) <= 126 for char in token):
            raise ValueError("Invalid gateway credential")
        return self


def _verify_principal(pool: SpeechPool, schema: str) -> None:
    with pool.connection(timeout=3) as connection:
        connection.execute("SET LOCAL statement_timeout='3s'")
        identities = connection.execute(
            "SELECT SESSION_USER AS login, CURRENT_USER AS effective"
        ).fetchone()
        if identities is None:
            raise ValueError("Disable worker identity unavailable")
        for role in dict.fromkeys((identities["login"], identities["effective"])):
            unsafe = connection.execute(
                "SELECT r.rolsuper OR pg_catalog.has_schema_privilege(%s::name,%s,'CREATE') "
                "OR pg_catalog.pg_has_role(%s::name,d.datdba,'MEMBER') OR EXISTS ("
                "SELECT 1 FROM pg_catalog.pg_class c "
                "JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
                "WHERE n.nspname=%s "
                "AND pg_catalog.pg_has_role(%s::name,c.relowner,'MEMBER')) AS forbidden "
                "FROM pg_catalog.pg_roles r CROSS JOIN pg_catalog.pg_database d "
                "WHERE r.rolname=%s AND d.datname=CURRENT_DATABASE()",
                (role, schema, role, schema, role, role),
            ).fetchone()
            if unsafe is None or unsafe["forbidden"]:
                raise ValueError("Disable worker requires a non-owner identity")
            rows = connection.execute(
                "SELECT c.relname, a.attname, "
                "pg_catalog.has_column_privilege(%s::name,c.oid,a.attnum,'SELECT') AS can_read, "
                "pg_catalog.has_column_privilege(%s::name,c.oid,a.attnum,'INSERT') AS can_insert, "
                "pg_catalog.has_column_privilege(%s::name,c.oid,a.attnum,'UPDATE,REFERENCES') "
                "AS can_change, "
                "pg_catalog.has_table_privilege(%s::name,c.oid,'DELETE,TRUNCATE,TRIGGER') "
                "AS destructive "
                "FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
                "JOIN pg_catalog.pg_attribute a ON a.attrelid=c.oid "
                "AND a.attnum>0 AND NOT a.attisdropped "
                "WHERE n.nspname=%s AND c.relkind IN ('r','p','v','m')",
                (role, role, role, role, schema),
            ).fetchall()
            seen: set[str] = set()
            source_columns: dict[str, set[str]] = {table: set() for table in _SOURCE_COLUMNS}
            for row in rows:
                table, column = row["relname"], row["attname"]
                permitted_read = table in _TARGETS or column in _SOURCE_COLUMNS.get(table, set())
                permitted_insert = table in _TARGETS
                if (
                    row["can_change"]
                    or row["destructive"]
                    or bool(row["can_read"]) != permitted_read
                    or bool(row["can_insert"]) != permitted_insert
                ):
                    raise ValueError("Disable worker privileges do not match the coordinator port")
                if table in _TARGETS or table in _SOURCE_COLUMNS:
                    seen.add(table)
                if table in source_columns and permitted_read:
                    source_columns[table].add(column)
            if seen != _TARGETS | _SOURCE_COLUMNS.keys():
                raise ValueError("Disable worker schema is incomplete")
            if source_columns != _SOURCE_COLUMNS:
                raise ValueError("Disable worker source columns are incomplete")


def _verify_settlement_principal(pool: SpeechPool, schema: str) -> None:
    with pool.connection(timeout=3) as connection:
        connection.execute("SET LOCAL statement_timeout='3s'")
        identities = connection.execute(
            "SELECT SESSION_USER AS login, CURRENT_USER AS effective"
        ).fetchone()
        if identities is None:
            raise ValueError("Settlement worker identity unavailable")
        for role in dict.fromkeys((identities["login"], identities["effective"])):
            unsafe = connection.execute(
                "SELECT r.rolsuper OR pg_catalog.has_schema_privilege(%s::name,%s,'CREATE') "
                "OR pg_catalog.pg_has_role(%s::name,d.datdba,'MEMBER') OR EXISTS ("
                "SELECT 1 FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n "
                "ON n.oid=c.relnamespace WHERE n.nspname=%s "
                "AND pg_catalog.pg_has_role(%s::name,c.relowner,'MEMBER')) AS forbidden "
                "FROM pg_catalog.pg_roles r CROSS JOIN pg_catalog.pg_database d "
                "WHERE r.rolname=%s AND d.datname=CURRENT_DATABASE()",
                (role, schema, role, schema, role, role),
            ).fetchone()
            if unsafe is None or unsafe["forbidden"]:
                raise ValueError("Settlement worker requires a non-owner identity")
            rows = connection.execute(
                "SELECT c.relname,a.attname, "
                "pg_catalog.has_column_privilege(%s::name,c.oid,a.attnum,'SELECT') AS can_read, "
                "pg_catalog.has_column_privilege(%s::name,c.oid,a.attnum,'UPDATE') AS can_update, "
                "pg_catalog.has_column_privilege(%s::name,c.oid,a.attnum,'INSERT,REFERENCES') "
                "AS other_write, "
                "pg_catalog.has_table_privilege(%s::name,c.oid,'DELETE,TRUNCATE,TRIGGER') "
                "AS destructive FROM pg_catalog.pg_class c "
                "JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
                "JOIN pg_catalog.pg_attribute a ON a.attrelid=c.oid "
                "AND a.attnum>0 AND NOT a.attisdropped "
                "WHERE n.nspname=%s AND c.relkind IN ('r','p','v','m')",
                (role, role, role, role, schema),
            ).fetchall()
            seen: dict[str, set[str]] = {}
            for row in rows:
                table, column = row["relname"], row["attname"]
                can_read = table in _SETTLEMENT_FULL_READ or column in _SETTLEMENT_READ.get(
                    table, set()
                )
                can_update = column in _SETTLEMENT_UPDATE.get(table, set())
                if (
                    bool(row["can_read"]) != can_read
                    or bool(row["can_update"]) != can_update
                    or row["other_write"]
                    or row["destructive"]
                ):
                    raise ValueError("Settlement worker privileges do not match the lease port")
                if can_read or can_update:
                    seen.setdefault(table, set()).add(column)
            if not seen.keys() >= _SETTLEMENT_FULL_READ:
                raise ValueError("Settlement worker schema is incomplete")
            for requirements in (_SETTLEMENT_READ, _SETTLEMENT_UPDATE):
                for table, columns in requirements.items():
                    if not columns <= seen.get(table, set()):
                        raise ValueError("Settlement worker columns are incomplete")


def _verify_pool_pair(scanner: SpeechPool, settlement: SpeechPool) -> None:
    def identity(pool: SpeechPool) -> tuple[object, ...]:
        with pool.connection(timeout=3) as connection:
            row = connection.execute(
                "SELECT current_database() AS db, inet_server_addr() AS host, "
                "inet_server_port() AS port, SESSION_USER AS login, CURRENT_USER AS effective"
            ).fetchone()
            if row is None:
                raise ValueError("Disable worker database identity unavailable")
            return row["db"], row["host"], row["port"], row["login"], row["effective"]

    a, b = identity(scanner), identity(settlement)
    if a[:3] != b[:3] or set(a[3:]) & set(b[3:]):
        raise ValueError("Disable scan and settlement require distinct roles in the same database")


async def _run_loop(coordinator: BindingDisableCoordinator, poll_seconds: float) -> None:
    while True:
        try:
            result = await coordinator.run_pass()
        except psycopg.Error, PoolClosed, PoolTimeout, TooManyRequests:
            # Never log provider details, which can include private connection data.
            _LOG.warning("Disable scan unavailable; original work will be retried")
        else:
            _LOG.info(
                "Disable scan: attempted=%d queried=%d unavailable=%d settled=%d pending=%d",
                result.attempted,
                result.queried,
                result.unavailable,
                result.settled,
                result.pending,
            )
        await asyncio.sleep(poll_seconds)


async def run(settings: BindingDisableWorkerSettings) -> None:
    pool_name = "speech-disable-" + uuid.uuid4().hex
    diagnostic = _PoolDiagnostics(pool_name)
    logger = logging.getLogger("psycopg.pool")
    logger.addFilter(diagnostic)
    pool = speech_pool(
        settings.database_url.get_secret_value(),
        schema=settings.database_schema,
        name=pool_name,
    )
    settlement_pool = speech_pool(
        settings.settlement_database_url.get_secret_value(),
        schema=settings.database_schema,
        name=pool_name + "-settlement",
    )
    try:
        pool.open()
        settlement_pool.open()
        await asyncio.to_thread(_verify_principal, pool, settings.database_schema)
        await asyncio.to_thread(
            _verify_settlement_principal, settlement_pool, settings.database_schema
        )
        await asyncio.to_thread(_verify_pool_pair, pool, settlement_pool)
        async with GatewayExecutionClient(
            base_url=str(settings.gateway_url),
            credential=settings.credential,
            timeout_seconds=settings.gateway_timeout_seconds,
            max_retries=0,
            backoff_seconds=0,
        ) as gateway:
            coordinator = BindingDisableCoordinator(
                PostgresDisableCoordinatorStore(
                    pool, timeout_seconds=settings.storage_timeout_seconds
                ),
                gateway,
                gateway_id=settings.gateway_id,
                node_id=settings.node_id,
                batch_size=settings.batch_size,
                wait_timeout_ms=settings.wait_timeout_ms,
                settlement=LeaseService(
                    PostgresLeaseTransactions(
                        settlement_pool, timeout_seconds=settings.storage_timeout_seconds
                    )
                ),
            )
            await _run_loop(coordinator, settings.poll_seconds)
    finally:
        pool.close()
        settlement_pool.close()
        logger.removeFilter(diagnostic)


if __name__ == "__main__":
    try:
        asyncio.run(run(BindingDisableWorkerSettings()))
    except Exception:
        message = "Disable worker failed; verify its configuration and dependencies"
        raise SystemExit(message) from None
