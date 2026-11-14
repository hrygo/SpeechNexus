"""Exercise image routing and fail-closed gates without Docker or dependencies."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CALLER = (ROOT / ".github/workflows/ci.yml").read_text()
CALLED = (ROOT / ".github/workflows/node-controller-image.yml").read_text()


def step_script(workflow: str, name: str) -> str:
    step = workflow.split(f"      - name: {name}\n", 1)[1]
    body = step.split("        run: |\n", 1)[1]
    lines = []
    for line in body.splitlines(keepends=True):
        if line.strip() and not line.startswith("          "):
            break
        lines.append(line)
    return textwrap.dedent("".join(lines))


def execute(
    script: str, env: dict[str, str], *, cwd: Path = ROOT
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-euo", "pipefail", "-c", script],
        cwd=cwd,
        env={**os.environ, **env},
        capture_output=True,
        text=True,
        check=False,
    )


SCOPE = step_script(CALLED, "Check image inputs")
IMAGE_GATE = step_script(CALLED, "Require valid scope and applicable image results")
FINAL_GATE = step_script(CALLER, "Require successful routing and every applicable CI result")


class ImageWorkflowTests(unittest.TestCase):
    def test_workflow_is_called_once_and_gates_both_runners(self) -> None:
        trigger = CALLED.split("\non:\n", 1)[1].split("\npermissions:", 1)[0]
        self.assertIn("  workflow_call:\n", trigger)
        self.assertNotIn("pull_request:", trigger)
        self.assertNotIn("workflow_dispatch:", trigger)
        self.assertNotIn("push:", trigger)
        self.assertIn("value: ${{ jobs.scope.outputs.required }}", trigger)
        self.assertEqual(CALLER.count("uses: ./.github/workflows/node-controller-image.yml"), 1)
        self.assertIn("needs: [scope, image]", CALLED)
        self.assertIn("if: ${{ always() }}", CALLED)
        self.assertIn("if: ${{ needs.scope.outputs.required == 'true' }}", CALLED)
        self.assertIn("SCOPE_RESULT: ${{ needs.scope.result }}", CALLED)
        self.assertIn("REQUIRED: ${{ needs.scope.outputs.required }}", CALLED)
        self.assertIn("IMAGE_RESULT: ${{ needs.image.result }}", CALLED)
        self.assertIn("runner: [ubuntu-24.04, ubuntu-24.04-arm]", CALLED)
        self.assertIn("fail-fast: false", CALLED)
        self.assertIn("needs.node_controller_image.result", CALLER)
        self.assertIn("needs.node_controller_image.outputs.required", CALLER)

    def test_image_gate_truth_table(self) -> None:
        for scope in ("success", "failure", "cancelled", "skipped", ""):
            for required in ("true", "false", "", "TRUE", "invalid"):
                for image in ("success", "failure", "cancelled", "skipped", ""):
                    with self.subTest(scope=scope, required=required, image=image):
                        result = execute(
                            IMAGE_GATE,
                            {"SCOPE_RESULT": scope, "REQUIRED": required, "IMAGE_RESULT": image},
                        )
                        valid = scope == "success" and (
                            (required, image) in (("true", "success"), ("false", "skipped"))
                        )
                        self.assertEqual(
                            result.returncode == 0, valid, result.stdout + result.stderr
                        )

    def test_final_gate_requires_successful_image_call_and_boolean_output(self) -> None:
        base = {
            "MODE": "docs",
            "ROUTING_RESULT": "success",
            "DELIVERY_RESULT": "success",
            "DOCS_FORMAT_RESULT": "success",
            "CHECKS_RESULT": "skipped",
            "COORDINATION_RESULT": "skipped",
            "INTEGRATION_RESULT": "skipped",
            "WEB_RESULT": "skipped",
            "DEPLOYMENT_RESULT": "skipped",
            "ACCEPTANCE_RESULT": "skipped",
        }
        for result in ("success", "failure", "cancelled", "skipped", ""):
            for required in ("true", "false", "", "TRUE", "invalid"):
                with self.subTest(result=result, required=required):
                    actual = execute(
                        FINAL_GATE,
                        {**base, "CONTROLLER_RESULT": result, "CONTROLLER_REQUIRED": required},
                    )
                    valid = result == "success" and required in ("true", "false")
                    self.assertEqual(actual.returncode == 0, valid, actual.stdout + actual.stderr)

    def test_final_gate_requires_delivery_matrix_freshness(self) -> None:
        base = {
            "MODE": "docs",
            "ROUTING_RESULT": "success",
            "DOCS_FORMAT_RESULT": "success",
            "CHECKS_RESULT": "skipped",
            "COORDINATION_RESULT": "skipped",
            "INTEGRATION_RESULT": "skipped",
            "WEB_RESULT": "skipped",
            "DEPLOYMENT_RESULT": "skipped",
            "ACCEPTANCE_RESULT": "skipped",
            "CONTROLLER_RESULT": "success",
            "CONTROLLER_REQUIRED": "false",
        }
        for result in ("success", "failure", "cancelled", "skipped", ""):
            with self.subTest(result=result):
                actual = execute(FINAL_GATE, {**base, "DELIVERY_RESULT": result})
                self.assertEqual(actual.returncode == 0, result == "success", actual.stderr)

    def test_final_gate_requires_the_reference_acceptance_entry(self) -> None:
        base = {
            "MODE": "docs",
            "ROUTING_RESULT": "success",
            "DELIVERY_RESULT": "success",
            "DOCS_FORMAT_RESULT": "success",
            "CHECKS_RESULT": "skipped",
            "COORDINATION_RESULT": "skipped",
            "INTEGRATION_RESULT": "skipped",
            "WEB_RESULT": "skipped",
            "DEPLOYMENT_RESULT": "skipped",
            "ACCEPTANCE_RESULT": "skipped",
            "CONTROLLER_RESULT": "success",
            "CONTROLLER_REQUIRED": "false",
        }
        for result in ("success", "failure", "cancelled", "skipped", ""):
            with self.subTest(result=result):
                actual = execute(FINAL_GATE, {**base, "ACCEPTANCE_RESULT": result})
                self.assertEqual(actual.returncode == 0, result == "skipped", actual.stderr)

    def test_web_scope_expects_python_and_web_success_and_skips_the_rest(self) -> None:
        # The `web` branch of the final gate is a truth table of its own: Python and
        # Web must succeed, PG/InnoDB, delivery and the acceptance entry must be
        # skipped. Mirroring it here keeps tests/ci as strong as tests/contract,
        # which already asserts it.
        base = {
            "MODE": "web",
            "ROUTING_RESULT": "success",
            "DELIVERY_RESULT": "success",
            "DOCS_FORMAT_RESULT": "skipped",
            "CONTROLLER_RESULT": "success",
            "CONTROLLER_REQUIRED": "false",
            "CHECKS_RESULT": "success",
            "COORDINATION_RESULT": "skipped",
            "INTEGRATION_RESULT": "success",
            "WEB_RESULT": "success",
            "DEPLOYMENT_RESULT": "skipped",
            "ACCEPTANCE_RESULT": "skipped",
        }
        actual = execute(FINAL_GATE, base)
        self.assertEqual(actual.returncode, 0, actual.stdout + actual.stderr)
        wrong = {
            "DOCS_FORMAT_RESULT": "success",
            "CHECKS_RESULT": "skipped",
            "COORDINATION_RESULT": "success",
            "INTEGRATION_RESULT": "skipped",
            "WEB_RESULT": "skipped",
            "DEPLOYMENT_RESULT": "success",
            "ACCEPTANCE_RESULT": "success",
        }
        for name, value in wrong.items():
            with self.subTest(lane=name):
                actual = execute(FINAL_GATE, {**base, name: value})
                self.assertNotEqual(actual.returncode, 0, f"{name}={value} 必须让门禁失败")

    def test_final_gate_rejects_a_skipped_entry_where_the_scope_runs_it(self) -> None:
        # The acceptance entry runs for full/no-db scopes; a skip there means the
        # operator entry point was not executed, so it cannot satisfy the gate.
        base = {
            "ROUTING_RESULT": "success",
            "DELIVERY_RESULT": "success",
            "CONTROLLER_RESULT": "success",
            "CONTROLLER_REQUIRED": "false",
        }
        for mode in ("full", "no-db"):
            applied = {
                "DOCS_FORMAT_RESULT": "skipped",
                "CHECKS_RESULT": "success",
                "COORDINATION_RESULT": "success" if mode == "full" else "skipped",
                "INTEGRATION_RESULT": "success" if mode == "full" else "skipped",
                "WEB_RESULT": "success",
                "DEPLOYMENT_RESULT": "success",
                "ACCEPTANCE_RESULT": "success",
            }
            with self.subTest(mode=mode, result="success"):
                actual = execute(FINAL_GATE, {**base, **applied, "MODE": mode})
                self.assertEqual(actual.returncode, 0, actual.stderr)
            for result in ("skipped", "failure", "cancelled", ""):
                with self.subTest(mode=mode, result=result):
                    actual = execute(
                        FINAL_GATE, {**base, **applied, "MODE": mode, "ACCEPTANCE_RESULT": result}
                    )
                    self.assertNotEqual(actual.returncode, 0, actual.stderr)

    def test_docs_scope_owes_the_markdown_format_lane(self) -> None:
        # #659/#661 merged as prose-only pull requests while `ruff format --check`
        # was skipped, then `main` went red on the same Markdown. The docs branch of
        # the final gate must require the lane's success, not accept a skip.
        base = {
            "MODE": "docs",
            "ROUTING_RESULT": "success",
            "DELIVERY_RESULT": "success",
            "CHECKS_RESULT": "skipped",
            "COORDINATION_RESULT": "skipped",
            "INTEGRATION_RESULT": "skipped",
            "WEB_RESULT": "skipped",
            "DEPLOYMENT_RESULT": "skipped",
            "ACCEPTANCE_RESULT": "skipped",
            "CONTROLLER_RESULT": "success",
            "CONTROLLER_REQUIRED": "false",
        }
        actual = execute(FINAL_GATE, {**base, "DOCS_FORMAT_RESULT": "success"})
        self.assertEqual(actual.returncode, 0, actual.stdout + actual.stderr)
        for result in ("skipped", "failure", "cancelled", ""):
            with self.subTest(result=result):
                actual = execute(FINAL_GATE, {**base, "DOCS_FORMAT_RESULT": result})
                self.assertNotEqual(actual.returncode, 0, actual.stderr)


class ScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="image-route-test-")
        self.addCleanup(self.temp.cleanup)
        self.temp_path = Path(self.temp.name)
        self.repo = self.temp_path / "repo"
        self.repo.mkdir()
        self.git_env = {
            **os.environ,
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
        }
        self.git("init", "-q")
        self.write("node-controller/source.py", "controller\n")
        self.write("docs/source.md", "docs\n")
        self.commit("base")
        self.base = self.git("rev-parse", "HEAD").strip()

    def git(self, *args: str) -> str:
        return subprocess.run(
            [
                "git",
                "-c",
                "user.name=CI",
                "-c",
                "user.email=ci@example.invalid",
                "-c",
                "core.hooksPath=/dev/null",
                *args,
            ],
            cwd=self.repo,
            env=self.git_env,
            capture_output=True,
            text=True,
            check=True,
        ).stdout

    def write(self, name: str, value: str) -> None:
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value)

    def commit(self, name: str) -> None:
        self.git("add", "-A")
        self.git("commit", "-qm", name)

    def route(self, event: str = "pull_request", base: str | None = None) -> tuple[int, str]:
        output = self.temp_path / "output.txt"
        output.write_text("")
        result = execute(
            SCOPE,
            {
                **self.git_env,
                "GITHUB_OUTPUT": str(output),
                "RUNNER_TEMP": str(self.temp_path),
                "EVENT_NAME": event,
                "BASE_SHA": self.base if base is None else base,
            },
            cwd=self.repo,
        )
        return result.returncode, output.read_text()

    def test_unrelated_docs_skip_image(self) -> None:
        self.write("docs/source.md", "changed\n")
        self.commit("docs")
        self.assertEqual(self.route(), (0, "required=false\n"))

    def test_image_inputs_and_workflow_files_require_build(self) -> None:
        for name in (
            "node-controller/new.py",
            "deploy/images/new",
            ".dockerignore",
            "uv.lock",
            ".github/workflows/ci.yml",
            ".github/workflows/node-controller-image.yml",
            "tests/ci/test_node_controller_image_ci.py",
            "tests/contract/test_ci_workflow.py",
        ):
            with self.subTest(path=name):
                self.write(name, "change\n")
                self.commit("change")
                self.assertEqual(self.route(), (0, "required=true\n"))
                self.base = self.git("rev-parse", "HEAD").strip()

    def test_rename_out_of_controller_and_newline_in_filename_require_build(self) -> None:
        self.git("mv", "node-controller/source.py", "docs/moved.md")
        self.commit("rename")
        self.assertEqual(self.route(), (0, "required=true\n"))
        self.base = self.git("rev-parse", "HEAD").strip()
        self.write("node-controller/name\nwith-newline.py", "change\n")
        self.commit("newline")
        self.assertEqual(self.route(), (0, "required=true\n"))

    def test_empty_diff_and_non_pr_events_require_build(self) -> None:
        self.assertEqual(self.route(), (0, "required=true\n"))
        for event in ("push", "workflow_dispatch", "unknown"):
            with self.subTest(event=event):
                self.assertEqual(self.route(event, base=""), (0, "required=true\n"))

    def test_invalid_or_unavailable_base_fails_closed(self) -> None:
        for base in ("", "0" * 40, "invalid", "a" * 40):
            with self.subTest(base=base):
                code, output = self.route(base=base)
                self.assertNotEqual(code, 0)
                self.assertNotIn("required=false", output)


class TlsFailureTests(unittest.TestCase):
    def test_requires_target_request_and_matching_certificate_category(self) -> None:
        spec = importlib.util.spec_from_file_location(
            "image_verify", ROOT / "deploy/images/verify-node-controller-image.py"
        )
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        url = "https://wrong-host.internal:123/local_ca_probe-1.0-py3-none-any.whl"
        target = f"#9 1.2 error sending request for url\n#9 1.2 │   ({url})\n"
        issuer = "#9 1.2 ╰─▶ invalid peer certificate: UnknownIssuer\n"
        hostname = (
            "#9 1.2 ╰─▶ invalid peer certificate: certificate not valid for name "
            '"wrong-host.internal"\n'
        )
        module.assert_tls_failure(target + issuer, url)
        module.assert_tls_failure(target + hostname, url, hostname=True)
        for output, wrong_host in (
            ("registry certificate: UnknownIssuer", False),
            ("error sending request for url (https://registry.invalid/token)\n" + issuer, False),
            (target + "#8 1.2 invalid peer certificate: UnknownIssuer\n", False),
            (
                target + "#9 1.2 client error (Connect): Connection refused\n"
                "#9 1.2 error sending request for url (https://registry.invalid/token)\n" + issuer,
                False,
            ),
            (target + issuer, True),
            (target + hostname, False),
        ):
            with self.subTest(output=output, hostname=wrong_host), self.assertRaises(RuntimeError):
                module.assert_tls_failure(output, url, hostname=wrong_host)


if __name__ == "__main__":
    unittest.main()
