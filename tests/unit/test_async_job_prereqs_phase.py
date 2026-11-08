"""前置自检的三态判定:部署前 PENDING、部署后 FAIL、真实配置错误永远 FAIL.

`tests/unit/test_async_job_prereqs_check.py` 断言脚本复制的生产常量;本模块只测阶段语义:

- 桶名不一致是真实配置错误,`--phase pre` 也必须是 FAIL(最关键的不变量);
- 私有 gateway 配置不存在、引擎无 ready 实例属于部署产物,pre 阶段是 PENDING、post 阶段是 FAIL;
- DSN 为空、迁移落后 head、派发角色缺列权限在任何阶段都是 FAIL,不能被阶段降级。

合成输入只提供判定所需的最小证据,不代替真实部署。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
COMPOSE_PATH = ROOT / "deploy/compose.yaml"
COMPOSE_TEXT = COMPOSE_PATH.read_text(encoding="utf-8")
PHASES = ("pre", "post")
SPEC = importlib.util.spec_from_file_location(
    "check_async_job_prereqs_phase", ROOT / "scripts/dev-env/check-async-job-prereqs.py"
)


def checker() -> Any:
    assert SPEC is not None and SPEC.loader is not None
    module: Any = importlib.util.module_from_spec(SPEC)
    sys.modules[SPEC.name] = module
    SPEC.loader.exec_module(module)
    return module


def env_for(directory: Path, **extra: str) -> dict[str, str]:
    """最小 env:桶名与阶段判定用到的键,其余由调用方补充."""
    values = {
        "SPEECHNEXUS_PRIVATE_CONFIG_DIR": str(directory),
        "SPEECHNEXUS_EXECUTION_RESULT_BUCKET": "speechnexus-results",
        "SPEECHNEXUS_OBJECT_STORAGE_BUCKET": "speechnexus-results",
    }
    values.update(extra)
    return values


def write_gateway_configs(directory: Path, result_bucket: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for name in ("gateway-file-asr.json", "gateway-file-tts.json"):
        (directory / name).write_text(
            json.dumps({"objects": {"result_bucket": result_bucket}}), encoding="utf-8"
        )


def test_result_bucket_mismatch_fails_even_in_pre_phase(tmp_path: Path) -> None:
    """env 桶名与私有配置不一致时,部署前也必须是 FAIL 而不是 PENDING."""
    module = checker()
    directory = tmp_path / "private"
    write_gateway_configs(directory, "another-bucket")
    check = module.check_result_bucket(env_for(directory), COMPOSE_TEXT, COMPOSE_PATH, "pre")
    assert check.status == "FAIL"
    assert check.pending == []
    assert any(
        "result_bucket=another-bucket" in failure and "不一致" in failure
        for failure in check.failures
    )


def test_result_bucket_mismatch_fails_after_deploy(tmp_path: Path) -> None:
    """已部署且私有配置已生成,桶名写错就是 FAIL,不因"已部署"而改变判定."""
    module = checker()
    directory = tmp_path / "private"
    write_gateway_configs(directory, "another-bucket")
    check = module.check_result_bucket(env_for(directory), COMPOSE_TEXT, COMPOSE_PATH, "post")
    assert check.status == "FAIL"
    assert any("不一致" in failure for failure in check.failures)


def test_missing_private_config_is_pending_before_deploy(tmp_path: Path) -> None:
    """私有配置尚未生成 → pre 阶段 PENDING;同一输入在 post 阶段必须是 FAIL."""
    module = checker()
    directory = tmp_path / "not-generated"
    pre = module.check_result_bucket(env_for(directory), COMPOSE_TEXT, COMPOSE_PATH, "pre")
    assert pre.status == "PENDING"
    assert pre.failures == []
    assert any("尚未生成" in item for item in pre.pending)

    post = module.check_result_bucket(env_for(directory), COMPOSE_TEXT, COMPOSE_PATH, "post")
    assert post.status == "FAIL"
    assert any("尚未生成" in item for item in post.failures)


def test_matching_private_config_passes_in_both_phases(tmp_path: Path) -> None:
    """配置一致时 pre/post 都是 PASS,证明 PENDING 只针对未生成的产物."""
    module = checker()
    directory = tmp_path / "private"
    write_gateway_configs(directory, "speechnexus-results")
    for phase in PHASES:
        check = module.check_result_bucket(env_for(directory), COMPOSE_TEXT, COMPOSE_PATH, phase)
        assert check.status == "PASS"


@pytest.mark.parametrize("phase, expected", [("pre", "PENDING"), ("post", "FAIL")])
def test_engine_recipe_depends_on_deploy_phase(tmp_path: Path, phase: str, expected: str) -> None:
    """前置 4 的私有配方同样按阶段判定;env 里的引擎身份错误仍走 FAIL."""
    module = checker()
    env = env_for(
        tmp_path / "not-generated",
        SPEECHNEXUS_ENGINE_HEALTH_URL="http://127.0.0.1:18100",
        SPEECHNEXUS_ENGINE_INSTANCE_ID="instance-asr-1",
        SPEECHNEXUS_ENGINE_GENERATION="1",
    )
    check = module.check_engine(env, COMPOSE_PATH, True, phase)
    assert check.status == expected


def test_only_engine_and_bucket_are_post_phase(tmp_path: Path) -> None:
    """前置 1/2/3/6 的 required_phase 是 pre,不得被阶段降级."""
    module = checker()
    checks = module.build_checks(
        env_for(tmp_path / "private"), COMPOSE_TEXT, COMPOSE_PATH, True, "pre"
    )
    assert {check.number: check.required_phase for check in checks} == {
        1: "pre",
        2: "pre",
        3: "pre",
        4: "post",
        5: "post",
        6: "pre",
    }


@pytest.mark.parametrize("phase", PHASES)
def test_empty_dsns_fail_in_every_phase(tmp_path: Path, phase: str) -> None:
    """DSN 为空是配置错误:pre 与 post 都是 FAIL."""
    module = checker()
    env = env_for(tmp_path / "private")
    assert module.check_api_job_dsns(env, COMPOSE_TEXT, True, phase).status == "FAIL"
    assert module.check_migration(env, True, phase).status == "FAIL"
    assert module.check_dispatch_role(env, COMPOSE_TEXT, True, phase).status == "FAIL"


@pytest.mark.parametrize("phase", PHASES)
def test_migration_behind_head_fails_in_every_phase(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, phase: str
) -> None:
    """迁移落后 head 是真实配置错误,不能被阶段降级为 PENDING."""
    module = checker()
    tables = ",".join(module.REQUIRED_JOB_TABLES)

    def select(_dsn: object, _env: object, statement: str, _timeout: int = 12) -> object:
        if "string_agg(version_num" in statement:
            return module.PsqlResult(True, True, "0046_public_job_intents", "")
        if "count(*)" in statement:
            return module.PsqlResult(True, True, "1", "")
        if "pg_class" in statement:
            return module.PsqlResult(True, True, tables, "")
        raise AssertionError(f"unexpected statement: {statement}")

    monkeypatch.setattr(
        module, "probe_database", lambda *_args: module.PsqlResult(True, True, "", "")
    )
    monkeypatch.setattr(module, "psql_select", select)
    env = env_for(
        tmp_path / "private",
        SPEECHNEXUS_MIGRATION_DATABASE_URL=(
            "postgresql://sn_migrator:pw@127.0.0.1:55432/speechnexus"
        ),
    )
    check = module.check_migration(env, False, phase)
    assert check.status == "FAIL"
    assert any("落后 head" in failure for failure in check.failures)


@pytest.mark.parametrize("phase", PHASES)
def test_missing_dispatch_column_grant_fails_in_every_phase(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, phase: str
) -> None:
    """派发角色缺列级授权是真实权限缺失,pre 与 post 都是 FAIL."""
    module = checker()

    def select(_dsn: object, _env: object, statement: str, _timeout: int = 12) -> object:
        if "SESSION_USER" in statement:
            return module.PsqlResult(True, True, "sn_job_dispatch|sn_job_dispatch", "")
        if "AS forbidden" in statement:
            return module.PsqlResult(True, True, "f", "")
        if "has_column_privilege" in statement:
            return module.PsqlResult(True, True, "missing UPDATE on speech_jobs.error_code", "")
        if "LIMIT 0" in statement:
            return module.PsqlResult(True, True, "", "")
        raise AssertionError(f"unexpected statement: {statement}")

    monkeypatch.setattr(
        module, "probe_database", lambda *_args: module.PsqlResult(True, True, "", "")
    )
    monkeypatch.setattr(module, "psql_select", select)
    env = env_for(
        tmp_path / "private",
        SPEECHNEXUS_JOB_WRITER_DATABASE_URL=(
            "postgresql://sn_job_writer:pw@postgres:5432/speechnexus"
        ),
        SPEECHNEXUS_JOB_ADMISSION_DATABASE_URL=(
            "postgresql://sn_job_admission:pw@postgres:5432/speechnexus"
        ),
        SPEECHNEXUS_JOB_DISPATCH_DATABASE_URL=(
            "postgresql://sn_job_dispatch:pw@postgres:5432/speechnexus"
        ),
    )
    check = module.check_dispatch_role(env, COMPOSE_TEXT, False, phase)
    assert check.status == "FAIL"
    assert any("error_code" in failure for failure in check.failures)
