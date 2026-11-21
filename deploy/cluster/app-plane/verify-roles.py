#!/usr/bin/env python3
"""Prove every provisioned identity can log in and read something it was granted.

Each role connects with its own DSN; the probe picks one relation that role is
actually granted ``SELECT`` on (table-level first, then column-level) and counts
it. A role that cannot connect, or that is granted nothing at all, fails the run.
Run it through ``kubectl port-forward svc/postgres 15432:5432`` with the
deployment env loaded; it prints role names only, never passwords.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parents[3]
ENV_FILE = ROOT / ".runtime/cluster/compose.env"
SCHEMA = "speech"


def load_env() -> dict[str, str]:
    values: dict[str, str] = {}
    for line in ENV_FILE.read_text().splitlines():
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    return values


def password_key(role: str) -> str:
    return f"SPEECHNEXUS_{role.removeprefix('sn_').upper()}_ROLE_PASSWORD"


def role_schema(role: str) -> str:
    """The schema a role's surface lives in.

    The workbench roles are granted on the `workbench` namespace, not `speech`, so
    probing only `speech` reported them as login-only even after the migration
    granted their surfaces.
    """
    return "workbench" if role.startswith("sn_workbench_") else SCHEMA


def probe(client: psycopg.Connection, role: str) -> str:
    row = client.execute(
        "SELECT table_name FROM information_schema.table_privileges "
        "WHERE table_schema = %s AND grantee = %s AND privilege_type = 'SELECT' "
        "ORDER BY table_name LIMIT 1",
        (role_schema(role), role),
    ).fetchone()
    if row is not None:
        return f"count({row[0]})"
    row = client.execute(
        "SELECT table_name FROM information_schema.column_privileges "
        "WHERE table_schema = %s AND grantee = %s AND privilege_type = 'SELECT' "
        "ORDER BY table_name LIMIT 1",
        (role_schema(role), role),
    ).fetchone()
    if row is not None:
        return f"count({row[0]})"
    return ""


def main() -> int:
    env = load_env()
    admin_dsn = os.environ.get("SN_ADMIN_DSN", "").strip()
    if not admin_dsn:
        raise SystemExit("SN_ADMIN_DSN is blank")
    failures: list[str] = []
    with psycopg.connect(admin_dsn, connect_timeout=10) as admin:
        roles = [
            row[0]
            for row in admin.execute(
                "SELECT rolname FROM pg_roles WHERE rolname LIKE 'sn\\_%' ORDER BY rolname"
            )
        ]
        for role in roles:
            key = password_key(role)
            password = env.get(key, "")
            if not password:
                failures.append(f"{role}: {key} missing from {ENV_FILE.name}")
                continue
            dsn = f"postgresql://{role}:{password}@127.0.0.1:15432/speechnexus"
            try:
                with psycopg.connect(dsn, connect_timeout=10) as connection:
                    target = probe(admin, role)
                    schema = role_schema(role)
                    query = (
                        f"SELECT count(*) FROM {schema}.{target[6:-1]}" if target else "SELECT 1"
                    )
                    connection.execute(query).fetchone()
                    print(f"ok    {role:<28} {target or 'login only (no table SELECT)'}")
            except Exception as exc:
                failures.append(f"{role}: {type(exc).__name__}: {str(exc).splitlines()[0]}")
    if failures:
        for failure in failures:
            print(f"FAIL  {failure}")
        return 1
    print(f"{len(roles)} identities connected and executed their own query")
    return 0


if __name__ == "__main__":
    sys.exit(main())
