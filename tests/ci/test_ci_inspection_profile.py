"""Offline policy guards: every privileged command is mocked, never loaded here."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tarfile
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "ci_inspection_profile", ROOT / "scripts/dev-env/ci-inspection-profile.py"
)
assert SPEC is not None and SPEC.loader is not None
POLICY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(POLICY)


class PolicyTests(unittest.TestCase):
    def environment(self, temporary: str = "/synthetic") -> dict[str, str]:
        return {
            "GITHUB_ACTIONS": "true",
            "GITHUB_EVENT_NAME": "pull_request",
            "GITHUB_REPOSITORY": POLICY.REPOSITORY,
            "GITHUB_REF": "refs/pull/155/merge",
            "GITHUB_JOB": "python_checks",
            "GITHUB_RUN_ID": "101",
            "GITHUB_RUN_ATTEMPT": "1",
            "GITHUB_SHA": "a" * 40,
            "RUNNER_ENVIRONMENT": "github-hosted",
            "RUNNER_OS": "Linux",
            "RUNNER_ARCH": "X64",
            "RUNNER_NAME": "GitHub Actions synthetic 42",
            "RUNNER_TEMP": temporary,
        }

    def test_only_the_exact_hosted_job_and_event_classes_are_allowed(self) -> None:
        env = self.environment()
        runtime = POLICY.runtime_identity(env)
        self.assertNotIn("RUNNER_TEMP", runtime)
        for key in env.keys() - {"RUNNER_TEMP", "RUNNER_NAME"}:
            with self.subTest(field=key), self.assertRaises(RuntimeError):
                POLICY.runtime_identity({**env, key: "different"})
        for event, ref in (
            ("push", "refs/heads/main"),
            ("workflow_dispatch", "refs/heads/synthetic"),
        ):
            POLICY.runtime_identity({**env, "GITHUB_EVENT_NAME": event, "GITHUB_REF": ref})
        POLICY.runtime_identity({**env, "GITHUB_RUN_ATTEMPT": "2"})

    def test_local_invocation_does_not_run_any_command(self) -> None:
        with patch.dict(os.environ, {}, clear=True), patch.object(POLICY, "command") as call:
            with self.assertRaises(RuntimeError):
                POLICY.main()
            call.assert_not_called()

    def test_receipt_cannot_move_to_a_different_runner_or_attempt(self) -> None:
        with TemporaryDirectory() as temporary:
            env = self.environment(temporary)
            folder = Path(temporary) / "speech-inspection-profile"
            folder.mkdir()
            POLICY.save(folder, {"runtime": POLICY.runtime_identity(env), "load_attempted": True})
            for key, value in (("RUNNER_NAME", "another VM"), ("GITHUB_RUN_ATTEMPT", "2")):
                with (
                    self.subTest(field=key),
                    patch.dict(os.environ, {**env, key: value}, clear=True),
                    patch.object(POLICY, "load") as load,
                    patch.object(POLICY, "cleanup") as cleanup,
                    self.assertRaises(RuntimeError),
                ):
                    POLICY.main()
                load.assert_not_called()
                cleanup.assert_not_called()

    def test_no_existing_profile_or_local_override_is_replaced(self) -> None:
        with (
            patch.object(POLICY, "related_profiles", return_value={"bwrap": "enforce"}),
            self.assertRaisesRegex(RuntimeError, "never be replaced"),
        ):
            POLICY.check_existing_policy()
        with TemporaryDirectory() as temporary:
            existing = Path(temporary) / "bwrap"
            existing.touch()
            with (
                patch.object(POLICY, "related_profiles", return_value={}),
                patch.object(Path, "glob", return_value=[existing]),
                self.assertRaisesRegex(RuntimeError, "local overrides"),
            ):
                POLICY.check_existing_policy()

    def test_package_and_rule_hashes_are_both_required_before_loading(self) -> None:
        payload, package_bytes = b"synthetic never loaded", b"synthetic package"
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w") as tar:
            member = tarfile.TarInfo("./" + POLICY.MEMBER)
            member.size = len(payload)
            tar.addfile(member, io.BytesIO(payload))
        with TemporaryDirectory() as temporary:
            package = Path(temporary) / "fixture.deb"
            package.write_bytes(package_bytes)
            with patch.object(POLICY.subprocess, "run") as run:
                with self.assertRaisesRegex(RuntimeError, "package digest"):
                    POLICY.extract_profile(package)
                run.assert_not_called()
            with (
                patch.object(POLICY, "PACKAGE_SHA", hashlib.sha256(package_bytes).hexdigest()),
                patch.object(POLICY, "PROFILE_SHA", hashlib.sha256(payload).hexdigest()),
                patch.object(
                    POLICY.subprocess,
                    "run",
                    return_value=SimpleNamespace(stdout=archive.getvalue()),
                ),
            ):
                self.assertEqual(POLICY.extract_profile(package), payload)
                with (
                    patch.object(POLICY, "PROFILE_SHA", "0" * 64),
                    self.assertRaisesRegex(RuntimeError, "profile digest"),
                ):
                    POLICY.extract_profile(package)

    def test_claim_without_receipt_cannot_be_treated_as_no_load(self) -> None:
        with TemporaryDirectory() as temporary:
            folder = Path(temporary) / "speech-inspection-profile"
            folder.mkdir()
            POLICY.claim_once(folder)
            with (
                patch.dict(os.environ, self.environment(temporary), clear=True),
                patch.object(sys, "argv", ["policy", "cleanup"]),
                patch.object(POLICY, "command") as call,
                self.assertRaisesRegex(RuntimeError, "without receipt is unknown"),
            ):
                POLICY.main()
            call.assert_not_called()

    def test_timed_out_load_keeps_unknown_state_and_never_reloads_or_unloads(self) -> None:
        with TemporaryDirectory() as temporary:
            directory = Path(temporary)
            state = {
                "runtime": POLICY.runtime_identity(self.environment()),
                "load_attempted": False,
            }

            def command(args: list[str], *, cwd: Path | None = None) -> Any:
                if args[:2] == ["git", "rev-parse"]:
                    return SimpleNamespace(stdout="a" * 40)
                if args[:2] == ["apt-get", "download"]:
                    (directory / "apparmor-profiles_synthetic_all.deb").write_bytes(b"synthetic")
                if args[0] == "sudo":
                    raise subprocess.TimeoutExpired(args, 60)
                return SimpleNamespace(stdout="synthetic parser")

            with (
                patch.object(POLICY, "command", side_effect=command) as call,
                patch.object(POLICY, "check_existing_policy", return_value={}),
                patch.object(POLICY, "extract_profile", return_value=b"synthetic"),
                patch.object(POLICY, "process_snapshot", return_value={}),
                patch.object(POLICY, "related_profiles", return_value=POLICY.NAMES),
            ):
                with self.assertRaises(subprocess.TimeoutExpired):
                    POLICY.load(directory, state)
                self.assertTrue(state["load_attempted"])
                self.assertNotIn("load_command_finished", state)
                with self.assertRaisesRegex(RuntimeError, "never reload"):
                    POLICY.load(directory, state)
                with self.assertRaisesRegex(RuntimeError, "uncertain"):
                    POLICY.cleanup(directory, state)
                self.assertEqual(sum(x.args[0][0] == "sudo" for x in call.call_args_list), 1)

    def test_one_load_and_one_cleanup_with_exact_commands_and_no_install(self) -> None:
        with TemporaryDirectory() as temporary:
            directory = Path(temporary)
            payload = b"synthetic policy, all commands mocked"
            state = {
                "runtime": POLICY.runtime_identity(self.environment()),
                "load_attempted": False,
            }

            def command(args: list[str], *, cwd: Path | None = None) -> Any:
                if args[:2] == ["git", "rev-parse"]:
                    return SimpleNamespace(stdout="a" * 40)
                if args[:2] == ["apt-get", "download"]:
                    self.assertEqual(
                        args, ["apt-get", "download", "apparmor-profiles=" + POLICY.VERSION]
                    )
                    assert cwd == directory
                    (directory / "apparmor-profiles_synthetic_all.deb").write_bytes(b"synthetic")
                return SimpleNamespace(stdout="synthetic parser")

            with (
                patch.object(POLICY, "command", side_effect=command) as call,
                patch.object(POLICY, "check_existing_policy", return_value={}),
                patch.object(POLICY, "extract_profile", return_value=payload),
                patch.object(POLICY, "related_profiles", return_value=POLICY.NAMES) as profiles,
                patch.object(POLICY, "process_snapshot", return_value={"10": "100"}),
                patch.object(POLICY, "own_ancestors", return_value=set()),
                patch.object(POLICY, "PROFILE_SHA", hashlib.sha256(payload).hexdigest()),
            ):
                POLICY.load(directory, state)
                with self.assertRaisesRegex(RuntimeError, "never reload"):
                    POLICY.load(directory, state)
                profiles.return_value = {}
                profiles.side_effect = [POLICY.NAMES, {}, {}]
                POLICY.cleanup(directory, state)
                POLICY.cleanup(directory, state)
                kernel_commands = [x.args[0] for x in call.call_args_list if x.args[0][0] == "sudo"]
                self.assertEqual(
                    kernel_commands,
                    [
                        [
                            "sudo",
                            "-n",
                            POLICY.PARSER,
                            "-a",
                            "-K",
                            "-I",
                            "/etc/apparmor.d",
                            str(directory / "bwrap-userns-restrict"),
                        ],
                        [
                            "sudo",
                            "-n",
                            POLICY.PARSER,
                            "-R",
                            "-K",
                            "-I",
                            "/etc/apparmor.d",
                            str(directory / "bwrap-userns-restrict"),
                        ],
                    ],
                )
                receipt = json.loads((directory / "receipt.json").read_text())
                self.assertEqual(receipt["profiles_before"], {})
                self.assertEqual(receipt["profiles_after_load"], POLICY.NAMES)
                self.assertEqual(receipt["profiles_after_cleanup"], {})
                self.assertEqual(receipt["remaining_new_uid_processes"], [])

    def test_unknown_load_or_live_new_pid_only_queries_and_retains_policy(self) -> None:
        for finished in (False, True):
            with TemporaryDirectory() as temporary:
                state = {
                    "load_attempted": True,
                    "load_command_finished": finished,
                    "processes_before": {"10": "100"},
                    "load_returncode": 0,
                    "profiles_after_load": POLICY.NAMES,
                }
                with (
                    self.subTest(load_finished=finished),
                    patch.object(POLICY, "process_snapshot", return_value={"10": "101"}),
                    patch.object(POLICY, "own_ancestors", return_value=set()),
                    patch.object(POLICY, "related_profiles", return_value=POLICY.NAMES),
                    patch.object(POLICY, "command") as call,
                    self.assertRaises(RuntimeError),
                ):
                    POLICY.cleanup(Path(temporary), copy.deepcopy(state))
                call.assert_not_called()

    def test_failed_partial_add_never_removes_a_concurrently_present_rule(self) -> None:
        with TemporaryDirectory() as temporary:
            state = {"load_attempted": True, "load_command_finished": True, "load_returncode": 1}
            with (
                patch.object(POLICY, "related_profiles", return_value={"bwrap": "enforce"}),
                patch.object(POLICY, "command") as call,
                self.assertRaisesRegex(RuntimeError, "ownership uncertain"),
            ):
                POLICY.cleanup(Path(temporary), state)
            call.assert_not_called()

    def test_workflow_limits_loading_to_one_python_job_and_keeps_the_complete_gate(self) -> None:
        text = (ROOT / ".github/workflows/ci.yml").read_text()
        execution = text.split("  python_checks:\n")[1].split("  python_contracts:\n")[0]
        self.assertIn("runs-on: ubuntu-24.04", execution)
        self.assertNotIn("strategy:", execution)
        self.assertEqual(text.count("ci-inspection-profile.py load"), 1)
        self.assertEqual(text.count("ci-inspection-profile.py cleanup"), 1)
        self.assertIn(
            "if: always()\n        run: python3 scripts/dev-env/ci-inspection-profile.py cleanup",
            execution,
        )
        self.assertIn("scripts/dev-env/python-coverage.py run", execution)
        self.assertIn("--workers 2", execution)
        self.assertIn("media-smoke.py", execution)
        self.assertIn("uv build --all-packages", execution)
        self.assertIn("Verify inspection startup before complete regression", execution)
        self.assertNotIn("continue-on-error", execution)
        self.assertIn("permissions:\n  contents: read", text)


if __name__ == "__main__":
    unittest.main()
