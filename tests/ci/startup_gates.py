"""The one source of the startup-gate failure-output table (#928).

Every deployment unit that can refuse to start before it serves has exactly one row
here: which stage the refusal runs in (synchronous assembly, ASGI lifespan, or the
entry point's own ``main()``), what it really prints and exits with when measured at
the process level, what the operator documents promise, whether the two agree, and
which disposition was chosen. ``docs/operations/startup-gate-output.md`` carries the
same rows as a table; ``tests/ci/test_startup_gate_output_map.py`` renders the table
from this module and compares the text, so a row cannot be edited on one side only.

A row whose ``promise`` is ``ONE_FAIL_LINE`` also names the process-level case that
starts the real entry point, and the same guard checks that such a case exists in a
pytest module. ``#920``'s failure mode was a test that monkeypatched the entry
function instead of starting the entry; naming the case here keeps the promise and
its evidence together.

This module is text and standard library only: it never imports the backend, touches
PostgreSQL or starts a container, so ``python3 -m unittest discover -s tests/ci``
runs it under the system interpreter like every other guard in this directory.
"""

from __future__ import annotations

from dataclasses import dataclass

# `promise` values. ONE_FAIL_LINE means the documents promise one named `FAIL:` line
# (or one per missing item) and no traceback; TRACEBACK means they promise nothing
# about the failure shape; EXACT_LINE means they pin one literal non-`FAIL` message.
ONE_FAIL_LINE = "一行 FAIL:"
TRACEBACK = "无格式承诺"
EXACT_LINE = "一行具名消息"
UNVERIFIED = "未验证"

# Where a FAIL promise must be proven by starting the real entry point. A test that
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
    promise_kind: str = TRACEBACK
    case: str | None = None


GATES: tuple[Gate, ...] = (
    Gate(
        slug="api_job_schema",
        unit="`api`（`speechnexus-api`）· Job writer 迁移与角色闸门",
        stage="ASGI lifespan（`app.py` 的 `await job_runtime.open()`）",
        observed="`ERROR: Traceback ...`（经 starlette lifespan 到 `job_runtime.open`）+ 退出码 `3`"
        "，"
        "没有任何 `FAIL:` 行",
        promise="`async-job-runbook.md` §1 行 2 与 `#924` 的 §9.1 承诺一行 `FAIL: … migration profi"
        "le`"
        "且「不再 traceback」",
        agrees="否",
        disposition="由 `#924` 把文档改成实测为真（本卡不改 `app.py` / `main.py` / `job_runtime.py`"
        "）。"
        "若要把该预检移到入口换一行 `FAIL:`，须另立卡：它涉及启动顺序、二次连接与 lifespan 归属",
        promise_kind=ONE_FAIL_LINE,
    ),
    Gate(
        slug="api_settlement",
        unit="`api` · 同步 TTS 结算预检",
        stage="同步装配（`speech_runtime.build_speech`）",
        observed="一行 `FAIL: the settled-capacity preflight cannot read the admission tables …`，"
        "退出码 `1`，"
        "无 traceback",
        promise="`async-job-runbook.md` §8 表：日志首行 `FAIL: the settled-capacity preflight …`、"
        "退出码 `1`",
        agrees="是",
        disposition="补进程级用例钉住（`tests/integration`，需要隔离 PostgreSQL 的空 schema）",
        promise_kind=ONE_FAIL_LINE,
        case="test_api_settlement_gate_prints_one_fail_line_and_no_traceback",
    ),
    Gate(
        slug="api_recipe",
        unit="`api` · 私有配方加载（TTS `speech-recipe` 与转写 `transcription-recipe` 两个 loader"
        "）",
        stage="同步装配（uvicorn factory 内 `load_tts_recipe` / `load_transcription_recipe`）",
        observed="实测（TTS 配方）：`Traceback … ValueError: Invalid private speech admission recip"
        "e "
        "<abs path> (<Type>)`，退出码 `1`；转写 loader 用同一包装（"
        "`transcription_recipe.py:272`）抛 `Invalid private transcription admission recipe`，"
        "未单独起进程复现（结算预检先于它拒绝空 schema）",
        promise="`compose-minimal.md` §9 与 `async-job-runbook.md` §5：recipe 摘要不符时「API 启动"
        "报 "
        "recipe digest / `invalid transcription recipe`」，未承诺输出形态与退出码",
        agrees="是（无格式承诺）",
        disposition="改文档：在本表承认 traceback 与退出码。理由：这条失败来自部署方手改的私有文件"
        "，"
        "消息已点名文件与失败类；包一层 `FAIL:` 只会重复同一句，且会把「配方不可用」与"
        "「库未迁移」两类混淆",
    ),
    Gate(
        slug="api_settings",
        unit="`api` · 设置校验",
        stage="入口 `main()`（`Settings()`）",
        observed="`Traceback … pydantic ValidationError`，退出码 `1`",
        promise="`compose-minimal.md`：某些键非法时「stops API startup / 启动失败」，"
        "未承诺输出形态",
        agrees="是（无格式承诺）",
        disposition="改文档：在本表承认 traceback 与退出码。理由：pydantic 已在消息里点名键与规则，"
        "且这段在任何 uvicorn 装配之前运行，`main()` 无法在不吞掉真实缺陷的前提下统一改写",
    ),
    Gate(
        slug="api_lifespan_runtimes",
        unit="`api` · 其余 lifespan 运行时（binding list / catalogue / audit 等）",
        stage="ASGI lifespan（`run_in_threadpool(<runtime>.open)`）",
        observed="`ERROR: Traceback …`（`<runtime> startup rejected`）+ 退出码 `3`，无 `FAIL:` 行",
        promise="无（文档只描述这些身份与它所覆盖的读面）",
        agrees="是（无格式承诺）",
        disposition="改文档：承认 lifespan 阶段一律 traceback + 退出码 `3`。理由：uvicorn 0.54.0 的"
        "`Server.startup()` 在 lifespan 失败时 `sys.exit(3)`，入口的 `except` 到不了这个 raise 点",
    ),
    Gate(
        slug="gateway_settings",
        unit="`execution-gateway`（`speechnexus-gateway`）· 设置校验",
        stage="入口 `main()`（`Settings()`）",
        observed="`Traceback … pydantic ValidationError`，退出码 `1`",
        promise="`compose-minimal.md` §9 只点名原因文案，未承诺输出形态",
        agrees="是（无格式承诺）",
        disposition="改文档：承认 traceback 与退出码。理由：与 `api` 设置校验同构，"
        "且这是 uvicorn 之前的"
        "配置拒绝，没有第二个可执行阶段可挪",
    ),
    Gate(
        slug="gateway_file_speech",
        unit="`execution-gateway` · 文件语音配置（ASR/TTS/VoiceDesign/Clone 四 lane）",
        stage="同步装配（factory `create_app` 的 `load_file_speech_configuration`）",
        observed="`Traceback … ValueError: invalid gateway file ASR configuration`，退出码 `1`",
        promise="`compose-minimal.md` §9：`gateway 退出并报 invalid gateway file ASR configuration`"
        "，"
        "未承诺输出形态",
        agrees="是（无格式承诺）",
        disposition="改文档：承认 traceback 与退出码，并说明原因文案出现在 traceback 末行。理由："
        "这条拒绝发生在同步装配阶段，`main()` 外没有第二个入口；把它改写成一行 `FAIL:` 会新增一条"
        "需要单独维护的承诺，而当前文档只承诺点名原因",
    ),
    Gate(
        slug="gateway_vad_lifespan",
        unit="`execution-gateway` · 固定 CPU VAD 运行时与控制面",
        stage="ASGI lifespan（`_build_control` / `runtime.open`）",
        observed=UNVERIFIED + "：需要镜像内的 Silero ONNX 与固定依赖，本机 worktree 无法复现",
        promise="`compose-minimal.md` §9：`gateway 报 cannot initialize the pinned CPU VAD runtime"
        "`",
        agrees="未验证",
        disposition="记录为未验证项：按 lifespan 归属推断为 traceback + 退出码 `3`，但未实测；"
        "不得据此写成实测结论",
    ),
    Gate(
        slug="node_controller",
        unit="`node-controller`（`speechnexus-node-controller`，默认 Compose 不启动）",
        stage="入口 `main()`（`Settings()`）与 factory",
        observed="`Traceback … pydantic ValidationError`（设置）/ factory 拒绝，退出码 `1`",
        promise="`docs/components/node-controller.md`：启动即校验配置，未承诺输出形态",
        agrees="是（无格式承诺）",
        disposition="改文档：承认 traceback 与退出码。理由：本单元不在默认 Compose 内，没有部署文档"
        "承诺失败形态；先记录实测，不新增约定",
    ),
    Gate(
        slug="asset_inspector",
        unit="`asset-inspector`（`speechnexus-asset-inspector`）",
        stage="入口 `main()`（`InspectionSettings.from_env` 与运行时路径检查）",
        observed="一行 `asset inspector configuration error: <key rule>`（stderr），退出码 `1`，"
        "无 traceback；"
        "缺 runtime root 时一行 `asset inspector runtime root is missing: <path>`",
        promise="`async-job-runbook.md` 实测表：退出码 `1` 加同一句 `asset inspector configuration"
        " error …`",
        agrees="是",
        disposition="补进程级用例钉住这行（`EXACT_LINE`），并说明它不是 `FAIL:` 前缀",
        promise_kind=EXACT_LINE,
        case="test_asset_inspector_reports_one_line_and_no_traceback",
    ),
    Gate(
        slug="job_dispatch_worker",
        unit="`job-dispatch-worker` · 入口闸门 `deploy/job-startup-gate.py`",
        stage="入口脚本 `main()`（`exec` 之前）",
        observed="每个缺失项一行 `  FAIL: <具名条件>`（stdout）+ 末行汇总，退出码 `1`，"
        "失败时不 `exec`",
        promise="`compose-minimal.md` §6、`async-job-runbook.md` §1/§8/§12：`FAIL:` 点名并以非零退"
        "出",
        agrees="是",
        disposition="补进程级用例钉住；并修两处与承诺相悖的失败面：非法 `SPEECHNEXUS_DATABASE_SCHEM"
        "A` "
        "曾是 ValueError traceback，连接失败行曾回显主机、端口与库名。点名的对象是键名、桶名、"
        "schema 与表名，以及该容器自己的私有配置目录路径（容器内路径，不是构建机路径）",
        promise_kind=ONE_FAIL_LINE,
        case="test_job_dispatch_gate_names_missing_prerequisites_without_a_traceback",
    ),
    Gate(
        slug="artifact_retention",
        unit="`artifact-retention`（`speechnexus-artifact-retention`）",
        stage="入口 `main()`（`Settings()` 与 `runtime.open()`）",
        observed="`Traceback … ValidationError` 或启动拒绝消息，退出码 `1`",
        promise="`compose-minimal.md` §6 透传表：空值「容器启动即失败」、宽身份「启动点名拒绝」，"
        "未承诺形态",
        agrees="是（无格式承诺）",
        disposition="改文档：承认 traceback 与退出码。理由：拒绝消息本身已点名角色与关系；把它改写"
        "成"
        "`FAIL:` 会与本次未统一的其余 worker 形成第三种约定",
    ),
    Gate(
        slug="workbench_migrate",
        unit="`workbench-migrate`（`speechnexus-workbench-migrate`）",
        stage="入口 `main()`",
        observed="每个问题一行 `FAIL: <具名条件>`（stderr），退出码 `1`，无 traceback",
        promise="`compose-minimal.md` §6：`FAIL: <具名条件>` 并以非零退出",
        agrees="是",
        disposition="补进程级用例钉住",
        promise_kind=ONE_FAIL_LINE,
        case="test_workbench_migrate_names_missing_inputs_without_a_traceback",
    ),
    Gate(
        slug="workbench_api",
        unit="`workbench-api` · 入口闸门 `deploy/workbench-startup-gate.py`",
        stage="入口脚本（`exec` 之前；`check_database` / `check_cleanup_identity`）",
        observed="每个问题一行 `FAIL:`；运行期检查失败为一行 `FAIL: the workbench runtime database "
        "…`，"
        "退出码 `1`，失败时不 `exec`",
        promise="`compose-minimal.md` §6：`FAIL:` 点名并以非零退出",
        agrees="是",
        disposition="补进程级用例钉住",
        promise_kind=ONE_FAIL_LINE,
        case="test_workbench_gate_names_missing_inputs_without_a_traceback",
    ),
    Gate(
        slug="engine_register",
        unit="`engine-register`（`speechnexus-register-engine`）",
        stage="入口 `main()`",
        observed="失败为一行 `error: <code>: <message>`（stderr）+ 退出码（参数错 `2`、不可达等具名"
        " code），"
        "成功为一行 JSON",
        promise="`register-engine.md` 与 `compose-minimal.md` §8：JSON `outcome` 与具名 code，"
        "未承诺 `FAIL:`",
        agrees="是（无格式承诺）",
        disposition="改文档：承认 `error: <code>` 行与退出码；本单元是一次性命令，不是常驻启动闸门",
    ),
    Gate(
        slug="job_migrate",
        unit="迁移单元 `speechnexus-job-migrate`",
        stage="入口 `main()`（alembic `upgrade`）",
        observed="`Traceback …`（alembic / psycopg 异常），退出码 `1`",
        promise="`async-job-runbook.md` §5 只写失败时的退出码与如何对账，未承诺输出形态",
        agrees="是（无格式承诺）",
        disposition="改文档：承认 traceback 与退出码。理由：alembic 的异常类型多样（连接、版本冲突"
        "、"
        "SQL 失败），统一成一行会丢掉定位所需的分量",
    ),
    Gate(
        slug="binding_disable_worker",
        unit="`binding-disable-worker`",
        stage="入口 `main()`",
        observed="一行 `Disable worker failed; verify its configuration and dependencies`，"
        "退出码 `1`",
        promise="`docs/operations/binding-disable-worker.md` 未承诺失败输出形态",
        agrees="是（无格式承诺）",
        disposition="改文档：承认这行非 `FAIL:` 的消息与退出码；不新增约定",
    ),
    Gate(
        slug="asset_maintain",
        unit="`asset-maintain`（`speechnexus-asset-maintain`）",
        stage="入口 `main()`（`Settings()`）",
        observed="`Traceback … ValidationError`，退出码 `1`",
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
    ),
    Gate(
        slug="voice_evidence_worker",
        unit="`voice-evidence-worker`（`speechnexus-voice-evidence-worker`）",
        stage="入口 `main()`",
        observed="一行 `invalid voice evidence worker configuration`，退出码 `2`",
        promise="无",
        agrees="是（无格式承诺）",
        disposition="改文档：承认这行非 `FAIL:` 的消息与退出码 `2`",
    ),
    Gate(
        slug="engine_readiness",
        unit="`engine-readiness`（`speechnexus-engine-readiness`）",
        stage="入口 `main()`",
        observed="一行 `error: invalid_settings: <what to set>`，退出码 `2`",
        promise="`register-engine.md` 只描述只读报告，未承诺失败输出形态",
        agrees="是（无格式承诺）",
        disposition="改文档：承认 `error: <code>` 行与退出码 `2`",
    ),
    Gate(
        slug="minio_init",
        unit="`minio-init`（`deploy/init-object-storage.py`）",
        stage="一次性 init 容器的 `main()`",
        observed="缺 `MINIO_*` 时 `Traceback … KeyError`，退出码 `1`；桶/版本化不符时抛异常退出",
        promise="`compose-minimal.md` §4 只写 `bucket ready` 与版本化要求",
        agrees="是（无格式承诺）",
        disposition="改文档：承认 traceback 与退出码，"
        "并说明 Compose 渲染期的 `:?` 守卫先于它拦住缺键，"
        "这条 traceback 只在直接运行脚本时可达",
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


def process_evidence_rows() -> tuple[Gate, ...]:
    """The rows whose documents promise an output shape and whose text is already true.

    Those are the rows that must carry process-level evidence. An inconsistent row
    (``agrees != 是``) is owned by another card and carries no case here on purpose:
    the guard refuses a case for it, so evidence cannot be invented for a promise the
    documents are about to rewrite.
    """
    return tuple(
        gate
        for gate in GATES
        if gate.promise_kind in {ONE_FAIL_LINE, EXACT_LINE} and gate.agrees == "是"
    )


def inconsistent_rows() -> tuple[Gate, ...]:
    """The rows whose documents promise something the measured output does not do."""
    return tuple(gate for gate in GATES if gate.agrees == "否")


def unverified_rows() -> tuple[Gate, ...]:
    """The rows whose real output could not be reproduced in this checkout."""
    return tuple(gate for gate in GATES if gate.agrees == "未验证")
