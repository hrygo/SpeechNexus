"""Provision the cluster's non-owner runtime roles, or replay their grants.

The first run creates every role and installs the declared grant surface. A later
release that declares a new table (or a new column on an existing one) must be able
to install **only** the grants on a deployment whose roles already exist: role
creation is guarded against existing roles on purpose, so `--grants-only` is the
upgrade path. It refuses to create anything and refuses to run when a role is
missing, so a replay can neither add an identity nor quietly widen the surface: it
re-executes the same declared statements (`create` and `replay` call one function).
"""

import argparse
import os

import psycopg
from psycopg import sql

from speechnexus_backend.speech.infrastructure.admission_grants import (
    binding_disable_admission_grants,
)
from speechnexus_backend.speech.infrastructure.artifact_schema import (
    artifact_reader_grants,
    artifact_retention_grants,
)
from speechnexus_backend.speech.infrastructure.asr_settlement_schema import (
    asr_settlement_writer_grants,
)
from speechnexus_backend.speech.infrastructure.asset_schema import asset_runtime_grants
from speechnexus_backend.speech.infrastructure.binding_disable_targets_schema import (
    disable_coordinator_grants,
    disable_settlement_grants,
)
from speechnexus_backend.speech.infrastructure.binding_draft_schema import binding_draft_grants
from speechnexus_backend.speech.infrastructure.binding_publication_evidence_schema import (
    binding_admin_reader_grants,
)
from speechnexus_backend.speech.infrastructure.builtin_voice_grants import (
    operator_attestation_writer_grants,
    qualification_reader_grants,
)
from speechnexus_backend.speech.infrastructure.coordination_grants import (
    coordination_runtime_grants,
)
from speechnexus_backend.speech.infrastructure.gateway_abort_intent_schema import (
    gateway_abort_grants,
    gateway_binding_barrier_grants,
)
from speechnexus_backend.speech.infrastructure.gateway_query_grants import (
    gateway_keyed_lock_grants,
    gateway_query_grants,
    gateway_query_target_grants,
)
from speechnexus_backend.speech.infrastructure.job_cancellation_fence_schema import (
    gateway_cancellation_fence_grants,
)
from speechnexus_backend.speech.infrastructure.publication_lifecycle_schema import (
    audit_reader_grants,
    binding_lifecycle_grants,
    binding_publication_grants,
)
from speechnexus_backend.speech.infrastructure.publication_schema import (
    model_catalog_reader_grants,
)
from speechnexus_backend.speech.infrastructure.qualified_speech_facts_postgres import (
    speech_synthesis_reader_grants,
)
from speechnexus_backend.speech.infrastructure.quarantine_release_schema import (
    quarantine_release_grants,
)
from speechnexus_backend.speech.infrastructure.settlement_worker_verifier import (
    settlement_worker_grants,
)
from speechnexus_backend.speech.infrastructure.workspace_schema import workspace_writer_grants

schema = "speech"
roles = {
    "sn_api": os.environ["SPEECHNEXUS_API_ROLE_PASSWORD"],
    "sn_gateway": os.environ["SPEECHNEXUS_GATEWAY_ROLE_PASSWORD"],
    "sn_gateway_query": os.environ["SPEECHNEXUS_GATEWAY_QUERY_ROLE_PASSWORD"],
    "sn_workspace_writer": os.environ["SPEECHNEXUS_WORKSPACE_WRITER_ROLE_PASSWORD"],
    "sn_audit_reader": os.environ["SPEECHNEXUS_AUDIT_READER_ROLE_PASSWORD"],
    "sn_model_instance_reader": os.environ["SPEECHNEXUS_MODEL_INSTANCE_READER_ROLE_PASSWORD"],
    "sn_quarantine_release": os.environ["SPEECHNEXUS_QUARANTINE_RELEASE_ROLE_PASSWORD"],
    "sn_catalog_reader": os.environ["SPEECHNEXUS_CATALOG_READER_ROLE_PASSWORD"],
    "sn_artifact_reader": os.environ["SPEECHNEXUS_ARTIFACT_READER_ROLE_PASSWORD"],
    "sn_resource_pool_reader": os.environ["SPEECHNEXUS_RESOURCE_POOL_READER_ROLE_PASSWORD"],
    "sn_binding_list_reader": os.environ["SPEECHNEXUS_BINDING_LIST_READER_ROLE_PASSWORD"],
    "sn_binding_writer": os.environ["SPEECHNEXUS_BINDING_WRITER_ROLE_PASSWORD"],
    "sn_binding_admin_reader": os.environ["SPEECHNEXUS_BINDING_ADMIN_READER_ROLE_PASSWORD"],
    "sn_binding_lifecycle": os.environ["SPEECHNEXUS_BINDING_LIFECYCLE_ROLE_PASSWORD"],
    # Synchronous TTS settlement runs in its own pool, so it must not reuse
    # `sn_api`: that would put the API's schema-wide authority on a second
    # connection and the worker would refuse to start on the wider surface.
    "sn_speech_settlement": os.environ["SPEECHNEXUS_SPEECH_SETTLEMENT_ROLE_PASSWORD"],
    # The workbench unit's two roles: the serving identity and the erasing one.
    # Their grants target the workbench namespace, which the §6 migration step
    # bootstraps and grants; this script only creates the logins.
    "sn_workbench_runtime": os.environ["SPEECHNEXUS_WORKBENCH_RUNTIME_ROLE_PASSWORD"],
    "sn_workbench_cleanup": os.environ["SPEECHNEXUS_WORKBENCH_CLEANUP_ROLE_PASSWORD"],
    "sn_artifact_retention": os.environ["SPEECHNEXUS_ARTIFACT_RETENTION_ROLE_PASSWORD"],
    # Binding-disable worker: scanning and settlement are separate low-privilege
    # identities on the same database, as the reference runbook §12 requires. They
    # must not be the API role, and neither may see the other's port.
    "sn_disable_scan": os.environ["SPEECHNEXUS_DISABLE_SCAN_ROLE_PASSWORD"],
    "sn_disable_settle": os.environ["SPEECHNEXUS_DISABLE_SETTLE_ROLE_PASSWORD"],
    # External-engine qualification: the host-side attestation command appends one
    # approval plus one declaration row and holds nothing wider.
    "sn_operator_attestation": os.environ["SPEECHNEXUS_OPERATOR_ATTESTATION_ROLE_PASSWORD"],
    # The provenance report runs on the host and only needs to read the ledger:
    # qualifications, approvals, the provenance labels and the evidence rows that
    # back them. It must not be the schema owner, or the audit would confirm
    # whatever the writer wrote without any privilege boundary in between.
    "sn_qualification_reader": os.environ["SPEECHNEXUS_QUALIFICATION_READER_ROLE_PASSWORD"],
}
_arguments = argparse.ArgumentParser(description=__doc__)
_arguments.add_argument(
    "--grants-only",
    action="store_true",
    help="apply the declared grants to existing roles; never create or drop a role",
)
_options = _arguments.parse_args()

with psycopg.connect(os.environ["SN_ADMIN_DSN"], autocommit=True) as conn:
    if _options.grants_only:
        # 升级路径:角色已存在,只补声明面。先核对齐全,避免把一条 GRANT 打到半套身份上。
        missing = [
            role
            for role in roles
            if conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone() is None
        ]
        if missing:
            raise SystemExit(
                "missing roles: " + ", ".join(missing) + "; run without --grants-only first"
            )
    if not _options.grants_only:
        # PostgreSQL 角色属于整个集群,即使数据库是专用的也要先核对。
        for role in roles:
            if conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone():
                raise SystemExit(f"role {role} already exists; inspect ownership before proceeding")
        database = conn.execute("SELECT current_database()").fetchone()[0]
        conn.execute(
            sql.SQL("REVOKE CREATE, TEMPORARY ON DATABASE {} FROM PUBLIC").format(
                sql.Identifier(database)
            )
        )
        conn.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
        large_object_writers = (
            "lo_create",
            "lo_creat",
            "lo_from_bytea",
            "lo_import",
            "lo_export",
            "lo_unlink",
            "lo_put",
            "lo_truncate",
            "lo_truncate64",
            "lowrite",
        )
        for (signature,) in conn.execute(
            "SELECT p.oid::regprocedure::text FROM pg_proc p "
            "JOIN pg_namespace n ON n.oid=p.pronamespace "
            "WHERE n.nspname='pg_catalog' AND p.proname=ANY(%s)",
            (list(large_object_writers),),
        ).fetchall():
            # regprocedure is server-generated, including the complete argument types.
            conn.execute(
                sql.SQL("REVOKE EXECUTE ON FUNCTION {} FROM PUBLIC").format(sql.SQL(signature))
            )
        for role, password in roles.items():
            exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone()
            if exists is not None:
                raise SystemExit(f"role {role} appeared during setup; stop without changing it")
            conn.execute(
                sql.SQL(
                    "CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT"
                ).format(sql.Identifier(role))
            )
            conn.execute(
                sql.SQL("ALTER ROLE {} PASSWORD {}").format(
                    sql.Identifier(role), sql.Literal(password)
                )
            )
            memberships = conn.execute(
                "SELECT 1 FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.member "
                "WHERE r.rolname = %s LIMIT 1",
                (role,),
            ).fetchone()
            forbidden = conn.execute(
                "SELECT rolsuper OR rolcreatedb OR rolcreaterole FROM pg_roles WHERE rolname = %s",
                (role,),
            ).fetchone()
            if memberships is not None or forbidden is None or any(forbidden):
                raise SystemExit(f"role {role} is not a fresh non-owner runtime role")

    for statement in coordination_runtime_grants(
        schema, speech_role="sn_api", gateway_role="sn_gateway"
    ):
        conn.execute(statement)
    for statement in asset_runtime_grants(schema, "sn_api"):
        conn.execute(statement)
    # Synchronous TTS reads published voice/binding facts and writes its own
    # settlement root in the admission transaction, so one declaration covers both
    # halves. Without it the factory reports no qualified voice and keeps TTS off
    # even when every TTS key and the recipe file are present.
    for statement in speech_synthesis_reader_grants(schema, "sn_api"):
        conn.execute(statement)
    # Synchronous ASR writes its settlement root in the same admission
    # transaction, so the API identity needs exactly that column-level write.
    for statement in asr_settlement_writer_grants(schema, "sn_api"):
        conn.execute(statement)
    # The settlement runtime opens a second pool under its own identity. The
    # single declaration below is the exact union both workers verify at startup:
    # lease-settlement reads/writes, the TTS root/cancellation surface and the ASR
    # root cursor. Granting anything else to this role makes startup refuse.
    for statement in settlement_worker_grants(schema, "sn_speech_settlement"):
        conn.execute(statement)
    # The retention sweep runs as its own identity: SELECT plus
    # UPDATE(artifact_state, deleted_at_us) and nothing else. It refuses to start on a role that
    # holds any wider authority, so it cannot share `sn_api`'s schema privileges.
    # Binding-disable worker ports. The coordinator scans intents; the settlement
    # port returns capacity after a verified stop. The API keeps the admission
    # port that lets it record a disable request in the same transaction.
    for statement in (
        *disable_coordinator_grants(schema, "sn_disable_scan"),
        *disable_settlement_grants(schema, "sn_disable_settle"),
        *binding_disable_admission_grants(schema, "sn_api"),
    ):
        conn.execute(statement)
    for statement in artifact_retention_grants(schema, "sn_artifact_retention"):
        conn.execute(statement)
    # An operator-attested qualification is declared by its own minimal identity:
    # column-level INSERT on the approval ledger plus one attestation row, no
    # UPDATE/DELETE anywhere and no publication-head authority.
    for statement in operator_attestation_writer_grants(schema, "sn_operator_attestation"):
        conn.execute(statement)
    # The provenance audit reads published qualifications and the evidence rows
    # behind their source label; it is read-only and holds no write anywhere.
    for statement in qualification_reader_grants(schema, "sn_qualification_reader"):
        conn.execute(statement)
    for statement in gateway_query_grants(schema, "sn_gateway_query"):
        conn.execute(statement)
    # The catalog mutex's lock row: the backend side holds the same grant from the
    # Job writer's declaration, and both call the same grant function so neither
    # side can be provisioned with a narrower lock surface by accident.
    for statement in gateway_keyed_lock_grants(schema, "sn_gateway"):
        conn.execute(statement)
    for statement in gateway_cancellation_fence_grants(schema, "sn_gateway"):
        conn.execute(statement)
    for statement in gateway_query_target_grants(schema, "sn_gateway"):
        conn.execute(statement)
    for statement in (
        *gateway_abort_grants(schema, "sn_gateway"),
        *gateway_binding_barrier_grants(schema, "sn_gateway"),
    ):
        conn.execute(statement)
    for statement in workspace_writer_grants(schema, "sn_workspace_writer"):
        conn.execute(statement)
    for statement in audit_reader_grants(schema, "sn_audit_reader"):
        conn.execute(statement)
    for statement in model_catalog_reader_grants(schema, "sn_catalog_reader"):
        conn.execute(statement)
    for statement in artifact_reader_grants(schema, "sn_artifact_reader"):
        conn.execute(statement)
    for statement in binding_draft_grants(schema, "sn_binding_writer"):
        conn.execute(statement)
    for statement in binding_admin_reader_grants(schema, "sn_binding_admin_reader"):
        conn.execute(statement)
    # Lifecycle publication records evidence in the same transaction that
    # advances the selected head, so both declarations belong to one identity.
    for statement in (
        *binding_lifecycle_grants(schema, "sn_binding_lifecycle"),
        *binding_publication_grants(schema, "sn_binding_lifecycle"),
    ):
        conn.execute(statement)
    # The release transaction writes B's stop evidence and A's capacity counters,
    # so its identity is declared by quarantine_release_grants(): schema USAGE
    # plus the exact column surface of the twelve relations it reads, updates and
    # inserts. Do not hand-write this GRANT and do not reuse the owner, the api
    # runtime role or any gateway/worker writer. Upgrading an existing deployment
    # must re-run this function and cancellation_debt_reader_grants: the declared
    # surface now reads three more relations column-level (speech_jobs, the
    # cancellation fence and the reconciliation ledger), and the optional debt
    # probe additionally reads execution_stop_evidence and
    # submit_intents.submit_intent_id.
    for statement in quarantine_release_grants(schema, "sn_quarantine_release"):
        conn.execute(statement)
    # No grants function declares these three projections yet, so mirror the
    # read-only reader functions exactly: schema USAGE plus SELECT on the
    # relations each runtime verifies at startup (READABLE in
    # model_instance_runtime.py, resource_pool_runtime.py and
    # binding_list_runtime.py).
    conn.execute(
        sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
            sql.Identifier(schema), sql.Identifier("sn_model_instance_reader")
        )
    )
    conn.execute(
        sql.SQL("GRANT SELECT ON {}.engine_instances, {}.speech_binding_revisions TO {}").format(
            sql.Identifier(schema),
            sql.Identifier(schema),
            sql.Identifier("sn_model_instance_reader"),
        )
    )
    conn.execute(
        sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
            sql.Identifier(schema), sql.Identifier("sn_resource_pool_reader")
        )
    )
    conn.execute(
        sql.SQL("GRANT SELECT ON {}.engine_instances, {}.resource_pools TO {}").format(
            sql.Identifier(schema),
            sql.Identifier(schema),
            sql.Identifier("sn_resource_pool_reader"),
        )
    )
    conn.execute(
        sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
            sql.Identifier(schema), sql.Identifier("sn_binding_list_reader")
        )
    )
    conn.execute(
        sql.SQL(
            "GRANT SELECT ON {}.speech_binding_families, {}.speech_binding_revisions, "
            "{}.speech_publications, {}.speech_publication_heads, "
            "{}.speech_publication_retirements TO {}"
        ).format(
            sql.Identifier(schema),
            sql.Identifier(schema),
            sql.Identifier(schema),
            sql.Identifier(schema),
            sql.Identifier(schema),
            sql.Identifier("sn_binding_list_reader"),
        )
    )

    # Transcription resolves the selected published binding and checks its
    # retirement through the runtime facts port.
    conn.execute(
        sql.SQL(
            "GRANT SELECT ON {}.speech_publications, {}.speech_binding_revisions, "
            "{}.speech_publication_heads, {}.speech_publication_retirements TO {}"
        ).format(
            sql.Identifier(schema),
            sql.Identifier(schema),
            sql.Identifier(schema),
            sql.Identifier(schema),
            sql.Identifier("sn_api"),
        )
    )
    state = "cluster grants replayed for existing roles" if _options.grants_only else "roles ready"
    print(state + ": " + " ".join(sorted(roles)))
