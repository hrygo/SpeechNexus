"""Fail-closed privilege audit for the independent execution recovery principal."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast
from uuid import UUID

from speechnexus_gateway.infrastructure.postgres_pool import GatewayPool
from speechnexus_gateway.infrastructure.submit_sql import SubmitCursor

_READ_COLUMNS: dict[str, frozenset[str]] = {
    "submit_intents": frozenset(
        [
            "submit_intent_id",
            "lease_id",
            "operation_id",
            "attempt_id",
            "command_digest",
            "request_digest",
            "request_document",
            "command_redacted_at_us",
            "command_deletion_job_id",
            "command_deletion_evidence_digest",
            "voice_revision_id",
            "deadline_at_us",
            "recorded_at_us",
            "updated_at_us",
            "execution_state",
            "cleanup_state",
            "engine_request_id",
            "dispatched_at_us",
            "error_code",
            "output_stream_id",
            "sample_rate_hz",
            "hard_output_ceiling_samples",
            "generated_pcm_samples",
            "staging_output_object_key",
            "staging_result_document",
            "version",
            "terminated_at_us",
        ]
    ),
    "admission_leases": frozenset(
        [
            "lease_id",
            "operation_id",
            "attempt_id",
            "stage_id",
            "pool_id",
            "host_node_id",
            "gateway_id",
            "instance_id",
            "instance_generation",
            "fencing_token",
            "lease_state",
        ]
    ),
    "admission_attempts": frozenset(
        ["operation_id", "attempt_id", "workspace_id", "binding_revision_id", "policy_revision_id"]
    ),
    "execution_stop_evidence": frozenset(["submit_intent_id", "evidence_kind", "evidence_digest"]),
    "output_sample_intervals": frozenset(
        ["operation_id", "attempt_id", "output_stream_id", "submit_intent_id"]
    ),
    "gateway_abort_intents": frozenset(
        [
            "submit_intent_id",
            "operation_id",
            "attempt_id",
            "fencing_token",
            "reason",
            "wait_timeout_ms",
            "received_at_us",
            "dispatched_at_us",
            "dispatch_outcome",
            "dispatch_attempts",
        ]
    ),
    "speech_publication_installation": frozenset(["singleton", "installation_id"]),
    "speech_job_cancellation_fences": frozenset(["job_id"]),
}


@dataclass(frozen=True, slots=True)
class ExecutionReaderTarget:
    """Persistent identity pinned at startup and checked in each read snapshot."""

    system_identifier: str
    database_name: str
    database_oid: int
    schema_name: str
    schema_oid: int
    installation_id: UUID


@dataclass(frozen=True, slots=True)
class _TargetObservation:
    target: ExecutionReaderTarget
    server_address: str | None
    server_port: int | None
    postmaster_started_at: datetime


def read_execution_reader_target(cursor: SubmitCursor, schema: str) -> _TargetObservation:
    """Observe target identity in the caller's existing read-only snapshot."""
    if not re.fullmatch(r"[a-z][a-z0-9_]{2,62}", schema):
        raise ValueError("invalid execution reader schema")
    cursor.execute(
        "SELECT pg_catalog.current_database() AS database_name, "
        "d.oid::bigint AS database_oid, pg_catalog.current_schema() AS schema_name, "
        "n.oid::bigint AS schema_oid, pg_catalog.current_setting('search_path') AS search_path, "
        "ctl.system_identifier::text AS system_identifier, "
        "pg_catalog.pg_postmaster_start_time() AS postmaster_started_at, "
        "pg_catalog.inet_server_addr()::text AS server_address, "
        "pg_catalog.inet_server_port() AS server_port, "
        "pg_catalog.pg_is_in_recovery() AS in_recovery, "
        "i.singleton, i.installation_id::text AS installation_id "
        f'FROM "{schema}".speech_publication_installation i '
        "CROSS JOIN pg_catalog.pg_database d "
        "CROSS JOIN pg_catalog.pg_namespace n "
        "CROSS JOIN pg_catalog.pg_control_system() ctl "
        "WHERE d.datname=pg_catalog.current_database() AND n.nspname=%s",
        (schema,),
    )
    rows = cursor.fetchall()
    if len(rows) != 1:
        raise ValueError("execution reader installation is unavailable")
    row = rows[0]
    started = row["postmaster_started_at"]
    address, port = row["server_address"], row["server_port"]
    if (
        not isinstance(row["database_name"], str)
        or not row["database_name"]
        or type(row["database_oid"]) is not int
        or row["database_oid"] <= 0
        or row["schema_name"] != schema
        or type(row["schema_oid"]) is not int
        or row["schema_oid"] <= 0
        or row["search_path"] != f"{schema},pg_catalog,pg_temp"
        or not isinstance(row["system_identifier"], str)
        or not row["system_identifier"].isdigit()
        or not isinstance(started, datetime)
        or started.tzinfo is None
        or started.utcoffset() is None
        or not (
            (address is None and port is None)
            or (isinstance(address, str) and type(port) is int and 0 < port <= 65535)
        )
        or row["in_recovery"] is not False
        or row["singleton"] is not True
        or not isinstance(row["installation_id"], str)
    ):
        raise ValueError("execution reader target is invalid")
    target = ExecutionReaderTarget(
        row["system_identifier"],
        row["database_name"],
        row["database_oid"],
        schema,
        row["schema_oid"],
        UUID(row["installation_id"]),
    )
    return _TargetObservation(target, address, port, started)


def verify_execution_reader_target(
    writer_pool: GatewayPool, reader_pool: GatewayPool, schema: str
) -> ExecutionReaderTarget | None:
    """Pin both pools to one installation and compare their current primary instance."""
    deadline = time.monotonic() + 3
    try:
        observations: list[_TargetObservation] = []
        for pool in (writer_pool, reader_pool):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            with pool.connection(timeout=remaining) as connection:
                connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                connection.execute(
                    "SELECT pg_catalog.set_config('statement_timeout',%s,true)",
                    (str(max(1, int(remaining * 1000))),),
                )
                with connection.cursor() as raw:
                    observations.append(
                        read_execution_reader_target(cast(SubmitCursor, raw), schema)
                    )
        if observations[0] != observations[1] or time.monotonic() >= deadline:
            return None
        return observations[0].target
    except Exception:
        # A target mismatch, inaccessible catalog and pool failures all fail closed.
        return None


def verify_execution_reader_authority(pool: GatewayPool, schema: str) -> bool:
    """Audit login, effective role, every reachable role and PUBLIC without changing grants.

    This is stricter than transaction READ ONLY: it rejects writes and DDL through
    inheritance, role switches, built-in capabilities, owners, functions and sequences.
    Unreadable catalogs, unexpected role state and incomplete column access all fail closed.
    """
    if not re.fullmatch(r"[a-z][a-z0-9_]{2,62}", schema):
        return False
    deadline = time.monotonic() + 3
    try:
        with pool.connection(timeout=3) as connection:
            connection.execute("SET TRANSACTION READ ONLY")

            def query(statement: str, params: tuple[object, ...] = ()) -> Any:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError
                connection.execute(
                    "SELECT pg_catalog.set_config('statement_timeout',%s,true)",
                    (str(max(1, int(remaining * 1000))),),
                )
                return connection.execute(statement, params)

            identity = query(
                "SELECT pg_catalog.current_setting('server_version_num')::integer AS version, "
                "SESSION_USER AS login, CURRENT_USER AS effective, "
                "pg_catalog.current_setting('search_path') AS search_path"
            ).fetchone()
            if (
                identity is None
                or identity["version"] < 160000
                or identity["login"] != identity["effective"]
                or identity["search_path"] != f"{schema},pg_catalog,pg_temp"
            ):
                return False
            role_rows = query(
                "WITH RECURSIVE reachable(role_oid) AS ("
                "SELECT oid FROM pg_catalog.pg_roles WHERE rolname IN (%s,%s) UNION "
                "SELECT m.roleid FROM pg_catalog.pg_auth_members AS m "
                "JOIN reachable AS r ON m.member=r.role_oid) "
                "SELECT r.rolname FROM pg_catalog.pg_roles AS r "
                "JOIN reachable ON r.oid=reachable.role_oid ORDER BY r.rolname",
                (identity["login"], identity["effective"]),
            ).fetchall()
            roles = {row["rolname"] for row in role_rows}
            if (
                not roles
                or identity["login"] not in roles
                or any(not isinstance(role, str) or role.startswith("pg_") for role in roles)
            ):
                return False
            expected = {
                (schema, table, column) for table, cols in _READ_COLUMNS.items() for column in cols
            }
            for role in sorted(roles):
                unsafe = query(
                    "SELECT r.rolsuper OR r.rolcreaterole OR r.rolcreatedb OR r.rolreplication "
                    "OR r.rolbypassrls "
                    "OR pg_catalog.pg_has_role(%s::name,d.datdba,'MEMBER') "
                    "OR pg_catalog.has_database_privilege(%s::name,d.oid,'CREATE,TEMPORARY') "
                    "OR pg_catalog.has_database_privilege("
                    "%s::name,d.oid,'CONNECT WITH GRANT OPTION') "
                    "OR EXISTS (SELECT 1 FROM pg_catalog.pg_parameter_acl p "
                    "WHERE pg_catalog.has_parameter_privilege(%s::name,p.parname,'ALTER SYSTEM')) "
                    "OR EXISTS (SELECT 1 FROM pg_catalog.pg_shdepend dep "
                    "WHERE dep.refclassid='pg_catalog.pg_authid'::pg_catalog.regclass "
                    "AND dep.refobjid=r.oid AND dep.deptype='o' AND dep.dbid IN (0,d.oid)) "
                    "OR EXISTS (SELECT 1 FROM pg_catalog.pg_roles x "
                    "WHERE x.oid<>r.oid AND pg_catalog.pg_has_role(%s::name,x.oid,"
                    "'MEMBER WITH ADMIN OPTION')) "
                    "OR EXISTS (SELECT 1 FROM pg_catalog.pg_namespace n "
                    "WHERE ("
                    "pg_catalog.pg_has_role(%s::name,n.nspowner,'MEMBER') OR "
                    "pg_catalog.has_schema_privilege(%s::name,n.oid,'CREATE') OR "
                    "pg_catalog.has_schema_privilege(%s::name,n.oid,'USAGE WITH GRANT OPTION'))) "
                    "OR EXISTS (SELECT 1 FROM pg_catalog.pg_class c "
                    "JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
                    "WHERE pg_catalog.pg_has_role(%s::name,c.relowner,'MEMBER')) AS forbidden "
                    "FROM pg_catalog.pg_roles r CROSS JOIN pg_catalog.pg_database d "
                    "WHERE r.rolname=%s AND d.datname=CURRENT_DATABASE()",
                    (role, role, role, role, role, role, role, role, role, role),
                ).fetchone()
                if unsafe is None or unsafe["forbidden"] is not False:
                    return False
                rows = query(
                    "SELECT n.nspname AS schema_name,c.relname,a.attname, "
                    "c.relrowsecurity AS row_security, "
                    "pg_catalog.has_column_privilege(%s::name,c.oid,a.attnum,'SELECT') "
                    "AS can_read, "
                    "pg_catalog.has_column_privilege(%s::name,c.oid,a.attnum,'INSERT') "
                    "AS can_insert, "
                    "pg_catalog.has_column_privilege(%s::name,c.oid,a.attnum,'UPDATE,REFERENCES') "
                    "AS can_change, "
                    "pg_catalog.has_column_privilege(%s::name,c.oid,a.attnum,"
                    "'SELECT WITH GRANT OPTION') AS can_delegate, "
                    "pg_catalog.has_table_privilege(%s::name,c.oid,"
                    "'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER') AS destructive, "
                    "EXISTS (SELECT 1 FROM pg_catalog.aclexplode("
                    "COALESCE(c.relacl,pg_catalog.acldefault('r',c.relowner))) acl "
                    "WHERE acl.grantee IN (0,(SELECT oid FROM pg_catalog.pg_roles "
                    "WHERE rolname=%s)) "
                    "AND (acl.privilege_type IN ('INSERT','UPDATE','DELETE','TRUNCATE',"
                    "'REFERENCES','TRIGGER','MAINTAIN') OR acl.is_grantable) "
                    "AND NOT (scope.native_settings AND acl.grantee=0 "
                    "AND acl.privilege_type='UPDATE' AND NOT acl.is_grantable)) "
                    "OR EXISTS (SELECT 1 FROM pg_catalog.aclexplode(a.attacl) acl "
                    "WHERE acl.grantee IN (0,(SELECT oid FROM pg_catalog.pg_roles "
                    "WHERE rolname=%s)) "
                    "AND (acl.privilege_type IN ('INSERT','UPDATE','REFERENCES') "
                    "OR acl.is_grantable)) AS explicit_write, "
                    "CASE WHEN n.nspname='pg_catalog' THEN EXISTS ("
                    "SELECT 1 FROM pg_catalog.pg_init_privs p "
                    "CROSS JOIN LATERAL pg_catalog.aclexplode(p.initprivs) acl "
                    "WHERE p.classoid='pg_catalog.pg_class'::pg_catalog.regclass "
                    "AND p.objoid=c.oid AND p.objsubid IN (0,a.attnum) AND p.privtype='i' "
                    "AND acl.grantee=0 AND acl.privilege_type='SELECT') "
                    "WHEN n.nspname='information_schema' AND c.oid::bigint<16384 THEN true "
                    "ELSE false END AS trusted_metadata, scope.native_settings "
                    "FROM pg_catalog.pg_class c "
                    "JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
                    "JOIN pg_catalog.pg_attribute a ON a.attrelid=c.oid "
                    "CROSS JOIN LATERAL (SELECT n.nspname='pg_catalog' "
                    "AND c.relname='pg_settings' AND c.relkind='v' AND c.oid::bigint<16384 "
                    "AND EXISTS (SELECT 1 FROM pg_catalog.pg_init_privs p "
                    "CROSS JOIN LATERAL pg_catalog.aclexplode(p.initprivs) acl "
                    "WHERE p.classoid='pg_catalog.pg_class'::pg_catalog.regclass "
                    "AND p.objoid=c.oid AND p.objsubid=0 AND p.privtype='i' "
                    "AND acl.grantee=0 AND acl.privilege_type='UPDATE' "
                    "AND NOT acl.is_grantable) AS native_settings) scope "
                    "WHERE c.relkind IN ('r','p','v','m','f') "
                    "AND a.attnum>0 AND NOT a.attisdropped",
                    (role, role, role, role, role, role, role),
                ).fetchall()
                seen: set[tuple[str, str, str]] = set()
                for row in rows:
                    key = (row["schema_name"], row["relname"], row["attname"])
                    if any(
                        row[name] is not False
                        for name in ("can_insert", "can_delegate", "explicit_write")
                    ):
                        return False
                    # pg_settings' original PUBLIC UPDATE changes only the caller's
                    # session GUCs, just like SET; no new ACL write is exempted.
                    if row["native_settings"] is not True and (
                        row["can_change"] is not False or row["destructive"] is not False
                    ):
                        return False
                    if key in expected and row["row_security"] is not False:
                        return False
                    if row["can_read"] is True:
                        # Only standard, initially PUBLIC system metadata is exempt.
                        if row["trusted_metadata"] is True:
                            continue
                        if key not in expected:
                            return False
                        seen.add(key)
                    elif row["can_read"] is not False:
                        return False
                if role == identity["effective"] and seen != expected:
                    return False
                other = query(
                    "SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_class c "
                    "JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
                    "WHERE CASE WHEN c.relkind='S' THEN ("
                    "pg_catalog.has_sequence_privilege(%s::name,c.oid,'SELECT,UPDATE,USAGE') "
                    "OR EXISTS (SELECT 1 FROM pg_catalog.aclexplode("
                    "COALESCE(c.relacl,pg_catalog.acldefault('s',c.relowner))) acl "
                    "WHERE acl.grantee IN (0,(SELECT oid FROM pg_catalog.pg_roles "
                    "WHERE rolname=%s)))) "
                    "ELSE false END) "
                    "OR EXISTS (SELECT 1 FROM pg_catalog.pg_proc p "
                    "JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace "
                    "WHERE p.prosecdef "
                    "AND pg_catalog.has_function_privilege(%s::name,p.oid,'EXECUTE')) "
                    "OR EXISTS (SELECT 1 FROM pg_catalog.pg_proc p WHERE "
                    "pg_catalog.has_function_privilege(%s::name,p.oid,"
                    "'EXECUTE WITH GRANT OPTION')) "
                    "OR EXISTS (SELECT 1 FROM pg_catalog.pg_proc p "
                    "JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace "
                    "WHERE n.nspname='pg_catalog' AND p.proname IN "
                    "('lo_create','lo_creat','lo_from_bytea','lo_import','lo_export','lo_unlink',"
                    "'lo_put','lo_truncate','lo_truncate64','lowrite') "
                    "AND pg_catalog.has_function_privilege(%s::name,p.oid,'EXECUTE')) "
                    "AS other_capability",
                    (role, role, role, role, role),
                ).fetchone()
                if other is None or other["other_capability"] is not False:
                    return False
            usable = query(
                "SELECT pg_catalog.has_schema_privilege(CURRENT_USER,%s,'USAGE') AS usable",
                (schema,),
            ).fetchone()
            return usable is not None and usable["usable"] is True and time.monotonic() < deadline
    except Exception:
        # Catalog/permission/pool failures never disclose identities or grant details.
        return False
