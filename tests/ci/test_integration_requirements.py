"""Tool routing follows the exact integration partition without importing pytest."""

from __future__ import annotations

import ast
import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "integration_requirements_gate", ROOT / "scripts/dev-env/integration-shards.py"
)
assert SPEC is not None and SPEC.loader is not None
GATE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = GATE
SPEC.loader.exec_module(GATE)


class IntegrationRequirementsTests(unittest.TestCase):
    def test_browser_dependencies_follow_actual_parameterized_nodeids(self) -> None:
        browser = [
            "tests/integration/test_workbench_example_browser.py::test_page[postgresql]",
            "tests/integration/test_workbench_example_browser.py::test_page[mysql]",
            "tests/integration/test_workbench_orphan_probes.py::test_probe",
            "tests/integration/test_workbench_realtime_synthetic_probes.py::test_asr",
        ]
        full = [*browser, *GATE.NODE_CONTRACT_TESTS, "tests/integration/test_plain.py::test_db"]
        for count in range(1, 17):
            for index in range(count):
                with self.subTest(count=count, index=index):
                    selected = set(GATE.partition(full, index, count))
                    self.assertEqual(
                        GATE.tool_requirements(full, index, count),
                        {
                            "node_contracts": bool(selected.intersection(GATE.NODE_CONTRACT_TESTS)),
                            "browser": bool(selected.intersection(browser)),
                        },
                    )

    def test_no_browser_is_installed_for_a_database_only_partition(self) -> None:
        full = ["tests/integration/test_plain.py::test_db"]
        self.assertEqual(
            GATE.tool_requirements(full, 0, 1), {"node_contracts": False, "browser": False}
        )

    def test_every_current_probe_consumer_declares_browser_tools(self) -> None:
        consumers = []
        for path in (ROOT / "tests/integration").glob("test_*.py"):
            literals = {
                node.value
                for node in ast.walk(ast.parse(path.read_text()))
                if isinstance(node, ast.Constant) and isinstance(node.value, str)
            }
            if not any(
                value.startswith("scripts/dev-env/") and value.endswith(".mjs")
                for value in literals
            ):
                continue
            nodeid = f"{path.relative_to(ROOT)}::test_dependency_inventory"
            self.assertTrue(GATE.tool_requirements([nodeid], 0, 1)["browser"], str(path))
            consumers.append(path)
        self.assertGreaterEqual(len(consumers), 10)


if __name__ == "__main__":
    unittest.main()
