#!/usr/bin/env python3
"""Provision the three non-owner runtime roles for the public Job path.

`docs/operations/compose-minimal.md` §6 and the asynchronous Job runbook both
point here so the SQL is written once. Run it with the migration owner connection
while `deploy/compose.env` is loaded; it reads the role names and passwords from
the three Job DSNs, so the grants always land on the identity the deployment
actually uses:

    set -a; . deploy/compose.env; set +a
    export SN_ADMIN_DSN=...   # approved secret channel, never the command line
    uv run --all-packages python scripts/dev-env/provision-job-roles.py

The script only creates roles and appends grants. It refuses to run when any of
the three roles already exists, and it never touches product data or migrations.
`--print-statements` renders the SQL without connecting, which is what the CI
guard uses to prove every declared grant group is still named.

The worker's deployment-side startup gate (``deploy/job-startup-gate.py``) checks
the same surface before the dispatcher starts, so a role provisioned with one
group missing stops at container start instead of failing after the first claim.
"""

from __future__ import annotations

import argparse
import os
from urllib.parse import unquote, urlsplit

from psycopg import sql

from speechnexus_backend.speech.infrastructure.coordination_grants import (
    coordination_runtime_grants,
)
from speechnexus_backend.speech.infrastructure.job_cancellation_fence_schema import (
    cancellation_not_submitted_grants,
    cancellation_reconciliation_grants,
    job_cancellation_fence_grants,
)
from speechnexus_backend.speech.infrastructure.job_schema import (
    job_dispatch_grants,
    job_runtime_grants,
)
from speechnexus_backend.speech.infrastructure.public_job_admission_grants import (
    public_job_admission_grants,
)
from speechnexus_backend.speech.infrastructure.public_job_schema import public_job_grants
from speechnexus_backend.speech.infrastructure.public_job_submission_schema import (
    public_job_submission_grants,
)
from speechnexus_backend.speech.infrastructure.voice_draft_schema import (
    voice_draft_grants,
    voice_validation_worker_grants,
)

SCHEMA = "speech"
WRITER_DSN = "SPEECHNEXUS_JOB_WRITER_DATABASE_URL"
ADMISSION_DSN = "SPEECHNEXUS_JOB_ADMISSION_DATABASE_URL"
DISPATCH_DSN = "SPEECHNEXUS_JOB_DISPATCH_DATABASE_URL"
ROLE_KEYS = (WRITER_DSN, ADMISSION_DSN, DISPATCH_DSN)
GATEWAY_ROLE_ENV = "SN_GATEWAY_ROLE"
DEFAULT_GATEWAY_ROLE = "sn_gateway"
KINDS_ENV = "SPEECHNEXUS_JOB_DISPATCH_KINDS"
DEFAULT_KINDS = "asr,tts"

# The API's Job writer also reads publication and asset facts that no Job grant
# function declares, and updates the coordination claim columns. Missing any of
# them stops API startup (speechnexus_backend.job_runtime.REQUIRED_SELECT plus the
# coordination projection), so they are installed here with the Job grants.
WRITER_BASELINE_SELECT = (
    "speech_workspaces",
    "speech_publications",
    "speech_publication_heads",
    "speech_catalog_state",
    "speech_publication_retirements",
    "speech_binding_revisions",
    "speech_assets",
    "speech_pronunciation_revisions",
    "admission_operations",
    "coordination_jobs",
)
WRITER_BASELINE_UPDATE = ("claim_owner_id", "claim_expires_at_us", "claim_version", "job_state")


def identity(key: str) -> tuple[str, str]:
    """Return the role name and password embedded in one Job DSN."""
    raw = os.environ.get(key, "").strip()
    if not raw:
        raise SystemExit(
            f"{key} is blank; write the three Job DSNs into deploy/compose.env first"
            " (docs/operations/compose-minimal.md §6)"
        )
    parts = urlsplit(raw)
    if parts.scheme not in {"postgresql", "postgres"} or not parts.username or not parts.hostname:
        raise SystemExit(f"{key} is not a usable postgresql:// DSN")
    return unquote(parts.username), unquote(parts.password or "")


def configured_kinds(env: dict[str, str] | None = None) -> tuple[str, ...]:
    raw = (env if env is not None else dict(os.environ)).get(KINDS_ENV, DEFAULT_KINDS)
    return tuple(part.strip() for part in raw.split(",") if part.strip()) or ("asr", "tts")


def role_names() -> tuple[str, str, str]:
    """Return (writer, admission, dispatch) and require three distinct roles."""
    writer, admission, dispatch = (identity(key)[0] for key in ROLE_KEYS)
    if len({writer, admission, dispatch}) != 3:
        raise SystemExit("the three Job DSNs must name three distinct non-owner roles")
    return writer, admission, dispatch


def statements(
    schema: str, writer: str, admission: str, dispatch: str, gateway: str, kinds: tuple[str, ...]
) -> list[str]:
    """Render the complete grant set for the three roles, in install order."""
    grants: list[str] = []
    grants += list(job_runtime_grants(schema, writer))
    grants += list(job_dispatch_grants(schema, writer))
    grants += list(public_job_grants(schema, writer))
    grants += list(voice_draft_grants(schema, writer))
    grants += list(public_job_submission_grants(schema, writer))
    grants += list(job_cancellation_fence_grants(schema, writer))
    grants += list(cancellation_not_submitted_grants(schema, writer))
    grants += list(cancellation_reconciliation_grants(schema, writer))
    grants += list(public_job_admission_grants(schema, admission))
    grants += list(job_runtime_grants(schema, dispatch))
    grants += list(job_dispatch_grants(schema, dispatch))
    grants += list(public_job_grants(schema, dispatch))
    grants += list(cancellation_reconciliation_grants(schema, dispatch))
    grants += list(coordination_runtime_grants(schema, speech_role=dispatch, gateway_role=gateway))
    # The dispatcher's startup gate verifies the exact migration head before any
    # lease is claimed. The alembic bookkeeping table is read-only metadata, and a
    # runtime role otherwise cannot read it, so it is granted with the dispatch set.
    grants.append(
        sql.SQL("GRANT SELECT ON {}.alembic_version TO {}")
        .format(sql.Identifier(schema), sql.Identifier(dispatch))
        .as_string(None)
    )
    if "voice_validation" in kinds:
        grants += list(voice_validation_worker_grants(schema, dispatch))
    for table in WRITER_BASELINE_SELECT:
        grants.append(
            sql.SQL("GRANT SELECT ON {}.{} TO {}")
            .format(sql.Identifier(schema), sql.Identifier(table), sql.Identifier(writer))
            .as_string(None)
        )
    grants.append(
        sql.SQL("GRANT UPDATE ({}) ON {}.{} TO {}")
        .format(
            sql.SQL(", ").join(sql.Identifier(column) for column in WRITER_BASELINE_UPDATE),
            sql.Identifier(schema),
            sql.Identifier("coordination_jobs"),
            sql.Identifier(writer),
        )
        .as_string(None)
    )
    return grants


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--schema",
        default=SCHEMA,
        help="schema holding the Job relations (default: speech)",
    )
    parser.add_argument(
        "--print-statements",
        action="store_true",
        help="print the rendered GRANT statements and exit without connecting",
    )
    arguments = parser.parse_args(argv)
    writer, admission, dispatch = role_names()
    gateway = os.environ.get(GATEWAY_ROLE_ENV, DEFAULT_GATEWAY_ROLE)
    kinds = configured_kinds()
    grants = statements(arguments.schema, writer, admission, dispatch, gateway, kinds)
    if arguments.print_statements:
        print(";\n".join(grants) + ";")
        return 0

    import psycopg

    admin = os.environ.get("SN_ADMIN_DSN", "").strip()
    if not admin:
        raise SystemExit("SN_ADMIN_DSN is blank; run this as the migration owner")
    passwords = {
        role: identity(key)[1]
        for key, role in zip(ROLE_KEYS, (writer, admission, dispatch), strict=True)
    }
    with psycopg.connect(admin, autocommit=True) as connection:
        if (
            connection.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (gateway,)).fetchone()
            is None
        ):
            raise SystemExit(
                f"gateway role {gateway} is missing; run the baseline roles in "
                "docs/operations/compose-minimal.md §6 first"
            )
        existing = [
            role
            for role in (writer, admission, dispatch)
            if connection.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone()
        ]
        if existing:
            raise SystemExit(
                f"roles already exist: {', '.join(existing)}; inspect ownership before proceeding"
            )
        for role, password in passwords.items():
            connection.execute(
                sql.SQL(
                    "CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT"
                ).format(sql.Identifier(role))
            )
            connection.execute(
                sql.SQL("ALTER ROLE {} PASSWORD {}").format(
                    sql.Identifier(role), sql.Literal(password)
                )
            )
        for statement in grants:
            connection.execute(statement)
        for role in (*passwords, gateway):
            row = connection.execute(
                "SELECT rolsuper OR rolcreatedb OR rolcreaterole FROM pg_roles WHERE rolname = %s",
                (role,),
            ).fetchone()
            if row is None or any(row):
                raise SystemExit(f"role {role} is not a usable non-owner role")
    print(f"job roles ready: {writer} {admission} {dispatch}; grants: {len(grants)} statements")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
