"""Keep the runbook's build commands executable on a host without GitHub egress.

`#900`: `DEL-536` ran the runbook from empty volumes on a network-restricted host.
`up -d --build postgres minio` (§4) and `up -d --build api gateway` (§7) both failed at
`target minio: failed to solve`, because `api`/`gateway` pull `minio` in through
`depends_on` and `deploy/images/minio.Dockerfile` fetches a LICENSE from
`raw.githubusercontent.com` while building. The only sequence that ran was a one-time
`build` of the explicitly named product images, followed by `up -d` without `--build`.

Two properties keep that sequence true, and both are mechanical:

* every `docker compose ... build ...` in the runbook names its services and none of
  them is `minio` — a bare `build` or a list that reaches `minio` reintroduces the
  failure this card removes;
* no `up` in the runbook carries `--build` — image building happens once, in §0.

The role-count sentences the same card reports are pinned here as well, because a
count that drifts back to the old numbers sends the reader to the wrong number of
passwords.
"""

from __future__ import annotations

import re
import shlex
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RUNBOOK = ROOT / "docs" / "operations" / "compose-minimal.md"
ENV_EXAMPLE = ROOT / "deploy" / "compose.env.example"

FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
COMPOSE_INVOCATION = re.compile(r'^(?:docker\s+compose|"\$\{COMPOSE\[@\]\}")')
VALUE_OPTIONS = frozenset(
    {
        "--env-file",
        "-f",
        "--file",
        "--profile",
        "-p",
        "--project-name",
        "--project-directory",
        "--ansi",
        "--progress",
    }
)

#: §0's one-time build. `minio` is deliberately absent: it comes from an image import.
EXPECTED_BUILD_SERVICES = (
    "api",
    "gateway",
    "migrate",
    "engine-register",
    "job-dispatch-worker",
    "minio-init",
    "asset-inspector",
)


def command_lines() -> list[tuple[int, str]]:
    """Every compose command in the runbook as ``(line number, single line)``.

    Continuations joined at the trailing backslash; fenced blocks are the only place a
    command is offered for copy-paste, so prose that mentions `--build` is not a
    command.
    """
    lines = RUNBOOK.read_text(encoding="utf-8").splitlines()
    commands: list[tuple[int, str]] = []
    in_fence = False
    buffer: list[str] = []
    start = 0
    for number, line in enumerate(lines, start=1):
        if FENCE.match(line):
            in_fence = not in_fence
            continue
        if not in_fence:
            continue
        stripped = line.strip()
        if not buffer:
            if not COMPOSE_INVOCATION.match(stripped):
                continue
            start = number
        buffer.append(stripped)
        if not stripped.endswith("\\"):
            commands.append((start, " ".join(buffer)))
            buffer = []
    if buffer:
        commands.append((start, " ".join(buffer)))
    return commands


def invocations() -> list[tuple[int, list[str]]]:
    """Split each command into ``(line number, tokens)`` with the compose binary removed."""
    parsed: list[tuple[int, list[str]]] = []
    for number, command in command_lines():
        normalized = command.replace("\\", " ")
        normalized = normalized.replace('"${COMPOSE[@]}"', "docker compose")
        tokens = shlex.split(normalized)
        if tokens[:2] != ["docker", "compose"]:
            raise AssertionError(f"runbook:{number} is not a docker compose command: {command}")
        parsed.append((number, tokens[2:]))
    return parsed


def subcommand(tokens: list[str]) -> tuple[str, list[str]]:
    """Return the compose subcommand and the tokens after it."""
    index = 0
    while index < len(tokens) and tokens[index].startswith("-"):
        index += 2 if tokens[index] in VALUE_OPTIONS else 1
    if index >= len(tokens):
        raise AssertionError(f"a compose command carries no subcommand: {tokens}")
    return tokens[index], tokens[index + 1 :]


def build_services(tokens: list[str]) -> list[str]:
    """Service names a ``build`` invocation names, ignoring build-only flags."""
    return [token for token in tokens if not token.startswith("-")]


class BuildSequenceTest(unittest.TestCase):
    """The runbook builds once, by name, and never reaches the MinIO Dockerfile."""

    def test_every_documented_build_names_its_services_and_skips_minio(self) -> None:
        builds = []
        for number, tokens in invocations():
            name, rest = subcommand(tokens)
            if name == "build":
                builds.append((number, build_services(rest)))
        self.assertTrue(builds, "the runbook documents no build command at all")
        for number, services in builds:
            with self.subTest(line=number):
                self.assertTrue(
                    services,
                    f"runbook:{number} runs a bare `build`, which builds every service "
                    "including minio",
                )
                self.assertNotIn(
                    "minio",
                    services,
                    f"runbook:{number} builds minio; on a host without "
                    "raw.githubusercontent.com that target fails and takes the command down",
                )

    def test_the_one_time_build_lists_every_product_image_exactly_once(self) -> None:
        builds = []
        for _, tokens in invocations():
            name, rest = subcommand(tokens)
            if name == "build":
                builds.append(build_services(rest))
        expected = list(EXPECTED_BUILD_SERVICES)
        self.assertIn(
            expected,
            builds,
            "the runbook no longer offers §0's one-time build of the explicit service list",
        )
        self.assertEqual(
            builds.count(expected),
            1,
            "the one-time build list is documented more than once; keep one source of truth",
        )

    def test_no_documented_up_carries_build(self) -> None:
        for number, tokens in invocations():
            name, rest = subcommand(tokens)
            if name != "up":
                continue
            with self.subTest(line=number):
                self.assertNotIn(
                    "--build",
                    rest,
                    f"runbook:{number} puts --build on an `up`, which builds minio through "
                    "api/gateway dependencies",
                )

    def test_build_never_appears_inside_a_code_block(self) -> None:
        for number, command in command_lines():
            self.assertNotIn(
                "--build",
                command,
                f"runbook:{number} offers a `--build` command; §0 builds once with `build`",
            )


class RoleCountTest(unittest.TestCase):
    """The prose counts match the passwords the environment example declares."""

    def setUp(self) -> None:
        self.text = RUNBOOK.read_text(encoding="utf-8")

    def test_the_stale_count_is_gone(self) -> None:
        self.assertNotIn("十六个非基线角色口令", self.text)
        self.assertIn("十七个非基线角色口令", self.text)

    def test_the_total_is_named_once(self) -> None:
        self.assertIn("二十三个 `*_ROLE_PASSWORD`", self.text)

    def test_the_environment_example_holds_the_promised_passwords(self) -> None:
        keys = re.findall(r"(?m)^SPEECHNEXUS_[A-Z0-9_]*_ROLE_PASSWORD=", ENV_EXAMPLE.read_text())
        self.assertEqual(
            len(keys), 23, f"deploy/compose.env.example declares {len(keys)} role passwords"
        )

    def test_the_old_bucket_sync_sentence_is_gone(self) -> None:
        collapsed = " ".join(self.text.split())
        self.assertNotIn("两份 私有 JSON 的桶名必须同步", collapsed)
        self.assertNotIn("两份私有 JSON 的桶名必须同步", collapsed)


if __name__ == "__main__":
    unittest.main()
