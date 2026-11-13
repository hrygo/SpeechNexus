"""Guard the compose-minimal role provisioning steps against doc/code drift.

The runbook creates seven non-owner roles and calls repository grant functions by
name, states the current migration head, and runs host commands that import the
workspace members. A renamed grant function, a re-pointed head, or a host
command that cannot import ``speechnexus_backend`` turns the runbook into
instructions that fail on a clean machine, and a stale 503 code sends the
operator to the wrong endpoint.

The instance-reader step is the one role whose authority is not declared by a
``*_grants`` function: the runbook grants its two relations inline, so the
function-name check above cannot see it. ``model_instance_reader_relations``
closes that blind spot by comparing the inline ``GRANT`` against the relations
the runtime verifies at startup (``READABLE`` in ``model_instance_runtime.py``).

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
READABLE = re.compile(r"^READABLE = \(([^)]*)\)$", re.MULTILINE)
INSTANCE_READER_GRANT = re.compile(
    r'"GRANT SELECT ON (?P<relations>[^"]*) TO \{\}"\)\s*\.format\((?P<arguments>.*?)\n\s*\)',
    re.DOTALL,
)
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


def model_instance_reader_relations() -> list[str]:
    source = read(BACKEND / "speechnexus_backend/model_instance_runtime.py")
    declaration = READABLE.search(source)
    if declaration is None:
        raise AssertionError("model_instance_runtime.py declares no READABLE tuple")
    return re.findall(r'"([a-z0-9_]+)"', declaration.group(1))


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
        unknown: list[str] = []
        for code in sorted(codes):
            if f'"{code}"' in sources:
                continue
            # The pronunciation router composes its wire codes from one stable
            # prefix plus the shared error table, so `pronunciation_unavailable`
            # is correct while the literal never appears in a handler. Keep the
            # exception narrow: the prefix must be applied by string concatenation
            # in the same module that declares the bare code.
            prefix, _, bare = code.rpartition("_")
            built = f'"{prefix}_" + code' in sources and f'"{bare}"' in sources
            if not built:
                unknown.append(code)
        self.assertEqual(unknown, [], f"documented 503 codes are not raised: {unknown}")

    def test_the_base_compose_forwards_the_model_instance_reader_dsn(self) -> None:
        # Same contract as the workspace writer: the runbook's instance-reader
        # step ends by writing this URL, so the base api service must keep
        # forwarding it with the empty default.
        self.assertIn(
            "SPEECHNEXUS_MODEL_INSTANCE_READER_DATABASE_URL: "
            "${SPEECHNEXUS_MODEL_INSTANCE_READER_DATABASE_URL:-}",
            read(COMPOSE),
        )

    def test_the_instance_reader_grant_matches_the_runtime_readable_relations(self) -> None:
        relations = model_instance_reader_relations()
        self.assertTrue(relations, "runtime must declare the relations it verifies")
        expected = ", ".join("{}." + relation for relation in relations)
        grants = [
            found.group("relations")
            for found in INSTANCE_READER_GRANT.finditer(self.document)
            if '"sn_model_instance_reader"' in found.group("arguments")
        ]
        # Both provisioning paths grant this role inline: the new-install script
        # and the upgrade script. A renamed relation in either one drifts from
        # the runtime's own startup check.
        self.assertEqual(len(grants), 2, "both provisioning scripts must grant the instance reader")
        self.assertEqual(grants, [expected, expected], "grant drifted from READABLE")
        self.assertNotIn(
            "model_instance_reader_grants(",
            self.document,
            "no such helper exists; the runbook must grant inline until one does",
        )

    def test_the_base_compose_forwards_the_quarantine_release_dsn(self) -> None:
        # The release endpoint is fail-closed at startup, so without this line the
        # role the runbook provisions could never be reached from the api service.
        self.assertIn(
            "SPEECHNEXUS_QUARANTINE_RELEASE_DATABASE_URL: "
            "${SPEECHNEXUS_QUARANTINE_RELEASE_DATABASE_URL:-}",
            read(COMPOSE),
        )

    def test_both_provisioning_paths_install_the_quarantine_release_role(self) -> None:
        # The authority is declared by the repository grant function, so both
        # scripts (fresh install and upgrade of an existing deployment) must call
        # it for the same role name, and that role's password must come from the
        # environment exactly like the other optional identities.
        calls = re.findall(
            r'quarantine_release_grants\(schema, "(sn_quarantine_release)"\)', self.document
        )
        self.assertEqual(len(calls), 2, "both provisioning scripts must install the release role")
        self.assertIn(
            '"sn_quarantine_release": os.environ["SPEECHNEXUS_QUARANTINE_RELEASE_ROLE_PASSWORD"]',
            self.document,
        )

    def test_the_runbook_writes_the_quarantine_release_dsn_back(self) -> None:
        self.assertIn('"SPEECHNEXUS_QUARANTINE_RELEASE_DATABASE_URL": (', self.document)
        self.assertIn("postgresql://sn_quarantine_release:", self.document)

    def test_the_quarantine_release_section_names_the_startup_refusal(self) -> None:
        self.assertIn("Quarantine release startup rejected", self.document)
        self.assertIn("sn_quarantine_release", read(ENV_EXAMPLE))

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
