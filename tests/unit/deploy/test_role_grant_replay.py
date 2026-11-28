"""The upgrade path: replay declared grants onto existing roles, and nothing else.

A release that declares a new table -- or a new column on an existing one -- has to be
installed on a deployment whose roles already exist. Role creation refuses to run
twice on purpose, so both provisioning scripts grew a ``--grants-only`` mode. These
tests pin the two properties that make that mode safe:

1. it never touches identities (no ``CREATE ROLE``, no ``ALTER ROLE ... PASSWORD``,
   no role DDL of any kind) -- a replay must not be able to mint a role or widen an
   identity; and
2. it applies the **same** declared statements as a first install, minus the role
   DDL. If a grant function gains a statement, both modes reach it through the same
   call; if somebody hand-writes a grant into one mode only, this test fails.

Both checks run the scripts against a recording fake of ``psycopg.connect`` -- in
process, so the backend modules still import the real library -- and assert on the SQL
actually issued rather than on the source text. The lane runs ``unittest`` with no
third-party test dependency, so the fixtures are explicit.
"""

from __future__ import annotations

import os
import re
import sys
import unittest
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).resolve().parents[3]
IDENTITY_DDL = ("CREATE ROLE", "ALTER ROLE", "DROP ROLE", "CREATE USER", "DROP OWNED")
# Statements only a first install runs: they harden PUBLIC or re-assert role
# attributes. A replay must leave every existing privilege and identity alone.
INSTALL_ONLY = ("REVOKE CREATE, TEMPORARY ON DATABASE", "REVOKE CREATE ON SCHEMA")


class RecordingCursor:
    def __init__(self, log: list[str], existing: set[str]) -> None:
        self.log = log
        self.existing = existing
        self.description = None
        self._statement = ""
        self._parameters: tuple[Any, ...] = ()

    def execute(self, statement: Any, parameters: Any = None) -> RecordingCursor:
        self._statement = str(statement)
        self._parameters = tuple(parameters or ())
        self.log.append(self._statement)
        return self

    def fetchone(self) -> Any:
        statement = self._statement
        if "rolsuper OR rolcreatedb" in statement:
            return (False,)
        if "FROM pg_roles WHERE rolname" in statement:
            role = self._parameters[0] if self._parameters else ""
            return (1,) if role in self.existing else None
        if "current_database" in statement:
            return ("speechnexus",)
        return None

    def fetchall(self) -> list[Any]:
        return []

    def __enter__(self) -> RecordingCursor:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class RecordingConnection:
    def __init__(self, existing: set[str]) -> None:
        self.log: list[str] = []
        self.existing = existing

    def execute(self, statement: Any, parameters: Any = None) -> RecordingCursor:
        return RecordingCursor(self.log, self.existing).execute(statement, parameters)

    def cursor(self) -> RecordingCursor:
        return RecordingCursor(self.log, self.existing)

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None

    def close(self) -> None:
        return None

    def __enter__(self) -> RecordingConnection:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def _cluster_roles() -> list[str]:
    source = (ROOT / "deploy/cluster/provision-roles.py").read_text(encoding="utf-8")
    return [
        line.split('"')[1]
        for line in source.splitlines()
        if line.strip().startswith('"sn_') and "_ROLE_PASSWORD" in line
    ]


def _cluster_environment() -> dict[str, str]:
    """The password variables come from the script itself.

    A role added there must not need a second edit here: that is the drift this file
    exists to prevent.
    """
    source = (ROOT / "deploy/cluster/provision-roles.py").read_text(encoding="utf-8")
    names = sorted(set(re.findall(r'os\.environ\["(SPEECHNEXUS_[A-Z_]*_ROLE_PASSWORD)"\]', source)))
    return dict.fromkeys(names, "synthetic-password")


JOB_ENV = {
    "SPEECHNEXUS_JOB_WRITER_DATABASE_URL": "postgresql://sn_job_writer:pw@h/db",
    "SPEECHNEXUS_JOB_ADMISSION_DATABASE_URL": "postgresql://sn_job_admission:pw@h/db",
    "SPEECHNEXUS_JOB_DISPATCH_DATABASE_URL": "postgresql://sn_job_dispatch:pw@h/db",
}
ALL_JOB_ROLES = ["sn_job_writer", "sn_job_admission", "sn_job_dispatch", "sn_gateway"]


class ReplayTestCase(unittest.TestCase):
    """Runs one provisioning script in-process against a recording connection."""

    def setUp(self) -> None:
        import psycopg

        self._psycopg = psycopg
        self._original_connect = psycopg.connect
        self._environment = dict(os.environ)
        self._argv = sys.argv

    def tearDown(self) -> None:
        self._psycopg.connect = cast(Any, self._original_connect)
        os.environ.clear()
        os.environ.update(self._environment)
        sys.argv = self._argv

    def run_script(
        self,
        *,
        script: str,
        arguments: list[str],
        existing: list[str],
        environment: dict[str, str],
    ) -> tuple[int, list[str], str]:
        connection = RecordingConnection(set(existing))
        self._psycopg.connect = cast(Any, lambda *args, **kwargs: connection)
        for name, value in environment.items():
            os.environ[name] = value
        os.environ["SN_ADMIN_DSN"] = "postgresql://admin:pw@127.0.0.1:1/speechnexus"
        sys.argv = [str(ROOT / script), *arguments]
        path = ROOT / script
        namespace: dict[str, Any] = {"__name__": "__main__", "__file__": str(path)}
        source = path.read_text(encoding="utf-8")
        code = 0
        message = ""
        try:
            exec(compile(source, str(path), "exec"), namespace)
        except SystemExit as exit_signal:
            # ``raise SystemExit("why")`` carries the message as the exit value; the
            # provisioning scripts use that to stop on a missing role.
            raw = exit_signal.code
            code = raw if isinstance(raw, int) else 1
            message = "" if raw is None or isinstance(raw, int) else str(raw)
        return code, connection.log, message

    def assert_no_identity_or_install_only(self, statements: list[str]) -> None:
        for statement in statements:
            for ddl in IDENTITY_DDL:
                self.assertNotIn(ddl, statement)
            for only in INSTALL_ONLY:
                self.assertNotIn(only, statement)

    def assert_no_grant(self, statements: list[str]) -> None:
        grants = [statement for statement in statements if "GRANT" in statement.upper()]
        self.assertEqual(grants, [], "it must stop before issuing any grant")

    def test_job_role_replay_applies_the_declared_grants_and_creates_nothing(self) -> None:
        code, statements, message = self.run_script(
            script="scripts/dev-env/provision-job-roles.py",
            arguments=["--grants-only"],
            existing=ALL_JOB_ROLES,
            environment=JOB_ENV,
        )
        self.assertEqual(code, 0, message)
        self.assertTrue(statements, "the replay issued no statement at all")
        self.assert_no_identity_or_install_only(statements)
        # The declared surface, unchanged: a replay is the same list, not a hand-picked
        # subset. The void table's admission read is the release that motivated the mode.
        self.assertTrue(
            any(
                'GRANT SELECT ON "speech".runtime_readiness_voids TO "sn_job_admission"'
                in statement
                for statement in statements
            )
        )

    def test_job_role_replay_refuses_when_a_role_is_missing(self) -> None:
        code, statements, message = self.run_script(
            script="scripts/dev-env/provision-job-roles.py",
            arguments=["--grants-only"],
            existing=["sn_job_writer", "sn_job_dispatch", "sn_gateway"],
            environment=JOB_ENV,
        )
        self.assertNotEqual(code, 0)
        self.assertIn("missing roles: sn_job_admission", message)
        self.assert_no_grant(statements)

    def test_cluster_replay_reuses_the_first_install_statements(self) -> None:
        roles = _cluster_roles()
        environment = _cluster_environment()
        self.assertTrue(roles, "the cluster script names no roles")
        self.assertTrue(environment, "the cluster script reads no role password")

        install_code, install_statements, install_message = self.run_script(
            script="deploy/cluster/provision-roles.py",
            arguments=[],
            existing=[],
            environment=environment,
        )
        self.assertEqual(install_code, 0, install_message)
        replay_code, replay_statements, replay_message = self.run_script(
            script="deploy/cluster/provision-roles.py",
            arguments=["--grants-only"],
            existing=roles,
            environment=environment,
        )
        self.assertEqual(replay_code, 0, replay_message)
        self.assertTrue(replay_statements, "the replay issued no statement at all")
        self.assert_no_identity_or_install_only(replay_statements)
        # Everything a replay issues also runs on the first install: no statement may
        # exist only on the upgrade path.
        self.assertLessEqual(set(replay_statements), set(install_statements))
        # The invariant: every GRANT runs in both modes. Only identity DDL, the PUBLIC
        # hardening and the catalog reads behind it are install-only.
        skipped = set(install_statements) - set(replay_statements)
        self.assertTrue(skipped, "the replay skipped nothing, so it is not the upgrade path")
        for statement in skipped:
            self.assertNotIn("GRANT", statement.upper())
        self.assertTrue(
            any(any(token in statement for token in IDENTITY_DDL) for statement in skipped)
        )
        self.assertTrue(any(token in statement for token in INSTALL_ONLY for statement in skipped))
        self.assertTrue(
            any("runtime_readiness_voids" in statement for statement in replay_statements)
        )

    def test_cluster_replay_refuses_when_a_role_is_missing(self) -> None:
        roles = _cluster_roles()
        self.assertTrue(roles, "the cluster script names no roles")
        code, statements, message = self.run_script(
            script="deploy/cluster/provision-roles.py",
            arguments=["--grants-only"],
            existing=roles[:-1],
            environment=_cluster_environment(),
        )
        self.assertNotEqual(code, 0)
        self.assertIn("missing roles: " + roles[-1], message)
        self.assert_no_grant(statements)

    def test_both_scripts_document_the_replay_mode(self) -> None:
        for script in (
            "deploy/cluster/provision-roles.py",
            "scripts/dev-env/provision-job-roles.py",
        ):
            with self.subTest(script=script):
                source = (ROOT / script).read_text(encoding="utf-8")
                self.assertIn("--grants-only", source)
                self.assertTrue(
                    "never create" in source or "never creates" in source,
                    "the mode's identity guarantee is not documented",
                )

    def test_the_replay_mode_is_named_in_the_runbook(self) -> None:
        # An upgrade path nobody documents is an upgrade path operators will not find;
        # the runbook is what the deployment checklist points at.
        runbook = (ROOT / "docs/operations/minimal-isomorphic-cluster.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("--grants-only", runbook)


if __name__ == "__main__":
    unittest.main()
