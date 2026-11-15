"""Guard the reference deployment's admission poller against silent removal.

The report at ``GET /v1/nexus/jobs/admission-readiness`` is only worth having if
something reads it. A deployment started from ``deploy/compose.yaml`` must poll it
without the operator building a timer, and the poller must not be able to turn a
degraded deployment into a restart loop -- a restart cannot renew a readiness
receipt, it only hides the degradation (#845).

This module parses text only, like the other ``tests/ci`` guards. It runs under the
system interpreter and never imports the backend, renders Compose, connects to
PostgreSQL or starts a container. Mutations that must turn it red:

- deleting the ``job-admission-watch`` service, or dropping its ``profiles:`` entry
  so the minimal base surface silently grows an observer it cannot guarantee;
- dropping ``--profile admission-watch`` from the documented reference start
  command, or dropping the acceptance step that requires the poller to be
  running/healthy (a profile without that step is a silent absence);
- changing its ``restart: "no"`` to a restart policy, adding a ``depends_on`` so the
  observation waits on the condition it reports, or dropping the healthcheck that
  reads its own last observation;
- dropping ``read_only`` or the private state-file tmpfs;
- making ``api``'s healthcheck point at the admission report instead of ``/readyz``;
- dropping the console script from ``backend/pyproject.toml``, or letting the poller
  import a non-stdlib dependency, spawn a process or open a database connection;
- dropping the poller's keys from ``deploy/compose.env.example``;
- dropping one of the three required causes from the declared enum or from the
  domain single source of the codes;
- deleting the operator doc that states the division of labour.

The behaviour behind this wiring -- the exact bodies, the statuses, the mapping from
each cause to the public create code, and the poller's own classification -- is
proven by ``tests/contract/test_job_admission_readiness.py``,
``tests/unit/backend/test_job_admission_readiness.py``,
``tests/unit/backend/test_admission_watch.py`` and the PostgreSQL HTTP case in
``tests/integration/test_job_admission_readiness_pg.py``. This guard only requires
that the deployment keeps assembling the entry points they describe.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "deploy/compose.yaml"
ENV_EXAMPLE = ROOT / "deploy/compose.env.example"
PYPROJECT = ROOT / "backend/pyproject.toml"
CONTRACT = ROOT / "contracts/speech-openapi.yaml"
BINDINGS = ROOT / "contracts/bindings.json"
WATCH_MODULE = ROOT / "backend/src/speechnexus_backend/admission_watch.py"
DOMAIN_MODULE = ROOT / "backend/src/speechnexus_backend/speech/domain/job_admission_readiness.py"
OPERATOR_DOC = ROOT / "docs/operations/engine-readiness-observability.md"

SERVICE = "job-admission-watch"
COMMAND = "speechnexus-admission-watch"
PATH = "/v1/nexus/jobs/admission-readiness"
OPERATION = "getJobAdmissionReadiness"
CAUSES = ("engine_receipt_expired", "capacity_exhausted", "storage_unavailable")
OPTIONAL_CAUSE = "workspace_not_provisioned"
ENV_KEYS = (
    "SPEECHNEXUS_ADMISSION_WATCH_URL",
    "SPEECHNEXUS_ADMISSION_WATCH_INTERVAL_SECONDS",
    "SPEECHNEXUS_ADMISSION_WATCH_TIMEOUT_SECONDS",
)
STATE_FILE_KEY = "SPEECHNEXUS_ADMISSION_WATCH_STATE_FILE"
HEALTHCHECK_FLAG = "--healthcheck"


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


def operation_block(document: str, operation_id: str) -> str:
    """Return one declared operation, from its first line to its responses."""
    marker = document.index(f"operationId: {operation_id}")
    start = document.rindex("\n    get:\n", 0, marker)
    end = document.index("responses:", marker)
    return document[start:end]


class ComposePollerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.compose = read(COMPOSE)

    def test_the_poller_is_its_own_opt_in_profile(self) -> None:
        block = service_block(self.compose, SERVICE)
        # The base surface is the minimum needed to serve traffic: an observer that
        # can be absent (or silently `Exited`) without failing anything must not be
        # declared there. Its absence is caught by the acceptance step instead.
        self.assertIn('profiles: ["admission-watch"]', block)
        self.assertIn(f'command: ["{COMMAND}"]', block)
        header = self.compose[: self.compose.index("services:")]
        self.assertIn("`admission-watch`", header, "the header must list the profile")

    def test_the_poller_cannot_restart_anything(self) -> None:
        block = service_block(self.compose, SERVICE)
        self.assertIn('restart: "no"', block, "a restart cannot renew a receipt")
        # The observation must not wait on the condition it reports.
        self.assertNotIn("depends_on:", block)
        # And it must hold no write authority of its own.
        self.assertIn("read_only: true", block)
        self.assertIn("cap_drop:", block)
        self.assertIn("no-new-privileges:true", block)

    def test_the_poller_reads_the_report_and_publishes_its_own_state(self) -> None:
        block = service_block(self.compose, SERVICE)
        self.assertIn("SPEECHNEXUS_ADMISSION_WATCH_URL", block)
        self.assertIn(PATH, block)
        self.assertIn(f"{STATE_FILE_KEY}: /run/admission-watch/state.json", block)
        # The state file lives on a private tmpfs, so the root can stay read-only.
        self.assertIn("/run/admission-watch:", block)
        self.assertIn(HEALTHCHECK_FLAG, block)
        self.assertIn(COMMAND, block.split("healthcheck:", 1)[1])

    def test_the_poller_is_an_http_only_client(self) -> None:
        block = service_block(self.compose, SERVICE)
        # No database role, no engine credential: the report is the only input.
        for forbidden in ("DATABASE_URL", "ENGINE_HEALTH_URL", "CREDENTIAL", "RECIPE_FILE"):
            self.assertNotIn(forbidden, block, f"the poller must not hold {forbidden}")

    def test_no_service_gates_itself_on_the_poller(self) -> None:
        # The API keeps its process-health probe: pointing it at a business surface
        # would evict the whole API for a create-only degradation (#828 review B8).
        block = service_block(self.compose, "api")
        self.assertIn("/readyz", block)
        self.assertNotIn("admission-readiness", block)
        # No other service lists the poller under `depends_on` either: a startup
        # gate on it would make a startup depend on the observation.
        self.assertIsNone(
            re.search(rf"(?m)^      {re.escape(SERVICE)}(:|\?)\s*$", self.compose),
            "no service may depend on the poller",
        )

    def test_the_base_service_list_stays_minimal(self) -> None:
        header = self.compose[: self.compose.index("services:")]
        base = header[header.index("Base `docker compose config --services`") :]
        # The listing is the commented block up to the first bare `#` line.
        listing = base.split("\n#\n", 1)[0]
        for name in ("postgres", "minio", "minio-init", "api", "gateway"):
            self.assertIn(name, listing)
        self.assertNotIn(SERVICE, listing, "the poller is opt-in, not part of the base surface")


class PollerEntryPointTests(unittest.TestCase):
    def test_the_poller_is_an_installed_console_script(self) -> None:
        self.assertIn(
            f'{COMMAND} = "speechnexus_backend.admission_watch:main"',
            read(PYPROJECT),
            "the poller must be an installed entry point, not an ad-hoc module path",
        )

    def test_the_poller_is_standard_library_only(self) -> None:
        # The healthcheck runs this same entry point: a probe must report the truth
        # without the application's dependency tree.
        module = read(WATCH_MODULE)
        imports = re.findall(r"(?m)^(?:from|import) ([a-zA-Z0-9_.]+)", module)
        for name in imports:
            self.assertFalse(
                name.startswith(("speechnexus", "psycopg", "httpx", "pydantic", "fastapi")),
                f"admission_watch must stay stdlib-only, found {name}",
            )

    def test_the_poller_cannot_repair_or_restart_anything(self) -> None:
        # Prose may name the renewal and Docker; the code must not be able to reach
        # either. Restarting cannot renew a receipt, so a poller that could restart
        # would only hide the degradation it was added to expose.
        body = read(WATCH_MODULE).split('"""', 2)[-1]
        for forbidden in ("subprocess", "Popen", "os.system", "psycopg", "requests", "httpx"):
            self.assertNotIn(forbidden, body, f"the poller code must not reach {forbidden}")

    def test_the_poller_forwards_the_cause_enum_verbatim(self) -> None:
        # The classes are defined once, in the declared enum: the poller copies
        # `causes` through instead of maintaining a second vocabulary.
        body = read(WATCH_MODULE)
        self.assertIn('causes = document.get("causes")', body)
        self.assertIn("sorted(causes)", body)

    def test_the_state_file_has_a_stale_guard(self) -> None:
        # A dead loop must not leave the last green observation standing.
        module = read(WATCH_MODULE)
        self.assertIn("observed_at_us", module)
        self.assertIn("age_seconds > stale_after_seconds", module)


class OperatorSurfaceTests(unittest.TestCase):
    def test_the_env_example_documents_the_poller_keys(self) -> None:
        example = read(ENV_EXAMPLE)
        for key in ENV_KEYS:
            self.assertIn(f"{key}=", example, f"compose.env.example must document {key}")
        # The degradation is not silent, and the docs must say why the poller is
        # preferred over a deployment-side timer.
        self.assertIn("job-admission-watch", example)
        for cause in CAUSES:
            self.assertIn(cause, example, f"compose.env.example must name {cause}")

    def test_the_declared_enum_carries_all_three_required_causes(self) -> None:
        contract = read(CONTRACT)
        block = operation_block(contract, OPERATION)
        for cause in CAUSES:
            self.assertIn(f"`{cause}`", block, f"the operation description must name {cause}")
        self.assertIn(PATH, contract)
        schema = contract[contract.index("    AdmissionReadiness:\n") :]
        enum_block = schema[: schema.index("workspace_id:")]
        for cause in (*CAUSES, OPTIONAL_CAUSE):
            self.assertIn(f"- {cause}", enum_block, f"the declared enum must carry {cause}")
        # The document is a declared, generated contract surface, not a free-form one.
        self.assertIn("AdmissionReadiness", read(BINDINGS))

    def test_the_operation_states_where_the_verdict_lives(self) -> None:
        block = operation_block(read(CONTRACT), OPERATION)
        # The status code says whether the report could be produced.
        self.assertIn("200", block)
        self.assertIn("`job_unavailable`", block)
        # And the division of labour with the process-health surface survives.
        self.assertIn("/readyz", block)
        self.assertIn("load balancer", block)

    def test_the_domain_module_is_the_single_source_of_the_codes(self) -> None:
        domain = read(DOMAIN_MODULE)
        for code in (
            "job_capacity_exhausted",
            "job_unavailable",
            "workspace_not_configured",
            "PUBLIC_ADMISSION_CODES",
        ):
            self.assertIn(code, domain, f"the domain mapping must single-source {code}")
        for cause in (*CAUSES, OPTIONAL_CAUSE):
            self.assertIn(f'{cause}"', domain, f"the domain enum must carry {cause}")

    def test_the_operator_doc_states_the_division_of_labour(self) -> None:
        doc = read(OPERATOR_DOC)
        for fact in (PATH, SERVICE, "/readyz", "engine_receipt_expired", *CAUSES):
            self.assertIn(fact, doc, f"the operator doc must explain {fact}")
        # The doc must state that the surface changes no admission decision and
        # restarts nothing, and name the symbol that would have to move otherwise.
        self.assertIn("decide_admission", doc)
        self.assertIn('restart: "no"', doc)
        self.assertIn("RestartCount", doc)

    def test_the_operator_doc_states_the_two_boundaries_of_the_signal(self) -> None:
        doc = read(OPERATOR_DOC)
        # "Readable" is not "will be seen": this deployment ships no log shipping and
        # no alert consumer, and a later reader must not assume one exists.
        self.assertIn("没有日志采集", doc)
        self.assertIn("不是**「信号会被主动推送」", doc)
        # And it does not report process health: that stays `/readyz`'s question.
        self.assertIn("不报「进程坏了」", doc)

    def test_the_operator_doc_ships_the_reference_profile_command(self) -> None:
        # Forgetting the profile must be a doc failure, not a silent absence: the
        # documented reference start command has to enable the poller.
        doc = read(OPERATOR_DOC)
        commands = [
            line.strip() for line in doc.splitlines() if "docker compose" in line and " up" in line
        ]
        self.assertTrue(commands, "the doc must ship a reference start command")
        self.assertTrue(
            any(re.search(r"--profile\s+admission-watch", line) for line in commands),
            f"the reference start command must enable the profile: {commands}",
        )

    def test_the_operator_doc_has_an_acceptance_step_for_the_poller(self) -> None:
        # This is the only place where "the observer is absent" becomes a visible
        # failure. Without it the profile, the doc and the report can all be right
        # while the deployment observes nothing -- the #845 shape again.
        doc = read(OPERATOR_DOC)
        start = doc.index("**验收清单**")
        end = doc.index("\n## ", start)
        section = doc[start:end]
        self.assertIn("ps", section, "the acceptance list must query the service state")
        # Wrapped list items are one step: the numbering is the step boundary.
        steps = re.split(r"(?m)^\d+\.\s+", section)[1:]
        checked = [
            step for step in steps if SERVICE in step and ("healthy" in step or "running" in step)
        ]
        self.assertTrue(
            checked,
            "the acceptance list must have a step requiring the poller running/healthy",
        )


if __name__ == "__main__":
    unittest.main()
