"""Guard the one-command reference acceptance entry point against silent regressions.

`scripts/dev-env/reference-acceptance.py` is the single executable entry for
#572's acceptance: it preflights the environment, migrates, provisions the Job
roles, initializes the buckets, brings the API and gateway up, runs the startup
gate and probes health. An entry point is only useful if these properties hold,
so each has a test here:

- a blank or missing environment key fails the tools step and *names the key*;
- ``--dry-run`` performs static checks and plans only, never a write;
- a second run over an already provisioned database is idempotent *and* compares
  the rendered GRANT set with the installed ACL, so a revoked non-probe grant is
  red instead of "consistent";
- a failing ``docker compose up`` is red even when the old containers are still
  running/healthy, and the build output is printed on success;
- the stack step runs before the gate step, and the gate step still refuses to
  pass when the gateway was never started;
- the gate's FAIL line wins over an OK line, a malformed host port becomes a
  named step failure, and a non-StepFailureError is reported with the step name
  and exit code instead of a traceback;
- only-the-last-step failure keeps the earlier PASS lines and prints no final
  PASS;
- argument errors return 2 and the summary records pass/fail/not-run, the
  commit, the environment and the service images.

The migration check must derive the head from the revision graph. One test
removes a revision file from a fixture directory and requires the verdict to
follow the new head, so a hardcoded head string cannot pass.

Everything runs under the system interpreter: the fake runner replaces Docker,
PostgreSQL and MinIO, so no container or daemon is needed.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "reference_acceptance", ROOT / "scripts/dev-env/reference-acceptance.py"
)

COMPOSE_TEXT = """\
services:
  api:
    environment:
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:?set POSTGRES_PASSWORD}
      SPEECHNEXUS_API_DATABASE_URL: ${SPEECHNEXUS_API_DATABASE_URL:?set api dsn}
"""

JOB_DSNS = {
    "SPEECHNEXUS_JOB_WRITER_DATABASE_URL": "postgresql://sn_job_writer:pw-writer@postgres:5432/db",
    "SPEECHNEXUS_JOB_ADMISSION_DATABASE_URL": (
        "postgresql://sn_job_admission:pw-admission@postgres:5432/db"
    ),
    "SPEECHNEXUS_JOB_DISPATCH_DATABASE_URL": (
        "postgresql://sn_job_dispatch:pw-dispatch@postgres:5432/db"
    ),
}

BASE_ENV: dict[str, str] = {
    "POSTGRES_USER": "speechnexus",
    "POSTGRES_PASSWORD": "pw-owner",
    "POSTGRES_DB": "speechnexus",
    "POSTGRES_HOST_PORT": "55432",
    "SPEECHNEXUS_MIGRATION_DATABASE_URL": "postgresql://speechnexus:pw-owner@postgres:5432/db",
    "SPEECHNEXUS_API_DATABASE_URL": "postgresql://sn_api:pw-api@postgres:5432/db",
    "SPEECHNEXUS_GATEWAY_CONTROL_DATABASE_URL": "postgresql://sn_gateway:pw-gw@postgres:5432/db",
    "SPEECHNEXUS_GATEWAY_CONTROL_QUERY_DATABASE_URL": (
        "postgresql://sn_gateway_query:pw-gwq@postgres:5432/db"
    ),
    "SPEECHNEXUS_OBJECT_STORAGE_BUCKET": "results-bucket",
    "SPEECHNEXUS_TRANSCRIPTION_INPUT_BUCKET": "inputs-bucket",
    "SPEECHNEXUS_EXECUTION_RESULT_BUCKET": "results-bucket",
    "SPEECHNEXUS_DATABASE_SCHEMA": "speech",
    "SN_ADMIN_DSN": "postgresql://speechnexus:pw-owner@127.0.0.1:55432/speechnexus",
    "SN_GATEWAY_ROLE": "sn_gateway",
    **JOB_DSNS,
}

# The three shapes `provision-job-roles.py --print-statements` renders today.
# speech_publications is deliberately outside the eight probes: a revoked grant
# on it is what the old idempotent branch called "consistent".
RENDERED_STATEMENTS = (
    'GRANT USAGE ON SCHEMA "speech" TO "sn_job_writer";\n'
    'GRANT SELECT ON "speech".speech_jobs TO "sn_job_writer";\n'
    'GRANT SELECT, INSERT ON "speech".speech_publications TO "sn_job_writer";\n'
)
REVOKED_LABEL = "sn_job_writer 缺少 speech.speech_publications 的 SELECT"


def module() -> Any:
    assert SPEC is not None and SPEC.loader is not None
    loaded: Any = importlib.util.module_from_spec(SPEC)
    sys.modules[SPEC.name] = loaded
    SPEC.loader.exec_module(loaded)
    return loaded


HARNESS: Any = module()


def command(argv: Sequence[str], code: int = 0, stdout: str = "", stderr: str = "") -> Any:
    return HARNESS.CommandResult(tuple(argv), code, stdout, stderr)


def joined(argv: Sequence[str]) -> str:
    return " ".join(argv)


def step(ident: str) -> Any:
    return next(item for item in HARNESS.STEPS if item.ident == ident)


class FakeRunner:
    """Answer commands from a responder and record every invocation."""

    def __init__(self, responder: Callable[[tuple[str, ...]], Any]) -> None:
        self.responder = responder
        self.calls: list[tuple[str, ...]] = []

    def run(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> Any:
        self.calls.append(tuple(argv))
        return self.responder(tuple(argv))


def write_fixture_repo(
    root: Path,
    *,
    revisions: Sequence[tuple[str, str | None]] = (("0001_init", None),),
    compose_text: str = COMPOSE_TEXT,
) -> Path:
    """Create ``alembic.ini``, a revision graph and ``compose.yaml`` in ``root``."""
    (root / "migrations/versions").mkdir(parents=True, exist_ok=True)
    (root / "alembic.ini").write_text(
        "[alembic]\nscript_location = %(here)s/migrations\n", encoding="utf-8"
    )
    for name, parent in revisions:
        down = "None" if parent is None else f'"{parent}"'
        (root / f"migrations/versions/{name}.py").write_text(
            f'revision = "{name}"\ndown_revision = {down}\n', encoding="utf-8"
        )
    compose = root / "compose.yaml"
    compose.write_text(compose_text, encoding="utf-8")
    return compose


def write_env_file(root: Path, values: Mapping[str, str]) -> Path:
    path = root / "compose.env"
    path.write_text("".join(f"{key}={value}\n" for key, value in values.items()), encoding="utf-8")
    return path


def make_context(
    root: Path,
    responder: Callable[[tuple[str, ...]], Any],
    *,
    env: Mapping[str, str] | None = None,
    dry_run: bool = False,
    gate_mode: str = "compose",
    compose_file: Path | None = None,
) -> tuple[Any, FakeRunner]:
    runner = FakeRunner(responder)
    effective = dict(BASE_ENV)
    effective.update(env if env is not None else {})
    env_file = write_env_file(root, effective)
    context = HARNESS.Context(
        env=effective,
        env_file=env_file,
        compose_file=compose_file or (root / "compose.yaml"),
        project="sn-test",
        runner=runner,
        dry_run=dry_run,
        gate_mode=gate_mode,
        repo_root=root,
        child_env=effective,
    )
    return context, runner


def write_private_inputs(root: Path) -> Path:
    private = root / "private"
    private.mkdir()
    for name in ("gateway-file-asr.json", "gateway-file-tts.json"):
        (private / name).write_text(
            json.dumps({"objects": {"result_bucket": "results-bucket"}}), encoding="utf-8"
        )
    (private / "transcription-recipe.json").write_text("{}", encoding="utf-8")
    return private


def compose_config_document(private_dir: Path) -> str:
    """A resolved `docker compose config` document covering both consumers."""
    document = {
        "services": {
            "gateway": {
                "environment": {
                    "SPEECHNEXUS_GATEWAY_FILE_ASR_CONFIG_FILE": (
                        "/run/speechnexus-private/gateway-file-asr.json"
                    ),
                    "SPEECHNEXUS_GATEWAY_FILE_TTS_CONFIG_FILE": (
                        "/run/speechnexus-private/gateway-file-tts.json"
                    ),
                },
                "volumes": [
                    {"source": str(private_dir), "target": "/run/speechnexus-private"},
                ],
            },
            "api": {
                "environment": {
                    "SPEECHNEXUS_TRANSCRIPTION_RECIPE_FILE": (
                        "/run/speechnexus-private/transcription-recipe.json"
                    )
                }
            },
            "job-dispatch-worker": {
                "environment": {
                    "SPEECHNEXUS_DATABASE_URL": JOB_DSNS["SPEECHNEXUS_JOB_DISPATCH_DATABASE_URL"],
                    "SPEECHNEXUS_DATABASE_SCHEMA": "speech",
                    "SPEECHNEXUS_JOB_GATE_PRIVATE_DIR": "/run/speechnexus-private",
                    "SPEECHNEXUS_JOB_DISPATCH_KINDS": "asr,tts",
                    "SPEECHNEXUS_OBJECT_STORAGE_ENDPOINT": "http://minio:9000",
                    "SPEECHNEXUS_OBJECT_STORAGE_BUCKET": "results-bucket",
                    "SPEECHNEXUS_EXECUTION_RESULT_BUCKET": "results-bucket",
                    "SPEECHNEXUS_OBJECT_STORAGE_ACCESS_KEY": "key",
                    "SPEECHNEXUS_OBJECT_STORAGE_SECRET_KEY": "secret",
                },
                "volumes": [
                    {"source": str(ROOT / "deploy/job-startup-gate.py"), "target": "/opt/x.py"},
                    {"source": str(private_dir), "target": "/run/speechnexus-private"},
                ],
            },
        }
    }
    return json.dumps(document)


def healthy_services(services: Sequence[str]) -> str:
    return "\n".join(
        json.dumps({"Service": service, "State": "running", "Health": "healthy"})
        for service in services
    )


def standard_responder(
    *,
    state: dict[str, Any] | None = None,
    ps_output: str = "",
    compose_document: str = "{}",
    version: str = "0001_init",
) -> Callable[[tuple[str, ...]], Any]:
    """A responder that behaves like an already provisioned deployment."""

    shared: dict[str, Any] = state if state is not None else {}
    shared.setdefault(
        "roles",
        {
            "sn_api",
            "sn_gateway",
            "sn_gateway_query",
            "sn_job_writer",
            "sn_job_admission",
            "sn_job_dispatch",
        },
    )
    shared.setdefault("acl_gaps", [])
    shared.setdefault("rendered", RENDERED_STATEMENTS)

    def respond(argv: tuple[str, ...]) -> Any:
        text = joined(argv)
        if argv[:2] == ("docker", "version"):
            return command(argv, stdout="29.9.3\n")
        if argv[:3] == ("docker", "compose", "version"):
            return command(argv, stdout="Docker Compose version v5.7.0\n")
        if argv[:2] == ("uv", "--version"):
            return command(argv, stdout="uv 0.13.5\n")
        if "config" in argv and "--quiet" in argv:
            return command(argv)
        if "config" in argv and "--format" in argv:
            return command(argv, stdout=compose_document)
        if "images" in argv and "--format" in argv:
            return command(
                argv,
                stdout=json.dumps(
                    {
                        "Service": "api",
                        "Repository": "sn-test-api",
                        "Tag": "latest",
                        "ID": "sha256:0123456789abcdef",
                    }
                )
                + "\n",
            )
        if " ps " in f" {text} ":
            return command(argv, stdout=ps_output)
        if "psql" in argv:
            sql = argv[-1]
            if any(
                function in sql
                for function in (
                    "has_table_privilege",
                    "has_column_privilege",
                    "has_schema_privilege",
                )
            ):
                missing = [gap for gap in shared["acl_gaps"] if gap in sql]
                return command(argv, stdout="\n".join(missing))
            if "pg_roles" in sql:
                requested = {name for name in shared["roles"] if name in sql}
                return command(argv, stdout="\n".join(sorted(requested)))
            if "alembic_version" in sql:
                return command(argv, stdout=f"{version}\n")
            return command(argv)
        if "provision-job-roles.py" in text:
            if "--print-statements" in argv:
                return command(argv, stdout=shared["rendered"])
            return command(
                argv,
                stdout=(
                    "job roles ready: sn_job_writer sn_job_admission sn_job_dispatch; "
                    "grants: 3 statements\n"
                ),
            )
        if "minio-init" in text:
            if "-c" in argv:
                return command(argv, stdout="buckets verified: inputs-bucket, results-bucket\n")
            return command(
                argv,
                stdout=(
                    "bucket ready: inputs-bucket (versioning enabled)\n"
                    "bucket ready: results-bucket (versioning enabled)\n"
                ),
            )
        if "job-startup-gate.py" in text or "job-dispatch-worker" in text:
            return command(
                argv,
                stdout=(
                    "job dispatch startup gate: deployment prerequisites before any claim\n"
                    "  ok: database: role guard, startup SELECT probes\n"
                    "  pending: real engine\n"
                    "job dispatch startup gate: OK\n"
                ),
            )
        if " up " in f" {text} " or " run " in f" {text} ":
            return command(argv)
        return command(argv)

    return respond


def run_capture(context: Any, steps: Sequence[Any] | None = None) -> tuple[int, str]:
    steps = list(HARNESS.STEPS if steps is None else steps)
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = HARNESS.run_steps(steps, context)
    return code, buffer.getvalue()


def healthy_probes(url: str) -> tuple[int, str, str]:
    status = "ready" if url.endswith("/readyz") else "ok"
    return 200, json.dumps({"status": status}), ""


@contextlib.contextmanager
def patched_http_json(
    responder: Callable[[str], tuple[int, str, str]],
) -> Iterator[None]:
    original = HARNESS.http_json
    HARNESS.http_json = responder
    try:
        yield
    finally:
        HARNESS.http_json = original


class ToolsPreflightTest(unittest.TestCase):
    """A missing or blank environment key is a named failure, not a silent skip."""

    def test_missing_key_is_named_and_fails_the_tools_step(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_fixture_repo(root)
            env = dict(BASE_ENV)
            compose_text = (
                COMPOSE_TEXT + "      SPEECHNEXUS_REQUIRED_BY_COMPOSE: ${MISSING_KEY:?set it}\n"
            )
            compose = root / "compose.yaml"
            compose.write_text(compose_text, encoding="utf-8")
            context, _runner = make_context(
                root, standard_responder(), env=env, compose_file=compose
            )
            code, output = run_capture(context, (HARNESS.STEPS[0],))

        self.assertEqual(code, HARNESS.EXIT_TOOLS)
        self.assertIn("MISSING_KEY", output)
        self.assertIn("FAIL step=tools", output)

    def test_blank_value_counts_as_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_fixture_repo(root)
            env = dict(BASE_ENV)
            env["SN_ADMIN_DSN"] = "   "
            context, _runner = make_context(root, standard_responder(), env=env)
            code, output = run_capture(context, (HARNESS.STEPS[0],))

        self.assertEqual(code, HARNESS.EXIT_TOOLS)
        self.assertIn("SN_ADMIN_DSN", output)

    def test_indistinct_job_dsns_are_named(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_fixture_repo(root)
            env = dict(BASE_ENV)
            env["SPEECHNEXUS_JOB_ADMISSION_DATABASE_URL"] = JOB_DSNS[
                "SPEECHNEXUS_JOB_WRITER_DATABASE_URL"
            ]
            context, _runner = make_context(root, standard_responder(), env=env)
            code, output = run_capture(context, (HARNESS.STEPS[0],))

        self.assertEqual(code, HARNESS.EXIT_TOOLS)
        self.assertIn("互不相同", output)

    def test_key_sources_are_reported_separately(self) -> None:
        """D1: compose-derived keys and entry-extra keys are two different sources."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_fixture_repo(root)
            context, _runner = make_context(root, standard_responder())
            code, output = run_capture(context, (step("tools"),))

        self.assertEqual(code, 0, output)
        self.assertIn("2 个 compose 派生键", output)
        self.assertIn(f"{len(HARNESS.EXTRA_REQUIRED)} 个入口额外键", output)
        self.assertIn("共 8 个", output)


class DryRunTest(unittest.TestCase):
    """`--dry-run` plans the write steps and never invokes them."""

    def test_dry_run_plans_every_step_without_mutating(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            private = write_private_inputs(root)
            write_fixture_repo(root)
            env = dict(BASE_ENV)
            env["SPEECHNEXUS_PRIVATE_CONFIG_DIR"] = str(private)
            responder = standard_responder(compose_document=compose_config_document(private))
            context, runner = make_context(root, responder, env=env, dry_run=True)
            code, output = run_capture(context)

        self.assertEqual(code, 0, output)
        self.assertIn("DRY-RUN PASS", output)
        mutated = [
            call
            for call in runner.calls
            if any(word in call for word in ("up", "run", "build", "--build"))
        ]
        self.assertEqual(mutated, [], "dry-run must not invoke a write command")
        self.assertTrue(context.planned, "dry-run must still print the plan")

    def test_mutate_refuses_to_run_under_dry_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context, runner = make_context(root, standard_responder(), dry_run=True)
            with self.assertRaises(RuntimeError):
                context.mutate(("docker", "compose", "up", "-d"))
        self.assertEqual(runner.calls, [])


class IdempotencyTest(unittest.TestCase):
    """The roles step creates once and then compares the rendered grants with the ACL."""

    def _context(self, root: Path, state: dict[str, Any]) -> tuple[Any, FakeRunner]:
        write_fixture_repo(root)
        responder = standard_responder(state=state)
        return make_context(root, responder)

    def test_roles_step_creates_once_then_reports_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state: dict[str, Any] = {"roles": {"sn_gateway"}}
            context, runner = self._context(root, state)

            def responder(argv: tuple[str, ...]) -> Any:
                if "provision-job-roles.py" in joined(argv) and "--print-statements" not in argv:
                    state["roles"] |= {"sn_job_writer", "sn_job_admission", "sn_job_dispatch"}
                return standard_responder(state=state)(argv)

            runner.responder = responder
            first_code, first_output = run_capture(context, (step("roles"),))
            second_code, second_output = run_capture(context, (step("roles"),))

        self.assertEqual(first_code, 0, first_output)
        self.assertIn("已创建", first_output)
        self.assertIn("grants: 3 statements", first_output)
        self.assertIn("已装 ACL", first_output)
        self.assertEqual(second_code, 0, second_output)
        self.assertIn("幂等", second_output)
        self.assertIn("重渲染 3 条 GRANT", second_output)
        creates = [
            call
            for call in runner.calls
            if "provision-job-roles.py" in joined(call) and "--print-statements" not in call
        ]
        self.assertEqual(len(creates), 1, "a second run must not re-create anything")

    def test_partial_role_state_fails_instead_of_overwriting(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state: dict[str, Any] = {"roles": {"sn_gateway", "sn_job_writer"}}
            context, _runner = self._context(root, state)
            code, output = run_capture(context, (step("roles"),))

        self.assertEqual(code, HARNESS.EXIT_ROLES)
        self.assertIn("部分存在", output)
        self.assertIn("sn_job_admission", output)

    def test_missing_gateway_baseline_role_is_named(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state: dict[str, Any] = {"roles": set()}
            context, _runner = self._context(root, state)
            code, output = run_capture(context, (step("roles"),))

        self.assertEqual(code, HARNESS.EXIT_ROLES)
        self.assertIn("sn_gateway", output)
        self.assertIn("§6", output)

    def test_idempotent_branch_verifies_the_installed_acl(self) -> None:
        """All declared grants present -> green with an explicit comparison claim."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state: dict[str, Any] = {}
            context, runner = self._context(root, state)
            code, output = run_capture(context, (step("roles"),))
            acl_calls = [
                call
                for call in runner.calls
                if "psql" in call
                and any(
                    function in call[-1]
                    for function in (
                        "has_table_privilege",
                        "has_column_privilege",
                        "has_schema_privilege",
                    )
                )
            ]

        self.assertEqual(code, 0, output)
        self.assertIn("重渲染 3 条 GRANT", output)
        self.assertIn("已装 ACL 逐条核对全部在位", output)
        self.assertTrue(acl_calls, "the idempotent branch must read the installed ACL")
        self.assertIn("has_schema_privilege", acl_calls[0][-1])

    def test_idempotent_branch_detects_a_revoked_declared_grant(self) -> None:
        """B2 counterexample: REVOKE a non-probe grant -> red, not "consistent"."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state: dict[str, Any] = {"acl_gaps": [REVOKED_LABEL]}
            context, _runner = self._context(root, state)
            code, output = run_capture(context, (step("roles"),))

        self.assertEqual(code, HARNESS.EXIT_ROLES)
        self.assertIn("FAIL step=roles", output)
        self.assertIn(REVOKED_LABEL, output)
        self.assertIn("不一致", output)
        self.assertNotIn("全部在位", output)

    def test_unknown_grant_shape_fails_loudly(self) -> None:
        """A new GRANT shape must not be silently skipped as installed."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state: dict[str, Any] = {
                "rendered": 'GRANT EXECUTE ON FUNCTION "speech".f() TO "sn_job_writer";\n'
            }
            context, _runner = self._context(root, state)
            code, output = run_capture(context, (step("roles"),))

        self.assertEqual(code, HARNESS.EXIT_ROLES)
        self.assertIn("无法解析", output)


class GrantParsingTest(unittest.TestCase):
    """Rendered statements become catalog predicates, or the step fails."""

    def test_parses_the_rendered_shapes(self) -> None:
        checks = HARNESS.parse_grant_statement(
            'GRANT UPDATE (claim_owner_id, "claim_version") '
            'ON "speech".coordination_jobs TO "sn_job_writer";'
        )
        labels = [check.label for check in checks]
        predicates = [check.predicate for check in checks]
        self.assertEqual(
            labels,
            [
                "sn_job_writer 缺少 speech.coordination_jobs.claim_owner_id 的 UPDATE",
                "sn_job_writer 缺少 speech.coordination_jobs.claim_version 的 UPDATE",
            ],
        )
        self.assertIn("has_column_privilege", predicates[0])
        schema_checks = HARNESS.parse_grant_statement(
            'GRANT USAGE ON SCHEMA "speech" TO "sn_job_writer";'
        )
        self.assertIn("has_schema_privilege", schema_checks[0].predicate)
        table_checks = HARNESS.parse_grant_statement(
            'GRANT SELECT, INSERT ON "speech".speech_jobs TO "sn_job_writer";'
        )
        self.assertEqual(len(table_checks), 2)
        self.assertTrue(all("has_table_privilege" in item.predicate for item in table_checks))

    def test_unknown_target_class_is_refused(self) -> None:
        with self.assertRaises(HARNESS.StepFailureError):
            HARNESS.parse_grant_statement(
                'GRANT SELECT ON ALL TABLES IN SCHEMA "speech" TO "sn_job_writer";'
            )


class StackExitCodeTest(unittest.TestCase):
    """B1: `compose up` exit code is authoritative; healthy old containers are not."""

    def _context(self, root: Path) -> tuple[Any, FakeRunner]:
        private = write_private_inputs(root)
        write_fixture_repo(root)
        env = dict(BASE_ENV)
        env["SPEECHNEXUS_PRIVATE_CONFIG_DIR"] = str(private)
        base = standard_responder(
            ps_output=healthy_services(("api", "gateway")),
            compose_document=compose_config_document(private),
        )

        def responder(argv: tuple[str, ...]) -> Any:
            text = f" {joined(argv)} "
            if " up " in text and "api" in argv and "gateway" in argv:
                return command(
                    argv,
                    code=1,
                    stdout="api Building\n#9 exporting layers\n",
                    stderr=(
                        "failed to solve: failed to read dockerfile: open Dockerfile: no such file"
                    ),
                )
            return base(argv)

        return make_context(root, responder, env=env)

    def test_up_exit_nonzero_with_healthy_ps_is_red(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context, _runner = self._context(root)
            code, output = run_capture(context, (step("stack"),))

        self.assertEqual(code, HARNESS.EXIT_STACK, output)
        self.assertIn("FAIL step=stack", output)
        self.assertIn("退出码 1", output)
        self.assertIn("failed to solve", output)
        self.assertNotIn("结论: PASS", output)

    def test_success_prints_the_build_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            private = write_private_inputs(root)
            write_fixture_repo(root)
            env = dict(BASE_ENV)
            env["SPEECHNEXUS_PRIVATE_CONFIG_DIR"] = str(private)
            base = standard_responder(
                ps_output=healthy_services(("api", "gateway")),
                compose_document=compose_config_document(private),
            )

            def responder(argv: tuple[str, ...]) -> Any:
                text = f" {joined(argv)} "
                if " up " in text and "api" in argv and "gateway" in argv:
                    return command(argv, stdout="api Built\napi Started\ngateway Started\n")
                return base(argv)

            context, _runner = make_context(root, responder, env=env)
            code, output = run_capture(context, (step("stack"),))

        self.assertEqual(code, 0, output)
        self.assertIn("构建/启动输出", output)
        self.assertIn("gateway Started", output)
        self.assertIn("compose up 退出 0", output)


class StepOrderTest(unittest.TestCase):
    """B3: the stack is started before the gate runs, and the gate still guards."""

    def test_stack_precedes_gate(self) -> None:
        idents = [item.ident for item in HARNESS.STEPS]
        self.assertEqual(
            idents, ["tools", "migrate", "roles", "buckets", "stack", "gate", "health"]
        )
        self.assertLess(idents.index("stack"), idents.index("gate"))

    def test_fresh_environment_fails_at_stack_not_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_fixture_repo(root)
            # No private inputs and no running gateway: exactly a from-zero host.
            context, _runner = make_context(root, standard_responder())
            code, output = run_capture(context)

        self.assertEqual(code, HARNESS.EXIT_STACK, output)
        self.assertIn("FAIL step=stack", output)
        self.assertNotIn("FAIL step=gate", output)
        self.assertIn("未执行: gate, health", output)

    def test_gate_without_a_healthy_gateway_is_red(self) -> None:
        """Even run alone, the gate step cannot pass when the gateway never started."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            private = write_private_inputs(root)
            write_fixture_repo(root)
            env = dict(BASE_ENV)
            env["SPEECHNEXUS_PRIVATE_CONFIG_DIR"] = str(private)
            context, _runner = make_context(
                root,
                standard_responder(compose_document=compose_config_document(private)),
                env=env,
            )
            code, output = run_capture(context, (step("gate"),))

        self.assertEqual(code, HARNESS.EXIT_GATE)
        self.assertIn("gateway 未 healthy", output)
        self.assertIn("stack", output)

    def test_gate_after_a_healthy_stack_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            private = write_private_inputs(root)
            write_fixture_repo(root)
            env = dict(BASE_ENV)
            env["SPEECHNEXUS_PRIVATE_CONFIG_DIR"] = str(private)
            context, _runner = make_context(
                root,
                standard_responder(
                    ps_output=healthy_services(("api", "gateway")),
                    compose_document=compose_config_document(private),
                ),
                env=env,
            )
            code, output = run_capture(context, (step("gate"),))

        self.assertEqual(code, 0, output)
        self.assertIn("闸门 OK", output)


class GateVerdictTest(unittest.TestCase):
    """Parsed verdicts, with FAIL winning over OK."""

    def test_gate_verdict_reads_ok_and_pending_lines(self) -> None:
        result = command(
            ("python", "gate"),
            stdout=(
                "  ok: database: role guard\n"
                "  pending: real engine\n"
                "job dispatch startup gate: OK\n"
            ),
        )
        ok_lines, pending = HARNESS.gate_verdict(result)
        self.assertEqual(ok_lines, ["database: role guard"])
        self.assertEqual(pending, ["real engine"])

    def test_gate_fail_lines_raise_named_failure(self) -> None:
        result = command(
            ("python", "gate"),
            code=1,
            stdout="  FAIL: bucket x does not exist\nrefusing to start\n",
        )
        with self.assertRaises(HARNESS.StepFailureError) as caught:
            HARNESS.gate_verdict(result)
        self.assertIn("bucket x does not exist", caught.exception.message)

    def test_ok_and_fail_together_is_red_even_on_exit_zero(self) -> None:
        """D7/N3: OK first used to win; FAIL must be decided first."""
        for code in (0, 1):
            result = command(
                ("python", "gate"),
                code=code,
                stdout=(
                    "  FAIL: bucket x does not exist\n"
                    "  ok: database: role guard\n"
                    "job dispatch startup gate: OK\n"
                ),
            )
            with self.assertRaises(HARNESS.StepFailureError) as caught:
                HARNESS.gate_verdict(result)
            self.assertIn("bucket x does not exist", caught.exception.message)


class HealthStepTest(unittest.TestCase):
    """step_health and its non-StepFailure escape path."""

    def test_healthy_probes_pass(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_fixture_repo(root)
            context, _runner = make_context(root, standard_responder())
            with patched_http_json(healthy_probes):
                code, output = run_capture(context, (step("health"),))

        self.assertEqual(code, 0, output)
        self.assertIn("4/4 探针 200", output)

    def test_http_500_is_red(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_fixture_repo(root)
            context, _runner = make_context(root, standard_responder())

            def degraded(url: str) -> tuple[int, str, str]:
                if url.endswith(":8000/healthz"):
                    return 500, '{"status":"degraded"}', ""
                return healthy_probes(url)

            with patched_http_json(degraded):
                code, output = run_capture(context, (step("health"),))

        self.assertEqual(code, HARNESS.EXIT_HEALTH)
        self.assertIn("FAIL step=health", output)
        self.assertIn("返回 500", output)

    def test_malformed_port_is_a_named_step_failure(self) -> None:
        """D6: SPEECHNEXUS_API_HOST_PORT=8o00 used to escape as InvalidURL."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_fixture_repo(root)
            context, _runner = make_context(
                root, standard_responder(), env={"SPEECHNEXUS_API_HOST_PORT": "8o00"}
            )
            code, output = run_capture(context, (step("health"),))

        self.assertEqual(code, HARNESS.EXIT_HEALTH, output)
        self.assertIn("FAIL step=health", output)
        self.assertIn("SPEECHNEXUS_API_HOST_PORT", output)
        self.assertNotIn("Traceback", output)

    def test_http_json_returns_an_error_instead_of_raising(self) -> None:
        status, body, error = HARNESS.http_json("http://127.0.0.1:8o00/healthz")
        self.assertEqual(status, 0)
        self.assertEqual(body, "")
        self.assertIn("InvalidURL", error)


class RunStepsTest(unittest.TestCase):
    """Summary, exit codes and the unexpected-exception guard."""

    def test_only_last_step_failure_keeps_earlier_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            private = write_private_inputs(root)
            write_fixture_repo(root)
            env = dict(BASE_ENV)
            env["SPEECHNEXUS_PRIVATE_CONFIG_DIR"] = str(private)
            responder = standard_responder(
                ps_output=healthy_services(("api", "gateway")),
                compose_document=compose_config_document(private),
            )
            context, _runner = make_context(root, responder, env=env)

            def down(url: str) -> tuple[int, str, str]:
                if url.endswith(":8000/healthz"):
                    return 500, '{"status":"degraded"}', ""
                return healthy_probes(url)

            with patched_http_json(down):
                code, output = run_capture(context)

        self.assertEqual(code, HARNESS.EXIT_HEALTH, output)
        self.assertEqual(output.count("结论: PASS"), 6, output)
        self.assertIn("reference acceptance: FAIL step=health exit=70", output)
        self.assertNotIn("reference acceptance: PASS", output)
        self.assertIn("步骤记录: tools=PASS", output)
        self.assertIn("health=FAIL", output)
        self.assertIn("环境记录:", output)

    def test_unexpected_exception_gets_the_step_name_and_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context, _runner = make_context(root, standard_responder())

            def explode(_context: Any) -> str:
                raise OSError("disk gone")

            boom = HARNESS.Step("boom", "内核异常", 99, "前置检查", "预期输出", explode)
            code, output = run_capture(context, (boom,))

        self.assertEqual(code, 99)
        self.assertIn("FAIL step=boom", output)
        self.assertIn("exit=99", output)
        self.assertIn("OSError", output)
        self.assertNotIn("Traceback", output)

    def test_full_pass_records_steps_environment_commit_and_images(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            private = write_private_inputs(root)
            write_fixture_repo(root)
            env = dict(BASE_ENV)
            env["SPEECHNEXUS_PRIVATE_CONFIG_DIR"] = str(private)
            responder = standard_responder(
                ps_output=healthy_services(("api", "gateway")),
                compose_document=compose_config_document(private),
            )
            context, _runner = make_context(root, responder, env=env)
            with patched_http_json(healthy_probes):
                code, output = run_capture(context)

        self.assertEqual(code, 0, output)
        self.assertIn("reference acceptance: PASS steps=7", output)
        self.assertIn("步骤记录: tools=PASS", output)
        self.assertIn("health=PASS", output)
        self.assertNotIn("未运行", output)
        self.assertIn("环境记录:", output)
        self.assertIn("commit=", output)
        self.assertIn("镜像记录:", output)
        self.assertIn("api=sn-test-api:latest@", output)


class MainArgumentsTest(unittest.TestCase):
    """D6: argument errors return 2 with a message, never a traceback."""

    def test_missing_compose_file_returns_two(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            env_file = write_env_file(root, BASE_ENV)
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(io.StringIO()):
                code = HARNESS.main(
                    [
                        "--compose-file",
                        str(root / "missing-compose.yaml"),
                        "--env-file",
                        str(env_file),
                    ]
                )
        self.assertEqual(code, 2)
        self.assertIn("compose 文件不存在", stderr.getvalue())

    def test_missing_env_file_returns_two(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            compose = write_fixture_repo(root)
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(io.StringIO()):
                code = HARNESS.main(
                    [
                        "--compose-file",
                        str(compose),
                        "--env-file",
                        str(root / "missing.env"),
                    ]
                )
        self.assertEqual(code, 2)
        self.assertIn("环境文件不存在", stderr.getvalue())


class BuildFlagTest(unittest.TestCase):
    """D7: migrate and gate must build the image they run, not reuse a stale one."""

    def test_migrate_step_builds_before_running(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_fixture_repo(root)
            context, runner = make_context(root, standard_responder(version="0001_init"))
            code, output = run_capture(context, (step("migrate"),))
        self.assertEqual(code, 0, output)
        runs = [call for call in runner.calls if "migrate" in call and "run" in call]
        self.assertTrue(runs)
        self.assertIn("--build", runs[0])

    def test_gate_compose_step_builds_before_running(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            private = write_private_inputs(root)
            write_fixture_repo(root)
            env = dict(BASE_ENV)
            env["SPEECHNEXUS_PRIVATE_CONFIG_DIR"] = str(private)
            context, runner = make_context(
                root,
                standard_responder(
                    ps_output=healthy_services(("api", "gateway")),
                    compose_document=compose_config_document(private),
                ),
                env=env,
            )
            code, output = run_capture(context, (step("gate"),))
        self.assertEqual(code, 0, output)
        worker_runs = [call for call in runner.calls if "job-dispatch-worker" in call]
        self.assertTrue(worker_runs)
        self.assertIn("--build", worker_runs[0])


class StopOnFailureTest(unittest.TestCase):
    """The first failing step ends the run with its own exit code."""

    def test_behind_migration_stops_before_roles_buckets_and_stack(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_fixture_repo(root, revisions=(("0001_init", None), ("0002_next", "0001_init")))
            context, runner = make_context(root, standard_responder(version="0001_init"))
            code, output = run_capture(context)

        self.assertEqual(code, HARNESS.EXIT_MIGRATE)
        self.assertIn("FAIL step=migrate", output)
        self.assertIn("0002_next", output)
        self.assertIn("未执行: roles, buckets, stack, gate, health", output)
        invoked = " ".join(joined(call) for call in runner.calls)
        for later in ("provision-job-roles.py", "minio-init", "job-startup-gate.py"):
            self.assertNotIn(later, invoked, f"{later} must not run after a failed migrate")


class DerivedMigrationHeadTest(unittest.TestCase):
    """The head comes from the revision graph, so a removed file changes the verdict."""

    def test_head_follows_the_versions_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            versions = Path(temporary) / "versions"
            versions.mkdir()
            (versions / "0001_init.py").write_text(
                'revision = "0001_init"\ndown_revision = None\n', encoding="utf-8"
            )
            first_head, _parents = HARNESS.migration_heads(versions)
            (versions / "0002_more.py").write_text(
                'revision = "0002_more"\ndown_revision = "0001_init"\n', encoding="utf-8"
            )
            second_head, _parents = HARNESS.migration_heads(versions)
            (versions / "0002_more.py").unlink()
            removed_head, _parents = HARNESS.migration_heads(versions)

        self.assertEqual(first_head, "0001_init")
        self.assertEqual(second_head, "0002_more")
        self.assertEqual(removed_head, "0001_init", "removing a revision must move the head")
        self.assertIsNone(HARNESS.migration_verify("0002_more", "0002_more"))
        failure = HARNESS.migration_verify(removed_head, "0002_more")
        self.assertIsNotNone(failure)
        assert failure is not None
        self.assertIn("0001_init", failure)
        self.assertIn("0002_more", failure)

    def test_branched_revision_graph_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            versions = Path(temporary) / "versions"
            versions.mkdir()
            (versions / "a.py").write_text(
                'revision = "a"\ndown_revision = None\n', encoding="utf-8"
            )
            (versions / "b.py").write_text(
                'revision = "b"\ndown_revision = "a"\n', encoding="utf-8"
            )
            (versions / "c.py").write_text(
                'revision = "c"\ndown_revision = "a"\n', encoding="utf-8"
            )
            with self.assertRaises(HARNESS.StepFailureError) as caught:
                HARNESS.migration_heads(versions)

        self.assertIn("2 个 head", str(caught.exception))


class MigrationStepTest(unittest.TestCase):
    """The migrate step's verdict must follow the fixture's revision graph.

    Removing one revision script is the counterexample for a hardcoded head: the
    derived head moves back, the database row does not, and the step has to go red
    naming both. No real migration file is touched.
    """

    def test_migrate_step_follows_a_removed_revision(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_fixture_repo(root, revisions=(("0001_init", None), ("0002_next", "0001_init")))
            context, _runner = make_context(root, standard_responder(version="0002_next"))
            green_code, green_output = run_capture(context, (HARNESS.STEPS[1],))

            (root / "migrations/versions/0002_next.py").unlink()
            red_context, _runner = make_context(root, standard_responder(version="0002_next"))
            red_code, red_output = run_capture(red_context, (HARNESS.STEPS[1],))

        self.assertEqual(green_code, 0, green_output)
        self.assertIn("0002_next", green_output)
        self.assertEqual(red_code, HARNESS.EXIT_MIGRATE, red_output)
        self.assertIn("FAIL step=migrate", red_output)
        self.assertIn("0001_init", red_output)
        self.assertIn("0002_next", red_output)


class BucketAndGateParsingTest(unittest.TestCase):
    """Bucket names follow the init script rules; bucket output is parsed."""

    def test_bucket_names_follow_the_init_script_rules(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_fixture_repo(root)
            env = dict(BASE_ENV)
            env.pop("SPEECHNEXUS_EXECUTION_RESULT_BUCKET")
            context, _runner = make_context(root, standard_responder(), env=env)
            names = HARNESS.bucket_names(context)
        self.assertEqual(names, ["inputs-bucket", "results-bucket"])


if __name__ == "__main__":
    unittest.main()
