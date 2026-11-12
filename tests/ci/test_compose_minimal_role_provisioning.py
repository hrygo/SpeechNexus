"""Guard the compose-minimal role provisioning steps against doc/code drift.

The runbook creates five non-owner roles and calls repository grant functions by
name, states the current migration head, and runs host commands that import the
workspace members. A renamed grant function, a re-pointed head, or a host
command that cannot import ``speechnexus_backend`` turns the runbook into
instructions that fail on a clean machine, and a stale 503 code sends the
operator to the wrong endpoint.

These checks parse text only: they run under the system interpreter, like the
other ``tests/ci`` guards, and never import the backend or open a database.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOC = ROOT / "docs/operations/compose-minimal.md"
COMPOSE = ROOT / "deploy/compose.yaml"
ENV_EXAMPLE = ROOT / "deploy/compose.env.example"
BACKEND = ROOT / "backend/src"
VERSIONS = BACKEND / "speechnexus_backend/speech/infrastructure/migrations/versions"

GRANT_CALL = re.compile(r"\b([a-z][a-z0-9_]*_grants)\(")
GRANT_BACKTICK = re.compile(r"`([a-z][a-z0-9_]*_grants)`")
GRANT_DEFINITION = re.compile(r"^def ([a-z][a-z0-9_]*_grants)\(", re.MULTILINE)
DATABASE_URL = re.compile(r"\bSPEECHNEXUS_[A-Z0-9_]*_DATABASE_URL\b")
ROLE_PASSWORD_USE = re.compile(
    r'"(sn_[a-z_]+)": os\.environ\["(SPEECHNEXUS_[A-Z0-9_]*_ROLE_PASSWORD)"\]'
)
SETTINGS_FIELD = re.compile(r"^\s*([a-z0-9_]+_database_url)\s*:", re.MULTILINE)
UNAVAILABLE_CODE = re.compile(r"`503 ([a-z][a-z0-9_]+)`")
SHELL_BLOCK = re.compile(r"```bash\n(.*?)```", re.DOTALL)
REVISION = re.compile(r'^revision = "([^"]+)"$', re.MULTILINE)
DOWN_REVISION = re.compile(r'^down_revision = "([^"]+)"$', re.MULTILINE)


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def backend_sources() -> list[str]:
    return [read(path) for path in BACKEND.rglob("*.py")]


def defined_grant_functions() -> set[str]:
    names: set[str] = set()
    for source in backend_sources():
        names |= set(GRANT_DEFINITION.findall(source))
    return names


def named_grant_functions(document: str) -> set[str]:
    return set(GRANT_CALL.findall(document)) | set(GRANT_BACKTICK.findall(document))


def migration_head() -> str:
    revisions: set[str] = set()
    down_revisions: set[str] = set()
    for path in VERSIONS.glob("*.py"):
        source = read(path)
        revision = REVISION.search(source)
        if revision is None:
            raise AssertionError(f"{path.name} declares no revision")
        revisions.add(revision.group(1))
        down_revision = DOWN_REVISION.search(source)
        if down_revision is not None:
            down_revisions.add(down_revision.group(1))
    heads = sorted(revisions - down_revisions)
    if len(heads) != 1:
        raise AssertionError(f"expected one migration head, found {heads}")
    return heads[0]


class ComposeMinimalRoleProvisioningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.document = read(DOC)

    def test_every_grant_function_named_in_the_runbook_exists(self) -> None:
        missing = sorted(named_grant_functions(self.document) - defined_grant_functions())
        self.assertEqual(missing, [], f"runbook names undefined grant functions: {missing}")

    def test_the_runbook_states_the_current_migration_head(self) -> None:
        head = migration_head()
        self.assertTrue(
            f"仓库当前迁移链的 head 为 `{head}`" in self.document,
            "runbook must state the migration head this repository actually has",
        )

    def test_host_commands_that_import_the_backend_install_workspace_members(self) -> None:
        # The root pyproject is `package = false`; a plain `uv run` installs no
        # workspace member, so `import speechnexus_backend` fails on a clean
        # checkout. Only the blocks that import the backend need the flag.
        offenders = [
            line
            for block in SHELL_BLOCK.findall(self.document)
            if "speechnexus_backend" in block
            for line in block.splitlines()
            if line.startswith("uv run ") and "--all-packages" not in line
        ]
        self.assertEqual(offenders, [], f"host commands missing --all-packages: {offenders}")

    def test_runbook_database_urls_are_known_to_compose_or_the_backend(self) -> None:
        known = set(DATABASE_URL.findall(read(COMPOSE)))
        known |= set(DATABASE_URL.findall(read(ENV_EXAMPLE)))
        for source in backend_sources():
            known |= set(DATABASE_URL.findall(source))
            known |= {"SPEECHNEXUS_" + field.upper() for field in SETTINGS_FIELD.findall(source)}
        unknown = sorted(set(DATABASE_URL.findall(self.document)) - known)
        self.assertEqual(unknown, [], f"runbook names unknown database URLs: {unknown}")

    def test_each_provisioned_role_has_a_password_source(self) -> None:
        example = read(ENV_EXAMPLE)
        documented = set(
            re.findall(
                r"^export (SPEECHNEXUS_[A-Z0-9_]*_ROLE_PASSWORD)=", self.document, re.MULTILINE
            )
        )
        roles = set(ROLE_PASSWORD_USE.findall(self.document))
        self.assertTrue(roles, "runbook must read each role password from the environment")
        missing = sorted(
            f"{role} -> {password}"
            for role, password in roles
            if password not in example and password not in documented
        )
        self.assertEqual(missing, [], f"roles read passwords no step provides: {missing}")

    def test_documented_unavailable_codes_exist_in_the_http_handlers(self) -> None:
        codes = set(UNAVAILABLE_CODE.findall(self.document))
        self.assertTrue(codes, "runbook must name the 503 codes operators will see")
        sources = "\n".join(backend_sources())
        unknown = sorted(code for code in codes if f'"{code}"' not in sources)
        self.assertEqual(unknown, [], f"documented 503 codes are not raised: {unknown}")

    def test_the_base_compose_forwards_the_workspace_writer_dsn(self) -> None:
        # The runbook's workspace writer step ends by writing this URL; the base
        # api service must keep forwarding it or the step enables nothing.
        self.assertIn(
            "SPEECHNEXUS_WORKSPACE_WRITER_DATABASE_URL: "
            "${SPEECHNEXUS_WORKSPACE_WRITER_DATABASE_URL:-}",
            read(COMPOSE),
        )


if __name__ == "__main__":
    unittest.main()
