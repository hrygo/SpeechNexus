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


def gateway_branches(tree: ast.Module, constants: dict[str, str]) -> tuple[str, str]:
    """The two origins ``gateway_origin`` can return, as the renderer states them."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name != "gateway_origin":
            continue
        for inner in ast.walk(node):
            if not isinstance(inner, ast.IfExp):
                continue
            taken = resolved_string(inner.body, constants)
            fallback = resolved_string(inner.orelse, constants)
            if taken is not None and fallback is not None:
                return taken, fallback
    raise AssertionError(
        "the cluster app-plane renderer no longer selects between two stated gateway "
        "origins; re-check this guard against the two-host assembly"
    )


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
        """The renderer states both origins once, and derives both keys from that rule.

        The gateway binds a loopback address on the compute host, so ``in-cluster``
        presents that loopback from a unit that joined the host network, while the
        two-host topologies still present the bridge Service. Both are legitimate only
        as long as the runtime keeps seeing one origin per topology, which is why this
        guard reads the branch expressions instead of one rendered dict: a second
        literal copy of either origin is a second definition point.
        """
        tree = tree_of(CLUSTER_RENDERER)
        constants = module_constants(tree)
        in_cluster, two_host = gateway_branches(tree, constants)
        text = CLUSTER_RENDERER.read_text(encoding="utf-8")

        self.assertTrue(
            is_non_loopback_plain(two_host),
            f"{two_host!r}: the tunnel and data-plane topologies must keep presenting the "
            "bridge Service over plain HTTP, or #1079 comes back",
        )
        self.assertIn(
            urlsplit(in_cluster).hostname,
            LOOPBACK_HOSTS,
            f"{in_cluster!r}: an in-cluster unit reaches the gateway through the host "
            "network, so its stated origin is host loopback",
        )
        for origin in (in_cluster, two_host):
            with self.subTest(origin=origin):
                self.assertEqual(
                    text.count(origin),
                    1,
                    "the gateway origin has a second definition point",
                )

        derived = 0
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            for key, value in zip(node.keys, node.values, strict=True):
                if key is None or value is None:
                    continue
                if resolved_string(key, constants) not in (
                    "SPEECHNEXUS_EXECUTION_GATEWAY_BASE_URL",
                    "SPEECHNEXUS_EXECUTION_GATEWAY_ALLOW_PLAINTEXT_ORIGIN",
                ):
                    continue
                derived += 1
                with self.subTest(key=resolved_string(key, constants)):
                    self.assertIsInstance(
                        value,
                        ast.Call,
                        "the gateway origin and its plaintext opt-in must both be "
                        "derived from gateway_origin(topology), never written out again",
                    )
        self.assertEqual(
            derived,
            6,
            "each credential-presenting unit must derive both gateway keys from "
            "gateway_origin(topology): the api secret, the dispatch secret and the "
            "cancellation pass (#1293), three units times two keys",
        )


if __name__ == "__main__":
    unittest.main()
