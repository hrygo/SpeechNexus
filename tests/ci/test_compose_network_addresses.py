"""Keep the Compose subnet and MinIO address configurable without moving the default.

`#897`: `deploy/compose.yaml` pinned the stack network to `172.28.0.0/24` and MinIO to
`172.28.0.10`, so a host that already uses that range had to edit the repository file
to follow the runbook. Both values are now interpolated host-side variables whose
defaults are the previous literals, and `deploy/compose-network.py` derives the pair
from one CIDR so the operator never computes the MinIO address by hand.

The properties that keep the default path byte-for-byte identical, each checked here:

* the interpolation default in `deploy/compose.yaml` is still `172.28.0.0/24` and
  `172.28.0.10` — a default that moved would silently change every existing stack;
* the helper derives exactly those two values for the default subnet, so the copy of
  the default in Compose and the derivation rule cannot drift apart;
* the private gateway examples keep the default endpoint, which the runbook tells the
  operator to replace only in its copies;
* the runbook's change-the-subnet block prints the pair the helper actually produces.

The helper is imported through `importlib` because `deploy/` is not a package; this
module stays standard-library-only so the dependency-free guards job runs it.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HELPER_PATH = ROOT / "deploy" / "compose-network.py"
COMPOSE = ROOT / "deploy" / "compose.yaml"
ENV_EXAMPLE = ROOT / "deploy" / "compose.env.example"
RUNBOOK = ROOT / "docs" / "operations" / "compose-minimal.md"
PRIVATE = ROOT / "deploy" / "private"

DEFAULT_SUBNET = "172.28.0.0/24"
DEFAULT_MINIO_IP = "172.28.0.10"
DEFAULT_ENDPOINT = f"http://{DEFAULT_MINIO_IP}:9000"

SUBNET_VARIABLE = "SPEECHNEXUS_COMPOSE_SUBNET"
MINIO_VARIABLE = "SPEECHNEXUS_MINIO_STATIC_IP"

EXAMPLE_SUBNET = "172.30.0.0/24"
EXAMPLE_HOST_PORT = "8099"

INTERPOLATION = re.compile(r"\$\{(?P<name>[A-Z0-9_]+):-(?P<default>[^}]*)\}")

_SPEC = importlib.util.spec_from_file_location("compose_network_helper", HELPER_PATH)
assert _SPEC is not None and _SPEC.loader is not None
HELPER = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = HELPER
_SPEC.loader.exec_module(HELPER)


def compose_interpolations() -> dict[str, list[int]]:
    """Return ``{variable: [line numbers]}`` for the defaults in ``compose.yaml``."""
    found: dict[str, list[int]] = {}
    for number, line in enumerate(COMPOSE.read_text(encoding="utf-8").splitlines(), start=1):
        for match in INTERPOLATION.finditer(line):
            found.setdefault(match.group("name"), []).append(number)
    return found


def compose_default(variable: str) -> str:
    """Return the default Compose uses when ``variable`` is unset."""
    for line in COMPOSE.read_text(encoding="utf-8").splitlines():
        for match in INTERPOLATION.finditer(line):
            if match.group("name") == variable:
                return match.group("default")
    raise AssertionError(f"deploy/compose.yaml never interpolates {variable}")


class ComposeDefaultTest(unittest.TestCase):
    """Unset variables keep the addresses the existing reference stack already uses."""

    def test_the_default_subnet_stays_the_documented_one(self) -> None:
        self.assertEqual(compose_default(SUBNET_VARIABLE), DEFAULT_SUBNET)
        self.assertEqual(HELPER.derive(DEFAULT_SUBNET).subnet.compressed, DEFAULT_SUBNET)

    def test_the_default_minio_address_stays_the_documented_one(self) -> None:
        self.assertEqual(compose_default(MINIO_VARIABLE), DEFAULT_MINIO_IP)
        self.assertEqual(str(HELPER.derive(DEFAULT_SUBNET).minio_ip), DEFAULT_MINIO_IP)

    def test_the_helper_and_compose_derive_the_same_pair_for_the_default(self) -> None:
        derived = HELPER.derive(DEFAULT_SUBNET)
        subnet_line, minio_line = derived.env_assignments()
        self.assertEqual(subnet_line, f"{SUBNET_VARIABLE}={compose_default(SUBNET_VARIABLE)}")
        self.assertEqual(minio_line, f"{MINIO_VARIABLE}={compose_default(MINIO_VARIABLE)}")

    def test_each_variable_is_interpolated_exactly_once(self) -> None:
        interpolations = compose_interpolations()
        self.assertEqual(interpolations.get(SUBNET_VARIABLE), [899])
        self.assertEqual(interpolations.get(MINIO_VARIABLE), [91])

    def test_no_other_network_literal_is_pinned_in_compose(self) -> None:
        for number, line in enumerate(COMPOSE.read_text(encoding="utf-8").splitlines(), start=1):
            if not re.search(r"\b172\.28\.\d+\.\d+", line):
                continue
            self.assertTrue(
                SUBNET_VARIABLE in line or MINIO_VARIABLE in line,
                f"deploy/compose.yaml:{number} pins an address the subnet variable cannot move",
            )

    def test_the_example_environment_file_names_both_variables(self) -> None:
        text = ENV_EXAMPLE.read_text(encoding="utf-8")
        self.assertIn(f"\n{SUBNET_VARIABLE}={DEFAULT_SUBNET}\n", text)
        self.assertIn(f"\n{MINIO_VARIABLE}={DEFAULT_MINIO_IP}\n", text)


class PrivateExampleTest(unittest.TestCase):
    """The committed examples keep the default address; only copies are edited."""

    def test_every_gateway_example_pins_the_default_endpoint(self) -> None:
        endpoints = {}
        for path in sorted(PRIVATE.glob("gateway-file-*.example.json")):
            document = json.loads(path.read_text(encoding="utf-8"))
            objects = document.get("objects")
            if not isinstance(objects, dict) or "endpoint" not in objects:
                continue
            endpoints[path.name] = objects["endpoint"]
        self.assertEqual(
            set(endpoints),
            {
                "gateway-file-asr.example.json",
                "gateway-file-tts.example.json",
                "gateway-file-voice-clone.example.json",
                "gateway-file-voice-design.example.json",
            },
        )
        for name, endpoint in endpoints.items():
            with self.subTest(example=name):
                self.assertEqual(endpoint, DEFAULT_ENDPOINT)
                self.assertEqual(endpoint, HELPER.derive(DEFAULT_SUBNET).endpoint)


class DerivationTest(unittest.TestCase):
    """One CIDR input produces the MinIO address and the endpoint that goes with it."""

    def test_an_occupied_default_range_derives_the_documented_example(self) -> None:
        derived = HELPER.derive(EXAMPLE_SUBNET)
        self.assertEqual(derived.env_assignments()[0], f"{SUBNET_VARIABLE}={EXAMPLE_SUBNET}")
        self.assertEqual(derived.env_assignments()[1], f"{MINIO_VARIABLE}=172.30.0.10")
        self.assertEqual(derived.endpoint, "http://172.30.0.10:9000")

    def test_the_network_address_is_normalised_not_copied(self) -> None:
        self.assertEqual(str(HELPER.derive("10.77.0.0/16").minio_ip), "10.77.0.10")

    def test_a_subnet_that_cannot_hold_the_address_is_refused(self) -> None:
        for text in ("172.30.0.0/29", "172.28.0.5/24", "8.8.8.0/24", "fd00::/64", "not-a-cidr"):
            with self.subTest(subnet=text), self.assertRaises(HELPER.SubnetError):
                HELPER.derive(text)

    def test_the_command_prints_assignments_and_refuses_bad_input(self) -> None:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            self.assertEqual(HELPER.main([EXAMPLE_SUBNET]), 0)
        written = stdout.getvalue()
        self.assertIn(f"\n{SUBNET_VARIABLE}={EXAMPLE_SUBNET}\n", f"\n{written}")
        self.assertIn(f"\n{MINIO_VARIABLE}=172.30.0.10\n", f"\n{written}")
        self.assertIn("http://172.30.0.10:9000", written)

        stderr = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(stderr):
            self.assertEqual(HELPER.main(["8.8.8.0/24"]), 2)
        self.assertIn("private (RFC 1918)", stderr.getvalue())

    def test_the_command_without_an_argument_prints_the_default(self) -> None:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            self.assertEqual(HELPER.main([]), 0)
        self.assertIn(f"{SUBNET_VARIABLE}={DEFAULT_SUBNET}", stdout.getvalue())
        self.assertIn(f"{MINIO_VARIABLE}={DEFAULT_MINIO_IP}", stdout.getvalue())


class RunbookStepTest(unittest.TestCase):
    """The runbook's copy-paste block carries the values the helper produces."""

    def setUp(self) -> None:
        self.text = RUNBOOK.read_text(encoding="utf-8")

    def test_the_runbook_no_longer_says_the_addresses_have_no_entry_point(self) -> None:
        self.assertNotIn("没有环境变量入口", self.text)

    def test_the_runbook_documents_the_supported_variables(self) -> None:
        self.assertIn(SUBNET_VARIABLE, self.text)
        self.assertIn(MINIO_VARIABLE, self.text)

    def test_the_occupied_range_example_matches_the_helper_output(self) -> None:
        derived = HELPER.derive(EXAMPLE_SUBNET)
        for line in derived.env_assignments():
            self.assertIn(line, self.text)
        self.assertIn(derived.endpoint, self.text)

    def test_the_example_host_port_is_the_documented_one(self) -> None:
        self.assertIn(f"SPEECHNEXUS_API_HOST_PORT={EXAMPLE_HOST_PORT}", self.text)


if __name__ == "__main__":
    unittest.main()
