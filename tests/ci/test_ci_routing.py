"""Run routing regressions with the runner's Python, without project dependencies."""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/dev-env/ci-routing.py"
SHARD_SCRIPT = ROOT / "scripts/dev-env/integration-shards.py"
WORKFLOW = ROOT / ".github/workflows/ci.yml"
GUARD_STEP_NAME = "Require the Web lanes to select actual Web consumers"
SPEC = importlib.util.spec_from_file_location("ci_routing", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
ROUTER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ROUTER)
SHARD_SPEC = importlib.util.spec_from_file_location("integration_partitioner", SHARD_SCRIPT)
assert SHARD_SPEC is not None and SHARD_SPEC.loader is not None
PARTITIONER = importlib.util.module_from_spec(SHARD_SPEC)
sys.modules[SHARD_SPEC.name] = PARTITIONER
SHARD_SPEC.loader.exec_module(PARTITIONER)


class PathRoutingTests(unittest.TestCase):
    def test_only_approved_prose_is_lightweight(self) -> None:
        for paths in (
            ["AGENTS.md"],
            ["README.md", "README.en.md"],
            ["docs/architecture/design.md", "docs/含空格 的说明.md"],
        ):
            with self.subTest(paths=paths):
                self.assertEqual(ROUTER.classify(paths), "docs")

    def test_web_source_and_prose_keep_the_web_lane(self) -> None:
        self.assertEqual(
            ROUTER.classify(["web/workbench/src/App.tsx", "web/shared/tokens.css", "README.md"]),
            "web",
        )

    def test_skill_documents_and_metadata_are_lightweight(self) -> None:
        paths = [
            ".agents/skills/delivery/SKILL.md",
            ".agents/skills/delivery/references/evidence.md",
            ".agents/skills/delivery/agents/openai.yaml",
            ".agents/skills/delivery/agents/alternate.yml",
            ".agents/skills/delivery/evals/evals.json",
            ".agents/skills/delivery/evals/fixtures.md",
        ]
        for path in paths:
            with self.subTest(path=path):
                self.assertEqual(ROUTER.classify([path]), "docs")
        self.assertEqual(ROUTER.classify([*paths, "README.md"]), "docs")
        self.assertEqual(ROUTER.classify([*paths, "web/admin/src/App.tsx"]), "web")
        self.assertEqual(ROUTER.classify([*paths, "backend/module.py"]), "full")

    def test_skill_executables_unknown_files_and_other_agent_config_stay_full(self) -> None:
        for path in (
            ".agents/skills/delivery/scripts/check.py",
            ".agents/skills/delivery/scripts/check.sh",
            ".agents/skills/delivery/scripts/check.mjs",
            ".agents/skills/delivery/data.bin",
            ".agents/skills/SKILL.md",
            ".agents/settings.json",
            ".agents/skills-other/delivery/SKILL.md",
            ".agents/skills/delivery/../settings.yaml",
        ):
            with self.subTest(path=path):
                self.assertEqual(ROUTER.classify([path]), "full")

    def test_unknown_or_shared_inputs_require_every_lane(self) -> None:
        for path in (
            ".github/workflows/ci.yml",
            "scripts/dev-env/ci-routing.py",
            "tests/ci/test_ci_routing.py",
            "contracts/speech-openapi.yaml",
            "packages/contracts/src/index.ts",
            "backend/src/module.py",
            "migrations/001.sql",
            "deploy/compose.yaml",
            "uv.lock",
            "pnpm-lock.yaml",
            ".nvmrc",
            "package.json",
            "web/admin/package.json",
            "web/admin/vite.config.ts",
            "docs/schema.json",
            # A README `COPY`ed into an image build is still a build input.
            "backend/README.md",
            "new-component/component.ts",
        ):
            with self.subTest(path=path):
                self.assertEqual(ROUTER.classify(["README.md", path]), "full")

    def test_standalone_engines_skip_only_database_checks(self) -> None:
        for path in (
            "deploy/engines/vllm-asr-lifecycle/src/speechnexus_vllm_asr/plugin.py",
            "deploy/engines/vllm-asr-lifecycle/pyproject.toml",
            "deploy/engines/cpu-vad/requirements.txt",
            "tests/unit/gateway/test_native_asr_scope.py",
            "tests/unit/gateway/test_worker_fence.py",
        ):
            for paths in (
                [path],
                ["README.md", path, "web/workbench/src/App.tsx"],
                ["web/workbench/src/App.tsx", path, "README.md"],
            ):
                with self.subTest(paths=paths):
                    self.assertEqual(ROUTER.classify(paths), "no-db")

    def test_engine_changes_cannot_hide_database_or_shared_inputs(self) -> None:
        engine = "deploy/engines/vllm-asr-lifecycle/worker-pin.json"
        for path in (
            "backend/src/module.py",
            "tests/unit/gateway/test_file_asr_command.py",
            "tests/support/coordination.py",
            "tests/integration/test_gateway_file_worker.py",
            "contracts/internal/execution-command.schema.json",
            "uv.lock",
            "pnpm-lock.yaml",
            ".github/workflows/ci.yml",
            "deploy/engines/../compose.yaml",
            "deploy/engines.py",
            "deploy/engines/unknown.py",
            "deploy/engines/new-engine/module.py",
        ):
            for paths in ([engine, path], [path, engine]):
                with self.subTest(paths=paths):
                    self.assertEqual(ROUTER.classify(paths), "full")

    def test_empty_and_noncanonical_paths_do_not_authorize_skips(self) -> None:
        for paths in ([], [""], ["/docs/a.md"], ["docs/../backend/a.md"], ["docs\\a.md"]):
            with self.subTest(paths=paths):
                self.assertEqual(ROUTER.classify(paths), "full")

    def test_script_is_compatible_with_runner_python_312(self) -> None:
        ast.parse(SCRIPT.read_text(), feature_version=(3, 12))


PROSE_BASENAME = re.compile(r"^(?:AGENTS\.md|README(?:\.[^/]+)?\.md)$")


def tracked_paths() -> list[str]:
    """Tracked files, so a sweep never walks `node_modules`/`.venv`."""
    listing = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT, text=True)
    return [name for name in listing.split("\0") if name]


def prose_paths() -> list[str]:
    """Every tracked agent-rules or README document in the repository."""
    return [name for name in tracked_paths() if PROSE_BASENAME.match(name.rsplit("/", 1)[-1])]


def dockerfile_readme_copies() -> set[str]:
    """README paths a Dockerfile `COPY`s: the build-input set, derived not restated.

    Only literal `COPY <path>.md` tokens are derived. Directory copies
    (`COPY web web`), `ADD` and globs are invisible here; today no such copy
    publishes a prose path (the Web image's only published output is `dist`), but
    a future one has to be registered in `_IMAGE_COPY_PROSE` by hand.
    """
    copies: set[str] = set()
    for name in tracked_paths():
        base = name.rsplit("/", 1)[-1]
        if base != "Dockerfile" and not base.endswith(".Dockerfile"):
            continue
        for line in (ROOT / name).read_text(encoding="utf-8").splitlines():
            if not line.lstrip().upper().startswith("COPY "):
                continue
            # Drop flags such as `--from=...`; the final token is the destination.
            tokens = [token for token in line.split()[1:] if not token.startswith("--")]
            for token in tokens[:-1]:
                if PROSE_BASENAME.match(token.rsplit("/", 1)[-1]):
                    copies.add(token)
    return copies


# Any module input that would make a `.md` part of a bundle: static import,
# dynamic import, CommonJS require, Vite glob or a `new URL(..., import.meta.url)`
# asset reference. Markdown is not one of these today; the regression below keeps
# the "in-tree rules are not inputs" premise honest instead of leaving it a grep
# fact recorded in a review.
MARKDOWN_IMPORT = re.compile(
    r"""(?:import\s[^;\n]*?from|import\s*\(|require\s*\(|import\.meta\.glob\s*\(|new\s+URL\s*\(\s*)"""
    r"""\s*["'][^"'\n]+\.md(?:[?#][^"'\n]*)?["']"""
)


def is_web_public_prose(path: str) -> bool:
    """`web/<app>/public/**` prose: Vite copies that directory verbatim into `dist`."""
    parts = PurePosixPath(path).parts
    return len(parts) >= 4 and parts[0] == "web" and parts[2] == "public"


def expected_prose_lanes(paths: list[str]) -> dict[str, str]:
    """The lane each document needs, derived from the three independent rules."""
    copies = dockerfile_readme_copies()
    return {
        name: (
            "full"
            if name in copies or name in ROUTER._TEST_READ_PROSE or is_web_public_prose(name)
            else "docs"
        )
        for name in paths
    }


class ProseRoutingTests(unittest.TestCase):
    """`AGENTS.md`/`README*` route by content, not by directory depth (#812).

    The defect: `classify(["web/AGENTS.md"])` answered `full` while
    `classify(["AGENTS.md"])` answered `docs`, so the most frequently edited class
    of document paid the whole matrix by accident of location. The lane is now
    decided by what the file is; only a real build or test consumer keeps a
    heavier route, and each such position is pinned below.

    The expected lanes are derived from three independent rules — the Dockerfile
    `COPY`list, the unit-test readers and the `public/` bundle rule — and compared
    in both directions with `classify` and `_prose_route`, so adding an exception
    to the router without teaching this test about it is red, and a new legitimate
    document is not. Directory-form `COPY`, `ADD` and glob inputs are outside the
    derivation and need manual registration.
    """

    def assert_lanes(self, paths: list[str]) -> None:
        routed = {name: ROUTER.classify([name]) for name in paths}
        expected = expected_prose_lanes(paths)
        self.assertEqual(routed, expected, "classify disagrees with the derived lane")
        for name in paths:
            with self.subTest(path=name):
                # The judgment itself must agree too: `classify` picking the right
                # answer through another branch would hide a broken prose rule.
                self.assertEqual(ROUTER._prose_route(PurePosixPath(name)), expected[name])

    def test_web_agents_is_no_longer_full(self) -> None:
        mode = ROUTER.classify(["web/AGENTS.md"])
        self.assertEqual(mode, "docs")
        self.assertNotEqual(mode, "full")

    def test_the_same_document_gets_the_same_lane_at_every_level(self) -> None:
        for path in (
            "AGENTS.md",
            "web/AGENTS.md",
            "contracts/AGENTS.md",
            "tests/integration/AGENTS.md",
            # Documentation inside a source tree is still documentation: Vite
            # follows imports (no `.md` is imported anywhere, pinned below), `tsc`
            # compiles only TS/TSX, ESLint globs only TS/TSX, Prettier's
            # `format:check` skips `*.md` and the Web image publishes `dist`.
            "web/workbench/src/AGENTS.md",
            "web/shared/src/README.md",
            "README.md",
            "README.zh-CN.md",
            "contracts/README.md",
            "tests/integration/README.md",
            "deploy/README.md",
            "deploy/private/README.md",
            "deploy/engines/cpu-vad/README.md",
            "deploy/engines/file-asr-audio/evidence/gb10-20261027/README.md",
        ):
            with self.subTest(path=path):
                self.assertEqual(ROUTER.classify([path]), "docs")

    def test_a_document_never_hides_a_code_change(self) -> None:
        self.assertEqual(ROUTER.classify(["web/AGENTS.md", "web/workbench/src/App.tsx"]), "web")
        self.assertEqual(
            ROUTER.classify(["web/AGENTS.md", "deploy/engines/cpu-vad/requirements.in"]), "no-db"
        )
        for source in ("backend/src/module.py", "scripts/dev-env/ci-routing.py", "uv.lock"):
            with self.subTest(source=source):
                self.assertEqual(ROUTER.classify(["web/AGENTS.md", source]), "full")

    def test_every_tracked_document_has_a_content_lane(self) -> None:
        tracked = prose_paths()
        self.assertIn("web/AGENTS.md", tracked)
        self.assertIn("deploy/README.md", tracked)
        self.assert_lanes(tracked)

    def test_a_new_tracked_document_keeps_the_sweep_green(self) -> None:
        # Regression for #816: the sweep's expectation used to list only the
        # Dockerfile and unit-test inputs, so committing a legitimate
        # `web/<app>/public/README.md` (correctly `full`) turned it red with a
        # message about "a source lane". Derive the expected lane instead.
        self.assert_lanes([*prose_paths(), "web/workbench/public/README.md"])

    def test_readmes_copied_into_an_image_keep_the_full_lane(self) -> None:
        copies = dockerfile_readme_copies()
        self.assertTrue(copies, "no `COPY <path>.md` was parsed out of the Dockerfiles")
        self.assertEqual(copies, ROUTER._IMAGE_COPY_PROSE)
        for name in sorted(copies):
            with self.subTest(path=name):
                self.assertEqual(ROUTER.classify([name]), "full")

    def test_prose_in_a_web_public_directory_stays_full(self) -> None:
        # Vite copies `public/` verbatim into `dist`, so a Markdown file there is a
        # shipped product asset, not prose. Any depth below the app root matches.
        for path in (
            "web/workbench/public/AGENTS.md",
            "web/admin/public/README.md",
            "web/workbench/public/notes/AGENTS.md",
        ):
            with self.subTest(path=path):
                self.assertEqual(ROUTER.classify([path]), "full")

    def test_a_nested_src_public_directory_is_not_a_bundle_input(self) -> None:
        # The Vite default `publicDir` is the app root's `public`; `src/public`
        # is not copied into `dist`, so prose there is documentation.
        self.assertEqual(ROUTER.classify(["web/workbench/src/public/README.md"]), "docs")

    def test_readmes_a_unit_test_asserts_keep_the_full_lane(self) -> None:
        # `tests/unit/gateway/test_engine_language_semantics.py` reads both files and
        # fails on wording that promises reproducible seed bytes. The docs lane does
        # not run pytest, so these two cannot take the prose route.
        self.assertEqual(
            ROUTER._TEST_READ_PROSE,
            {
                "deploy/engines/file-voice-clone/README.md",
                "deploy/engines/file-voice-design/README.md",
            },
        )
        for path in sorted(ROUTER._TEST_READ_PROSE):
            with self.subTest(path=path):
                self.assertEqual(ROUTER.classify([path]), "full")
                self.assertEqual(ROUTER.classify([path, "web/AGENTS.md"]), "full")

    def test_unrecognised_names_stay_fail_closed(self) -> None:
        # Only `AGENTS.md` and `README*.md` are prose. Variants keep the heavier
        # lane so a typo can never buy a narrower route; #812's same-document
        # complaint still holds for these names and is documented as such.
        for path in (
            "web/agents.md",
            "web/AGENTS.MD",
            "web/Readme.md",
            "web/readme.md",
            "web/README",
            "web/README.rst",
            "web/README.en.txt",
        ):
            with self.subTest(path=path):
                self.assertEqual(ROUTER.classify([path]), "full")

    def test_web_sources_do_not_import_markdown(self) -> None:
        """In-tree rules stay non-inputs only while nothing imports them.

        If a `.md` becomes a module/glob/URL input, changing it changes the bundle
        while the document still routes as prose (#816). This turns that moment
        red so the routing decision is revisited, instead of leaving the premise a
        `grep` fact recorded in a review.
        """
        offenders: list[str] = []
        for name in tracked_paths():
            if not name.startswith("web/") or Path(name).suffix not in {
                ".ts",
                ".tsx",
                ".mjs",
                ".js",
            }:
                continue
            for number, line in enumerate(
                (ROOT / name).read_text(encoding="utf-8").splitlines(), 1
            ):
                if MARKDOWN_IMPORT.search(line):
                    offenders.append(f"{name}:{number}: {line.strip()}")
        self.assertEqual(offenders, [])


class GitRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.git("init", "-q", "-b", "main")
        self.write("README.md", "# Initial\n")
        self.write("backend/module.py", "value = 1\n")
        self.base = self.commit()

    def git(self, *args: str) -> str:
        return subprocess.check_output(
            [
                "git",
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "commit.gpgsign=false",
                "-c",
                "user.name=CI",
                "-c",
                "user.email=ci@example.invalid",
                *args,
            ],
            cwd=self.root,
            text=True,
            stderr=subprocess.PIPE,
        ).strip()

    def write(self, name: str, content: str) -> None:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    def commit(self) -> str:
        self.git("add", ".")
        self.git("commit", "-qm", "Synthetic routing fixture")
        return self.git("rev-parse", "HEAD")

    def test_real_diff_uses_nul_paths_and_includes_a_deleted_source(self) -> None:
        self.write("docs/one\ntwo.md", "# Notes\n")
        self.commit()
        mode, _ = ROUTER.detect_route("pull_request", self.base, self.root)
        self.assertEqual(mode, "docs")
        (self.root / "backend/module.py").unlink()
        self.commit()
        mode, _ = ROUTER.detect_route("pull_request", self.base, self.root)
        self.assertEqual(mode, "full")

    def test_rename_into_docs_does_not_hide_the_original_source(self) -> None:
        self.git("mv", "backend/module.py", "README.en.md")
        self.commit()
        mode, _ = ROUTER.detect_route("pull_request", self.base, self.root)
        self.assertEqual(mode, "full")

    def test_skill_diff_is_lightweight_but_source_renamed_into_skill_is_full(self) -> None:
        self.write(".agents/skills/delivery/SKILL.md", "# Delivery\n")
        self.write(".agents/skills/delivery/agents/openai.yaml", "interface: {}\n")
        self.write(".agents/skills/delivery/evals/evals.json", '{"evals": []}\n')
        self.commit()
        mode, _ = ROUTER.detect_route("pull_request", self.base, self.root)
        self.assertEqual(mode, "docs")
        self.git("mv", "backend/module.py", ".agents/skills/delivery/references.md")
        self.commit()
        mode, _ = ROUTER.detect_route("pull_request", self.base, self.root)
        self.assertEqual(mode, "full")

    def test_rename_shared_source_into_engine_still_requires_databases(self) -> None:
        self.write("deploy/engines/cpu-vad/README.md", "# Engine\n")
        self.git("mv", "backend/module.py", "deploy/engines/cpu-vad/module.py")
        self.commit()
        mode, _ = ROUTER.detect_route("pull_request", self.base, self.root)
        self.assertEqual(mode, "full")

    def test_real_engine_diff_skips_databases_but_main_stays_full(self) -> None:
        self.write("deploy/engines/cpu-vad/module.py", "value = 2\n")
        self.commit()
        mode, _ = ROUTER.detect_route("pull_request", self.base, self.root)
        self.assertEqual(mode, "no-db")
        for event in ("push", "workflow_dispatch"):
            mode, _ = ROUTER.detect_route(event, self.base, self.root)
            self.assertEqual(mode, "full")

    def test_main_manual_unknown_events_and_empty_diff_are_full(self) -> None:
        self.write("README.md", "# Updated\n")
        self.commit()
        for event in ("push", "workflow_dispatch", "merge_group", ""):
            with self.subTest(event=event):
                mode, _ = ROUTER.detect_route(event, self.base, self.root)
                self.assertEqual(mode, "full")
        mode, _ = ROUTER.detect_route("pull_request", self.git("rev-parse", "HEAD"), self.root)
        self.assertEqual(mode, "full")

    def test_invalid_or_unavailable_base_falls_back_to_full(self) -> None:
        for base in ("", "0" * 40, "f" * 40, "--upload-pack=bad", "main; echo unsafe"):
            with self.subTest(base=base):
                mode, _ = ROUTER.detect_route("pull_request", base, self.root)
                self.assertEqual(mode, "full")

    def test_merge_result_is_compared_to_the_exact_target_commit(self) -> None:
        self.git("switch", "-qc", "topic")
        self.write("README.md", "# Topic notes\n")
        self.commit()
        self.git("switch", "-q", "main")
        self.write("backend/module.py", "value = 2\n")
        target = self.commit()
        self.git("merge", "--no-ff", "-qm", "Synthetic merge", "topic")
        mode, _ = ROUTER.detect_route("pull_request", target, self.root)
        self.assertEqual(mode, "docs")

    def test_cli_needs_no_site_packages_and_writes_a_closed_output(self) -> None:
        self.write("README.md", "# Updated\n")
        self.commit()
        output = self.root / "github-output"
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                "-S",
                str(SCRIPT),
                "--event=pull_request",
                f"--base={self.base}",
                f"--output={output}",
            ],
            cwd=self.root,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["mode"], "docs")
        self.assertEqual(output.read_text(), "mode=docs\nexcluded_shards=[]\n")

    def test_lightweight_route_still_rejects_bad_diffs(self) -> None:
        self.write("README.md", "# Updated with trailing space \n")
        self.commit()
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--event=pull_request", f"--base={self.base}"],
            cwd=self.root,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)


class IntegrationLaneTests(unittest.TestCase):
    """The route's integration lanes must be the partitioner's Web-capable lanes (#757)."""

    def _collection(self) -> list[str]:
        browser = [
            "tests/integration/test_workbench_example_browser.py::test_page[postgresql]",
            "tests/integration/test_workbench_example_browser.py::test_page[mysql]",
            "tests/integration/test_workbench_orphan_probes.py::test_probe",
        ]
        node = list(PARTITIONER.NODE_CONTRACT_TESTS)
        ordinary = [f"tests/integration/test_plain_{index}.py::test_db" for index in range(40)]
        return [*browser, *node, *ordinary]

    def test_web_lanes_are_exactly_the_partition_indices_that_select_web_consumers(self) -> None:
        collection = self._collection()
        count = ROUTER.INTEGRATION_SHARD_COUNT
        selected_web_lanes = {
            index
            for index in range(count)
            if any(
                PARTITIONER.requires_browser(nodeid) or nodeid in PARTITIONER.NODE_CONTRACT_TESTS
                for nodeid in PARTITIONER.partition(collection, index, count)
            )
        }
        self.assertEqual(selected_web_lanes, set(ROUTER.web_lanes(count)))
        # Every Web consumer has to run under `web`, not just some of them.
        executed = {
            nodeid
            for index in ROUTER.web_lanes(count)
            for nodeid in PARTITIONER.partition(collection, index, count)
        }
        self.assertLessEqual(set(collection[:5]), executed)
        # The runtime guard only asserts that each lane selects *a* Web consumer
        # (either kind). Which lanes select which kind is pinned here, against the
        # real partitioner, so a classifier that stops seeing one kind turns this
        # test red instead of leaving the runtime guard green on a shrinking lane.
        for index in ROUTER.web_lanes(count):
            requirements = PARTITIONER.tool_requirements(collection, index, count)
            self.assertTrue(requirements["browser"], f"lane {index} selects no browser consumer")
            self.assertTrue(
                requirements["node_contracts"], f"lane {index} selects no node contract"
            )

    def test_only_the_web_route_removes_lanes_and_it_keeps_its_own_web_lanes(self) -> None:
        count = ROUTER.INTEGRATION_SHARD_COUNT
        self.assertEqual(ROUTER.excluded_shards("full"), ())
        self.assertEqual(ROUTER.excluded_shards("docs"), ())
        self.assertEqual(ROUTER.excluded_shards("no-db"), ())
        excluded = ROUTER.excluded_shards("web")
        self.assertEqual(
            [row["shard"] for row in excluded],
            sorted(set(range(count)) - set(ROUTER.web_lanes(count))),
        )
        # An empty matrix is a workflow error, so a route that runs the job must keep
        # at least the Web-capable lanes.
        self.assertTrue(ROUTER.web_lanes(count))
        self.assertLess(len(excluded), count)

    def test_router_and_workflow_agree_on_the_partition_count(self) -> None:
        count = ROUTER.INTEGRATION_SHARD_COUNT
        workflow = WORKFLOW.read_text()
        self.assertIn(f"--shard-count {count}", workflow)
        self.assertIn(f"--count {count}", workflow)
        self.assertIn(f"shard: [{', '.join(str(index) for index in range(count))}]", workflow)

    def test_static_lanes_stay_inside_the_declared_partition_count(self) -> None:
        self.assertEqual(tuple(range(ROUTER.INTEGRATION_SHARD_COUNT)), tuple(range(6)))
        self.assertEqual(ROUTER.web_lanes(3), (2,))
        self.assertEqual(ROUTER.web_lanes(6), (4, 5))
        self.assertEqual(ROUTER.web_lanes(16), (11, 12, 13, 14, 15))


def guard_script() -> str:
    """The Web-lane guard's `run:` payload, exactly as `bash` receives it."""
    lines = WORKFLOW.read_text().splitlines()
    start = next(index for index, line in enumerate(lines) if GUARD_STEP_NAME in line)
    run = next(index for index in range(start, len(lines)) if lines[index].strip() == "run: |")
    indent = len(lines[run]) - len(lines[run].lstrip()) + 2
    body: list[str] = []
    for line in lines[run + 1 :]:
        stripped = line.lstrip()
        if stripped and len(line) - len(stripped) < indent:
            break
        body.append(line[indent:] if stripped else "")
    return "\n".join(body)


class WebLaneGuardTests(unittest.TestCase):
    """The guard must judge exactly the two selection kinds its name promises (#757)."""

    def _guard(self, requirements: dict[str, bool] | None) -> subprocess.CompletedProcess[str]:
        env = {
            key: value
            for key, value in os.environ.items()
            if key not in {"BROWSER", "NODE_CONTRACTS", "SHARD"}
        }
        env["SHARD"] = "4"
        if requirements is not None:
            env["BROWSER"] = "true" if requirements["browser"] else "false"
            env["NODE_CONTRACTS"] = "true" if requirements["node_contracts"] else "false"
        return subprocess.run(
            ["bash", "-euo", "pipefail", "-c", guard_script()],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def test_a_lane_selecting_only_node_contract_consumers_passes(self) -> None:
        # Counterexample for the step name: a lane that selects the Node HTTP
        # contract consumers but no browser module is a legitimate Web lane, and the
        # old browser-only judgement failed it. The real partitioner produces this
        # state when the browser classifier stops recognising `*_browser.py`.
        result = self._guard({"browser": False, "node_contracts": True})
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertNotIn("::error::", result.stdout)

    def test_a_lane_selecting_only_browser_consumers_passes(self) -> None:
        result = self._guard({"browser": True, "node_contracts": False})
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)

    def test_a_lane_selecting_neither_kind_fails_closed(self) -> None:
        result = self._guard({"browser": False, "node_contracts": False})
        self.assertEqual(result.returncode, 1)
        self.assertIn("::error::Web-scope integration partition 4", result.stdout)
        self.assertIn("browser=false", result.stdout)
        self.assertIn("node_contracts=false", result.stdout)

    def test_missing_selection_outputs_fail_closed(self) -> None:
        # `steps.<id>.outputs.<name>` is empty when the requirements step did not
        # publish it; an empty value must not read as "selected something".
        result = self._guard(None)
        self.assertEqual(result.returncode, 1)
        self.assertIn("<empty>", result.stdout)

    def test_the_two_kinds_come_from_the_requirements_step(self) -> None:
        script = guard_script()
        self.assertIn("${BROWSER:-}", script)
        self.assertIn("${NODE_CONTRACTS:-}", script)
        self.assertIn('!= "true" &&', script)


if __name__ == "__main__":
    unittest.main()
