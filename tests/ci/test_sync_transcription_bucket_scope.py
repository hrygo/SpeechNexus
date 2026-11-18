"""Keep the synchronous upload bucket on the gateway's single input bucket.

#972/DEL-539: the reference deployment started from `deploy/compose.env.example`
and `deploy/compose.yaml` defaults answered `503 backend_unavailable` for every
`POST /v1/audio/transcriptions`. The API wrote the upload to
`SPEECHNEXUS_TRANSCRIPTION_INPUT_BUCKET` (`speechnexus-inputs`) while the gateway
lane's private `objects.input_bucket` read `SPEECHNEXUS_OBJECT_STORAGE_BUCKET`
(`speechnexus-results`). The gateway compares the reference with its own
`objects.input_bucket` and raises `execution_input_scope_mismatch` *before* any
S3 or engine I/O — measured 16.4 ms after the dispatch mark, with no
`POST /v1/audio/transcriptions` in the engine log — so the synchronous route was
100% unavailable under the documented defaults.

The gateway has one `objects.input_bucket` per file lane today and serves both
Job inputs and synchronous uploads from it, so the two deployment keys must name
the same bucket. Each property that keeps them aligned is pinned here:

* `deploy/compose.env.example` assigns both keys one value;
* every `deploy/compose.yaml` default for either key resolves to that value, and a
  line that mentions a key in any other shape is reported instead of skipped — a
  new bare `KEY: value` pin cannot slip past the equality assertions;
* the operator-facing entry point fails before the request (`tools` step) with a
  readable reason instead of surfacing the 503 at request time — for the env pair
  and for the private lane `objects.input_bucket` when that file already exists;
* the bootstrap script still creates exactly one versioned bucket when the keys
  collapse onto one name.

Counterexample, run and restored: setting
`SPEECHNEXUS_TRANSCRIPTION_INPUT_BUCKET=speechnexus-inputs` in the example turns
the file guards red; a new literal compose pin is red too. The original output is
recorded in the PR body. The `reference-acceptance.py` preflight has its own cases
(`test_split_sync_upload_bucket_is_named_before_any_request`,
`test_shared_sync_upload_bucket_passes`,
`test_private_lane_input_bucket_must_match_the_env`,
`test_private_lane_input_bucket_equal_to_env_passes`).

This module stays standard-library-only: the `guards` CI job runs
`python3 -m unittest discover -s tests/ci` on the system interpreter with nothing
installed, so `_install_minio_stub()` stands in for the `minio` package that
`deploy/init-object-storage.py` imports at module scope. Under `uv run pytest` the
real package is used instead.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import re
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
ENV_EXAMPLE = ROOT / "deploy" / "compose.env.example"
COMPOSE = ROOT / "deploy" / "compose.yaml"
INIT_STORAGE = ROOT / "deploy" / "init-object-storage.py"

UPLOAD_BUCKET = "SPEECHNEXUS_TRANSCRIPTION_INPUT_BUCKET"
ASSET_BUCKET = "SPEECHNEXUS_OBJECT_STORAGE_BUCKET"

# `KEY=value` in the env example, with an optional `export ` prefix.
ENV_ASSIGNMENT = re.compile(
    rf"^(?:export\s+)?(?P<key>{UPLOAD_BUCKET}|{ASSET_BUCKET})=(?P<value>.*)$"
)

KEY_ALTERNATION = f"{UPLOAD_BUCKET}|{ASSET_BUCKET}"
KEY_MENTION = re.compile(rf"\b(?:{KEY_ALTERNATION})\b")
# The two legal Compose forms for these keys: a mapping or a list entry. The
# nested default must be a literal because Compose's nested interpolation is
# unreliable. Any other line that mentions a key is a problem, not ignorable.
COMPOSE_MAPPING_DEFAULT = re.compile(
    rf"^\s*(?P<key>{KEY_ALTERNATION}):\s*\$\{{(?P=key):-(?P<default>[^}}]*)\}}\s*(?:#.*)?$"
)
COMPOSE_LIST_DEFAULT = re.compile(
    rf"^\s*-\s*(?P<key>{KEY_ALTERNATION})=\$\{{(?P=key):-(?P<default>[^}}]*)\}}\s*(?:#.*)?$"
)


def _install_minio_stub() -> None:
    """Keep this module importable under the dependency-free `guards` job.

    That job runs `python3 -m unittest discover -s tests/ci` on the system
    interpreter and installs nothing, while `deploy/init-object-storage.py` does
    `from minio import Minio` at import time. The real package is used when it is
    present; otherwise three stub modules cover exactly the symbols the bootstrap
    script imports, and every test replaces `Minio` with a fake client anyway.
    """
    try:
        for name in ("minio.commonconfig", "minio.versioningconfig"):
            importlib.import_module(name)
        return
    except ModuleNotFoundError:
        pass
    minio = types.ModuleType("minio")
    minio.Minio = object  # type: ignore[attr-defined]
    commonconfig = types.ModuleType("minio.commonconfig")
    commonconfig.ENABLED = "Enabled"  # type: ignore[attr-defined]
    versioningconfig = types.ModuleType("minio.versioningconfig")

    def _versioning_config(status: object) -> SimpleNamespace:
        return SimpleNamespace(status=status)

    versioningconfig.VersioningConfig = _versioning_config  # type: ignore[attr-defined]
    minio.commonconfig = commonconfig  # type: ignore[attr-defined]
    minio.versioningconfig = versioningconfig  # type: ignore[attr-defined]
    sys.modules.setdefault("minio", minio)
    sys.modules.setdefault("minio.commonconfig", commonconfig)
    sys.modules.setdefault("minio.versioningconfig", versioningconfig)


_install_minio_stub()
_INIT_SPEC = importlib.util.spec_from_file_location("init_object_storage", INIT_STORAGE)
assert _INIT_SPEC is not None and _INIT_SPEC.loader is not None
INIT: Any = importlib.util.module_from_spec(_INIT_SPEC)
sys.modules[_INIT_SPEC.name] = INIT
_INIT_SPEC.loader.exec_module(INIT)


class FakeMinio:
    """Record the buckets `init-object-storage.py` would create and version."""

    def __init__(self) -> None:
        self.created: list[str] = []
        self.versioned: list[str] = []

    def bucket_exists(self, bucket: str) -> bool:
        return False

    def make_bucket(self, bucket: str) -> None:
        self.created.append(bucket)

    def set_bucket_versioning(self, bucket: str, config: object) -> None:
        self.versioned.append(bucket)

    def get_bucket_versioning(self, bucket: str) -> SimpleNamespace:
        return SimpleNamespace(status=INIT.ENABLED)


def run_init_storage(env: dict[str, str]) -> tuple[FakeMinio, str]:
    """Run the bootstrap script against a fake client; return what it touched."""
    client = FakeMinio()

    def factory(*_args: object, **_kwargs: object) -> FakeMinio:
        return client

    buffer = io.StringIO()
    with (
        mock.patch.dict(os.environ, env, clear=True),
        mock.patch.object(INIT, "Minio", factory),
        contextlib.redirect_stdout(buffer),
    ):
        INIT.main()
    return client, buffer.getvalue()


def _clean(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    return value


def env_example_values() -> dict[str, str]:
    """Map each bucket key to the value the example assigns it."""
    values: dict[str, str] = {}
    for number, line in enumerate(ENV_EXAMPLE.read_text(encoding="utf-8").splitlines(), start=1):
        match = ENV_ASSIGNMENT.match(line)
        if match is None:
            continue
        key = match.group("key")
        if key in values:
            raise AssertionError(f"{ENV_EXAMPLE.name}:{number} 重复赋值 {key}")
        values[key] = _clean(match.group("value"))
    return values


def compose_bucket_lines(path: Path = COMPOSE) -> tuple[dict[str, list[str]], list[str]]:
    """Parse every bucket-key line; a line in an unknown shape is a problem.

    Silently skipping a non-interpolated pin is how a new literal site could split
    the pair while every equality assertion still passed: the site would simply
    not be in the list. So a line that mentions either key must match the mapping
    or the list form, and anything else is reported with its line number.
    """
    defaults: dict[str, list[str]] = {UPLOAD_BUCKET: [], ASSET_BUCKET: []}
    problems: list[str] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if line.lstrip().startswith("#") or KEY_MENTION.search(line) is None:
            continue
        match = COMPOSE_MAPPING_DEFAULT.match(line) or COMPOSE_LIST_DEFAULT.match(line)
        if match is None:
            problems.append(
                f"{path.name}:{number} 提到桶键但不是 ${{KEY:-literal}} 形态: {line.strip()}"
            )
            continue
        defaults[match.group("key")].append(_clean(match.group("default")))
    return defaults, problems


def compose_defaults() -> dict[str, list[str]]:
    """Map each bucket key to every interpolated default `compose.yaml` declares."""
    return compose_bucket_lines()[0]


class ObjectStorageInitTest(unittest.TestCase):
    """Aligned names must still create and version exactly one bucket."""

    def test_shared_names_create_one_versioned_bucket(self) -> None:
        client, output = run_init_storage(
            {
                "MINIO_ENDPOINT": "http://minio:9000",
                "MINIO_ACCESS_KEY": "key",
                "MINIO_SECRET_KEY": "secret",
                UPLOAD_BUCKET: "speechnexus-results",
                ASSET_BUCKET: "speechnexus-results",
                "SPEECHNEXUS_EXECUTION_RESULT_BUCKET": "speechnexus-results",
            }
        )
        self.assertEqual(client.created, ["speechnexus-results"])
        self.assertEqual(client.versioned, ["speechnexus-results"])
        self.assertEqual(output, "bucket ready: speechnexus-results (versioning enabled)\n")

    def test_distinct_names_still_create_each_bucket(self) -> None:
        """The script keeps working when the operator leaves the old split pair."""
        client, _output = run_init_storage(
            {
                "MINIO_ENDPOINT": "http://minio:9000",
                "MINIO_ACCESS_KEY": "key",
                "MINIO_SECRET_KEY": "secret",
                UPLOAD_BUCKET: "speechnexus-inputs",
                ASSET_BUCKET: "speechnexus-results",
            }
        )
        self.assertEqual(client.created, ["speechnexus-inputs", "speechnexus-results"])
        self.assertEqual(client.versioned, ["speechnexus-inputs", "speechnexus-results"])


class ComposeEnvExampleBucketTest(unittest.TestCase):
    """The copyable environment file must not split the pair."""

    def test_env_example_assigns_both_keys_one_bucket(self) -> None:
        values = env_example_values()
        self.assertIn(UPLOAD_BUCKET, values, "env example 不再赋值同步上传桶")
        self.assertIn(ASSET_BUCKET, values, "env example 不再赋值 Job 资产桶")
        self.assertEqual(
            values[UPLOAD_BUCKET],
            values[ASSET_BUCKET],
            f"{UPLOAD_BUCKET} 必须等于 {ASSET_BUCKET}；分开会让同步转写固定 503",
        )


class ComposeYamlBucketDefaultsTest(unittest.TestCase):
    """Every Compose site that mentions a bucket key is a default, and aligned."""

    def test_every_compose_default_resolves_to_the_asset_bucket(self) -> None:
        defaults, problems = compose_bucket_lines()
        self.assertEqual(problems, [], "桶键出现在无法解析的形态里，可能被渲染成分叉的桶")
        # `minio-init` and `api` both declare the upload key, and the asset key
        # appears in those services and in the Artifact/staging ones. At least
        # two sites each keeps the bootstrap and the API on one bucket.
        self.assertGreaterEqual(len(defaults[UPLOAD_BUCKET]), 2, defaults[UPLOAD_BUCKET])
        self.assertGreaterEqual(len(defaults[ASSET_BUCKET]), 2, defaults[ASSET_BUCKET])
        self.assertEqual(len(set(defaults[UPLOAD_BUCKET])), 1, defaults[UPLOAD_BUCKET])
        self.assertEqual(len(set(defaults[ASSET_BUCKET])), 1, defaults[ASSET_BUCKET])
        self.assertEqual(
            defaults[UPLOAD_BUCKET][0],
            defaults[ASSET_BUCKET][0],
            f"{UPLOAD_BUCKET} 的 compose 默认值必须等于 {ASSET_BUCKET}",
        )

    def test_env_example_and_compose_defaults_agree(self) -> None:
        """A stack started without an env file must still get the example's bucket."""
        self.assertEqual(
            compose_defaults()[UPLOAD_BUCKET][0],
            env_example_values()[UPLOAD_BUCKET],
            "示例环境文件与 compose 默认值必须指向同一个桶",
        )

    def test_a_literal_pin_is_reported_instead_of_skipped(self) -> None:
        """Adding a bare `KEY: value` site must turn this guard red (#972 F2)."""
        with tempfile.TemporaryDirectory() as temporary:
            compose = Path(temporary) / "compose.yaml"
            compose.write_text(
                "services:\n"
                "  artifact-retention:\n"
                "    environment:\n"
                f"      {UPLOAD_BUCKET}: speechnexus-inputs\n",
                encoding="utf-8",
            )
            _defaults, problems = compose_bucket_lines(compose)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn(f"{UPLOAD_BUCKET}: speechnexus-inputs", problems[0])

    def test_both_legal_interpolation_shapes_are_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            compose = Path(temporary) / "compose.yaml"
            compose.write_text(
                "services:\n"
                "  api:\n"
                "    environment:\n"
                f"      {UPLOAD_BUCKET}: ${{{UPLOAD_BUCKET}:-speechnexus-results}}\n"
                "      env_file_list:\n"
                f"        - {ASSET_BUCKET}=${{{ASSET_BUCKET}:-speechnexus-results}}\n",
                encoding="utf-8",
            )
            defaults, problems = compose_bucket_lines(compose)
        self.assertEqual(problems, [])
        self.assertEqual(defaults[UPLOAD_BUCKET], ["speechnexus-results"])
        self.assertEqual(defaults[ASSET_BUCKET], ["speechnexus-results"])


if __name__ == "__main__":
    unittest.main()
