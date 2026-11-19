"""Guard the reference deployment's independent Workbench API unit against drift.

`deploy/compose.env.example` used to point `WEB_WORKBENCH_UPSTREAM` at a host the
reference deployment never assembled, so the frontend's workbench requests failed
at DNS resolution and the B6 row of the MVP readiness table stayed open. The
`workbench-api` profile now assembles that unit: `workbench-migrate` bootstraps
and verifies the workbench namespace and grants its two dedicated roles, and
`workbench-api` is the service the Web tier proxies `/v1`/`/v2/workbench/*` to.

This module parses text only, like the other ``tests/ci`` guards. It runs under
the system interpreter and never imports the backend, builds an image, connects
to PostgreSQL or starts a container. Mutations that must turn it red:

- pointing `WEB_WORKBENCH_UPSTREAM` at a host `deploy/compose.yaml` does not
  define, or removing the `workbench-api` service that host names;
- removing the profile from either service, or letting `workbench-api` start
  without the migration completing first;
- dropping the startup gate from the entrypoint, its bind mount, or replacing the
  healthcheck probe with something that is not the workbench's own status route;
- forwarding a ``SPEECHNEXUS_WORKBENCH_*`` key the settings class declares no
  field for (the service refuses an unrecognised name, so this is a real
  misconfiguration, not a style question);
- dropping a role, the migration entry point or the profile command from the
  minimal runbook.

The behaviour that cannot be proven from text - the gate names each blank input,
exits 1 on a refusal, never ``exec``s after a failure and only starts on a clean
run - is proven by ``tests/unit/backend/test_workbench_startup_gate.py``, and the
migration's named outcomes by ``tests/unit/backend/test_workbench_migrate.py``.
"""

from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

from tests.ci.interpreter_floor import source_tree

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "deploy/compose.yaml"
ENV_EXAMPLE = ROOT / "deploy/compose.env.example"
GATE = ROOT / "deploy/workbench-startup-gate.py"
RUNBOOK = ROOT / "docs/operations/compose-minimal.md"
README = ROOT / "deploy/README.md"
SETTINGS = ROOT / "backend/src/speechnexus_backend/workbench/runtime_settings.py"
MIGRATE = ROOT / "backend/src/speechnexus_backend/workbench/migrate.py"
PYPROJECT = ROOT / "backend/pyproject.toml"

PROFILE = "workbench-api"
API_SERVICE = "workbench-api"
MIGRATE_SERVICE = "workbench-migrate"
GATE_TARGET = "/opt/speechnexus/workbench-startup-gate.py"
STATUS_PATH = "/v1/workbench/status"
ROLES = ("sn_workbench_runtime", "sn_workbench_cleanup")
ENV_PREFIX = "SPEECHNEXUS_WORKBENCH_"
# Host-side names inside the workbench namespace that are not settings fields: the
# two role passwords, the published port, and the test harness's own names.
HOST_SIDE_SUFFIXES = ("_ROLE_PASSWORD", "_HOST_PORT")
HOST_SIDE_KEYS = {
    "SPEECHNEXUS_WORKBENCH_TEST_DSN",
    "SPEECHNEXUS_WORKBENCH_BROWSER_OUTPUT",
}


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def service_block(compose: str, name: str) -> str:
    """Return the YAML text of one service, up to the next two-space key."""
    match = re.search(rf"(?m)^  {re.escape(name)}:\n", compose)
    if match is None:
        raise AssertionError(f"deploy/compose.yaml defines no {name} service")
    rest = compose[match.end() :]
    end = re.search(r"(?m)^  [a-zA-Z0-9_-]+:\n|^volumes:\n", rest)
    return rest[: end.start()] if end is not None else rest


def settings_fields(tree: ast.Module) -> set[str]:
    """The declared field names of ``WorkbenchSettings``, read without importing."""
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "WorkbenchSettings":
            return {
                item.target.id
                for item in node.body
                if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name)
            }
    raise AssertionError("runtime_settings.py declares no WorkbenchSettings class")


class WorkbenchDeploymentUnitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.compose = read(COMPOSE)
        self.env_example = read(ENV_EXAMPLE)
        self.runbook = read(RUNBOOK)
        self.readme = read(README)

    def test_the_upstream_names_a_service_the_deployment_assembles(self) -> None:
        upstream = re.search(r"(?m)^WEB_WORKBENCH_UPSTREAM=(\S+)$", self.env_example)
        self.assertIsNotNone(upstream, "the example env declares no workbench upstream")
        assert upstream is not None
        shape = re.fullmatch(r"http://([a-z0-9][a-z0-9-]*):(\d+)", upstream.group(1))
        self.assertIsNotNone(shape, f"unexpected upstream shape {upstream.group(1)!r}")
        assert shape is not None
        host = shape
        self.assertEqual(host.group(1), API_SERVICE)
        self.assertEqual(host.group(2), "8000")
        self.assertIn(f"  {host.group(1)}:\n", self.compose, "the upstream host is not a service")
        api = service_block(self.compose, API_SERVICE)
        self.assertIn('"127.0.0.1:${SPEECHNEXUS_WORKBENCH_API_HOST_PORT', api)
        self.assertIn(':8000"', api, "the published port must map to the served port")

    def test_both_services_are_opt_in_and_the_api_waits_for_the_migration(self) -> None:
        for name in (MIGRATE_SERVICE, API_SERVICE):
            block = service_block(self.compose, name)
            self.assertIn(f'profiles: ["{PROFILE}"]', block, f"{name} has no profile")
        api = service_block(self.compose, API_SERVICE)
        self.assertIn(f"{MIGRATE_SERVICE}:\n        condition: service_completed_successfully", api)
        self.assertIn("postgres:\n        condition: service_healthy", api)
        self.assertIn("restart: unless-stopped", api)
        migration = service_block(self.compose, MIGRATE_SERVICE)
        self.assertIn('command: ["speechnexus-workbench-migrate"]', migration)
        self.assertIn("SPEECHNEXUS_MIGRATION_DATABASE_URL", migration)
        self.assertIn('restart: "no"', migration)

    def test_the_gate_is_the_entrypoint_and_the_healthcheck_is_the_real_route(self) -> None:
        api = service_block(self.compose, API_SERVICE)
        self.assertIn(f'entrypoint: ["python", "{GATE_TARGET}"]', api)
        self.assertIn(f"./workbench-startup-gate.py:{GATE_TARGET}:ro", api)
        self.assertIn(STATUS_PATH, api)
        healthcheck = api[api.index("healthcheck:") :]
        self.assertIn(f"127.0.0.1:8000{STATUS_PATH}", healthcheck)
        self.assertNotIn(
            "/healthz",
            healthcheck.split("healthcheck:", 1)[1].split("interval:", 1)[0],
            "the workbench has no unauthenticated health route",
        )
        self.assertIn("SPEECHNEXUS_WORKBENCH_API_HOST_PORT", api)
        self.assertIn("read_only: true", api)
        self.assertIn("no-new-privileges:true", api)
        self.assertIn("cap_drop:\n      - ALL", api)

    def test_the_forwarded_workbench_keys_are_all_declared(self) -> None:
        """A key the settings class does not declare stops the service at startup."""
        fields = settings_fields(source_tree(SETTINGS))
        allowed = {ENV_PREFIX + name.upper() for name in fields}
        forwarded = set()
        for name in (MIGRATE_SERVICE, API_SERVICE):
            forwarded |= set(
                re.findall(rf"\b({ENV_PREFIX}[A-Z0-9_]+)\b", service_block(self.compose, name))
            )
        undeclared = {
            key
            for key in forwarded
            if key not in allowed
            and key not in HOST_SIDE_KEYS
            and not key.endswith(HOST_SIDE_SUFFIXES)
        }
        self.assertEqual(sorted(undeclared), [], "forwarded workbench keys are undeclared")
        # The two role passwords are host-side provisioning inputs, never forwarded.
        for role in ("RUNTIME", "CLEANUP"):
            self.assertNotIn(f"SPEECHNEXUS_WORKBENCH_{role}_ROLE_PASSWORD", self.compose)
            self.assertIn(f"SPEECHNEXUS_WORKBENCH_{role}_ROLE_PASSWORD", self.env_example)

    def test_the_migration_entry_point_is_a_console_script_that_exists(self) -> None:
        self.assertIn(
            'speechnexus-workbench-migrate = "speechnexus_backend.workbench.migrate:main"',
            read(PYPROJECT),
        )
        self.assertTrue(MIGRATE.is_file())
        self.assertIn("def main(", read(MIGRATE))

    def test_the_runbook_documents_the_roles_the_migration_and_the_profile(self) -> None:
        for role in ROLES:
            self.assertIn(role, self.runbook, f"the runbook never names {role}")
        self.assertIn("speechnexus-workbench-migrate", self.runbook)
        self.assertIn(f"--profile {PROFILE}", self.runbook)
        self.assertIn("runtime_grants_sql", self.runbook)
        self.assertIn("cleanup_grants_sql", self.runbook)
        # The unit's evidence command is the real status route, not a liveness stub.
        self.assertIn(STATUS_PATH, self.runbook)

    def test_the_readme_names_the_unit_and_stops_calling_it_absent(self) -> None:
        self.assertIn(API_SERVICE, self.readme)
        self.assertIn(PROFILE, self.readme)
        self.assertNotIn("does not start the independent Workbench API", self.readme)

    def test_the_gate_starts_the_documented_factory(self) -> None:
        source = read(GATE)
        self.assertIn("speechnexus_backend.workbench.runtime:create_app_from_env", source)
        self.assertIn('"--factory"', source)
        self.assertIn('"--port"', source)
        # The gate must consume the runtime's own checks instead of restating them.
        self.assertIn("from speechnexus_backend.workbench.runtime import (", source)
        self.assertIn("check_database", source)
        self.assertIn("check_cleanup_identity", source)


if __name__ == "__main__":
    unittest.main()
