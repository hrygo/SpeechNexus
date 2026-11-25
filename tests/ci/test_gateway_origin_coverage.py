"""Every credential-presenting path goes through the one gateway-origin rule.

``#1084``: while the Job dispatcher, the TTS settlement worker, the binding-disable
worker and the TTS recovery command line applied the shared rule, the synchronous
ASR settlement worker, the synchronous transcription route and the cancellation
reconciliation worker presented ``SPEECHNEXUS_EXECUTION_GATEWAY_COMPONENT_CREDENTIAL``
without it. A hand-written list of "covered paths" rots on the next entry point, so
this guard derives the set from the source instead:

* a module is a credential-presenting path because it names the credential
  environment key — that is how the operator supplies the bearer token, and it is
  the reading every previously covered path performed;
* such a module must call ``require_safe_gateway_origin``. Adding a credential
  reader without the rule turns this red instead of shipping a path that can send
  the token over plain HTTP;
* the three functions that decide an origin are defined in exactly one module, so a
  second normaliser or a second allow-key read cannot appear unnoticed.

It also holds the deployment shape to the same rule, because a rule the documented
deployment cannot satisfy is the defect ``#1079`` reported:

* the Compose API's non-loopback plain gateway origin must be stated verbatim in
  ``deploy/compose.env.example`` (removing that line is what made ``#1079`` real);
* the cluster app-plane renderer must pass the opt-in for its bridge origin, or the
  two-host assembly refuses to start for the same reason.

Residual gap, stated rather than implied: the derivation is the credential key, so a
future path that receives the credential through a settings object without naming
the key (a new wrapper module) is not enumerated here. The rule still applies to it;
add the call and, if it reads the credential some other way, extend the derivation.

Standard-library only (``ast``, ``re``, ``urllib.parse``) so the dependency-free
guards job runs it.
"""

from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend/src/speechnexus_backend"
RULE_MODULE = BACKEND / "speech/domain/gateway_origin.py"
RULE_FUNCTIONS = ("gateway_origin", "plaintext_origin_opt_in", "require_safe_gateway_origin")
CREDENTIAL_KEY = "SPEECHNEXUS_EXECUTION_GATEWAY_COMPONENT_CREDENTIAL"
COMPOSE = ROOT / "deploy/compose.yaml"
ENV_EXAMPLE = ROOT / "deploy/compose.env.example"
CLUSTER_RENDERER = ROOT / "deploy/cluster/app-plane/render-config.py"

LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
BASE_URL_FIELD = re.compile(r"SPEECHNEXUS_EXECUTION_GATEWAY_BASE_URL:\s*(\S+)")
ALLOW_FIELD = re.compile(r"SPEECHNEXUS_EXECUTION_GATEWAY_ALLOW_PLAINTEXT_ORIGIN:\s*(\S+)")


def tree_of(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def names_the_credential(tree: ast.Module) -> bool:
    return any(
        isinstance(node, ast.Constant) and node.value == CREDENTIAL_KEY for node in ast.walk(tree)
    )


def calls_the_rule(tree: ast.Module) -> bool:
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        if isinstance(function, ast.Name) and function.id == "require_safe_gateway_origin":
            return True
        if isinstance(function, ast.Attribute) and function.attr == "require_safe_gateway_origin":
            return True
    return False


def is_non_loopback_plain(origin: str) -> bool:
    parsed = urlsplit(origin)
    return parsed.scheme == "http" and parsed.hostname not in LOOPBACK_HOSTS


def module_constants(tree: ast.Module) -> dict[str, str]:
    return {
        target.id: node.value.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
        for target in node.targets
        if isinstance(target, ast.Name)
    }


def resolved_string(node: ast.expr, constants: dict[str, str]) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return constants.get(node.id)
    return None


class CredentialPathCoverageTest(unittest.TestCase):
    def test_every_credential_reader_applies_the_shared_rule(self) -> None:
        readers = {
            path: tree_of(path)
            for path in sorted(BACKEND.rglob("*.py"))
            if path != RULE_MODULE and names_the_credential(tree_of(path))
        }
        self.assertTrue(
            readers,
            "no product module reads the component credential; the derivation is broken, "
            "not the coverage",
        )
        uncovered = sorted(
            str(path.relative_to(ROOT))
            for path, tree in readers.items()
            if not calls_the_rule(tree)
        )
        self.assertEqual(
            uncovered,
            [],
            "these modules read the component credential but never call "
            "require_safe_gateway_origin: " + ", ".join(uncovered),
        )

    def test_the_origin_rule_has_one_definition_point(self) -> None:
        definitions: dict[str, list[str]] = {}
        for path in sorted(BACKEND.rglob("*.py")):
            for node in ast.walk(tree_of(path)):
                if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and (
                    node.name in RULE_FUNCTIONS
                ):
                    definitions.setdefault(node.name, []).append(
                        str(path.relative_to(ROOT)).replace("\\", "/")
                    )
        self.assertEqual(
            definitions,
            {
                name: ["backend/src/speechnexus_backend/speech/domain/gateway_origin.py"]
                for name in RULE_FUNCTIONS
            },
        )


class DeploymentShapeTest(unittest.TestCase):
    def test_the_compose_shape_states_its_non_loopback_gateway_origin(self) -> None:
        origins = [
            match.group(1)
            for match in BASE_URL_FIELD.finditer(COMPOSE.read_text(encoding="utf-8"))
            if is_non_loopback_plain(match.group(1))
        ]
        self.assertTrue(
            origins,
            "deploy/compose.yaml configures no non-loopback plain gateway origin; if the "
            "API moved to HTTPS or loopback this guard no longer covers #1079",
        )
        example = ENV_EXAMPLE.read_text(encoding="utf-8")
        allowances = {
            match.group(1) for match in ALLOW_FIELD.finditer(COMPOSE.read_text(encoding="utf-8"))
        }
        self.assertTrue(
            allowances,
            "deploy/compose.yaml no longer forwards the opt-in to its API",
        )
        for origin in origins:
            with self.subTest(origin=origin):
                self.assertIn(
                    f"\nSPEECHNEXUS_EXECUTION_GATEWAY_ALLOW_PLAINTEXT_ORIGIN={origin}\n",
                    example,
                    "deploy/compose.env.example must state the exact origin the API is "
                    "configured with, byte for byte",
                )

    def test_the_cluster_app_plane_states_its_bridge_origin(self) -> None:
        tree = tree_of(CLUSTER_RENDERER)
        constants = module_constants(tree)
        rendered = 0
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            entries = {
                key_value: resolved_string(value, constants)
                for key, value in zip(node.keys, node.values, strict=True)
                if (key_value := resolved_string(key, constants) if key is not None else None)
                is not None
            }
            origin = entries.get("SPEECHNEXUS_EXECUTION_GATEWAY_BASE_URL")
            if origin is None or not is_non_loopback_plain(origin):
                continue
            rendered += 1
            with self.subTest(origin=origin):
                self.assertEqual(
                    entries.get("SPEECHNEXUS_EXECUTION_GATEWAY_ALLOW_PLAINTEXT_ORIGIN"),
                    origin,
                    "the cluster app-plane renders a non-loopback plain gateway origin "
                    "without stating it in SPEECHNEXUS_EXECUTION_GATEWAY_ALLOW_PLAINTEXT_ORIGIN",
                )
        self.assertGreaterEqual(
            rendered,
            1,
            "the cluster app-plane renderer no longer configures a non-loopback plain "
            "gateway origin; re-check this guard against the two-host assembly",
        )


if __name__ == "__main__":
    unittest.main()
