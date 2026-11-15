"""Exercise the Web image build-context guard without Docker or dependencies.

The guard's `--build` mode is the decisive check and runs in the CI Web job; these
cases cover the static half, the parsing it depends on and the two failure shapes
#834 had: test material inside the copy set, and a production source that imports it.
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/dev-env/verify-web-image-context.py"
SPEC = importlib.util.spec_from_file_location("verify_web_image_context", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
GUARD = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = GUARD
SPEC.loader.exec_module(GUARD)

ROOT_FILES = {
    "package.json": '{"name":"root","private":true}\n',
    "pnpm-lock.yaml": "lockfileVersion: '9.0'\n",
    "pnpm-workspace.yaml": 'packages:\n  - "web/*"\n',
    "tsconfig.base.json": '{"compilerOptions":{"strict":true}}\n',
    ".npmrc": "engine-strict=true\n",
}
ROOT_COPY = "package.json pnpm-lock.yaml pnpm-workspace.yaml tsconfig.base.json .npmrc ./"
APP_MANIFEST = """\
{
  "name": "@speechnexus/admin",
  "private": true,
  "scripts": {"build": "tsc -p tsconfig.build.json && vite build"}
}
"""
APP_TSCONFIG = """\
{
  "extends": "../../tsconfig.base.json",
  "include": ["src", "vite.config.ts"]
}
"""
APP_BUILD_TSCONFIG = """\
{
  "extends": "./tsconfig.json",
  "exclude": ["src/**/*.test.ts", "src/**/*.test.tsx", "src/**/dev/**"]
}
"""


def dockerfile(*copies: str) -> str:
    body = "".join(f"COPY {copy}\n" for copy in copies)
    return (
        "FROM node:26-bookworm-slim AS build\n"
        f"ARG APP_NAME=admin\n{body}"
        'RUN pnpm --filter "@speechnexus/${APP_NAME}" build\n'
        "FROM nginx:alpine\n"
        "COPY --from=build /workspace/web-dist /usr/share/nginx/html\n"
    )


class Repository:
    """A synthetic repository the guard can run against."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def write(self, name: str, content: str) -> None:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(content), encoding="utf-8")

    def app(
        self,
        *,
        directory: str = "web/admin",
        name: str = "@speechnexus/admin",
        build: str = "tsc -p tsconfig.build.json && vite build",
        source: str = "export const app = 1;\n",
    ) -> None:
        self.write(
            f"{directory}/package.json",
            APP_MANIFEST.replace("@speechnexus/admin", name).replace(
                "tsc -p tsconfig.build.json && vite build", build
            ),
        )
        self.write(f"{directory}/tsconfig.json", APP_TSCONFIG)
        self.write(f"{directory}/tsconfig.build.json", APP_BUILD_TSCONFIG)
        self.write(f"{directory}/src/main.ts", source)
        self.write(f"{directory}/vite.config.ts", "export default {};\n")

    def dockerfile(self, *copies: str) -> None:
        self.write(GUARD.DOCKERFILE, dockerfile(*copies))


class GuardTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.repository = Repository(Path(self._directory.name))
        for name, content in ROOT_FILES.items():
            self.repository.write(name, content)
        self.repository.app()

    def tearDown(self) -> None:
        self._directory.cleanup()

    def assert_fails(self, expected: str) -> None:
        with self.assertRaises(GUARD.CheckError) as raised:
            GUARD.static_checks(self.repository.root)
        self.assertIn(expected, str(raised.exception))


class StageParsingTests(unittest.TestCase):
    def test_copy_sources_are_read_from_the_builder_stage_only(self) -> None:
        stages = GUARD._dockerfile_stages(
            dockerfile("web web", "packages/contracts packages/contracts")
        )
        self.assertEqual([stage.name for stage in stages], ["build"])
        self.assertEqual(stages[0].copy_sources, ("web", "packages/contracts"))

    def test_stage_output_and_directory_destinations(self) -> None:
        text = (
            "FROM node:26 AS build\n"
            "COPY package.json pnpm-lock.yaml ./\n"
            "COPY --from=build /workspace/web-dist /usr/share/nginx/html\n"
            "COPY a.mjs a.d.mts scripts/dev-env/\n"
        )
        stages = GUARD._dockerfile_stages(text)
        self.assertEqual(
            stages[0].copy_sources, ("package.json", "pnpm-lock.yaml", "a.mjs", "a.d.mts")
        )
        self.assertNotIn("--from=build", " ".join(stages[0].copy_sources))

    def test_several_sources_with_a_file_destination_are_rejected(self) -> None:
        text = "FROM node:26 AS build\nCOPY a b c\n"
        with self.assertRaises(GUARD.CheckError):
            GUARD._dockerfile_stages(text)


class PatternTests(unittest.TestCase):
    def test_tsconfig_globs_cover_nested_paths(self) -> None:
        self.assertTrue(GUARD._pattern_matches("src", "src/task-center/TaskCenter.tsx"))
        self.assertTrue(GUARD._pattern_matches("src/**/*.test.tsx", "src/TaskCenter.test.tsx"))
        self.assertTrue(GUARD._pattern_matches("src/**/*.test.tsx", "src/a/b/C.test.tsx"))
        self.assertFalse(GUARD._pattern_matches("src/**/*.test.tsx", "src/a/b/C.tsx"))
        self.assertTrue(GUARD._pattern_matches("src/**/dev/**", "src/task-center/dev/demo.ts"))
        self.assertFalse(GUARD._pattern_matches("src/test/**", "src/test.ts"))

    def test_dockerignore_last_match_wins(self) -> None:
        rules = ((False, "tests"), (True, "tests/fixtures"))
        self.assertFalse(GUARD.dockerignore_excluded("tests/fixtures/contracts/a.json", rules))
        self.assertTrue(GUARD.dockerignore_excluded("tests/ci/test.py", rules))
        self.assertFalse(GUARD.dockerignore_excluded("web/admin/src/main.tsx", rules))

    def test_repository_dockerignore_keeps_the_copy_sources(self) -> None:
        rules = GUARD.dockerignore_rules(ROOT)
        for source in GUARD.builder_copy_sources(ROOT):
            with self.subTest(source=source):
                self.assertFalse(GUARD.dockerignore_excluded(source, rules))


class StaticCheckTests(GuardTestCase):
    def test_a_complete_copy_set_passes(self) -> None:
        self.repository.dockerfile(ROOT_COPY, "web web")
        self.assertTrue(GUARD.static_checks(self.repository.root))

    def test_test_material_in_the_copy_set_is_rejected(self) -> None:
        self.repository.write("tests/fixtures/contracts/file-workflow.json", "{}\n")
        self.repository.dockerfile(
            ROOT_COPY, "web web", "tests/fixtures/contracts tests/fixtures/contracts"
        )
        self.assert_fails("test material is part of the image build context")

    def test_missing_root_config_input_is_rejected(self) -> None:
        self.repository.dockerfile(
            "package.json pnpm-lock.yaml pnpm-workspace.yaml tsconfig.base.json ./", "web web"
        )
        self.assert_fails(".npmrc")

    def test_dockerignored_copy_source_is_rejected(self) -> None:
        self.repository.write(".dockerignore", "secrets\n")
        self.repository.write("secrets/token", "x\n")
        self.repository.dockerfile(ROOT_COPY, "web web", "secrets secrets")
        self.assert_fails("excluded by .dockerignore")

    def test_a_copy_source_that_does_not_exist_is_rejected(self) -> None:
        self.repository.dockerfile(ROOT_COPY, "web web", "docs docs")
        self.assert_fails("do not exist in the repository")

    def test_a_build_script_that_typechecks_the_whole_tree_is_rejected(self) -> None:
        self.repository.app(build="tsc -p tsconfig.json && vite build")
        self.repository.dockerfile(ROOT_COPY, "web web")
        self.assert_fails("does not typecheck tsconfig.build.json")

    def test_a_production_source_importing_test_data_is_rejected(self) -> None:
        self.repository.write("tests/fixtures/contracts/file-workflow.json", "{}\n")
        self.repository.app(
            source=(
                'import fixtures from "../../../tests/fixtures/contracts/file-workflow.json";\n'
                "export const app = fixtures;\n"
            )
        )
        self.repository.dockerfile(ROOT_COPY, "web web")
        self.assert_fails("production sources import test data")

    def test_a_test_source_importing_test_data_is_outside_the_program(self) -> None:
        self.repository.write("tests/fixtures/contracts/file-workflow.json", "{}\n")
        self.repository.write(
            "web/admin/src/TaskCenter.test.tsx",
            'import fixtures from "../../../tests/fixtures/contracts/file-workflow.json";\n'
            "export const app = fixtures;\n",
        )
        self.repository.write(
            "web/admin/src/task-center/dev/demo.ts",
            'import fixtures from "../../../../tests/fixtures/contracts/file-workflow.json";\n'
            "export const demo = fixtures;\n",
        )
        self.repository.dockerfile(ROOT_COPY, "web web")
        self.assertTrue(GUARD.static_checks(self.repository.root))

    def test_a_glob_reference_does_not_pull_a_module_into_the_program(self) -> None:
        self.repository.write(
            "web/admin/src/main.ts",
            'const pages = import.meta.env.DEV ? import.meta.glob("./dev/*.tsx") : {};\n'
            "export const app = pages;\n",
        )
        self.repository.dockerfile(ROOT_COPY, "web web")
        self.assertTrue(GUARD.static_checks(self.repository.root))

    def test_an_unknown_default_app_is_rejected(self) -> None:
        self.repository.write(
            GUARD.DOCKERFILE,
            dockerfile(ROOT_COPY, "web web").replace("ARG APP_NAME=admin", "ARG APP_NAME=console"),
        )
        self.assert_fails("not a workspace package")


class RealRepositoryTests(unittest.TestCase):
    def test_copy_set_holds_no_test_material(self) -> None:
        sources = GUARD.builder_copy_sources(ROOT)
        self.assertTrue(sources)
        for source in sources:
            with self.subTest(source=source):
                self.assertFalse(GUARD._is_test_data(source))

    def test_both_applications_pass_the_static_checks(self) -> None:
        notes = GUARD.static_checks(ROOT)
        self.assertEqual(len(notes), len(GUARD.workspace_packages(ROOT)) + 1)

    def test_production_programs_hold_no_test_files(self) -> None:
        for _name, directory in GUARD.workspace_packages(ROOT):
            program = GUARD.production_program(ROOT, directory)
            self.assertTrue(program, directory)
            for relative in program:
                with self.subTest(relative=relative):
                    self.assertNotIn(".test.", relative)
                    self.assertNotIn("/dev/", relative)
                    self.assertNotIn("/test/", relative)

    def test_materialised_context_excludes_test_material_and_build_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "context"
            copied = GUARD.materialise_context(ROOT, destination)
            self.assertTrue(copied)
            self.assertTrue((destination / "web/admin/src/main.tsx").is_file())
            self.assertTrue((destination / "pnpm-workspace.yaml").is_file())
            self.assertFalse((destination / "tests").exists())
            for relative in copied:
                with self.subTest(relative=relative):
                    self.assertNotIn("/node_modules/", relative)
                    self.assertNotIn("/dist/", relative)
