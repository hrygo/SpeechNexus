"""Guard the reference deployment's ``*_DATABASE_URL`` coverage table.

The same defect shipped three times: a setting exists, the endpoint is
fail-closed, and the reference deployment has neither a role-provisioning step
nor a statement that the capability is deliberately off. An operator then reads a
permanent 503 as a broken deployment. ``docs/operations/compose-minimal.md`` now
carries one table that classifies every Compose-forwarded DSN as provisioned or
deliberately unprovisioned, plus a section that explains each exemption.

This module is the guard that keeps that table from drifting. It parses text
only, like the other ``tests/ci`` guards: it runs under the system interpreter
and never imports the backend, renders Compose, connects to PostgreSQL or starts
a container. Mutations that must turn it red:

- forwarding a new ``*_DATABASE_URL`` in ``deploy/compose.yaml`` without either a
  documented provisioning step or an entry in the deliberate list;
- deleting a provisioning step for an already classified identity (the per-key
  anchors below pin the credential write-back and the role/grant path);
- deleting a deliberate exemption from the list, or dropping its reason;
- granting an inline reader projection a relation its runtime does not verify;
- removing the empty-default forward for an optional identity (a ``:?`` guard
  would fail ``docker compose config`` for deployments that never enable it);
- declaring a new ``*_database_url`` setting without forwarding it or recording
  it as outside the Compose surface.

The relation-level check mirrors ``tests/ci/test_compose_minimal_role_provisioning.py``:
the two read projections without a ``*_grants`` function are granted inline, so
their ``GRANT`` text is compared with the ``READABLE`` tuple the runtime verifies
at startup.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "deploy/compose.yaml"
ENV_EXAMPLE = ROOT / "deploy/compose.env.example"
DOC = ROOT / "docs/operations/compose-minimal.md"
SETTINGS = ROOT / "backend/src/speechnexus_backend/settings.py"
BACKEND = ROOT / "backend/src/speechnexus_backend"

FORWARDED = re.compile(r"\$\{(SPEECHNEXUS_[A-Z0-9_]*_DATABASE_URL)(?::[-?][^}]*)?\}")
REQUIRED_FORWARD = re.compile(
    r"\$\{(SPEECHNEXUS_[A-Z0-9_]*_DATABASE_URL):\?[^}]*\}",
)
ROW = re.compile(r"(?m)^\|\s*`(SPEECHNEXUS_[A-Z0-9_]*_DATABASE_URL)`\s*\|(.*)$")
SETTINGS_FIELD = re.compile(r"^\s*([a-z0-9_]+_database_url)\s*:", re.MULTILINE)
INLINE_GRANT = re.compile(
    r"sql\.SQL\(\s*(?P<sql>.*?)\)\s*\.format\((?P<arguments>.*?)\n\s+\)",
    re.DOTALL,
)
INLINE_RELATION = re.compile(r"\{\}\.([a-z0-9_]+)")
INLINE_ROLE = re.compile(r'sql\.Identifier\("(sn_[a-z_]+)"\)')
READABLE = re.compile(r"^READABLE = \((.*?)\)", re.MULTILINE | re.DOTALL)
SETTING_KEY = re.compile(r'"([a-z0-9_]+_database_url)"')
# Both the suffixed (SPEECHNEXUS_API_DATABASE_URL) and the bare
# (SPEECHNEXUS_DATABASE_URL) container forms exist.
DSN_ENV_NAME = r"SPEECHNEXUS_(?:[A-Z0-9_]+_)?DATABASE_URL"

PROVISIONED = "预置"
DELIBERATE = "故意不预置"
DELIBERATE_HEADING = "### 故意不预置的身份"

# Keys the Compose render itself needs: the base services cannot start without
# them, so a `:?` guard is correct there and the deployment prepares them in §2.
REQUIRED_AT_RENDER = {
    "SPEECHNEXUS_API_DATABASE_URL",
    "SPEECHNEXUS_GATEWAY_CONTROL_DATABASE_URL",
    "SPEECHNEXUS_GATEWAY_CONTROL_QUERY_DATABASE_URL",
    "SPEECHNEXUS_MIGRATION_DATABASE_URL",
    "SPEECHNEXUS_REGISTRATION_DATABASE_URL",
}

# One anchor per documented fact, not a second prose copy: the DSN write-back the
# deployment actually runs and the identity/grant path that makes it usable.
PROVISIONED_ANCHORS: dict[str, tuple[str, ...]] = {
    "SPEECHNEXUS_API_DATABASE_URL": (
        'value["SPEECHNEXUS_API_DATABASE_URL"]',
        '"sn_api": os.environ["SPEECHNEXUS_API_ROLE_PASSWORD"]',
    ),
    "SPEECHNEXUS_GATEWAY_CONTROL_DATABASE_URL": (
        'value["SPEECHNEXUS_GATEWAY_CONTROL_DATABASE_URL"]',
        '"sn_gateway": os.environ["SPEECHNEXUS_GATEWAY_ROLE_PASSWORD"]',
    ),
    "SPEECHNEXUS_GATEWAY_CONTROL_QUERY_DATABASE_URL": (
        'value["SPEECHNEXUS_GATEWAY_CONTROL_QUERY_DATABASE_URL"]',
        '"sn_gateway_query": os.environ["SPEECHNEXUS_GATEWAY_QUERY_ROLE_PASSWORD"]',
    ),
    "SPEECHNEXUS_MIGRATION_DATABASE_URL": (
        "owner_dsn = (",
        'value["SPEECHNEXUS_MIGRATION_DATABASE_URL"] = owner_dsn',
    ),
    "SPEECHNEXUS_REGISTRATION_DATABASE_URL": (
        "owner_dsn = (",
        'value["SPEECHNEXUS_REGISTRATION_DATABASE_URL"] = owner_dsn',
    ),
    "SPEECHNEXUS_PROFILE_WRITER_DATABASE_URL": (
        "SPEECHNEXUS_PROFILE_WRITER_DATABASE_URL=",
        "profile_publication_grants(schema, role)",
    ),
    "SPEECHNEXUS_WORKSPACE_WRITER_DATABASE_URL": (
        '"SPEECHNEXUS_WORKSPACE_WRITER_DATABASE_URL": (',
        '"sn_workspace_writer": os.environ["SPEECHNEXUS_WORKSPACE_WRITER_ROLE_PASSWORD"]',
    ),
    "SPEECHNEXUS_AUDIT_READER_DATABASE_URL": (
        '"SPEECHNEXUS_AUDIT_READER_DATABASE_URL": (',
        '"sn_audit_reader": os.environ["SPEECHNEXUS_AUDIT_READER_ROLE_PASSWORD"]',
    ),
    "SPEECHNEXUS_MODEL_INSTANCE_READER_DATABASE_URL": (
        '"SPEECHNEXUS_MODEL_INSTANCE_READER_DATABASE_URL": (',
        '"sn_model_instance_reader": os.environ["SPEECHNEXUS_MODEL_INSTANCE_READER_ROLE_PASSWORD"]',
    ),
    "SPEECHNEXUS_QUARANTINE_RELEASE_DATABASE_URL": (
        '"SPEECHNEXUS_QUARANTINE_RELEASE_DATABASE_URL": (',
        '"sn_quarantine_release": os.environ["SPEECHNEXUS_QUARANTINE_RELEASE_ROLE_PASSWORD"]',
    ),
    "SPEECHNEXUS_JOB_WRITER_DATABASE_URL": (
        '"SPEECHNEXUS_JOB_WRITER_DATABASE_URL": (',
        "scripts/dev-env/provision-job-roles.py",
    ),
    "SPEECHNEXUS_JOB_ADMISSION_DATABASE_URL": (
        '"SPEECHNEXUS_JOB_ADMISSION_DATABASE_URL": (',
        "scripts/dev-env/provision-job-roles.py",
    ),
    "SPEECHNEXUS_JOB_DISPATCH_DATABASE_URL": (
        '"SPEECHNEXUS_JOB_DISPATCH_DATABASE_URL": (',
        "scripts/dev-env/provision-job-roles.py",
    ),
    "SPEECHNEXUS_DISABLE_DATABASE_URL": (
        'disable_coordinator_grants("speech", "sn_disable_scan")',
        "SPEECHNEXUS_DISABLE_DATABASE_URL",
    ),
    "SPEECHNEXUS_DISABLE_SETTLEMENT_DATABASE_URL": (
        'disable_settlement_grants("speech", "sn_disable_settle")',
        "SPEECHNEXUS_DISABLE_SETTLEMENT_DATABASE_URL",
    ),
    "SPEECHNEXUS_MODEL_CATALOG_READER_DATABASE_URL": (
        '"SPEECHNEXUS_MODEL_CATALOG_READER_DATABASE_URL": (',
        'model_catalog_reader_grants(schema, "sn_catalog_reader")',
    ),
    "SPEECHNEXUS_ARTIFACT_READER_DATABASE_URL": (
        '"SPEECHNEXUS_ARTIFACT_READER_DATABASE_URL": (',
        'artifact_reader_grants(schema, "sn_artifact_reader")',
    ),
    "SPEECHNEXUS_RESOURCE_POOL_READER_DATABASE_URL": (
        '"SPEECHNEXUS_RESOURCE_POOL_READER_DATABASE_URL": (',
        '"sn_resource_pool_reader": os.environ["SPEECHNEXUS_RESOURCE_POOL_READER_ROLE_PASSWORD"]',
    ),
    "SPEECHNEXUS_BINDING_LIST_READER_DATABASE_URL": (
        '"SPEECHNEXUS_BINDING_LIST_READER_DATABASE_URL": (',
        '"sn_binding_list_reader": os.environ["SPEECHNEXUS_BINDING_LIST_READER_ROLE_PASSWORD"]',
    ),
    "SPEECHNEXUS_BINDING_WRITER_DATABASE_URL": (
        '"SPEECHNEXUS_BINDING_WRITER_DATABASE_URL": (',
        'binding_draft_grants(schema, "sn_binding_writer")',
    ),
    "SPEECHNEXUS_BINDING_ADMIN_READER_DATABASE_URL": (
        '"SPEECHNEXUS_BINDING_ADMIN_READER_DATABASE_URL": (',
        'binding_admin_reader_grants(schema, "sn_binding_admin_reader")',
    ),
    "SPEECHNEXUS_BINDING_LIFECYCLE_DATABASE_URL": (
        '"SPEECHNEXUS_BINDING_LIFECYCLE_DATABASE_URL": (',
        'binding_lifecycle_grants(schema, "sn_binding_lifecycle")',
    ),
}

# Deliberate exemptions: the key must be named in the section and the two markers
# must state the reason and the enablement path, so deleting either is red.
DELIBERATE_ANCHORS: dict[str, tuple[str, ...]] = {
    "SPEECHNEXUS_BINDING_DEFAULTS_WRITER_DATABASE_URL": (
        "Configure one explicit binding writer mode",
        "binding_defaults_grants",
    ),
    "SPEECHNEXUS_PRONUNCIATION_READER_DATABASE_URL": (
        "503 pronunciation_unavailable",
        "pronunciation_draft_grants",
    ),
    "SPEECHNEXUS_PRONUNCIATION_WRITER_DATABASE_URL": (
        "503 pronunciation_unavailable",
        "pronunciation_draft_grants",
    ),
    "SPEECHNEXUS_PRONUNCIATION_PUBLISHER_DATABASE_URL": (
        "422 pronunciation_publication_not_supported",
        "pronunciation_publication_grants",
    ),
    "SPEECHNEXUS_VOICE_PUBLISHER_DATABASE_URL": (
        "503 voice_draft_unavailable",
        "voice_publication_grants",
    ),
}

# Role -> the grant declaration the runbook must replay for that exact role. The
# count is required because both provisioning paths (fresh install and upgrade of
# an existing deployment) must cover it.
ROLE_GRANT_BINDINGS: dict[str, tuple[str, int]] = {
    "sn_catalog_reader": ("model_catalog_reader_grants", 2),
    "sn_artifact_reader": ("artifact_reader_grants", 2),
    "sn_binding_writer": ("binding_draft_grants", 2),
    "sn_binding_admin_reader": ("binding_admin_reader_grants", 2),
    "sn_binding_lifecycle": ("binding_lifecycle_grants", 2),
    "sn_workspace_writer": ("workspace_writer_grants", 2),
    "sn_audit_reader": ("audit_reader_grants", 2),
    "sn_quarantine_release": ("quarantine_release_grants", 2),
}

# Roles the runbook creates after the three base identities: the fresh-install
# script creates all of them, and the existing-deployment upgrade creates the
# eleven that arrived with later migrations. Losing either path leaves a real
# deployment half-provisioned.
BASE_ROLES = {"sn_api", "sn_gateway", "sn_gateway_query"}
UPGRADE_PATH_ROLES = {
    "sn_workspace_writer",
    "sn_audit_reader",
    "sn_model_instance_reader",
    "sn_quarantine_release",
    "sn_catalog_reader",
    "sn_artifact_reader",
    "sn_resource_pool_reader",
    "sn_binding_list_reader",
    "sn_binding_writer",
    "sn_binding_admin_reader",
    "sn_binding_lifecycle",
}

# Read-only projections with no *_grants function: the runbook grants them inline,
# so the GRANT text is the only place a relation name can drift from the runtime.
INLINE_READERS: dict[str, str] = {
    "sn_model_instance_reader": "model_instance_runtime.py",
    "sn_resource_pool_reader": "resource_pool_runtime.py",
    "sn_binding_list_reader": "binding_list_runtime.py",
}

# Settings that exist but are not part of this Compose surface; the runbook must
# say so instead of leaving a reader to guess whether the key is dead.
NON_COMPOSE_SETTINGS_KEYS = {"SPEECHNEXUS_SPEECH_SETTLEMENT_DATABASE_URL"}


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def forwarded_keys() -> set[str]:
    return set(FORWARDED.findall(read(COMPOSE)))


def coverage_table() -> dict[str, list[str]]:
    rows: dict[str, list[str]] = {}
    for key, tail in ROW.findall(read(DOC)):
        rows[key] = [cell.strip() for cell in tail.split("|")]
    return rows


def deliberate_section() -> str:
    document = read(DOC)
    start = document.index(DELIBERATE_HEADING) + len(DELIBERATE_HEADING)
    following = re.search(r"(?m)^## ", document[start:])
    end = start + following.start() if following is not None else len(document)
    return document[start:end]


def table_constants() -> dict[str, str]:
    """Resolve module-level ``NAME = "relation"`` constants by text.

    ``binding_list_runtime.READABLE`` names two relations through the constants
    it imports (``HEAD_TABLE`` and ``RETIREMENT_TABLE``), so a literal-only parse
    would compare against an incomplete surface and hide a real drift.
    """
    constants: dict[str, str] = {}
    for path in BACKEND.rglob("*.py"):
        for name, value in re.findall(r'(?m)^([A-Z][A-Z0-9_]*) = "([a-z0-9_]+)"$', read(path)):
            constants.setdefault(name, value)
    return constants


def readable_relations(module: str) -> set[str]:
    source = read(BACKEND / module)
    declaration = READABLE.search(source)
    if declaration is None:
        raise AssertionError(f"{module} declares no READABLE tuple")
    body = declaration.group(1)
    constants = table_constants()
    names: set[str] = set(re.findall(r'"([a-z0-9_]+)"', body))
    for identifier in re.findall(r"\b([A-Z][A-Z0-9_]*)\b", body):
        value = constants.get(identifier)
        if value is None:
            raise AssertionError(f"{module} READABLE names unresolved constant {identifier}")
        names.add(value)
    return names


def inline_grants(document: str) -> dict[str, list[set[str]]]:
    grants: dict[str, list[set[str]]] = {}
    for match in INLINE_GRANT.finditer(document):
        relations = set(INLINE_RELATION.findall(match.group("sql")))
        if not relations:
            continue
        for role in INLINE_ROLE.findall(match.group("arguments")):
            grants.setdefault(role, []).append(relations)
    return grants


class ComposeDsnCoverageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.document = read(DOC)
        self.table = coverage_table()

    def test_every_forwarded_dsn_appears_in_the_coverage_table(self) -> None:
        forwarded = forwarded_keys()
        classified = set(self.table)
        self.assertEqual(
            sorted(forwarded - classified),
            [],
            "deploy/compose.yaml forwards a DSN the runbook never classifies; add a "
            "provisioning step or a deliberate exemption",
        )
        self.assertEqual(
            sorted(classified - forwarded),
            [],
            "the coverage table names a DSN Compose no longer forwards; remove the row "
            "instead of leaving a stale claim",
        )

    def test_every_row_states_its_identity_usage_behaviour_and_disposition(self) -> None:
        problems: list[str] = []
        for key, cells in self.table.items():
            if len(cells) < 5:
                problems.append(f"{key}: fewer than five columns")
                continue
            identity, usage, behaviour, disposition = cells[0], cells[1], cells[2], cells[3]
            if not identity or not usage or not behaviour:
                problems.append(f"{key}: empty identity, usage or behaviour cell")
            # The behaviour must name the stable observable: a render-time failure,
            # a stable HTTP error or an explicit startup refusal. "It is optional"
            # is exactly the statement that let the original defects ship.
            if not re.search(r"\d{3} [a-z_]+|渲染期必填|启动", behaviour):
                problems.append(f"{key}: behaviour does not name a stable failure")
            if disposition not in (PROVISIONED, DELIBERATE):
                problems.append(f"{key}: disposition {disposition!r} is not classified")
        self.assertEqual(problems, [], "coverage table rows are incomplete: " + "; ".join(problems))

    def test_every_provisioned_row_keeps_its_step(self) -> None:
        problems: list[str] = []
        for key, cells in self.table.items():
            if cells[3] != PROVISIONED:
                continue
            anchors = PROVISIONED_ANCHORS.get(key)
            if anchors is None:
                problems.append(f"{key}: classified as provisioned without pinned anchors")
                continue
            problems.extend(
                f"{key}: missing step anchor {anchor!r}"
                for anchor in anchors
                if anchor not in self.document
            )
        self.assertEqual(problems, [], "runbook lost provisioning steps: " + "; ".join(problems))

    def test_every_later_role_is_created_in_both_provisioning_paths(self) -> None:
        blocks = re.findall(r"```bash\n(.*?)```", self.document, re.DOTALL)
        install = [block for block in blocks if "roles ready:" in block]
        upgrade = [block for block in blocks if "grants refreshed" in block]
        self.assertEqual(len(install), 1, "expected exactly one fresh-install script")
        self.assertEqual(len(upgrade), 1, "expected exactly one upgrade script")
        problems: list[str] = []
        for role in sorted(UPGRADE_PATH_ROLES):
            marker = f'"{role}": os.environ['
            if marker not in install[0]:
                problems.append(f"{role}: missing from the fresh-install role set")
            if marker not in upgrade[0]:
                problems.append(f"{role}: missing from the upgrade role set")
        for role in sorted(BASE_ROLES):
            self.assertIn(
                f'"{role}": os.environ[', install[0], f"{role} must exist on a fresh install"
            )
            self.assertNotIn(
                f'"{role}": os.environ[', upgrade[0], f"{role} predates the upgrade path"
            )
        self.assertEqual(problems, [], "provisioning paths diverged: " + "; ".join(problems))

    def test_the_anchor_sets_cover_the_table_exactly(self) -> None:
        # A row classified by prose alone is not a step; these two mappings are the
        # machine-checkable half of the table and must stay one-to-one with it.
        self.assertEqual(
            sorted(set(PROVISIONED_ANCHORS) | set(DELIBERATE_ANCHORS)),
            sorted(self.table),
            "the pinned anchors and the coverage table disagree",
        )

    def test_every_deliberate_exemption_is_explained(self) -> None:
        section = deliberate_section()
        self.assertIn(
            "部署方判断方式", section, "the exemption list must tell operators how to tell"
        )
        problems: list[str] = []
        for key, cells in self.table.items():
            if cells[3] != DELIBERATE:
                continue
            if key not in section:
                problems.append(f"{key}: not named in the deliberate-exemption section")
                continue
            problems.extend(
                f"{key}: missing exemption marker {anchor!r}"
                for anchor in DELIBERATE_ANCHORS[key]
                if anchor not in section
            )
        self.assertEqual(
            problems, [], "deliberate exemptions lost their reason: " + "; ".join(problems)
        )

    def test_the_disposition_column_matches_the_pinned_mappings(self) -> None:
        # The disposition cell is prose the operator reads; the two anchor mappings
        # are what the other tests execute. They must not disagree, or a provisioned
        # identity could quietly lose its step while the table still claims one.
        offenders = sorted(
            key
            for key, cells in self.table.items()
            if (cells[3] == DELIBERATE) != (key in DELIBERATE_ANCHORS)
        )
        self.assertEqual(offenders, [], f"table and pinned mappings disagree: {offenders}")

    def test_optional_identities_keep_an_empty_default(self) -> None:
        # A `:?` guard on an optional identity fails `docker compose config` for
        # every deployment that never enables it; the empty default is what lets
        # the base topology render while the endpoint stays fail-closed.
        compose = read(COMPOSE)
        guarded = set(REQUIRED_FORWARD.findall(compose))
        unexpected = sorted(guarded - REQUIRED_AT_RENDER)
        self.assertEqual(
            unexpected,
            [],
            "a required-variable guard makes the base render fail for deployments "
            f"that never enable the identity: {unexpected}",
        )
        for key in sorted(forwarded_keys() - REQUIRED_AT_RENDER):
            self.assertIn(
                f"${{{key}:-}}", compose, f"{key} must be forwarded with an empty default"
            )

    def test_the_example_environment_declares_every_forwarded_dsn(self) -> None:
        example = read(ENV_EXAMPLE)
        declared = set(re.findall(r"(?m)^(SPEECHNEXUS_[A-Z0-9_]*_DATABASE_URL)=", example))
        self.assertEqual(
            sorted(forwarded_keys() - declared), [], "example env misses a forwarded DSN"
        )
        self.assertEqual(
            sorted(declared - forwarded_keys()), [], "example env declares an unforwarded DSN"
        )

    def test_the_runbook_names_this_guard(self) -> None:
        self.assertIn(Path(__file__).name, self.document)


def readable_env_names() -> set[str]:
    """Container env names some settings class or module actually reads.

    ``env_prefix`` composition is why a literal scan is not enough: the gateway
    reads ``SPEECHNEXUS_GATEWAY_CONTROL_DATABASE_URL`` through its
    ``SPEECHNEXUS_GATEWAY_`` prefix plus the ``control_database_url`` field, so the
    full name never appears in any source file. The bare ``SPEECHNEXUS_DATABASE_URL``
    alias also has no intermediate segment, so the name pattern allows that form.
    """
    names: set[str] = set()
    for root in (ROOT / "backend/src", ROOT / "execution-gateway/src"):
        for path in root.rglob("*.py"):
            text = read(path)
            prefixes = re.findall(r'env_prefix="([A-Z_]+)"', text)
            fields = re.findall(r"(?m)^\s{4}([a-z0-9_]+_database_url)\s*:", text)
            for prefix in prefixes:
                names |= {prefix + field.upper() for field in fields}
            names |= set(re.findall(rf'validation_alias="({DSN_ENV_NAME})"', text))
            names |= set(re.findall(r'"(SPEECHNEXUS_DATABASE_URL)"', text))
            names |= set(re.findall(rf'"({DSN_ENV_NAME})"', text))
    return names


class SettingsDsnCoverageTests(unittest.TestCase):
    def test_every_backend_dsn_setting_is_forwarded_or_declared_outside_compose(self) -> None:
        fields = SETTINGS_FIELD.findall(read(SETTINGS))
        # The base Settings also lists the same fields again in its validators;
        # the declared surface is the class body, so de-duplicate through the set.
        settings_keys = {"SPEECHNEXUS_" + field.upper() for field in fields}
        forwarded = forwarded_keys()
        outside = NON_COMPOSE_SETTINGS_KEYS
        self.assertEqual(
            sorted(settings_keys - forwarded - outside),
            [],
            "a backend DSN setting is neither forwarded by Compose nor recorded as "
            "outside the deployment surface",
        )
        document = read(DOC)
        for key in sorted(outside):
            self.assertIn(
                key, document, f"{key} must be documented as outside this Compose surface"
            )
            self.assertIn("不是死键", document, "the runbook must state the dead-key verdict")

    def test_no_forwarded_dsn_is_a_dead_key(self) -> None:
        # Every forwarded key must reach a settings field, an explicit alias or a
        # literal os.environ lookup. A key that no consumer reads is a
        # configuration illusion, not a feature flag. Only the container-side name
        # matters: the Compose layer may rename a host variable (the API's
        # SPEECHNEXUS_API_DATABASE_URL becomes SPEECHNEXUS_DATABASE_URL).
        readable = readable_env_names()
        compose = read(COMPOSE)
        dead: list[str] = []
        for host in sorted(forwarded_keys()):
            container = {}
            for name, source in re.findall(
                rf"(?m)^\s*({DSN_ENV_NAME}): "
                rf"\$\{{({DSN_ENV_NAME})",
                compose,
            ):
                container[source] = name
            name = container.get(host, host)
            if name not in readable:
                dead.append(f"{host} -> {name}")
        self.assertEqual(dead, [], f"forwarded DSNs with no runtime consumer: {dead}")


class InlineReaderGrantTests(unittest.TestCase):
    """The two new read projections are granted inline; compare them to READABLE."""

    def setUp(self) -> None:
        self.document = read(DOC)
        self.grants = inline_grants(self.document)

    def test_the_inline_grant_matches_the_runtime_readable_relations(self) -> None:
        problems: list[str] = []
        for role, module in INLINE_READERS.items():
            expected = readable_relations(module)
            found = self.grants.get(role, [])
            if len(found) != 2:
                problems.append(f"{role}: expected both provisioning paths, found {len(found)}")
                continue
            for relations in found:
                if relations != expected:
                    problems.append(
                        f"{role}: GRANT {sorted(relations)} != READABLE {sorted(expected)} "
                        f"in {module}"
                    )
        self.assertEqual(problems, [], "inline reader grant drifted: " + "; ".join(problems))

    def test_role_grant_bindings_are_replayed_in_both_provisioning_paths(self) -> None:
        problems: list[str] = []
        for role, (grant, expected) in ROLE_GRANT_BINDINGS.items():
            found = len(re.findall(rf'{grant}\(schema, "{role}"\)', self.document))
            if found != expected:
                problems.append(f"{role}: {grant} appears {found} times, expected {expected}")
        self.assertEqual(problems, [], "role provisioning drifted: " + "; ".join(problems))

    def test_no_reader_grant_function_is_invented(self) -> None:
        # These projections have no declaration function yet. Naming one makes the
        # runbook look complete while the call would raise NameError at provisioning
        # time, which is exactly what the inline GRANT replaces.
        for name in ("resource_pool_reader_grants", "binding_list_reader_grants"):
            self.assertNotIn(name, self.document)


if __name__ == "__main__":
    unittest.main()
