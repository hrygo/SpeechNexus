"""Guard: browser-facing Host/Origin settings cannot fall out of the deployment unit.

Issue #851: the reference deployment refused browser uploads with
``403 asset_origin_rejected`` because ``Settings.asset_allowed_origins`` defaulted
to an empty tuple and no deployment input set it. An empty origin list is an empty
allow-list, not a same-origin default, so every request that carries an ``Origin``
-- which a browser always sends on ``POST`` -- was refused while the stack still
started cleanly. Only the first upload showed the defect.

This guard derives the key names from the settings models instead of listing them
by hand. ``tests/ci/service_environment_keys.py`` already reads every registered
settings surface with :mod:`ast` (falling back to a text reader when the system
interpreter cannot parse the source), so every field whose name ends in ``_hosts``
or ``_origins`` is a candidate boundary input: a new one turns this guard red until
the deployment covers it.

A key counts as covered when any of these holds, because each is a real deployment
or operator path:

1. ``deploy/compose.yaml`` forwards it to the service that runs the check;
2. ``deploy/compose.env.example`` has an entry for it, active or as a commented
   example;
3. ``deploy/README.md`` names it in the "Host and Origin inputs the deployment must
   cover" list, which is the escape hatch for a key that no single deployment
   default can supply.

The negative cases are part of the suite, not a one-off demonstration: removing a
key from all three sources must be red, and the README list, the compose
forwarding or the example entry alone must each keep it green. A guard nobody has
watched fail proves nothing.
"""

from __future__ import annotations

import re
import unittest
from collections.abc import Iterator
from unittest import mock

from tests.ci.service_environment_keys import ROOT, SURFACES, derived_keys

COMPOSE = "deploy/compose.yaml"
ENV_EXAMPLE = "deploy/compose.env.example"
README = "deploy/README.md"

#: The list in ``deploy/README.md`` that may cover a key no default can supply.
LIST_HEADING = "### Host and Origin inputs the deployment must cover"

#: The service whose container environment must forward the boundary inputs.
OWNING_SERVICE = "api"

#: Field-name suffixes that make a setting a browser-facing boundary input.
BROWSER_SUFFIXES = ("_hosts", "_origins")

TOKEN = re.compile(r"SPEECHNEXUS_[A-Z0-9_]+")
ENTRY = re.compile(r"^\s*#?\s*(SPEECHNEXUS_[A-Z0-9_]+)\s*=")
SERVICE = re.compile(r"^  ([A-Za-z0-9_.-]+):\s*$")
BACKTICKED = re.compile(r"`(SPEECHNEXUS_[A-Z0-9_]+)`")


def browser_boundary_keys() -> dict[str, str]:
    """Return ``{environment key: owning surface id}`` for every boundary setting.

    Derived from the registered settings surfaces, so a new ``*_hosts`` or
    ``*_origins`` field is a red guard until the deployment covers it. An empty key
    set would satisfy every check below, so it is an explicit failure.
    """
    keys: dict[str, str] = {}
    for surface in SURFACES:
        for field, key in derived_keys(surface).items():
            if field.endswith(BROWSER_SUFFIXES):
                keys[key] = surface.id
    if not keys:
        raise AssertionError(
            "no *_hosts/*_origins setting was derived from the settings models; the "
            "sources could not be read, so this guard would pass vacuously"
        )
    return keys


def read_sources() -> dict[str, str]:
    """Return the three deployment sources this guard judges."""
    return {
        COMPOSE: (ROOT / COMPOSE).read_text(encoding="utf-8"),
        ENV_EXAMPLE: (ROOT / ENV_EXAMPLE).read_text(encoding="utf-8"),
        README: (ROOT / README).read_text(encoding="utf-8"),
    }


def _body_lines(lines: list[str], start: int) -> Iterator[str]:
    for line in lines[start:]:
        if SERVICE.match(line) is not None or (line and not line[0].isspace()):
            return
        yield line


def service_block(text: str, service: str) -> str:
    """Return the body of one ``services:`` entry, delimited by indentation.

    A bare token search would accept a key forwarded to a different container, so
    the compose half of this guard only counts names inside the owning service.
    """
    lines = text.splitlines()
    for index, line in enumerate(lines):
        match = SERVICE.match(line)
        if match is not None and match.group(1) == service:
            return "\n".join(_body_lines(lines, index + 1))
    raise AssertionError(f"{COMPOSE} declares no {service!r} service")


def compose_forwarded_keys(text: str, service: str = OWNING_SERVICE) -> frozenset[str]:
    """Return the keys ``deploy/compose.yaml`` passes to ``service``."""
    return frozenset(TOKEN.findall(service_block(text, service)))


def env_example_keys(text: str) -> frozenset[str]:
    """Return the keys with an entry in the example environment file.

    A commented example counts: it is the operator-facing entry for a key whose
    value the deployment supplies elsewhere. A prose mention, which has no ``=``,
    does not count.
    """
    keys: set[str] = set()
    for line in text.splitlines():
        match = ENTRY.match(line)
        if match is not None:
            keys.add(match.group(1))
    return frozenset(keys)


def readme_listed_keys(text: str) -> frozenset[str]:
    """Return the keys named in the README must-cover list.

    The list is section-scoped on purpose: a mention anywhere in the document would
    let a stale sentence satisfy the guard.
    """
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.strip() != LIST_HEADING:
            continue
        body: list[str] = []
        for following in lines[index + 1 :]:
            if following.startswith("#"):
                break
            body.append(following)
        return frozenset(BACKTICKED.findall("\n".join(body)))
    raise AssertionError(
        f"{README} has no {LIST_HEADING!r} section; the must-cover list cannot be "
        "scanned, so a list-only key would be reported as missing without saying why"
    )


def uncovered_keys(texts: dict[str, str], keys: dict[str, str]) -> dict[str, str]:
    """Return ``{key: owning surface}`` for every boundary key no source covers."""
    forwarded = compose_forwarded_keys(texts[COMPOSE])
    documented = env_example_keys(texts[ENV_EXAMPLE])
    listed = readme_listed_keys(texts[README])
    return {
        key: owner
        for key, owner in sorted(keys.items())
        if key not in forwarded and key not in documented and key not in listed
    }


def _without(text: str, token: str) -> str:
    """Return ``text`` with every line that mentions ``token`` removed."""
    return "\n".join(line for line in text.splitlines() if token not in line) + "\n"


class BoundaryKeyDiscoveryTest(unittest.TestCase):
    """The guarded key set comes from the settings sources and is not empty."""

    def test_the_known_asset_boundary_is_discovered(self) -> None:
        keys = browser_boundary_keys()
        self.assertIn("SPEECHNEXUS_ASSET_ALLOWED_ORIGINS", keys)
        self.assertIn("SPEECHNEXUS_ASSET_ALLOWED_HOSTS", keys)
        self.assertEqual(keys["SPEECHNEXUS_ASSET_ALLOWED_ORIGINS"], "backend")

    def test_the_workbench_boundary_is_discovered(self) -> None:
        keys = browser_boundary_keys()
        self.assertIn("SPEECHNEXUS_WORKBENCH_ALLOWED_ORIGINS", keys)
        self.assertIn("SPEECHNEXUS_WORKBENCH_ALLOWED_HOSTS", keys)

    def test_every_settings_surface_yields_its_fields(self) -> None:
        """A surface whose key derivation broke must not silently shrink the guard."""
        for surface in SURFACES:
            with self.subTest(surface=surface.id):
                self.assertTrue(derived_keys(surface), surface.id)


class DeploymentCoverageTest(unittest.TestCase):
    """Every boundary setting is covered by the deployment unit or the README list."""

    def test_every_browser_boundary_key_is_covered(self) -> None:
        missing = uncovered_keys(read_sources(), browser_boundary_keys())
        self.assertEqual(
            missing,
            {},
            "these Host/Origin settings are in no deployment source: the guarded routes "
            "refuse the browser while the stack still starts cleanly. Forward each one "
            "from deploy/compose.yaml, document it in deploy/compose.env.example, or "
            "name it in the deploy/README.md must-cover list",
        )

    def test_the_readme_list_names_the_asset_boundary(self) -> None:
        listed = readme_listed_keys(read_sources()[README])
        self.assertIn("SPEECHNEXUS_ASSET_ALLOWED_ORIGINS", listed)
        self.assertIn("SPEECHNEXUS_ASSET_ALLOWED_HOSTS", listed)


class RedAndGreenTest(unittest.TestCase):
    """The guard fails on the reported defect and still answers to the escape hatch."""

    KEY = "SPEECHNEXUS_ASSET_ALLOWED_ORIGINS"

    def setUp(self) -> None:
        self.keys = browser_boundary_keys()
        self.texts = read_sources()

    def _doctored(self, *, compose: bool, env: bool, readme: bool) -> dict[str, str]:
        """Return the real sources with the key kept in exactly the named places."""

        def keep(name: str, kept: bool) -> str:
            return self.texts[name] if kept else _without(self.texts[name], self.KEY)

        return {
            COMPOSE: keep(COMPOSE, compose),
            ENV_EXAMPLE: keep(ENV_EXAMPLE, env),
            README: keep(README, readme),
        }

    def test_removing_a_key_from_every_source_is_red(self) -> None:
        """The #851 shape: no deployment input and no README entry."""
        for key in self.keys:
            with self.subTest(key=key):
                doctored = {name: _without(text, key) for name, text in self.texts.items()}
                missing = uncovered_keys(doctored, self.keys)
                self.assertIn(key, missing)
                self.assertEqual(missing[key], self.keys[key])

    def test_naming_the_key_in_the_readme_list_alone_is_green(self) -> None:
        covered = self._doctored(compose=False, env=False, readme=True)
        self.assertNotIn(self.KEY, uncovered_keys(covered, self.keys))

    def test_forwarding_the_key_in_compose_alone_is_green(self) -> None:
        covered = self._doctored(compose=True, env=False, readme=False)
        self.assertNotIn(self.KEY, uncovered_keys(covered, self.keys))

    def test_documenting_the_key_in_the_example_env_alone_is_green(self) -> None:
        covered = self._doctored(compose=False, env=True, readme=False)
        self.assertNotIn(self.KEY, uncovered_keys(covered, self.keys))

    def test_forwarding_to_another_service_does_not_cover_the_key(self) -> None:
        """A key in the wrong container starts clean and still refuses the browser."""
        self.assertIn(self.KEY, service_block(self.texts[COMPOSE], OWNING_SERVICE))
        doctored = self._doctored(compose=False, env=False, readme=False)
        wrong_service = f"      {self.KEY}: '[\"http://127.0.0.1:8080\"]'\n"
        doctored[COMPOSE] += f"\n  gateway:\n    environment:\n{wrong_service}"
        self.assertNotIn(self.KEY, compose_forwarded_keys(doctored[COMPOSE]))
        self.assertIn(self.KEY, uncovered_keys(doctored, self.keys))

    def test_a_prose_mention_in_the_example_env_is_not_an_entry(self) -> None:
        """A key named in a sentence must not satisfy the example-file check."""
        doctored = self._doctored(compose=False, env=False, readme=False)
        doctored[ENV_EXAMPLE] += f"# the {self.KEY} value belongs to the operator\n"
        self.assertNotIn(self.KEY, env_example_keys(doctored[ENV_EXAMPLE]))
        self.assertIn(self.KEY, uncovered_keys(doctored, self.keys))

    def test_a_missing_readme_list_fails_loudly(self) -> None:
        """Deleting the list must be a red naming the section, not a silent pass."""
        doctored = dict(self.texts)
        doctored[README] = doctored[README].replace(LIST_HEADING, "### Something else")
        with self.assertRaises(AssertionError) as error:
            readme_listed_keys(doctored[README])
        self.assertIn(LIST_HEADING, str(error.exception))

    def test_an_unreadable_settings_source_fails_loudly(self) -> None:
        """An empty derived key set would satisfy every check in this module."""
        with (
            mock.patch(f"{__name__}.derived_keys", lambda surface: {}),
            self.assertRaises(AssertionError) as error,
        ):
            browser_boundary_keys()
        self.assertIn("vacuously", str(error.exception))


if __name__ == "__main__":
    unittest.main()
