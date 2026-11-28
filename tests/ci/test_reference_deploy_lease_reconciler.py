"""Guard the reference deployment's bounded lease sweep against drift.

#1248 is a wiring-shaped defect, not a schema one: a Job that reaches a terminal
state before any dispatcher claims it keeps its admission lease ``reserved``, and
nothing in the product returns that capacity, so the next Job of that instance class
is refused with ``capacity_exhausted``. The fix is a scheduled coordinator pass; the
schedule is the part that can silently disappear (a deleted CronJob looks exactly
like a deployment where nothing has leaked yet).

This module parses text only, like the other ``tests/ci`` guards. It runs under the
system interpreter and never imports the backend, renders manifests, connects to
PostgreSQL or starts a container. Mutations that must turn it red:

- dropping the console script from ``backend/pyproject.toml``;
- deleting the ``lease-reconciler`` CronJob from the reference manifests, its
  ``schedule``/``concurrencyPolicy``, its ``--limit`` bound, or pointing it at a
  Secret name the renderer does not produce;
- widening that Secret's envelope: it must carry the release DSN and the schema and
  nothing else (a scheduled Pod must not read ``api-env``);
- dropping the sweep from ``deploy/compose.yaml``, or forwarding its DSN with a
  ``:?`` guard that breaks the base render;
- removing the service's image classification from ``deploy/release-assets.py``, so
  the published release Compose stops rendering;
- renaming the DSN variable on either end: the CLI's constant and the rendered
  Secret key are compared with each other, not with a copy written here.

The behaviour behind this wiring -- which leases are eligible, what the release does
to the four projections, idempotency and the two-engine parity -- is
``tests/integration/test_lease_reconciliation.py`` (PostgreSQL, real release role)
and ``tests/integration/test_lease_reconciliation_innodb.py`` (InnoDB). This guard
only requires that the deployment keeps assembling the entry point they describe.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKERS = ROOT / "deploy/cluster/app-plane/workers.yaml"
RENDER_CONFIG = ROOT / "deploy/cluster/app-plane/render-config.py"
COMPOSE = ROOT / "deploy/compose.yaml"
RELEASE_ASSETS = ROOT / "deploy/release-assets.py"
PYPROJECT = ROOT / "backend/pyproject.toml"
CLI = ROOT / "backend/src/speechnexus_backend/lease_reconciler.py"

COMMAND = "speechnexus-lease-reconciler"
CRONJOB = "lease-reconciler"
SECRET = "lease-reconciler-env"
SCHEDULE = "*/5 * * * *"
LIMIT = "8"
#: The operator release identity, shared with ``speechnexus-quarantine-release``.
DSN_VARIABLE = "SPEECHNEXUS_QUARANTINE_RELEASE_DATABASE_URL"
SCHEMA_VARIABLE = "SPEECHNEXUS_DATABASE_SCHEMA"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def cronjob_block(document: str, name: str) -> str:
    """Return one manifest document whose ``metadata.name`` is ``name``."""
    parts = re.split(r"(?m)^---$", document)
    for part in parts:
        if re.search(rf"(?m)^  name: {re.escape(name)}$", part) and "kind: CronJob" in part:
            return part
    raise AssertionError(f"deploy/cluster/app-plane/workers.yaml has no {name} CronJob")


def compose_service(document: str, name: str) -> str:
    header = re.search(rf"(?m)^  {re.escape(name)}:\n", document)
    if header is None:
        raise AssertionError(f"deploy/compose.yaml has no {name} service")
    rest = document[header.end() :]
    sibling = re.search(r"(?m)^  [a-z][a-z0-9-]*:\n", rest)
    return rest[: sibling.start()] if sibling is not None else rest


class ConsoleScriptTests(unittest.TestCase):
    def test_the_image_installs_the_sweep_entry_point(self) -> None:
        self.assertIn(
            f'{COMMAND} = "speechnexus_backend.lease_reconciler:main"',
            read(PYPROJECT),
        )


class ReferenceManifestTests(unittest.TestCase):
    def test_the_schedule_exists_and_is_serialized(self) -> None:
        block = cronjob_block(read(WORKERS), CRONJOB)
        self.assertIn(f'schedule: "{SCHEDULE}"', block)
        self.assertIn("concurrencyPolicy: Forbid", block)
        self.assertIn("restartPolicy: Never", block)

    def test_the_job_runs_the_bounded_sweep_with_its_own_secret(self) -> None:
        block = cronjob_block(read(WORKERS), CRONJOB)
        self.assertIn(f"- {COMMAND}", block)
        self.assertIn(f'- "{LIMIT}"', block)
        self.assertIn("--limit", block)
        self.assertIn(f"name: {SECRET}", block)
        self.assertNotIn("api-env", block)

    def test_the_secret_carries_the_release_dsn_and_the_schema_only(self) -> None:
        renderer = read(RENDER_CONFIG)
        self.assertIn(f'"{SECRET}": lease_reconciler', renderer)
        envelope = renderer[renderer.index("lease_reconciler = {") :]
        envelope = envelope[: envelope.index("}\n")]
        self.assertIn(f'"{DSN_VARIABLE}"', envelope)
        self.assertIn(f'"{SCHEMA_VARIABLE}"', envelope)
        # Two keys, no more: a broader envelope would hand the API's credentials to
        # a scheduled Pod.
        self.assertEqual(len(re.findall(r'"(SPEECHNEXUS_[A-Z0-9_]+)"', envelope)), 2)

    def test_the_cli_and_the_rendered_secret_name_the_same_variable(self) -> None:
        module = read(CLI)
        declared = re.search(r'(?m)^DSN_VARIABLE: Final = "([A-Z0-9_]+)"$', module)
        assert declared is not None, "lease_reconciler.py no longer names its DSN"
        self.assertEqual(declared.group(1), DSN_VARIABLE)


class ComposeTests(unittest.TestCase):
    def test_the_opt_in_one_shot_exists_and_is_not_part_of_up(self) -> None:
        block = compose_service(read(COMPOSE), CRONJOB)
        self.assertIn(f'profiles: ["{CRONJOB}"]', block)
        self.assertIn(f'command: ["{COMMAND}", "--limit", "{LIMIT}"]', block)
        self.assertIn('restart: "no"', block)

    def test_the_forwarded_dsn_keeps_an_empty_default(self) -> None:
        # A `:?` guard fails `docker compose config` for deployments that never
        # enable this profile; an empty value instead fails the process at startup.
        block = compose_service(read(COMPOSE), CRONJOB)
        self.assertIn(f"{DSN_VARIABLE}: ${{{DSN_VARIABLE}:-}}", block)

    def test_the_release_compose_can_pin_the_service_image(self) -> None:
        self.assertIn(
            f'"{CRONJOB}": "speechnexus-python"',
            read(RELEASE_ASSETS),
        )
