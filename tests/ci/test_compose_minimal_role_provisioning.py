"""Guard the compose-minimal role provisioning steps against doc/code drift.

The runbook creates eighteen non-owner roles (three base identities plus fifteen
that arrived with later migrations), calls repository grant functions by name,
states the current migration head, and runs host commands that import the
workspace members. A renamed grant function, a re-pointed head, or a host
command that cannot import ``speechnexus_backend`` turns the runbook into
instructions that fail on a clean machine, and a stale error code sends the
operator to the wrong endpoint.

The instance-reader step is the one role whose authority is not declared by a
``*_grants`` function: the runbook grants its two relations inline, so the
function-name check above cannot see it. ``model_instance_reader_relations``
closes that blind spot by comparing the inline ``GRANT`` against the relations
the runtime verifies at startup (``READABLE`` in ``model_instance_runtime.py``).

Error codes are checked against the handler that raises them: ``tests/ci/error_codes.py``
indexes each handler module's wire codes with their status, including the
pronunciation pair whose wire name is composed at the boundary and the ``422``
publication refusal. A code is only accepted when one module both composes (or
declares) that wire name and gives it the documented status.

These checks parse text only: they run under the system interpreter, like the
other ``tests/ci`` guards, and never import the backend or open a database.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from tests.ci.error_codes import declarations, documented_codes

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
SECTION_TWO_GENERATED = re.compile(r"^value = \{\n(.*?)\n\}$", re.DOTALL | re.MULTILINE)
SECTION_SIX_GENERATED = re.compile(r"^KEYS = \(\n(.*?)\n\)$", re.DOTALL | re.MULTILINE)
GENERATED_VALUE = re.compile(r'^\s+"([A-Z0-9_]+)": secrets\.token_(?:urlsafe|hex)\(', re.MULTILINE)
GENERATED_ROLE_KEY = re.compile(r'^    "([A-Z0-9_]+)",$', re.MULTILINE)
EXAMPLE_PLACEHOLDER = re.compile(r"^([A-Z0-9_]+)=change-me", re.MULTILINE)
LOAD_COMMAND = re.compile(r"^\. deploy/compose\.env$", re.MULTILINE)
GENERATOR = re.compile(r"^KEYS = \($", re.MULTILINE)
PROVISIONING = re.compile(r"^uv run --all-packages python", re.MULTILINE)
SECTION_SIX_HEADING = "## 6. 创建非 owner 运行角色与最小授权"
SECTION_SIX_END = "### 工作区写角色 `sn_workspace_writer`"
SETTINGS_FIELD = re.compile(r"^\s*([a-z0-9_]+_database_url)\s*:", re.MULTILINE)
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


def required(document: str, pattern: re.Pattern[str], missing: str) -> int:
    match = pattern.search(document)
    if match is None:
        raise AssertionError(missing)
    return match.start()


def generated_keys(block: re.Match[str], entry: re.Pattern[str]) -> set[str]:
    return set(entry.findall(block.group(1)))


def section_two_generated_keys(document: str) -> set[str]:
    """Keys the §2 environment step writes into ``deploy/compose.env`` as random values."""
    block = SECTION_TWO_GENERATED.search(document)
    if block is None:
        raise AssertionError("the §2 environment step no longer declares its generated values")
    keys = generated_keys(block, GENERATED_VALUE)
    if not keys:
        raise AssertionError("the §2 environment step generates no random value")
    return keys


def section_six_generated_keys(document: str) -> set[str]:
    """Role passwords the §6 step generates and writes back into ``deploy/compose.env``."""
    block = SECTION_SIX_GENERATED.search(document)
    if block is None:
        raise AssertionError("the §6 password step no longer lists the keys it writes")
    keys = generated_keys(block, GENERATED_ROLE_KEY)
    if not keys:
        raise AssertionError("the §6 password step lists no role password key")
    return keys


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
        """A key the example only carries as ``change-me-*`` is not a source.

        The runbook loads ``deploy/compose.env`` into the shell, so a placeholder
        left in that file replaces whatever the step exported earlier. A role
        password therefore counts as provided only when a documented step writes a
        generated value into the file (the §2 environment step for the three base
        identities, the §6 generator for the optional ones), or when the example
        does not carry the key at all, so loading the file cannot overwrite it.
        """
        example = read(ENV_EXAMPLE)
        generated = section_two_generated_keys(self.document) | section_six_generated_keys(
            self.document
        )
        placeholders = set(EXAMPLE_PLACEHOLDER.findall(example))
        roles = set(ROLE_PASSWORD_USE.findall(self.document))
        self.assertTrue(roles, "runbook must read each role password from the environment")
        missing = sorted(
            f"{role} -> {password}"
            for role, password in roles
            if password not in generated and password in placeholders
        )
        self.assertEqual(
            missing,
            [],
            "these roles would be created with the file's change-me placeholder: " + repr(missing),
        )

    def test_the_generated_password_list_covers_every_optional_role(self) -> None:
        """The §6 generator must list exactly the roles the provisioning script creates."""
        consumed = {password for _, password in ROLE_PASSWORD_USE.findall(self.document)}
        base = section_two_generated_keys(self.document)
        generated = section_six_generated_keys(self.document)
        self.assertEqual(
            sorted(generated),
            sorted(consumed - base),
            "the §6 password step does not cover exactly the optional identities",
        )

    def test_the_role_passwords_are_generated_after_the_environment_is_loaded(self) -> None:
        """Loading the file after exporting would restore the ``change-me-*`` values.

        This is the defect the step order used to carry: fourteen passwords were
        exported first and ``. deploy/compose.env`` then put the file's placeholders
        back, so every role and DSN silently used a value committed to the
        repository. The generated values are written into the file instead, and the
        shell reloads it, so a later session reuses the same set.
        """
        start = self.document.index(SECTION_SIX_HEADING)
        section = self.document[start : self.document.index(SECTION_SIX_END)]
        # Match the commands themselves: the prose discusses `. deploy/compose.env`
        # too, and an unanchored search would measure the explanation, not the step.
        load = required(section, LOAD_COMMAND, "no `. deploy/compose.env` step in §6")
        generate = required(section, GENERATOR, "no §6 role-password generator")
        provisioning = required(section, PROVISIONING, "no §6 role-provisioning command")
        self.assertLess(load, generate, "role passwords must be generated after the file is loaded")
        self.assertLess(generate, provisioning, "the generator must run before the roles exist")
        early = [
            match.start()
            for match in re.finditer(
                r"^export SPEECHNEXUS_[A-Z0-9_]*_ROLE_PASSWORD=", section, re.MULTILINE
            )
            if match.start() < load
        ]
        self.assertEqual(
            early,
            [],
            "a role password exported before `. deploy/compose.env` is overwritten by the "
            "file's placeholder",
        )
        self.assertIn("path.write_text(text)", section, "the generator must persist the values")
        self.assertIn("placeholders remaining: 0", section, "the generator must verify the file")

    def test_documented_error_codes_are_declared_by_a_handler_with_that_status(self) -> None:
        """A quoted code must be a code some handler raises with that status.

        Both halves matter. ``pronunciation_unavailable`` never appears literally:
        the boundary composes it from one stable prefix, so the check has to know
        that shape. ``422 pronunciation_publication_not_supported`` is not a 503 at
        all, and the earlier guard ignored every non-503 code. Reading them from a
        per-module index also keeps the prefix and the bare reason in the module
        that actually composes them instead of anywhere in the backend tree.
        """
        documented = documented_codes(self.document)
        self.assertTrue(documented, "runbook must name the error codes operators will see")
        index = declarations()
        problems = [
            f"{status} {code}"
            for code, status in sorted(documented.items())
            if not any(codes.get(code) == status for codes in index.values())
        ]
        self.assertEqual(problems, [], f"documented codes are not raised: {problems}")

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
