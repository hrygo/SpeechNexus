"""Keep the Web build's network arguments optional and out of the shipped topology.

`deploy/images/web.Dockerfile` takes its two base images and an optional package
registry from build arguments whose defaults are the product values, so a build that
passes none of them produces the image this file always produced; the example override
`deploy/compose.build-mirror.override.yaml` passes them for a host whose registry route
is throttled (#572/#829 measured 13.27 KiB/s on Docker Hub and an expired 900 s window).

The lane that runs this file installs no third-party package, so the two Compose
files are read by indentation rather than with a YAML library. The properties below
are still the ones pinned: an added `environment:` block, a moved Dockerfile or a
pinned network argument all change the significant lines and fail.

Three properties make that safe, and none of them needs a second copy of the build:

* the defaults stay ordinary Docker Hub references — a default that moved to a mirror
  would turn the throttled path into the normal one;
* `deploy/compose.yaml` never pins the arguments, so the deployment builds with the
  product values;
* the example changes arguments only, and its file name is not one Compose auto-loads.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = "deploy/images/web.Dockerfile"
COMPOSE = "deploy/compose.yaml"
OVERRIDE = "deploy/compose.build-mirror.override.yaml"

#: Argument → the value the product build relies on. `NPM_REGISTRY` defaults to empty.
BASE_IMAGE_ARGS = {
    "NODE_BASE_IMAGE": "node:26.12.1-bookworm-slim",
    "NGINX_BASE_IMAGE": "nginxinc/nginx-unprivileged:1.31-alpine",
}
REGISTRY_ARG = "NPM_REGISTRY"
NETWORK_ARGS = (*BASE_IMAGE_ARGS, REGISTRY_ARG)

DEFAULT = re.compile(r"(?m)^\s*ARG\s+(\w+)=(\S*)\s*$")
FORBIDDEN = (
    "strict-ssl=false",
    "NODE_TLS_REJECT_UNAUTHORIZED",
    "--insecure",
    "PASSWORD=",
    "_TOKEN=",
    "API_KEY=",
)


def read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def defaults() -> dict[str, str]:
    return dict(DEFAULT.findall(read(DOCKERFILE)))


def document_lines(text: str) -> list[str]:
    """Significant lines with their indentation: what a Compose reader sees."""
    return [
        line.rstrip()
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def indented_block(lines: list[str], start: int, indent: int) -> list[str]:
    """The lines nested one level below `lines[start]`."""
    block: list[str] = []
    for line in lines[start + 1 :]:
        if len(line) - len(line.lstrip()) <= indent:
            break
        block.append(line)
    return block


#: A Compose key at a given indentation. YAML allows space before the colon
#: (`dockerfile : x`), so the reader must tolerate it or the same key written with a
#: space slips past every check below.
KEY = re.compile(r"^( *)([A-Za-z_][\w.-]*)[ \t]*:(.*)$")
#: A YAML alias/merge at the start of a value. Deliberately anchored: a quoted scalar
#: such as `"x *.internal"` is a literal value, not an alias.
VALUE_REFERENCE = re.compile(r"^(?:<<|[&*][A-Za-z_])")


def keys_at(lines: list[str], indent: int) -> list[tuple[str, str]]:
    """`(key, value)` pairs declared at exactly `indent`, in document order."""
    pairs: list[tuple[str, str]] = []
    for line in lines:
        match = KEY.match(line)
        if match and len(match.group(1)) == indent:
            pairs.append((match.group(2), match.group(3).strip()))
    return pairs


def _require_single(service: str, pairs: list[tuple[str, str]], key: str, where: str) -> int:
    """YAML keeps the last duplicate key; this reader cannot tell which one wins."""
    found = [index for index, (name, _) in enumerate(pairs) if name == key]
    if not found:
        raise AssertionError(f"{COMPOSE}: {where} has no {key!r}")
    if len(found) > 1:
        raise AssertionError(
            f"{COMPOSE}: {service} repeats `{key}` in {where}; the last one wins in YAML, "
            "so this reader cannot tell which one the deployment uses"
        )
    return found[0]


def _reject_indirection(service: str, pairs: list[tuple[str, str]], block: list[str]) -> None:
    """Refuse YAML this reader cannot follow instead of green-lighting it.

    `deploy/compose.yaml` itself uses anchors (`x-python-build`,
    `x-engine-registration-env`), so `args: *web-args` or a `<<:` merge can move the
    arguments into a mapping the indentation reader never inspects. PyYAML resolved
    those and saw the pinned argument; reading text must stay fail-closed rather than
    silently pass a build this guard cannot see.
    """
    # `<<` is a merge key and cannot be matched by KEY (it is not an identifier), so the
    # raw block is scanned as well; otherwise `<<: *web-build` moves the args unseen.
    for line in block:
        if line.lstrip().startswith("<<"):
            raise AssertionError(
                f"{COMPOSE}: {service}'s build uses a YAML alias, anchor or merge key "
                f"({line.strip()!r}) this reader cannot follow; inline the mapping so "
                "the guard can see the arguments"
            )
    for key, value in pairs:
        if VALUE_REFERENCE.match(value):
            raise AssertionError(
                f"{COMPOSE}: {service}'s build uses a YAML alias, anchor or merge key "
                f"({key}: {value!r}) this reader cannot follow; inline the mapping so "
                "the guard can see the arguments"
            )


def _reject_duplicate_keys(service: str, pairs: list[tuple[str, str]]) -> None:
    names = [name for name, _ in pairs]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise AssertionError(
            f"{COMPOSE}: {service}'s build repeats {duplicates}; the last value wins in "
            "YAML, so this reader cannot tell which one the deployment uses"
        )


def _line_index(lines: list[str], key: str, indent: int) -> int:
    for index, line in enumerate(lines):
        match = KEY.match(line)
        if match and match.group(2) == key and len(match.group(1)) == indent:
            return index
    raise AssertionError(f"{COMPOSE}: no {key!r} at indentation {indent}")


def service_build(service: str) -> list[str]:
    """The `services.<service>.build` keys, located by indentation, not by PyYAML."""
    lines = document_lines(read(COMPOSE))
    # One `services:` and one definition per service, or the reader cannot say which
    # mapping the deployment actually uses -- YAML silently keeps the last one.
    _require_single(service, keys_at(lines, 0), "services", "the document")
    services = indented_block(lines, _line_index(lines, "services", 0), 0)
    _require_single(service, keys_at(services, 2), service, "the services mapping")
    service_lines = indented_block(services, _line_index(services, service, 2), 2)
    build_keys = keys_at(service_lines, 4)
    build_at = _line_index(service_lines, "build", 4)
    _require_single(service, build_keys, "build", f"{service}'s definition")
    if next(value for key, value in build_keys if key == "build"):
        raise AssertionError(
            f"{COMPOSE}: {service}'s build is a YAML alias or an inline value this "
            "reader cannot follow; write the `build:` mapping out"
        )
    build_lines = [
        line for line in service_lines[build_at + 1 :] if len(line) - len(line.lstrip()) > 4
    ]
    pairs = keys_at(build_lines, 6)
    _reject_indirection(service, pairs, build_lines)
    _reject_duplicate_keys(service, pairs)
    return build_lines


def effective_lines(text: str, *, yaml_document: bool = False) -> list[str]:
    """Non-comment lines: the files name the relaxations they forbid, and that prose
    must not be what the guard matches."""
    kept = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if yaml_document:
            line = line.split(" #", 1)[0].rstrip()
            if not line:
                continue
        kept.append(line)
    return kept


class ArgumentsTests(unittest.TestCase):
    def test_every_network_input_has_the_product_default(self) -> None:
        declared = defaults()
        for name, value in BASE_IMAGE_ARGS.items():
            with self.subTest(argument=name):
                self.assertEqual(declared.get(name), value)
        self.assertEqual(declared.get(REGISTRY_ARG), "")

    def test_the_base_image_defaults_are_registry_neutral(self) -> None:
        # A default naming a mirror host would make every ordinary build depend on that
        # mirror. A Docker Hub reference is either `name[:tag]` or `org/name[:tag]`.
        for name, value in BASE_IMAGE_ARGS.items():
            with self.subTest(argument=name):
                segments = value.split(":", 1)[0].split("/")
                self.assertLessEqual(len(segments), 2, value)
                self.assertNotIn(".", segments[0], value)

    def test_the_deployment_does_not_pin_the_arguments(self) -> None:
        for service in ("admin", "workbench"):
            build = service_build(service)
            with self.subTest(service=service):
                # Exactly one key, so a second `dockerfile:` (with or without the YAML
                # space before the colon) cannot win silently.
                dockerfiles = [value for key, value in keys_at(build, 6) if key == "dockerfile"]
                self.assertEqual(
                    dockerfiles,
                    [DOCKERFILE],
                    f"{COMPOSE} must set exactly one exact `dockerfile:` in {service}'s build",
                )
                text = "\n".join(build)
                for name in NETWORK_ARGS:
                    self.assertNotIn(
                        name,
                        text,
                        f"{COMPOSE} mentions {name} in {service}'s build; the deployment "
                        "must build with the Dockerfile defaults",
                    )


class OverrideTests(unittest.TestCase):
    def test_the_override_changes_arguments_only(self) -> None:
        expected = ["services:"]
        for service in ("admin", "workbench"):
            expected += [f"  {service}:", "    build:", "      args:"]
            for name in NETWORK_ARGS:
                default = BASE_IMAGE_ARGS.get(name, "")
                expected.append(f"        {name}: ${{SPEECHNEXUS_{name}:-{default}}}")
        self.assertEqual(
            document_lines(read(OVERRIDE)),
            expected,
            "the override must change the three build arguments and nothing else; an "
            "added environment, volume or service would move a shipped boundary",
        )

    def test_the_override_is_not_autoloaded(self) -> None:
        autoloaded = {"compose.override.yaml", "docker-compose.override.yaml"}
        self.assertNotIn(Path(OVERRIDE).name, autoloaded)
        self.assertFalse((ROOT / "deploy" / "compose.override.yaml").exists())
        self.assertNotIn("build-mirror", read(COMPOSE))

    def test_nothing_relaxes_tls_or_carries_a_credential(self) -> None:
        for relative, is_yaml in ((OVERRIDE, True), (DOCKERFILE, False)):
            lines = effective_lines(read(relative), yaml_document=is_yaml)
            for token in FORBIDDEN:
                for line in lines:
                    with self.subTest(file=relative, token=token):
                        self.assertNotIn(token, line)


if __name__ == "__main__":
    unittest.main()
