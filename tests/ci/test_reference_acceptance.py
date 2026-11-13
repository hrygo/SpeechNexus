"""Guard the one-command reference acceptance entry point against silent regressions.

`scripts/dev-env/reference-acceptance.py` is the single executable entry for
#572's acceptance: it preflights the environment, provisions the Job roles,
migrates, initializes the buckets, runs the startup gate and then brings the API
and gateway up. An entry point is only useful if four properties hold, so each
gets a test here:

- a blank or missing environment key fails the tools step and *names the key*;
- ``--dry-run`` performs static checks and plans only, never a write;
- a second run over an already provisioned database is idempotent;
- a failing step stops the run: later steps are reported as not executed.

The migration check must derive the head from the revision graph. The last test
removes one revision file from a fixture directory and requires the verdict to
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
from collections.abc import Callable, Mapping, Sequence
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
        "roles", {"sn_gateway", "sn_job_writer", "sn_job_admission", "sn_job_dispatch"}
    )

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
        if " ps " in f" {text} ":
            return command(argv, stdout=ps_output)
        if "psql" in argv:
            sql = argv[-1]
            if "has_column_privilege" in sql or "has_table_privilege" in sql:
                return command(argv)
            if "pg_roles" in sql:
                requested = {name for name in shared["roles"] if name in sql}
                return command(argv, stdout="\n".join(sorted(requested)))
            if "alembic_version" in sql:
                return command(argv, stdout=f"{version}\n")
            return command(argv)
        if "provision-job-roles.py" in text:
            if "--print-statements" in argv:
                return command(argv, stdout="GRANT A;\nGRANT B;\n")
            return command(
                argv,
                stdout=(
                    "job roles ready: sn_job_writer sn_job_admission sn_job_dispatch; "
                    "grants: 221 statements\n"
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
        if "job-startup-gate.py" in text:
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


class DryRunTest(unittest.TestCase):
    """`--dry-run` plans the write steps and never invokes them."""

    def test_dry_run_plans_every_step_without_mutating(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            private = root / "private"
            private.mkdir()
            for name in ("gateway-file-asr.json", "gateway-file-tts.json"):
                (private / name).write_text(
                    json.dumps({"objects": {"result_bucket": "results-bucket"}}), encoding="utf-8"
                )
            (private / "transcription-recipe.json").write_text("{}", encoding="utf-8")
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
    """The roles step creates once and then verifies instead of drifting."""

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
            first_code, first_output = run_capture(context, (HARNESS.STEPS[2],))
            second_code, second_output = run_capture(context, (HARNESS.STEPS[2],))

        self.assertEqual(first_code, 0, first_output)
        self.assertIn("已创建", first_output)
        self.assertIn("grants: 221 statements", first_output)
        self.assertEqual(second_code, 0, second_output)
        self.assertIn("幂等", second_output)
        self.assertIn("重渲染 2 条 GRANT", second_output)
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
            code, output = run_capture(context, (HARNESS.STEPS[2],))

        self.assertEqual(code, HARNESS.EXIT_ROLES)
        self.assertIn("部分存在", output)
        self.assertIn("sn_job_admission", output)

    def test_missing_gateway_baseline_role_is_named(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state: dict[str, Any] = {"roles": set()}
            context, _runner = self._context(root, state)
            code, output = run_capture(context, (HARNESS.STEPS[2],))

        self.assertEqual(code, HARNESS.EXIT_ROLES)
        self.assertIn("sn_gateway", output)
        self.assertIn("§6", output)


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
        self.assertIn("未执行: roles, buckets, gate, stack, health", output)
        invoked = " ".join(joined(call) for call in runner.calls)
        for later in ("provision-job-roles.py", "minio-init", "job-startup-gate.py"):
            self.assertNotIn(later, invoked, f"{later} must not run after a failed migrate")

    def test_gate_failure_names_the_gate_line(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            private = root / "private"
            private.mkdir()
            for name in ("gateway-file-asr.json", "gateway-file-tts.json"):
                (private / name).write_text(
                    json.dumps({"objects": {"result_bucket": "results-bucket"}}), encoding="utf-8"
                )
            write_fixture_repo(root)
            env = dict(BASE_ENV)
            env["SPEECHNEXUS_PRIVATE_CONFIG_DIR"] = str(private)

            def responder(argv: tuple[str, ...]) -> Any:
                if "job-dispatch-worker" in joined(argv):
                    return command(
                        argv,
                        code=1,
                        stdout=(
                            "job dispatch startup gate: deployment prerequisites before any claim\n"
                            "  FAIL: bucket results-bucket does not exist\n"
                        ),
                    )
                return standard_responder(
                    ps_output=healthy_services(("gateway",)),
                    compose_document=compose_config_document(private),
                )(argv)

            context, _runner = make_context(root, responder, env=env)
            code, output = run_capture(context, (HARNESS.STEPS[4],))

        self.assertEqual(code, HARNESS.EXIT_GATE)
        self.assertIn("FAIL step=gate", output)
        self.assertIn("bucket results-bucket does not exist", output)


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
    """Gate and bucket output are parsed, not assumed."""

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
