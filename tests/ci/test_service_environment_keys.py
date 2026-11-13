"""Meta-guard: the prefixed-environment registry is derived, complete and honest.

Issue #674 turns a service-prefixed variable that matches no declared setting
into a named startup refusal (the typo case) or a named warning (the
"belongs to another process" case). Two failure modes would make that rule worse
than the silence it replaces:

1. **Refusing a real deployment variable.** A name the repository itself puts
   into a container, or the test harness sets, must be a declared setting or a
   registered non-setting plane. Otherwise the refusal breaks a working
   deployment -- the exact regression the previous card (#660) shipped twice.
2. **A stale registry.** ``tests/ci/service_environment_keys.py`` derives every
   settings class and every service prefix from the sources, so it cannot rot
   silently: a new settings class, a new ``env_prefix``, a new entrypoint key, a
   new deployment variable or a divergent copy of the scan engine turns this
   guard red until the decision is recorded.

The checks are text-only (standard library, no service import, no container, no
database), which is what lets the ``changes`` job run them before dependencies
are installed.
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

from tests.ci.service_environment_keys import (
    BACKEND_ENVIRONMENT,
    CONTROLLER_ENVIRONMENT,
    GATEWAY_ENVIRONMENT,
    PACKAGE_ROOTS,
    ROOT,
    RUNTIME_REGISTRIES,
    SERVICE_PREFIXES,
    SHARED_DEFINITIONS,
    SURFACES,
    WORKBENCH_REGISTRY,
    all_planes,
    declared_prefix,
    derived_keys,
    discovered_prefixes,
    discovered_settings_classes,
    inventory_tokens,
    registered_planes,
    surface_keys,
)


def _shared_definition_sources(module: str) -> dict[str, str]:
    """Return ``{name: source}`` for the engine definitions that must stay identical."""
    tree = ast.parse((ROOT / module).read_text(encoding="utf-8"))
    sources: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name in SHARED_DEFINITIONS:
            sources[node.name] = ast.unparse(node)
        if isinstance(node, ast.FunctionDef) and node.name in SHARED_DEFINITIONS:
            sources[node.name] = ast.unparse(node)
    return sources


class SurfaceDiscoveryTest(unittest.TestCase):
    """A settings class or a service prefix cannot appear without being registered."""

    def test_every_settings_surface_under_the_package_roots_is_registered(self) -> None:
        discovered = discovered_settings_classes()
        registered = {(surface.module, surface.cls) for surface in SURFACES}
        self.assertEqual(discovered - registered, frozenset(), "unregistered settings surface")
        self.assertEqual(registered - discovered, set(), "registered surface no longer exists")
        self.assertTrue(discovered, "discovery found no settings surface at all")

    def test_every_declared_prefix_is_registered(self) -> None:
        discovered = discovered_prefixes()
        registered = {entry.prefix for entry in SERVICE_PREFIXES}
        unregistered = {
            prefix
            for prefix in discovered
            if prefix not in registered
            and not any(prefix.startswith(known) for known in registered)
        }
        self.assertEqual(unregistered, set(), "unregistered service prefix")
        for entry in SERVICE_PREFIXES:
            self.assertIn(entry.prefix, discovered, f"{entry.service} prefix is not declared")
            self.assertEqual(entry.prefix, declared_prefix(entry.service))

    def test_each_prefix_belongs_to_exactly_one_service(self) -> None:
        prefixes = [entry.prefix for entry in SERVICE_PREFIXES]
        self.assertEqual(len(prefixes), len(set(prefixes)))
        # The backend scans its own namespace, which contains the other services'.
        backend = next(entry for entry in SERVICE_PREFIXES if entry.service == "backend")
        for entry in SERVICE_PREFIXES:
            if entry.prefix != backend.prefix and entry.prefix.startswith(backend.prefix):
                self.assertIn(entry.service, backend.scans)


class RefusalSafetyTest(unittest.TestCase):
    """Nothing a real deployment sets may be refused, and no plan may shadow a field."""

    def test_the_backend_surface_recognises_every_other_surfaces_key(self) -> None:
        """The API scans ``SPEECHNEXUS_``, so every nested key needs an owner."""
        backend = next(entry for entry in SERVICE_PREFIXES if entry.service == "backend")
        # Only the surface that performs the scan knows these names; the derived
        # key sets of the *other* backend entrypoints are what a plane must cover.
        own = surface_keys()["backend"]
        planes = registered_planes(*backend.registry)  # type: ignore[misc]
        unowned: set[str] = set()
        for keys in surface_keys().values():
            for key in keys:
                if key in own or not key.startswith(backend.prefix):
                    continue
                if not any(plane.matches(key) for plane in planes):
                    unowned.add(key)
        self.assertEqual(
            unowned,
            set(),
            "the API would refuse these keys even though another entrypoint consumes them; "
            "register a plane in speechnexus_backend.environment.PLANES",
        )

    def test_a_narrow_surface_never_sees_another_surfaces_key(self) -> None:
        for entry in SERVICE_PREFIXES:
            if entry.service == "backend":
                continue
            foreign = {
                key
                for surface in SURFACES
                if surface.service != entry.service
                for key in surface_keys()[surface.id]
                if key.startswith(entry.prefix)
            }
            self.assertEqual(foreign, set(), f"{entry.service} prefix encloses a foreign key")

    def test_a_narrow_surface_plane_never_shadows_its_own_fields(self) -> None:
        for entry in SERVICE_PREFIXES:
            registry = entry.registry
            if registry is None or entry.service == "backend":
                continue
            own = {
                key
                for surface in SURFACES
                if surface.service == entry.service
                for key in surface_keys()[surface.id]
            }
            for plane in registered_planes(*registry):
                shadowed = {key for key in own if plane.matches(key)}
                self.assertEqual(
                    shadowed,
                    set(),
                    f"a {entry.service} plane would silently accept a declared key",
                )

    def test_the_backend_surface_refuses_what_no_plane_covers(self) -> None:
        """A typo of an own key must be reachable by the refusal branch."""
        backend = next(entry for entry in SERVICE_PREFIXES if entry.service == "backend")
        planes = registered_planes(*backend.registry)  # type: ignore[misc]
        for typo in (
            "SPEECHNEXUS_DATABASE_URLL",
            "SPEECHNEXUS_JOB_WRITER_DATABSE_URL",
            "SPEECHNEXUS_OBJECT_STORAGE_BUCKETT",
        ):
            self.assertFalse(any(plane.matches(typo) for plane in planes), typo)


class InventoryTest(unittest.TestCase):
    """Every prefixed variable the repository mentions is classified."""

    def test_every_inventory_token_is_a_setting_or_a_registered_plane(self) -> None:
        declared = {key for surface in SURFACES for key in derived_keys(surface).values()}
        planes = all_planes()
        unclassified = sorted(
            token
            for token in inventory_tokens()
            if token not in declared and not any(plane.matches(token) for plane in planes)
        )
        self.assertEqual(
            unclassified,
            [],
            "these prefixed variables are neither a settings key nor a registered plane; "
            "the startup refusal would reject them",
        )

    def test_no_registered_plane_is_dead(self) -> None:
        declared = {key for surface in SURFACES for key in derived_keys(surface).values()}
        tokens = inventory_tokens() | frozenset(declared)
        for plane in all_planes():
            self.assertTrue(
                any(plane.matches(token) for token in tokens),
                f"registered plane for {plane.owner!r} matches no variable in the repository",
            )

    def test_every_registered_plane_names_an_owner(self) -> None:
        for plane in all_planes():
            self.assertTrue(plane.owner.strip(), "a registered plane has no owner")
            self.assertTrue(
                plane.prefixes or plane.suffixes or plane.keys,
                "a registered plane covers nothing",
            )

    def test_a_runtime_plane_names_the_process_that_consumes_it(self) -> None:
        """A silent deployment plane must not be the only owner of a runtime name."""
        runtime_owners = {
            prefix: plane.owner
            for module, constant in RUNTIME_REGISTRIES.values()
            for plane in registered_planes(module, constant)
            if plane.runtime
            for prefix in plane.prefixes
        }
        for prefix in (
            "SPEECHNEXUS_GATEWAY_",
            "SPEECHNEXUS_INSPECTION_",
            "SPEECHNEXUS_JOB_DISPATCH_",
        ):
            self.assertIn(prefix, runtime_owners)


class EngineParityTest(unittest.TestCase):
    """The scan engine is duplicated for module boundaries; it may not drift."""

    def test_the_shared_definitions_are_identical_in_every_copy(self) -> None:
        modules = (BACKEND_ENVIRONMENT, GATEWAY_ENVIRONMENT, CONTROLLER_ENVIRONMENT)
        reference = _shared_definition_sources(modules[0])
        self.assertEqual(set(reference), set(SHARED_DEFINITIONS))
        for module in modules[1:]:
            self.assertEqual(
                _shared_definition_sources(module),
                reference,
                f"{module} diverged from {modules[0]}",
            )

    def test_every_runtime_module_registers_a_planes_tuple(self) -> None:
        for module, constant in (*RUNTIME_REGISTRIES.values(), WORKBENCH_REGISTRY):
            self.assertTrue((ROOT / module).is_file(), module)
            registered_planes(module, constant)

    def test_the_package_roots_are_the_four_service_packages(self) -> None:
        self.assertEqual(len(PACKAGE_ROOTS), 4)
        for root in PACKAGE_ROOTS:
            self.assertTrue((ROOT / root).is_dir(), root)
        self.assertNotIn(Path("tests"), {ROOT / entry for entry in PACKAGE_ROOTS})


if __name__ == "__main__":
    unittest.main()
