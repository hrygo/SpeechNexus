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
* every `deploy/compose.yaml` default for either key resolves to that value, so a
  stack started from the committed files cannot split the pair;
* the operator-facing entry point fails before the request (`tools` step) with a
  readable reason instead of surfacing the 503 at request time.

Counterexample, run and restored: setting
`SPEECHNEXUS_TRANSCRIPTION_INPUT_BUCKET=speechnexus-inputs` in the example turns
both file guards red; the original output is recorded in the PR body. The
`reference-acceptance.py` preflight has its own pair of cases
(`test_split_sync_upload_bucket_is_named_before_any_request` and
`test_shared_sync_upload_bucket_passes`).
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import re
import sys
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
# `KEY: ${KEY:-default}` in compose.yaml; the nested default must be a literal
# because Compose's nested interpolation is unreliable.
COMPOSE_DEFAULT = re.compile(
    r"^\s*(?P<key>[A-Z][A-Z0-9_]*):\s*\$\{(?P=key):-(?P<default>[^}]*)\}\s*$"
)


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


def compose_defaults() -> dict[str, list[str]]:
    """Map each bucket key to every interpolated default `compose.yaml` declares."""
    defaults: dict[str, list[str]] = {UPLOAD_BUCKET: [], ASSET_BUCKET: []}
    for line in COMPOSE.read_text(encoding="utf-8").splitlines():
        match = COMPOSE_DEFAULT.match(line)
        if match is None or match.group("key") not in defaults:
            continue
        defaults[match.group("key")].append(_clean(match.group("default")))
    return defaults


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
    """Both Compose services that carry the key must use the aligned literal."""

    def test_every_compose_default_resolves_to_the_asset_bucket(self) -> None:
        defaults = compose_defaults()
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


if __name__ == "__main__":
    unittest.main()
