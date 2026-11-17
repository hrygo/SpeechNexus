"""The one source of the startup-gate failure-output table (#928).

Every deployment unit that can refuse to start before it serves has exactly one row
here: which stage the refusal runs in (synchronous assembly, ASGI lifespan, or the
entry point's own ``main()``), what it really prints and exits with when measured at
the process level, what the operator documents promise, whether the two agree, and
which disposition was chosen. ``docs/operations/startup-gate-output.md`` carries the
same rows as a table; ``tests/ci/test_startup_gate_output_map.py`` renders the table
from this module and compares the text, so a row cannot be edited on one side only.

This module is the single source of the *table*, not of the messages: it is a test
file. The messages a row quotes verbatim are therefore declared in ``quotes`` as
``(literal, file)`` and verified against that product file, so a row cannot quote a
line the product no longer prints (#928 review A1). Entries whose file starts with
``external:`` name a pinned third-party message (uvicorn, alembic) that this
repository does not own; they are declared rather than skipped, so nothing loses
verification silently.

A row whose ``promise_kind`` is an output shape also names the process-level case
that starts the real entry point, and the same guard checks that such a case exists
in a pytest module. ``#920``'s failure mode was a test that mocked the entry instead
of starting it; naming the case here keeps the promise and its evidence together.

This module is text and standard library only: it never imports the backend, touches
PostgreSQL or starts a container, so ``python3 -m unittest discover -s tests/ci``
runs it under the system interpreter like every other guard in this directory.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

ONE_FAIL_LINE = "一行 FAIL:"
TRACEBACK_SHAPE = "traceback + 退出码"
EXACT_LINE = "一行具名消息"
NO_PROMISE = "无格式承诺"
UNVERIFIED = "未验证"
SHAPE_KINDS = frozenset({ONE_FAIL_LINE, TRACEBACK_SHAPE, EXACT_LINE})

# Where a shape promise must be proven by starting the real entry point. A test that
# calls the entry function under a monkeypatched collaborator is not evidence (#920).
PROCESS_TEST_MODULES = (
    "tests/unit/backend/test_startup_gate_process_output.py",
    "tests/integration/test_startup_gate_output_pg.py",
)


@dataclass(frozen=True)
class Gate:
    """One unit's startup refusal, measured once and decided once."""

    slug: str
    unit: str
    stage: str
    observed: str
    promise: str
    agrees: str
    disposition: str
    promise_kind: str = NO_PROMISE
    case: str | None = None
    quotes: tuple[tuple[str, str], ...] = ()


GATES: tuple[Gate, ...] = (
    Gate(
        slug="api_job_schema",
        unit="`api`（`speechnexus-api`）· Job 迁移与角色闸门",
        stage="ASGI lifespan（`job_schema_preflight` 的 `require_migrated_schema`）",
        observed="uvicorn 打印 `ERROR: Traceback …` 与 `Application startup failed. Exiting.` "
        "后以退出码 `3` 结束；没有 `FAIL:` 行；末行异常消息就是 `JOB_PREREQUISITES` 的 "
        "`schema_not_migrated` 原文 `The Job database has not run the migration profile: a "
        "required relation is missing, so no Job can be stored.`",
        promise="`compose-minimal.md` §9.1：启动期是 traceback + 退出码 3、"
        "**不是**一行 `FAIL:`；一行 `FAIL:` 只属于同步装配的结算预检",
        agrees="是",
        disposition="`#924` 已把文档写成实测为真；本卡补进程级用例钉住「traceback + 退出码 3"
        " + 无 FAIL 行」，并把 `async-job-runbook.md` §1 行 2 的过期承诺改成同一句。"
        "若要把它挪到入口换一行 `FAIL:`，须另立卡（启动顺序与二次连接）",
        promise_kind=TRACEBACK_SHAPE,
        case="test_api_job_schema_gate_fails_in_the_lifespan_with_exit_three",
        quotes=(
            (
                "The Job database has not run the migration profile: a required "
                "relation is missing, so no Job can be stored.",
                "backend/src/speechnexus_backend/speech/domain/job_prerequisites.py",
            ),
            ("Application startup failed. Exiting.", "external:uvicorn 0.54.0"),
        ),
    ),
    Gate(
        slug="api_settlement",
        unit="`api` · 同步 TTS 结算预检",
        stage="同步装配（`speech_runtime.build_speech`）",
        observed="一行 `FAIL:`，内容是 `the settled-capacity preflight cannot read the "
        "admission tables` 加关系名与迁移命令；退出码 `1`，无 traceback",
        promise="`async-job-runbook.md` §8 表：日志行 `FAIL: the settled-capacity "
        "preflight …`、退出码 `1`",
        agrees="是",
        disposition="补进程级用例钉住（`tests/integration`，需要隔离 PostgreSQL 的空 schema）",
        promise_kind=ONE_FAIL_LINE,
        case="test_api_settlement_gate_prints_one_fail_line_and_no_traceback",
        quotes=(
            (
                "the settled-capacity preflight cannot read the admission tables",
                "backend/src/speechnexus_backend/speech/infrastructure/tts_settlement_preflight.py",
            ),
        ),
    ),
    Gate(
        slug="api_recipe",
        unit="`api` · 私有配方加载（TTS 与转写两个 loader）",
        stage="同步装配（factory 内 `load_tts_recipe` / `load_transcription_recipe`）",
        observed="实测（TTS 配方）：`Traceback …` 后跟 `Invalid private speech admission "
        "recipe` 与绝对路径，退出码 `1`；转写 loader 用同一包装抛 "
        "`Invalid private transcription admission recipe`，"
        "未单独起进程复现（结算预检先于它拒绝空 schema）",
        promise="`compose-minimal.md` §9 与 `async-job-runbook.md` §5 只写「API 启动报"
        " recipe digest / 同名原因」，未承诺输出形态与退出码",
        agrees="是（无格式承诺）",
        disposition="改文档：在本表承认 traceback、退出码与绝对路径。理由：这条失败来自"
        "部署方手改的私有文件，消息已点名失败类；包一层 `FAIL:` 只会重复同一句，"
        "还会把「配方不可用」与「库未迁移」两类混淆",
        quotes=(
            (
                "Invalid private speech admission recipe",
                "backend/src/speechnexus_backend/speech/infrastructure/tts_recipe.py",
            ),
            (
                "Invalid private transcription admission recipe",
                "backend/src/speechnexus_backend/speech/infrastructure/transcription_recipe.py",
            ),
        ),
    ),
    Gate(
        slug="api_settings",
        unit="`api` · 设置校验",
        stage="入口 `main()`（`Settings()`）",
        observed="`Traceback …` 后跟 pydantic 的 `ValidationError`，退出码 `1`",
        promise="`compose-minimal.md`：某些键非法时「stops API startup」，未承诺输出形态",
        agrees="是（无格式承诺）",
        disposition="改文档：在本表承认 traceback 与退出码。理由：pydantic 已点名键与规则，"
        "且这段在任何 uvicorn 装配之前运行",
    ),
    Gate(
        slug="api_lifespan_runtimes",
        unit="`api` · 其余 lifespan 运行时（binding list / catalogue / audit 等）",
        stage="ASGI lifespan（`run_in_threadpool(<runtime>.open)`）",
        observed="uvicorn 打印 `ERROR: Traceback …`（形如 `<runtime> startup rejected`）"
        "后以退出码 `3` 结束，无 `FAIL:` 行",
        promise="无（文档只描述这些身份与它覆盖的读面）",
        agrees="是（无格式承诺）",
        disposition="改文档：承认 lifespan 阶段一律 traceback + 退出码 `3`。理由："
        "uvicorn 0.54.0 的 `Server.startup()` 在 lifespan 失败时 `sys.exit(3)`，"
        "入口的 `except` 到不了那个 raise 点",
    ),
    Gate(
        slug="gateway_settings",
        unit="`execution-gateway`（`speechnexus-gateway`）· 设置校验",
        stage="入口 `main()`（`Settings()`）",
        observed="`Traceback …` 后跟 pydantic 的 `ValidationError`，退出码 `1`",
        promise="`compose-minimal.md` §9 只点名原因文案，未承诺输出形态",
        agrees="是（无格式承诺）",
        disposition="改文档：承认 traceback 与退出码。理由：uvicorn 之前的配置拒绝，"
        "没有第二个可执行阶段可挪",
    ),
    Gate(
        slug="gateway_file_speech",
        unit="`execution-gateway` · 文件语音配置（四条 lane）",
        stage="同步装配（factory `create_app` 的 `load_file_speech_configuration`）",
        observed="`Traceback …` 末行是 ValueError 与 `invalid gateway file ASR configuration`，"
        "退出码 `1`",
        promise="`compose-minimal.md` §9：`gateway 退出并报 invalid gateway file ASR "
        "configuration`，未承诺输出形态",
        agrees="是（无格式承诺）",
        disposition="改文档：承认 traceback 与退出码，说明原因文案在 traceback 末行。"
        "理由：拒绝发生在同步装配阶段，改写成一行 `FAIL:` 会新增一条要单独维护的承诺",
        quotes=(
            (
                "invalid gateway file ASR configuration",
                "execution-gateway/src/speechnexus_gateway/file_asr_runtime.py",
            ),
        ),
    ),
    Gate(
        slug="gateway_control",
        unit="`execution-gateway` · 控制面（barrier / 查询 / ingress）装配",
        stage="ASGI lifespan（`_build_control`）",
        observed="控制面数据库不可达时 uvicorn 打印 `ERROR: Traceback …` 与 "
        "`Application startup failed. Exiting.` 后以退出码 `3` 结束，无 `FAIL:` 行；"
        "traceback 内含驱动原文",
        promise="无（文档未承诺控制面装配失败时的输出形态）",
        agrees="是（无格式承诺）",
        disposition="改文档：由本行把「控制面失败是 traceback + 退出码 3」写成实测结论，"
        "不再与 VAD 文案共用「未验证」",
        quotes=(("Application startup failed. Exiting.", "external:uvicorn 0.54.0"),),
    ),
    Gate(
        slug="gateway_vad",
        unit="`execution-gateway` · 固定 CPU VAD 运行时",
        stage="ASGI lifespan（`silero_onnx` 初始化）",
        observed="未验证：需要镜像内的 Silero ONNX 与固定依赖，本机 worktree 无法复现",
        promise="`compose-minimal.md` §9：`gateway 报 cannot initialize the pinned CPU VAD "
        "runtime`",
        agrees="未验证",
        disposition="记录为未验证项：只差这一句文案的实测；同阶段的控制面失败已由本表 "
        "`gateway_control` 行实测为 traceback + 退出码 `3`",
        promise_kind=UNVERIFIED,
        quotes=(
            (
                "cannot initialize the pinned CPU VAD runtime",
                "execution-gateway/src/speechnexus_gateway/infrastructure/silero_onnx.py",
            ),
        ),
    ),
    Gate(
        slug="node_controller",
        unit="`node-controller`（默认 Compose 不启动）",
        stage="入口 `main()`（`Settings()`）与 factory",
        observed="`Traceback …` 后跟 pydantic 的 `ValidationError`（设置）或 factory 拒绝，"
        "退出码 `1`",
        promise="`docs/components/node-controller.md`：启动即校验配置，未承诺输出形态",
        agrees="是（无格式承诺）",
        disposition="改文档：承认 traceback 与退出码。理由：本单元不在默认 Compose 内，"
        "没有部署文档承诺失败形态",
    ),
    Gate(
        slug="asset_inspector",
        unit="`asset-inspector`（`speechnexus-asset-inspector`）",
        stage="入口 `main()`（`InspectionSettings.from_env` 与运行时路径检查）",
        observed="一行 `asset inspector configuration error:` 加具体规则（如 "
        "`must be a non-empty bearer token`）到 stderr，退出码 `1`，无 traceback；"
        "也可为 `asset inspector runtime root is missing:` 或 "
        "`asset inspector prlimit is missing:` 加路径",
        promise="`async-job-runbook.md` 实测表：退出码 `1` 加同一句 "
        "`asset inspector configuration error` 消息",
        agrees="是",
        disposition="补进程级用例钉住这行（`EXACT_LINE`），并说明它不是 `FAIL:` 前缀",
        promise_kind=EXACT_LINE,
        case="test_asset_inspector_reports_one_line_and_no_traceback",
        quotes=(
            (
                "asset inspector configuration error:",
                "asset-inspector/src/speechnexus_asset_inspector/main.py",
            ),
            (
                "must be a non-empty bearer token",
                "asset-inspector/src/speechnexus_asset_inspector/settings.py",
            ),
            (
                "asset inspector runtime root is missing:",
                "asset-inspector/src/speechnexus_asset_inspector/main.py",
            ),
            (
                "asset inspector prlimit is missing:",
                "asset-inspector/src/speechnexus_asset_inspector/main.py",
            ),
        ),
    ),
    Gate(
        slug="job_dispatch_worker_gate",
        unit="`job-dispatch-worker` · 入口闸门 `deploy/job-startup-gate.py`",
        stage="入口脚本 `main()`（`exec` 之前）",
        observed="每个缺失项一行 `FAIL:`（含具名条件，stdout 上带两格缩进）加末行汇总，"
        "退出码 `1`，失败时不 `exec`；连接失败一行只说 `cannot connect to PostgreSQL` "
        "与异常类型名，不回显驱动原文；schema 非法时一行 `FAIL:` 含 "
        "`is not a safe lowercase SQL identifier`",
        promise="`compose-minimal.md` §6、`async-job-runbook.md` §1/§8/§12：`FAIL:` 点名"
        "并以非零退出",
        agrees="是",
        disposition="补进程级用例钉住；并修两处与承诺相悖的失败面：非法 "
        "`SPEECHNEXUS_DATABASE_SCHEMA` 曾是 ValueError traceback（现在点名该键），"
        "连接失败行曾回显主机、端口与库名（现在只给异常类型名与人话，"
        "因为 psycopg 连接失败时 `sqlstate` 恒为 None）。点名的对象是键名、桶名、"
        "schema 与表名，以及该容器自己的私有配置目录路径（容器内路径）",
        promise_kind=ONE_FAIL_LINE,
        case="test_job_dispatch_gate_names_missing_prerequisites_without_a_traceback",
        quotes=(
            ("cannot connect to PostgreSQL", "deploy/job-startup-gate.py"),
            (
                "is not a safe lowercase SQL identifier",
                "deploy/job-startup-gate.py",
            ),
        ),
    ),
    Gate(
        slug="job_dispatch_worker_main",
        unit="`job-dispatch-worker` 自身入口 `speechnexus-job-dispatch-worker`",
        stage="入口 `main()`（`JobDispatchSettings` 与依赖装配）",
        observed="配置不全时一行 `Job dispatch unavailable; no lease was claimed.` 到 stderr、"
        "退出码 `1`；配置通过但依赖装不起来时同一前缀 `Job dispatch unavailable:` 后接 "
        "`planner and result publisher are not assembled;` 再以 `no lease was claimed.` "
        "收尾、退出码 `2`；两种情况都无 traceback 且不回显 DSN",
        promise="无（文档未承诺该入口的失败输出形态）",
        agrees="是（无格式承诺）",
        disposition="改文档：由本行记录这两条具名拒绝与退出码 `1`/`2`，并补进程级用例钉住；"
        "不新增 `FAIL:` 承诺（它刻意不用 `FAIL:` 前缀）",
        case="test_job_dispatch_worker_refuses_without_settings_in_one_line",
        quotes=(
            (
                "Job dispatch unavailable; no lease was claimed.",
                "backend/src/speechnexus_backend/job_dispatch_worker.py",
            ),
            (
                "planner and result publisher are not assembled;",
                "backend/src/speechnexus_backend/job_dispatch_worker.py",
            ),
            (
                "Job dispatch unavailable:",
                "backend/src/speechnexus_backend/job_dispatch_worker.py",
            ),
            (
                "no lease was claimed.",
                "backend/src/speechnexus_backend/job_dispatch_worker.py",
            ),
        ),
    ),
    Gate(
        slug="artifact_retention",
        unit="`artifact-retention`（`speechnexus-artifact-retention`）",
        stage="入口 `main()`（`Settings()` 与 `runtime.open()`）",
        observed="`Traceback …` 后跟 `ValidationError` 或启动拒绝消息，退出码 `1`",
        promise="`compose-minimal.md` §6 透传表：空值「容器启动即失败」、宽身份「启动点名"
        "拒绝」，未承诺形态",
        agrees="是（无格式承诺）",
        disposition="改文档：承认 traceback 与退出码。理由：拒绝消息已点名角色与关系",
    ),
    Gate(
        slug="workbench_migrate",
        unit="`workbench-migrate`（`speechnexus-workbench-migrate`）",
        stage="入口 `main()`",
        observed="每个问题一行 `FAIL:` 到 stderr，退出码 `1`，无 traceback；owner 连接失败为"
        " `the owner connection could not complete the workbench migration` 加异常类型名",
        promise="`compose-minimal.md` §6：`FAIL: <具名条件>` 并以非零退出",
        agrees="是",
        disposition="补进程级用例钉住",
        promise_kind=ONE_FAIL_LINE,
        case="test_workbench_migrate_names_missing_inputs_without_a_traceback",
        quotes=(
            (
                "the owner connection could not complete the workbench migration",
                "backend/src/speechnexus_backend/workbench/migrate.py",
            ),
        ),
    ),
    Gate(
        slug="workbench_api",
        unit="`workbench-api` · 入口闸门 `deploy/workbench-startup-gate.py`",
        stage="入口脚本（`exec` 之前；`check_database` / `check_cleanup_identity`）",
        observed="每个问题一行 `FAIL:`；设置加载被拒为 `the workbench configuration was "
        "refused at load` 加异常类型名，运行期检查失败为一行 `FAIL:` 加 "
        "`the workbench runtime database was refused at startup:`；退出码 `1`，"
        "失败时不 `exec`",
        promise="`compose-minimal.md` §6：`FAIL:` 点名并以非零退出",
        agrees="是",
        disposition="补进程级用例钉住",
        promise_kind=ONE_FAIL_LINE,
        case="test_workbench_gate_names_missing_inputs_without_a_traceback",
        quotes=(
            (
                "the workbench runtime database was refused at startup:",
                "deploy/workbench-startup-gate.py",
            ),
            (
                "the workbench configuration was refused at load",
                "deploy/workbench-startup-gate.py",
            ),
        ),
    ),
    Gate(
        slug="engine_register",
        unit="`engine-register`（`speechnexus-register-engine`）",
        stage="入口 `main()`",
        observed="失败为一行 `error:` 加具名 code（stderr）与对应退出码，成功为一行 JSON",
        promise="`register-engine.md` 与 `compose-minimal.md` §8：JSON `outcome` 与具名 code，"
        "未承诺 `FAIL:`",
        agrees="是（无格式承诺）",
        disposition="改文档：承认 `error:` 行与退出码；本单元是一次性命令，不是常驻闸门",
    ),
    Gate(
        slug="job_migrate",
        unit="迁移单元 `speechnexus-job-migrate`",
        stage="入口 `main()`（alembic `upgrade`）",
        observed="`Traceback …` 后跟 alembic 或 psycopg 异常，退出码 `1`",
        promise="`async-job-runbook.md` §5 只写失败时的退出码与如何对账，未承诺输出形态",
        agrees="是（无格式承诺）",
        disposition="改文档：承认 traceback 与退出码。理由：alembic 的异常类型多样，"
        "统一成一行会丢掉定位所需的分量",
    ),
    Gate(
        slug="binding_disable_worker",
        unit="`binding-disable-worker`",
        stage="入口 `main()`",
        observed="一行 `Disable worker failed; verify its configuration and dependencies`，"
        "退出码 `1`",
        promise="`docs/operations/binding-disable-worker.md` 未承诺失败输出形态",
        agrees="是（无格式承诺）",
        disposition="改文档：承认这行非 `FAIL:` 的消息与退出码",
        quotes=(
            (
                "Disable worker failed; verify its configuration and dependencies",
                "backend/src/speechnexus_backend/binding_disable_worker.py",
            ),
        ),
    ),
    Gate(
        slug="asset_maintain",
        unit="`asset-maintain`（`speechnexus-asset-maintain`）",
        stage="入口 `main()`（`Settings()`）",
        observed="`Traceback …` 后跟 `ValidationError`，退出码 `1`",
        promise="无",
        agrees="是（无格式承诺）",
        disposition="改文档：承认 traceback 与退出码",
    ),
    Gate(
        slug="runtime_delivery_worker",
        unit="`runtime-delivery-worker`（`speechnexus-runtime-delivery-worker`）",
        stage="入口 `main()`",
        observed="一行 `Runtime delivery unavailable; original records retained.`，退出码 `1`",
        promise="`docs/operations/production-runtime-delivery.md` 未承诺失败输出形态",
        agrees="是（无格式承诺）",
        disposition="改文档：承认这行非 `FAIL:` 的消息与退出码",
        quotes=(
            (
                "Runtime delivery unavailable; original records retained.",
                "backend/src/speechnexus_backend/runtime_delivery_worker.py",
            ),
        ),
    ),
    Gate(
        slug="voice_evidence_worker",
        unit="`voice-evidence-worker`（`speechnexus-voice-evidence-worker`）",
        stage="入口 `main()`",
        observed="一行 `invalid voice evidence worker configuration`，退出码 `2`",
        promise="无",
        agrees="是（无格式承诺）",
        disposition="改文档：承认这行非 `FAIL:` 的消息与退出码 `2`",
        quotes=(
            (
                "invalid voice evidence worker configuration",
                "backend/src/speechnexus_backend/voice_evidence_worker.py",
            ),
        ),
    ),
    Gate(
        slug="engine_readiness",
        unit="`engine-readiness`（`speechnexus-engine-readiness`）",
        stage="入口 `main()`",
        observed="一行 `error:` 加具名 code（如 `invalid_settings`），退出码 `2`",
        promise="`register-engine.md` 只描述只读报告，未承诺失败输出形态",
        agrees="是（无格式承诺）",
        disposition="改文档：承认 `error:` 行与退出码 `2`",
    ),
    Gate(
        slug="minio_init",
        unit="`minio-init`（`deploy/init-object-storage.py`）",
        stage="一次性 init 容器的 `main()`",
        observed="缺 `MINIO_` 键时 `Traceback …` 后跟 `KeyError`，退出码 `1`",
        promise="`compose-minimal.md` §4 只写 `bucket ready` 与版本化要求",
        agrees="是（无格式承诺）",
        disposition="改文档：承认 traceback 与退出码，并说明 Compose 渲染期的键守卫先于它拦住"
        "缺键，这条 traceback 只在直接运行脚本时可达",
    ),
)


def render_table() -> str:
    """Render the documented table from :data:`GATES`; the doc must match byte for byte."""
    header = "| 单元 | 阶段 | 真实输出（实测） | 文档承诺 | 一致 | 处置 |"
    rule = "| --- | --- | --- | --- | --- | --- |"
    rows = [
        f"| {gate.unit} | {gate.stage} | {gate.observed} | {gate.promise} | {gate.agrees} "
        f"| {gate.disposition} |"
        for gate in GATES
    ]
    return "\n".join([header, rule, *rows])


def shape_rows() -> tuple[Gate, ...]:
    """The rows whose documents promise an output shape and whose text is already true.

    Those are the rows that must carry process-level evidence. A row that has not been
    reconciled (``agrees`` is neither ``是`` nor a "no promise" value) carries no case on
    purpose: the guard refuses a case for it, so evidence cannot be invented for a
    promise the documents are about to rewrite.
    """
    return tuple(
        gate
        for gate in GATES
        if gate.promise_kind in SHAPE_KINDS
        and gate.agrees == "是"
        and gate.promise_kind != UNVERIFIED
    )


def inconsistent_rows() -> tuple[Gate, ...]:
    """The rows whose documents promise something the measured output does not do."""
    return tuple(gate for gate in GATES if gate.agrees == "否")


def unverified_rows() -> tuple[Gate, ...]:
    """The rows whose real output could not be reproduced in this checkout."""
    return tuple(gate for gate in GATES if gate.agrees == "未验证")


# A message a row quotes verbatim is claimed to be what the product prints, so it must
# exist in the product file it names (#928 review A1). The extraction below is
# deliberately narrow: a backticked span counts as a claim only when it contains a
# space and no elision mark, so `FAIL:`-style prefixes, key names, paths and
# ``将 <x> 换成 <y>`` placeholders are not treated as verbatim messages.
_QUOTE = re.compile(r"`([^`]*)`")
_ELISION = ("...", "…", "<", ">")
_STRIPPED = re.compile(r"[\"'\\]")


def quoted_spans(cell: str) -> list[str]:
    """Every backticked span in ``cell`` that claims a verbatim product message."""
    return [
        span
        for span in _QUOTE.findall(cell)
        if " " in span.strip() and not any(mark in span for mark in _ELISION)
    ]


def normalized(text: str) -> str:
    """Collapse quoting and whitespace so a wrapped string literal still matches."""
    return " ".join(_STRIPPED.sub("", text).split())


def quote_problems(gate: Gate, root: Path) -> list[str]:
    """Return every reason this row's quoted product messages are not verifiable."""
    problems: list[str] = []
    declared = {literal for literal, _ in gate.quotes}
    cells = normalized(
        " ".join((gate.unit, gate.stage, gate.observed, gate.promise, gate.disposition))
    )
    for span in quoted_spans(gate.observed):
        if span not in declared:
            problems.append(f"{gate.slug}: quotes {span!r} without naming its source")
    for literal, path in gate.quotes:
        if normalized(literal) not in cells:
            problems.append(f"{gate.slug}: declares {literal!r} but the row no longer quotes it")
        if path.startswith("external:"):
            continue
        source = root / path
        if not source.is_file():
            problems.append(f"{gate.slug}: {literal!r} names a missing file {path}")
        elif normalized(literal) not in normalized(source.read_text(encoding="utf-8")):
            problems.append(f"{gate.slug}: {literal!r} is not in {path}")
    return problems
