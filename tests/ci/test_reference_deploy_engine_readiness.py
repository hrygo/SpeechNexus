"""Guard the reference deployment's readiness renewal path against drift.

A deployment can register one instance, render Compose and admit a Job on the
first day, then answer ``503 engine_not_ready`` on the second: the readiness
receipt admission re-checks has a TTL (24 h by default) and nothing used to renew
it or report how much of the window was left (#828). The fix is a periodic
renewal and a read-only report, not a schema change -- so both live entirely in
wiring that can silently disappear.

This module parses text only, like the other ``tests/ci`` guards. It runs under
the system interpreter and never imports the backend, renders Compose, connects to
PostgreSQL or starts a container. Mutations that must turn it red:

- removing the ``engine-readiness`` profile, its renewal one-shot
  (``--renew-readiness``) or the report service from ``deploy/compose.yaml``;
- dropping the report's console script from ``backend/pyproject.toml``;
- dropping ``SPEECHNEXUS_READINESS_WARNING_SECONDS`` from the env example;
- deleting the renewal/report section of ``deploy/README.md``, the runbook's
  renewal step or the registration doc's renewal flags and exit code;
- letting the startup gate's rejected-readiness answer revert to the storage
  ``job_unavailable`` wording, or dropping the renewal it points at;
- dropping ``engine_not_ready`` from the Job create contract description while
  keeping ``job_unavailable``.

The behaviour behind this wiring -- the code a request actually receives, the
report's classification and exit codes, and the renewal transaction -- is proven
by ``tests/contract/test_public_job_engine_readiness.py``,
``tests/unit/backend/test_engine_readiness_report.py``,
``tests/unit/backend/test_runtime_readiness_expiry.py`` and the PostgreSQL HTTP
case in ``tests/integration/test_public_job_admission_http_pg.py``. This guard only
requires that the deployment keeps assembling the entry points they describe.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "deploy/compose.yaml"
ENV_EXAMPLE = ROOT / "deploy/compose.env.example"
DEPLOY_README = ROOT / "deploy/README.md"
GATE = ROOT / "deploy/job-startup-gate.py"
PYPROJECT = ROOT / "backend/pyproject.toml"
CONTRACT = ROOT / "contracts/speech-openapi.yaml"
REGISTER_DOC = ROOT / "docs/operations/register-engine.md"
RUNBOOK = ROOT / "docs/operations/async-job-runbook.md"
REPORT_MODULE = ROOT / "backend/src/speechnexus_backend/engine_readiness.py"

PROFILE = "engine-readiness"
RENEW_SERVICE = "engine-readiness-renew"
REPORT_SERVICE = "engine-readiness"
RENEW_FLAG = "--renew-readiness"
REPORT_COMMAND = "speechnexus-engine-readiness"
CODE = "engine_not_ready"
WARNING_KEY = "SPEECHNEXUS_READINESS_WARNING_SECONDS"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def service_block(document: str, name: str) -> str:
    """Return one Compose service block without its siblings."""
    header = re.search(rf"(?m)^  {re.escape(name)}:\n", document)
    if header is None:
        raise AssertionError(f"deploy/compose.yaml has no {name} service")
    rest = document[header.end() :]
    sibling = re.search(r"(?m)^  [a-z][a-z0-9-]*:\n", rest)
    return rest[: sibling.start()] if sibling is not None else rest


def operation_description(document: str, operation_id: str) -> str:
    """Return the description block of one declared operation."""
    marker = document.index(f"operationId: {operation_id}")
    start = document.index("description:", marker)
    end = document.index("responses:", start)
    return document[start:end]


class ComposeReadinessProfileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.compose = read(COMPOSE)

    def test_the_renewal_one_shot_forces_a_fresh_receipt(self) -> None:
        block = service_block(self.compose, RENEW_SERVICE)
        self.assertIn(f'profiles: ["{PROFILE}"]', block)
        # The flag is what makes a timer shorter than the TTL work: a plain
        # registration only re-signs an already elapsed receipt.
        self.assertIn(RENEW_FLAG, block)
        self.assertIn("speechnexus-register-engine", block)
        # Both one-shots share the registration inputs, including the two a receipt
        # must come from: the coordination DSN, the private recipe and the probe URL.
        self.assertIn("environment: *engine-registration-env", block)
        shared = self.compose[self.compose.index("x-engine-registration-env:") :]
        for key in (
            "SPEECHNEXUS_REGISTRATION_DATABASE_URL",
            "SPEECHNEXUS_TRANSCRIPTION_RECIPE_FILE",
            "SPEECHNEXUS_ENGINE_HEALTH_URL",
        ):
            self.assertIn(key, shared[:1000], f"the shared registration env must forward {key}")

    def test_the_report_service_is_assembled_without_write_authority(self) -> None:
        block = service_block(self.compose, REPORT_SERVICE)
        self.assertIn(f'profiles: ["{PROFILE}"]', block)
        self.assertIn(REPORT_COMMAND, block)
        self.assertIn(WARNING_KEY, block)
        # It reads the coordination database and nothing else: no health URL, no
        # recipe and no writable mount.
        self.assertNotIn("SPEECHNEXUS_ENGINE_HEALTH_URL", block)
        self.assertNotIn("SPEECHNEXUS_TRANSCRIPTION_RECIPE_FILE", block)
        self.assertNotIn("volumes:", block)

    def test_neither_service_is_part_of_the_base_topology(self) -> None:
        for name in (RENEW_SERVICE, REPORT_SERVICE):
            block = service_block(self.compose, name)
            self.assertIn('restart: "no"', block, f"{name} must not restart on its own")

    def test_the_report_entry_point_is_an_installed_console_script(self) -> None:
        pyproject = read(PYPROJECT)
        self.assertIn(
            f'{REPORT_COMMAND} = "speechnexus_backend.engine_readiness:main"',
            pyproject,
            "the report must be an installed entry point, not an ad-hoc module path",
        )
        module = read(REPORT_MODULE)
        # The report must never renew: a receipt needs a probe, so write authority
        # here would let the surface invent one.
        self.assertIn("connection.read_only = True", module)
        self.assertIn("EXIT_UNAVAILABLE", module)


class ReadinessOperatorSurfaceTests(unittest.TestCase):
    def test_the_env_example_documents_the_warning_window(self) -> None:
        example = read(ENV_EXAMPLE)
        self.assertIn(f"{WARNING_KEY}=21600", example)
        self.assertIn(CODE, example)

    def test_the_deploy_readme_documents_the_renewal_and_the_report(self) -> None:
        readme = read(DEPLOY_README)
        self.assertIn("## Engine readiness renewal", readme)
        for fact in (RENEW_FLAG, REPORT_COMMAND, f"503 {CODE}", "TTL", PROFILE):
            self.assertIn(fact, readme, f"deploy/README.md must document {fact}")
        # The section must state how the answer differs from the outage and the
        # capacity refusals, not only name it.
        self.assertIn("job_unavailable", readme)
        self.assertIn("job_capacity_exhausted", readme)

    def test_the_registration_doc_documents_the_renewal_flags_and_exit_code(self) -> None:
        doc = read(REGISTER_DOC)
        for fact in (RENEW_FLAG, REPORT_COMMAND, CODE, "readiness.state", "readiness.renewed"):
            self.assertIn(fact, doc, f"docs/operations/register-engine.md must document {fact}")
        self.assertIn("`5`", doc)

    def test_the_runbook_names_the_refusal_and_the_timer(self) -> None:
        runbook = read(RUNBOOK)
        self.assertIn(f"503 {CODE}", runbook)
        self.assertIn(RENEW_FLAG, runbook)
        self.assertIn(PROFILE, runbook)
        self.assertIn("systemd", runbook)


CLUSTER_WORKERS = ROOT / "deploy/cluster/app-plane/workers.yaml"
CLUSTER_README = ROOT / "deploy/cluster/README.md"


class ClusterRenewalTests(unittest.TestCase):
    """The cluster renewal must take the generation from the ledger, not a manifest.

    A restated generation is a constant that goes stale as soon as the controller
    prepares a new one: the renewal then re-signs a receipt nobody serves, or every
    run answers ``stale_instance_generation`` and the receipt quietly runs out. Both
    failures are silent for hours, so the wiring is checked here (#1253).
    """

    def test_the_cluster_renewal_does_not_restate_the_generation(self) -> None:
        text = read(CLUSTER_WORKERS)
        renewal = text.split("name: engine-readiness-renew", 1)[1]
        # The command list, not the comments: the reason for the absence of the flag
        # is written next to it and naturally names it.
        flags = [
            line.strip()[2:] for line in renewal.splitlines() if line.strip().startswith("- --")
        ]
        self.assertIn(RENEW_FLAG, flags)
        self.assertIn("--instance", flags)
        self.assertNotIn("--generation", flags)
        # The reason it is absent must survive next to it: a reviewer who re-adds the
        # flag has to read why it was removed.
        self.assertIn("#1253", renewal)

    def test_the_cluster_readme_says_where_the_generation_comes_from(self) -> None:
        text = read(CLUSTER_README)
        self.assertIn("runtime_instance_heads", text)
        self.assertIn("generation_required", text)
        self.assertIn("#1253", text)


class StartupGateReadinessTests(unittest.TestCase):
    def test_the_gate_points_at_the_renewal_instead_of_the_storage_answer(self) -> None:
        gate = read(GATE)
        # The pending line is one wrapped print() call: slice from its first
        # literal to the call's terminator so a truncated message is still read.
        start = gate.index("pending: real engine")
        end = gate.index("\n    )", start)
        pending = gate[start:end]
        self.assertIn(f"503 {CODE}", pending)
        self.assertIn(RENEW_FLAG, pending)
        self.assertIn(REPORT_COMMAND, pending)
        # The other pending line keeps the writer/admission DSN fact, and the gate
        # must not describe the readiness refusal as the storage one.
        self.assertIn("503 job_unavailable", gate)
        self.assertNotIn(f"503 job_unavailable ({pending!r})", gate)


class ContractReadinessTests(unittest.TestCase):
    def test_the_create_description_keeps_both_503_causes_apart(self) -> None:
        description = operation_description(read(CONTRACT), "createJob")
        self.assertIn(f"`{CODE}`", description)
        # The readiness recovery is an operator step, so the code is not retryable;
        # only the contention class is, and the description must say both (#828 B1).
        self.assertIn("`retryable: false`", description)
        self.assertIn("`retryable: true`", description)
        self.assertIn("readiness receipt", description)
        self.assertIn(RENEW_FLAG, description)
        self.assertIn("`job_unavailable`", description)
        self.assertIn("`job_capacity_exhausted`", description)


if __name__ == "__main__":
    unittest.main()


CLUSTER_TEMPLATES = ROOT / "deploy/cluster/compute/approved-templates.json"


class ClusterTemplateBudgetTests(unittest.TestCase):
    """Every approved cluster template states how long its model may take to load.

    The controller's fallback budget is calibrated for one measured cold load, so a
    template that declares nothing silently inherits a number that may not describe
    it — and a load slower than that number is reported as ``warmup_deadline_exceeded``
    instead of plain progress (#1250). The declaration is per model, so the
    deployment has to keep one per template.
    """

    def test_every_approved_cluster_template_declares_its_load_budget(self) -> None:
        import json

        document = json.loads(read(CLUSTER_TEMPLATES))
        for template in document["templates"]:
            with self.subTest(template=template.get("template_id")):
                budget = template.get("load_budget_seconds")
                self.assertIsInstance(budget, int)
                self.assertGreaterEqual(budget, 300)
                self.assertLessEqual(budget, 3600)
