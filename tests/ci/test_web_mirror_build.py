"""Keep the restricted-network mirror build a build-only copy of the product build.

`deploy/images/web.mirror.Dockerfile` repeats `deploy/images/web.Dockerfile` with the
two base-image references and the package registry exposed as arguments, because a
host whose route to Docker Hub is throttled cannot build the product file at all
(#572/#829: `node:26.12.1-bookworm-slim` sat at 13.27 KiB/s and the Compose `web`
build expired its 900 s window). Two properties make that file safe, and both are
mechanical rather than promised in prose:

* it compiles the *same* instructions as the product file, so the default build path
  does not move and a host that does not need the mirror never touches it;
* it moves where bytes come from without weakening how they are trusted — no TLS
  relaxation, no credential in a shipped file.

The drift direction that matters is the boring one: someone extends the product
Dockerfile (a new `COPY`, a changed base image) and the mirror file quietly stops
being the same build. These cases fail that, and they also prove the comparison can
fail at all, so a green run means the two files were actually compared.
"""

from __future__ import annotations

import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
PRODUCT = "deploy/images/web.Dockerfile"
MIRROR = "deploy/images/web.mirror.Dockerfile"
OVERRIDE = "deploy/compose.build-mirror.override.yaml"
COMPOSE = "deploy/compose.yaml"
README = "deploy/README.md"

#: Base-image arguments the mirror file exposes, with the product instruction each one
#: replaces. Order is the order the `FROM` lines appear in.
BASE_IMAGE_ARGS = {
    "NODE_BASE_IMAGE": "node:26.12.1-bookworm-slim",
    "NGINX_BASE_IMAGE": "nginxinc/nginx-unprivileged:1.31-alpine",
}
PACKAGE_REGISTRY_ARG = "NPM_REGISTRY"

#: The one instruction with no product counterpart: it points npm/pnpm at another
#: registry. Matched by this marker so the comparison can drop exactly it.
REGISTRY_MARKER = "npm config set registry"

#: Tokens that would make the mirror build trust something the product build does not.
FORBIDDEN_TRUST = (
    "strict-ssl=false",
    "strict_ssl=false",
    "NODE_TLS_REJECT_UNAUTHORIZED",
    "GIT_SSL_NO_VERIFY",
    "--insecure",
    "--trusted-host",
    "verify_ssl=false",
    "sslverify=false",
    "PYTHONHTTPSVERIFY=0",
)

#: Tokens that would put a credential into a file that ships in the repository.
FORBIDDEN_CREDENTIAL = (
    "PASSWORD=",
    "_TOKEN=",
    "API_KEY=",
    "BASIC_AUTH",
    "http://user:",
    "://user:pass",
)


def read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def instructions(text: str) -> tuple[str, ...]:
    """Logical Dockerfile instructions, in order.

    Comment and blank lines are dropped and `\\` continuations are folded, so the
    comparison is about what the builder executes rather than how it is laid out.
    """
    result: list[str] = []
    pending = ""
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("#") or (not line and not pending):
            continue
        if line.startswith("#"):
            continue
        pending = f"{pending} {line}".strip() if pending else line
        if pending.endswith("\\"):
            pending = pending[:-1].rstrip()
            continue
        result.append(" ".join(pending.split()))
        pending = ""
    if pending:
        result.append(" ".join(pending.split()))
    return tuple(result)


def base_image_defaults(text: str) -> dict[str, str]:
    """Global-scope `ARG NAME=default` lines of the mirror file."""
    defaults: dict[str, str] = {}
    for instruction in instructions(text):
        if not instruction.startswith("ARG "):
            continue
        name, _, default = instruction.removeprefix("ARG ").partition("=")
        if name in BASE_IMAGE_ARGS or name == PACKAGE_REGISTRY_ARG:
            defaults[name] = default
    return defaults


def network_argument_names() -> set[str]:
    """The only `ARG` lines the mirror file may add to the product build."""
    return {*BASE_IMAGE_ARGS, PACKAGE_REGISTRY_ARG}


def effective_lines(text: str, *, yaml_document: bool = False) -> list[str]:
    """Lines a scanner may look at: comments carry prose, not build behaviour.

    The shipped files explain which relaxations are forbidden, and that prose must not
    be what the guard matches.
    """
    kept: list[str] = []
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if yaml_document:
            stripped = stripped.split(" #", 1)[0].rstrip()
            if not stripped:
                continue
        kept.append(stripped)
    return kept


def product_base_images(text: str) -> tuple[str, ...]:
    return tuple(
        instruction.removeprefix("FROM ").split(" AS ")[0]
        for instruction in instructions(text)
        if instruction.startswith("FROM ")
    )


def mismatch(product: str, mirror: str) -> str | None:
    """Return why the two definitions differ, or `None` when they agree."""
    defaults = base_image_defaults(mirror)
    missing = sorted(set(BASE_IMAGE_ARGS) - set(defaults))
    if missing:
        return f"{MIRROR} no longer declares {', '.join(missing)}"
    images = product_base_images(product)
    if len(images) != 2:
        return f"{PRODUCT} should have two stages, found {len(images)}"
    if [defaults[name] for name in BASE_IMAGE_ARGS] != list(images):
        return (
            f"{MIRROR} defaults {[defaults[name] for name in BASE_IMAGE_ARGS]} do not "
            f"match the product base images {list(images)}"
        )

    drop = network_argument_names()

    def is_network_argument(instruction: str) -> bool:
        if not instruction.startswith("ARG "):
            return False
        return instruction.removeprefix("ARG ").split("=", 1)[0] in drop

    expected: list[str] = []
    for instruction in instructions(mirror):
        if is_network_argument(instruction):
            continue
        if REGISTRY_MARKER in instruction:
            continue
        for name, value in defaults.items():
            instruction = instruction.replace(f"${{{name}}}", value)
        expected.append(instruction)
    actual = [
        instruction for instruction in instructions(product) if not is_network_argument(instruction)
    ]
    if expected == actual:
        return None
    for index, (left, right) in enumerate(zip(actual, expected, strict=False)):
        if left != right:
            return (
                f"{MIRROR} instruction {index} differs from {PRODUCT}:\n"
                f"  product: {left}\n  mirror:  {right}"
            )
    return (
        f"{MIRROR} has {len(expected)} instructions against {len(actual)} in "
        f"{PRODUCT}; the extra or missing one is at the tail"
    )


class ProductParityTests(unittest.TestCase):
    """The mirror file must stay the product build plus three network arguments."""

    def test_the_mirror_file_compiles_the_product_instructions(self) -> None:
        self.assertIsNone(mismatch(read(PRODUCT), read(MIRROR)))

    def test_the_mirror_file_changes_nothing_by_default(self) -> None:
        defaults = base_image_defaults(read(MIRROR))
        self.assertEqual(defaults[PACKAGE_REGISTRY_ARG], "")
        self.assertEqual(
            [defaults[name] for name in BASE_IMAGE_ARGS], list(product_base_images(read(PRODUCT)))
        )

    def test_a_changed_base_image_in_the_mirror_file_is_rejected(self) -> None:
        mirror = read(MIRROR).replace(
            f"ARG NODE_BASE_IMAGE={BASE_IMAGE_ARGS['NODE_BASE_IMAGE']}",
            "ARG NODE_BASE_IMAGE=node:22-bookworm-slim",
        )
        error = mismatch(read(PRODUCT), mirror)
        self.assertIsNotNone(error)
        self.assertIn("do not match the product base images", error or "")

    def test_a_new_product_instruction_the_mirror_file_lacks_is_rejected(self) -> None:
        product = read(PRODUCT).replace(
            "RUN pnpm --filter", "COPY pnpm-workspace.yaml .npmrc ./\nRUN pnpm --filter", 1
        )
        error = mismatch(product, read(MIRROR))
        self.assertIsNotNone(error)
        self.assertIn(f"differs from {PRODUCT}", error or "")

    def test_dropping_the_registry_redirect_still_compares_the_build(self) -> None:
        # The marker line is the only instruction the comparison is allowed to drop.
        # Renaming it would silently exempt whatever replaces it, so the redirect has
        # to keep its shape.
        self.assertIn(REGISTRY_MARKER, read(MIRROR))
        self.assertIn(f'if [ -n "${{{PACKAGE_REGISTRY_ARG}}}" ]', read(MIRROR))


class TrustBoundaryTests(unittest.TestCase):
    """Moving bytes to a mirror must not move the trust decision."""

    def test_the_shipped_files_relax_no_tls_verification(self) -> None:
        # Comment lines are skipped on purpose: both files *name* these relaxations in
        # order to forbid them, and a guard that matched its own prohibition would force
        # the explanation out of the file.
        for relative, is_yaml in ((MIRROR, False), (OVERRIDE, True)):
            lines = effective_lines(read(relative), yaml_document=is_yaml)
            self.assertTrue(lines, relative)
            for token in FORBIDDEN_TRUST:
                for line in lines:
                    with self.subTest(file=relative, token=token):
                        self.assertNotIn(token, line)

    def test_the_shipped_files_carry_no_credential(self) -> None:
        for relative, is_yaml in ((MIRROR, False), (OVERRIDE, True)):
            lines = effective_lines(read(relative), yaml_document=is_yaml)
            for token in FORBIDDEN_CREDENTIAL:
                for line in lines:
                    with self.subTest(file=relative, token=token):
                        self.assertNotIn(token, line)

    def test_the_guard_can_see_a_relaxation_when_one_is_added(self) -> None:
        # The scan above only means something if a real relaxation would trip it.
        relaxed = read(MIRROR).replace(
            "npm install --global", "npm install --strict-ssl=false --global", 1
        )
        self.assertIn("strict-ssl=false", "\n".join(effective_lines(relaxed)))

    def test_the_mirror_file_keeps_the_proxy_ca_secret(self) -> None:
        # A deployment or session CA still arrives as a BuildKit secret; the mirror
        # path must not grow its own certificate handling.
        self.assertEqual(read(MIRROR).count("--mount=type=secret,id=proxy_ca"), 2)
        self.assertEqual(read(MIRROR).count("NODE_EXTRA_CA_CERTS"), 2)


class DefaultPathTests(unittest.TestCase):
    """Nothing loads the mirror build unless a caller names it."""

    def test_the_compose_file_never_names_the_mirror_build(self) -> None:
        compose = read(COMPOSE)
        self.assertNotIn("web.mirror", compose)
        self.assertNotIn("build-mirror", compose)

    def test_the_override_is_not_in_a_compose_autoload_name(self) -> None:
        # Compose auto-loads `compose.override.yaml` from the project directory. The
        # example must not acquire that name, or a checkout would silently build with
        # the mirror file.
        autoloaded = {"compose.override.yaml", "docker-compose.override.yaml"}
        self.assertNotIn(Path(OVERRIDE).name, autoloaded)
        self.assertFalse((ROOT / "deploy" / "compose.override.yaml").exists())

    def test_the_override_only_redirects_the_web_builds(self) -> None:
        document = yaml.safe_load(read(OVERRIDE))
        services = document["services"]
        self.assertEqual(sorted(services), ["admin", "workbench"])
        for name, service in services.items():
            with self.subTest(service=name):
                self.assertEqual(list(service), ["build"])
                build = service["build"]
                self.assertEqual(build["dockerfile"], MIRROR)
                self.assertEqual(
                    sorted(build["args"]),
                    sorted(["APP_NAME", *BASE_IMAGE_ARGS, PACKAGE_REGISTRY_ARG]),
                )
                self.assertEqual(build["args"]["APP_NAME"], name)

    def test_the_override_mirrors_only_services_the_compose_file_builds(self) -> None:
        compose = yaml.safe_load(read(COMPOSE))
        for name in yaml.safe_load(read(OVERRIDE))["services"]:
            with self.subTest(service=name):
                self.assertEqual(compose["services"][name]["build"]["dockerfile"], PRODUCT)

    def test_the_override_defaults_are_the_product_arguments(self) -> None:
        document = yaml.safe_load(read(OVERRIDE))
        for name, default in BASE_IMAGE_ARGS.items():
            with self.subTest(argument=name):
                rendered = document["services"]["workbench"]["build"]["args"][name]
                # `${SPEECHNEXUS_...:-<product value>}` — the fallback is the product
                # value, so an unset variable still resolves to the default build.
                self.assertTrue(rendered.startswith("${"), rendered)
                self.assertTrue(rendered.endswith("}"), rendered)
                self.assertTrue(rendered.endswith(f":-{default}}}"), rendered)


class DocumentationTests(unittest.TestCase):
    """The gap this closes was a document claim, so the document must carry it."""

    def test_the_readme_states_the_criteria_and_the_commands(self) -> None:
        readme = read(README)
        for fact in (
            "deploy/images/web.mirror.Dockerfile",
            "deploy/compose.build-mirror.override.yaml",
            "SPEECHNEXUS_NODE_BASE_IMAGE",
            "SPEECHNEXUS_NPM_REGISTRY",
        ):
            with self.subTest(fact=fact):
                self.assertIn(fact, readme)


if __name__ == "__main__":
    unittest.main()
