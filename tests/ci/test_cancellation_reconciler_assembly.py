"""The cancellation pass is assembled, not just implemented (#1293).

``job_cancellation_worker`` / ``job_cancellation_runtime`` existed, and nothing ran
them: a cancelled Job therefore stopped at ``cancelling`` while its admission lease
stayed ``reserved``, so the next Job of that instance class was refused with
``capacity_exhausted``. Implementation without assembly is the failure this file
guards, so it asserts the three places a deployable unit has to appear in, and it
asserts the rendered Secret covers every setting the worker validates.

Each check reads the artifact a rollout reads (``pyproject.toml`` for the console
script, ``deploy/compose.yaml`` and ``deploy/cluster/app-plane/workers.yaml`` for the
units, the renderer's pure function for the values). Deleting the unit, the entry
point or one rendered key therefore fails here rather than on the cluster.

The lane runs ``unittest`` and installs nothing -- no PyYAML -- so the manifests are
scanned as text the way the neighbouring guard tests scan them: the fields under test
are single lines, and a YAML parser would only add a dependency the job cannot have.
The renderer is loaded by path and its pure function is called directly.
"""

from __future__ import annotations

import importlib.util
import re
import tomllib
import unittest
from pathlib import Path
from typing import Protocol, cast

ROOT = Path(__file__).resolve().parents[2]
COMMAND = "speechnexus-job-cancellation-reconciler"
WORKER_MODULE = "speechnexus_backend.job_cancellation_worker:main"

SETTINGS_ALIASES = (
    "SPEECHNEXUS_JOB_CANCELLATION_RECONCILIATION_ENABLED",
    "SPEECHNEXUS_JOB_CANCELLATION_RECONCILIATION_INTERVAL_SECONDS",
    "SPEECHNEXUS_JOB_CANCELLATION_RECONCILIATION_BATCH_SIZE",
    "SPEECHNEXUS_JOB_CANCELLATION_RECONCILIATION_MAX_ATTEMPTS",
    "SPEECHNEXUS_JOB_CANCELLATION_RECONCILIATION_MIN_AGE_SECONDS",
    "SPEECHNEXUS_JOB_CANCELLATION_RECONCILIATION_CONFIRM_TIMEOUT_SECONDS",
    "SPEECHNEXUS_JOB_CANCELLATION_RECONCILIATION_LEASE_SECONDS",
    "SPEECHNEXUS_JOB_CANCELLATION_RECONCILIATION_TRANSACTION_TIMEOUT_SECONDS",
    "SPEECHNEXUS_DATABASE_URL",
    "SPEECHNEXUS_DATABASE_SCHEMA",
    "SPEECHNEXUS_EXECUTION_GATEWAY_BASE_URL",
    "SPEECHNEXUS_EXECUTION_GATEWAY_COMPONENT_CREDENTIAL",
    "SPEECHNEXUS_EXECUTION_GATEWAY_TIMEOUT_SECONDS",
)


class Renderer(Protocol):
    def gateway_origin(self, topology: str) -> str: ...

    def job_cancellation_env(self, dispatch: dict[str, str], topology: str) -> dict[str, str]: ...


def _renderer() -> Renderer:
    path = ROOT / "deploy/cluster/app-plane/render-config.py"
    spec = importlib.util.spec_from_file_location("render_config_for_assembly", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return cast(Renderer, module)


DOCUMENT = re.compile(r"(?m)^---\s*$")
KIND = re.compile(r"(?m)^kind:\s*(\w+)\s*$")
NAME_UNDER_METADATA = re.compile(r"(?m)^metadata:\n  name:\s*([\w.-]+)\s*$")


def _unit_text(path: Path, select: str) -> str:
    """One document from a multi-document manifest, found by its ``metadata.name``."""
    documents = DOCUMENT.split(path.read_text(encoding="utf-8"))
    for document in documents:
        name = NAME_UNDER_METADATA.search(document)
        if name is not None and name.group(1) == select:
            return document
    raise AssertionError(f"{path.name} has no document named {select}")


def _command(text: str) -> str:
    """The container command as one line: the entry point plus its flags.

    The manifests declare it two ways -- a flow list (``["cmd", "--once"]``) and a
    block list (``- cmd`` lines) -- and a guard that only understood one of them would
    pass by reading nothing, so both forms are unfolded here.
    """
    flow = re.search(r"(?m)^\s+command:\s*\[(.*?)\]\s*$", text)
    if flow is not None:
        return " ".join(part.strip().strip("\"'") for part in flow.group(1).split(","))
    block = re.search(r"(?m)^\s+command:\s*\n((?:\s+-\s+\S+\s*\n)+)", text)
    if block is None:
        raise AssertionError("the unit declares no command")
    return " ".join(
        line.strip().removeprefix("- ").strip("\"'")
        for line in block.group(1).splitlines()
        if line.strip()
    )


def _service_text(path: Path, select: str) -> str:
    """One service block from ``deploy/compose.yaml``: from its key to the next key."""
    text = path.read_text(encoding="utf-8")
    start = text.index(f"\n  {select}:\n")
    rest = text[start + 1 :]
    following = re.search(r"(?m)^  [a-z][\w.-]*:\s*$", rest[1:])
    end = len(rest) if following is None else following.start() + 1
    return rest[:end]


DISPATCH = {
    "SPEECHNEXUS_DATABASE_URL": "postgresql://sn_job_dispatch:pw@postgres:5432/speechnexus",
    "SPEECHNEXUS_DATABASE_SCHEMA": "speech",
    "SPEECHNEXUS_EXECUTION_GATEWAY_BASE_URL": "http://gateway:8010",
    "SPEECHNEXUS_EXECUTION_GATEWAY_ALLOW_PLAINTEXT_ORIGIN": "http://gateway:8010",
    "SPEECHNEXUS_EXECUTION_GATEWAY_COMPONENT_CREDENTIAL": "credential",
    "SPEECHNEXUS_EXECUTION_GATEWAY_TIMEOUT_SECONDS": "10",
}


class ConsoleScriptTests(unittest.TestCase):
    def test_the_console_script_is_installed(self) -> None:
        # A module with a ``main`` is not a unit: the image runs console scripts, so
        # the entry point is the thing that makes the process exist.
        scripts = tomllib.loads((ROOT / "backend/pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(scripts["project"]["scripts"][COMMAND], WORKER_MODULE)


class ClusterUnitTests(unittest.TestCase):
    def test_the_cluster_schedules_the_bounded_pass(self) -> None:
        text = _unit_text(ROOT / "deploy/cluster/app-plane/workers.yaml", "job-cancellation")
        kind = KIND.search(text)
        if kind is None:
            self.fail("no scheduled unit runs the cancellation pass")
        self.assertEqual(kind.group(1), "CronJob")
        self.assertRegex(text, r"(?m)^  schedule: ")
        # Two passes at once would reconcile the same rows twice; ``Forbid`` is the
        # decision, not a default.
        self.assertIn("concurrencyPolicy: Forbid", text)
        self.assertIn(COMMAND, _command(text))
        # ``--once`` is what makes this a scheduled bounded pass instead of a loop a
        # CronJob would pile up.
        self.assertIn("--once", _command(text))
        self.assertIn("job-cancellation-env", text, "the unit mounts no settings")


class ComposeUnitTests(unittest.TestCase):
    def test_compose_runs_the_same_command(self) -> None:
        text = _service_text(ROOT / "deploy/compose.yaml", "job-cancellation-reconciler")
        self.assertIn(COMMAND, _command(text))
        self.assertIn("--once", _command(text))
        # The dispatch identity carries `cancellation_reconciliation_grants`; a fourth
        # runtime credential for the same database would be a new thing to provision.
        self.assertIn("SPEECHNEXUS_DATABASE_URL: ${SPEECHNEXUS_JOB_DISPATCH_DATABASE_URL:-}", text)
        self.assertIn("SPEECHNEXUS_JOB_CANCELLATION_RECONCILIATION_ENABLED", text)


class RenderedSecretTests(unittest.TestCase):
    def test_the_secret_covers_every_setting_the_worker_validates(self) -> None:
        values = _renderer().job_cancellation_env(DISPATCH, "compose")
        missing = sorted(alias for alias in SETTINGS_ALIASES if alias not in values)
        self.assertEqual(missing, [], "the rendered Secret misses settings the worker reads")
        # It must be the dispatch role's DSN, not a second identity's.
        self.assertEqual(values["SPEECHNEXUS_DATABASE_URL"], DISPATCH["SPEECHNEXUS_DATABASE_URL"])
        # The origin is derived from the shared rule, not copied through the dispatch
        # dict: that is the one-definition-point invariant, and it also has to move
        # with the topology instead of staying wherever the dispatcher was rendered.
        self.assertEqual(
            values["SPEECHNEXUS_EXECUTION_GATEWAY_BASE_URL"],
            _renderer().gateway_origin("compose"),
        )
        self.assertNotEqual(
            values["SPEECHNEXUS_EXECUTION_GATEWAY_BASE_URL"],
            DISPATCH["SPEECHNEXUS_EXECUTION_GATEWAY_BASE_URL"],
            "a copied value would hide a renderer that derived the wrong topology",
        )
        self.assertEqual(values["SPEECHNEXUS_JOB_CANCELLATION_RECONCILIATION_ENABLED"], "true")
        # No object-storage or engine credential leaks into this unit's Secret.
        self.assertEqual([key for key in values if "OBJECT_STORAGE" in key or "MINIO" in key], [])


if __name__ == "__main__":
    unittest.main()
