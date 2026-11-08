"""前置自检的三态判定:部署前 PENDING、部署后 FAIL、真实配置错误永远 FAIL.

`tests/unit/test_async_job_prereqs_check.py` 断言脚本复制的生产常量;本模块只测阶段语义:

- 桶名不一致是真实配置错误,`--phase pre` 也必须是 FAIL(最关键的不变量);
- 私有 gateway 配置未生成、引擎实例行查不到属于部署产物,pre 阶段是 PENDING、post 阶段是 FAIL;
- 实例行已存在但 `instance_state` 不是 `ready` 是部署后的活证据,任何阶段都是 FAIL;
- DSN 为空、迁移落后 head、派发角色缺列权限在任何阶段都是 FAIL,不能被阶段降级;
- 阶段判定本身(`resolve_phase` 的 auto 信号与显式覆盖)和 `main()` 的退出码有独立用例,
  恒 `pre` 或忽略 `--phase` 的变异必须变红。

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


def deployed_engine_env(directory: Path) -> dict[str, str]:
    """引擎前置所需的最小 env;配方与私有配置由调用方决定是否生成."""
    return env_for(
        directory,
        SPEECHNEXUS_ENGINE_HEALTH_URL="http://127.0.0.1:1",
        SPEECHNEXUS_ENGINE_INSTANCE_ID="instance-asr-1",
        SPEECHNEXUS_ENGINE_GENERATION="1",
        SPEECHNEXUS_MIGRATION_DATABASE_URL=(
            "postgresql://sn_migrator:pw@127.0.0.1:55432/speechnexus"
        ),
    )


def patch_engine_database(monkeypatch: pytest.MonkeyPatch, module: Any, rows_output: str) -> None:
    """让引擎前置在没有真实数据库时也能走完注册事实查询."""

    def select(_dsn: object, _env: object, statement: str, _timeout: int = 12) -> object:
        if "engine_instances" in statement:
            return module.PsqlResult(True, True, rows_output, "")
        raise AssertionError(f"unexpected statement: {statement}")

    def unreachable(*_args: object, **_kwargs: object) -> object:
        raise module.urllib.error.URLError("test: engine health unreachable")

    monkeypatch.setattr(
        module, "probe_database", lambda *_args: module.PsqlResult(True, True, "", "")
    )
    monkeypatch.setattr(module, "psql_select", select)
    monkeypatch.setattr(module.urllib.request, "urlopen", unreachable)


@pytest.mark.parametrize("phase", PHASES)
def test_engine_instance_not_ready_fails_in_every_phase(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, phase: str
) -> None:
    """实例行已存在却不是 ready:部署已发生,这是 FAIL,不是 PENDING."""
    module = checker()
    directory = tmp_path / "private"
    directory.mkdir()
    (directory / "transcription-recipe.json").write_text("{}", encoding="utf-8")
    patch_engine_database(monkeypatch, module, "1|draining")
    check = module.check_engine(deployed_engine_env(directory), COMPOSE_PATH, False, phase)
    assert check.status == "FAIL"
    assert check.pending == []
    assert any("instance_state=draining" in failure for failure in check.failures)


@pytest.mark.parametrize("phase, expected", [("pre", "PENDING"), ("post", "FAIL")])
def test_engine_unregistered_instance_depends_on_phase(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, phase: str, expected: str
) -> None:
    """查不到实例行才是"尚未注册"的部署产物;post 阶段同样记 FAIL."""
    module = checker()
    directory = tmp_path / "private"
    directory.mkdir()
    (directory / "transcription-recipe.json").write_text("{}", encoding="utf-8")
    patch_engine_database(monkeypatch, module, "")
    check = module.check_engine(deployed_engine_env(directory), COMPOSE_PATH, False, phase)
    assert check.status == expected
    assert any("未注册" in item for item in [*check.pending, *check.failures])


def make_compose(tmp_path: Path) -> Path:
    """把真实 compose 复制到临时目录,让 .runtime 信号不依赖本仓库状态."""
    path = tmp_path / "compose.yaml"
    path.write_text(COMPOSE_TEXT, encoding="utf-8")
    return path


def test_resolve_phase_without_signals_is_pre(tmp_path: Path) -> None:
    module = checker()
    decision = module.resolve_phase("auto", {}, make_compose(tmp_path), COMPOSE_TEXT, True)
    assert decision.phase == "pre"
    assert "尚未部署" in decision.reason


def test_resolve_phase_private_config_is_post(tmp_path: Path) -> None:
    module = checker()
    directory = tmp_path / "private"
    write_gateway_configs(directory, "speechnexus-results")
    decision = module.resolve_phase(
        "auto", env_for(directory), make_compose(tmp_path), COMPOSE_TEXT, True
    )
    assert decision.phase == "post"
    assert "私有配置目录" in decision.reason


def test_resolve_phase_scaffolding_dirs_stay_pre(tmp_path: Path) -> None:
    """`compose-minimal.md` 第 2 节的 `mkdir -p` 只是空骨架,不等于部署过."""
    module = checker()
    compose = make_compose(tmp_path)
    for name in ("private", "gateway-locks", "gateway-spool"):
        (tmp_path / ".runtime" / name).mkdir(parents=True)
    decision = module.resolve_phase("auto", {}, compose, COMPOSE_TEXT, True)
    assert decision.phase == "pre"


def test_resolve_phase_hidden_files_alone_stay_pre(tmp_path: Path) -> None:
    """`.DS_Store` 之类隐藏文件不是部署产物."""
    module = checker()
    compose = make_compose(tmp_path)
    runtime = tmp_path / ".runtime"
    runtime.mkdir()
    (runtime / ".DS_Store").write_bytes(b"\x00")
    decision = module.resolve_phase("auto", {}, compose, COMPOSE_TEXT, True)
    assert decision.phase == "pre"


def test_resolve_phase_generated_runtime_file_is_post(tmp_path: Path) -> None:
    module = checker()
    compose = make_compose(tmp_path)
    runtime = tmp_path / ".runtime"
    (runtime / "private").mkdir(parents=True)
    (runtime / "private" / "transcription-recipe.json").write_text("{}", encoding="utf-8")
    decision = module.resolve_phase("auto", {}, compose, COMPOSE_TEXT, True)
    assert decision.phase == "post"
    assert ".runtime" in decision.reason


def test_resolve_phase_containers_signal_post(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = checker()
    monkeypatch.setattr(
        module, "docker_containers", lambda _project: [("cid", "gateway", "running")]
    )
    decision = module.resolve_phase("auto", {}, make_compose(tmp_path), COMPOSE_TEXT, False)
    assert decision.phase == "post"
    assert "容器" in decision.reason


def test_explicit_pre_overrides_deploy_signals(tmp_path: Path) -> None:
    module = checker()
    compose = make_compose(tmp_path)
    runtime = tmp_path / ".runtime"
    (runtime / "private").mkdir(parents=True)
    (runtime / "private" / "transcription-recipe.json").write_text("{}", encoding="utf-8")
    decision = module.resolve_phase("pre", {}, compose, COMPOSE_TEXT, True)
    assert decision.phase == "pre"
    assert "--phase pre" in decision.reason


def test_explicit_post_overrides_clean_checkout(tmp_path: Path) -> None:
    module = checker()
    decision = module.resolve_phase("post", {}, make_compose(tmp_path), COMPOSE_TEXT, True)
    assert decision.phase == "post"
    assert "--phase post" in decision.reason


ONLY_PENDING_ENV = {
    "SPEECHNEXUS_JOB_WRITER_DATABASE_URL": "postgresql://sn_job_writer:pw@postgres:5432/speechnexus",
    "SPEECHNEXUS_JOB_ADMISSION_DATABASE_URL": (
        "postgresql://sn_job_admission:pw@postgres:5432/speechnexus"
    ),
    "SPEECHNEXUS_JOB_DISPATCH_DATABASE_URL": (
        "postgresql://sn_job_dispatch:pw@postgres:5432/speechnexus"
    ),
    "SPEECHNEXUS_MIGRATION_DATABASE_URL": ("postgresql://sn_migrator:pw@postgres:5432/speechnexus"),
    "SPEECHNEXUS_ENGINE_HEALTH_URL": "http://127.0.0.1:18100",
    "SPEECHNEXUS_ENGINE_INSTANCE_ID": "instance-asr-1",
    "SPEECHNEXUS_ENGINE_GENERATION": "1",
    "SPEECHNEXUS_EXECUTION_RESULT_BUCKET": "speechnexus-results",
    "SPEECHNEXUS_OBJECT_STORAGE_BUCKET": "speechnexus-results",
}


def write_env_file(tmp_path: Path, values: dict[str, str]) -> Path:
    path = tmp_path / "compose.env"
    path.write_text(
        "\n".join(f"{key}={value}" for key, value in values.items()) + "\n", encoding="utf-8"
    )
    return path


def test_main_only_pending_exits_zero_and_explicit_post_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """端到端:`--phase post` 是门禁逃生阀,不能被 main 静默忽略."""
    module = checker()
    compose = make_compose(tmp_path)
    env_path = write_env_file(tmp_path, ONLY_PENDING_ENV)
    args = ["--env-file", str(env_path), "-f", str(compose), "--offline"]

    assert module.main([*args, "--phase", "pre"]) == 0
    pre_output = capsys.readouterr().out
    assert "2 PENDING" in pre_output
    assert "0 FAIL" in pre_output
    assert "尚未到该阶段" in pre_output

    assert module.main([*args, "--phase", "post"]) == 1
    post_output = capsys.readouterr().out
    assert "0 PENDING" in post_output
    assert "2 FAIL" in post_output


def test_invalid_private_json_names_the_parse_error(tmp_path: Path) -> None:
    """坏 JSON 与缺字段要分开提示:前者修文件,后者修字段."""
    module = checker()
    directory = tmp_path / "private"
    directory.mkdir()
    (directory / "gateway-file-asr.json").write_text("{not json", encoding="utf-8")
    check = module.check_result_bucket(env_for(directory), COMPOSE_TEXT, COMPOSE_PATH, "post")
    assert check.status == "FAIL"
    assert any("不是合法 JSON" in failure for failure in check.failures)


def test_private_json_without_result_bucket_names_the_field(tmp_path: Path) -> None:
    module = checker()
    directory = tmp_path / "private"
    directory.mkdir()
    (directory / "gateway-file-asr.json").write_text(json.dumps({"objects": {}}), encoding="utf-8")
    check = module.check_result_bucket(env_for(directory), COMPOSE_TEXT, COMPOSE_PATH, "post")
    assert check.status == "FAIL"
    assert any("objects.result_bucket" in failure for failure in check.failures)
