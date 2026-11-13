"""Machine-readable scan of configuration keys that only work with a prerequisite.

Issue #660: a key can be configured, accepted at startup and still change nothing.
The known sample is ``SPEECHNEXUS_VOICE_PUBLISHER_DATABASE_URL``: the publisher
pool is created inside the public Job writer runtime, so with
``SPEECHNEXUS_JOB_WRITER_DATABASE_URL`` blank the process started clean and kept
answering ``503 voice_draft_unavailable``. A scan of the composition roots, the
runtime ``open()`` preconditions and the Compose profiles produced the table
documented in ``docs/operations/configuration-prerequisites.md``; this module is
its executable source.

This module is text-only data. It never imports a service package: the meta-guard
(``tests/ci/test_configuration_prerequisites.py``) runs under the system
interpreter, and the behaviour probes that do need the backend live in the
``tests/unit`` and ``tests/integration`` suites named by ``Row.evidence``.

Row fields
----------
``keys``
    The configured key (or the pair whose combination is refused). The first key
    is the subject the row classifies.
``prerequisite``
    What must also hold for the subject key to take effect, in operator words.
``behaviour``
    One of :data:`BEHAVIOURS`. Only ``点名拒绝`` and ``静默无效`` claim something
    about startup; ``文档已声明`` says the dependency is declared in an operator
    document, and ``启动告警点名`` says the process logs the missing keys without
    refusing to start.
``disposition``
    What this repository did about the row: ``已修 #660`` (fixed here),
    ``已开 issue #NNN`` (tracked elsewhere), or ``无需…`` with the reason it needs
    no change.
``evidence``
    ``<path>::<test function or document sentence>``. The meta-guard verifies the
    path exists and the sentinel is present, so a renamed test or a reworded
    paragraph turns the guard red instead of silently keeping a stale citation.
``names_keys``
    ``True`` when the refusal message contains the environment variable names.
    Legacy refusals that only name the identity (for example ``Configure both
    object-storage keys together``) are ``False`` and are listed as such rather
    than upgraded here: this card only converts *silent* configurations into
    named refusals, it does not rewrite refusals that already happen.
"""

from __future__ import annotations

from dataclasses import dataclass

NAMED = "点名拒绝"
SILENT = "静默无效"
DOCUMENTED = "文档已声明"
WARNED = "启动告警点名"
BEHAVIOURS = (NAMED, SILENT, DOCUMENTED, WARNED)

DOC = "docs/operations/configuration-prerequisites.md"
BACKEND = "backend/src/speechnexus_backend/settings.py"
WORKBENCH = "backend/src/speechnexus_backend/workbench/runtime_settings.py"
GATEWAY = "execution-gateway/src/speechnexus_gateway/settings.py"
CONTROLLER = "node-controller/src/speechnexus_node_controller/settings.py"

BACKEND_PREFIX = "SPEECHNEXUS_"
GATEWAY_PREFIX = "SPEECHNEXUS_GATEWAY_"
CONTROLLER_PREFIX = "SPEECHNEXUS_CONTROLLER_"
WORKBENCH_PREFIX = "SPEECHNEXUS_WORKBENCH_"

UNIT_PAIRS = "tests/unit/backend/test_startup_key_pairs.py"
INSPECTOR = "tests/unit/backend/test_asset_inspector_wiring.py"
GATEWAY_PAIRS = "tests/unit/gateway/test_settings_pairs.py"
CONTROLLER_PAIRS = "tests/unit/controller/test_composition_prerequisites.py"
WORKBENCH_PAIRS = "tests/unit/backend/test_workbench_prerequisites.py"
JOB_PG = "tests/integration/test_job_database_pair_pg.py"
GATE = "tests/unit/test_job_startup_gate.py"

# Identity DSNs each runtime constructs only under ``SPEECHNEXUS_PROCESS_ROLE=api``.
API_ROLE_IDENTITIES = (
    "SPEECHNEXUS_JOB_WRITER_DATABASE_URL",
    "SPEECHNEXUS_PROFILE_WRITER_DATABASE_URL",
    "SPEECHNEXUS_MODEL_CATALOG_READER_DATABASE_URL",
    "SPEECHNEXUS_RESOURCE_POOL_READER_DATABASE_URL",
    "SPEECHNEXUS_BINDING_LIST_READER_DATABASE_URL",
    "SPEECHNEXUS_AUDIT_READER_DATABASE_URL",
    "SPEECHNEXUS_MODEL_INSTANCE_READER_DATABASE_URL",
    "SPEECHNEXUS_WORKSPACE_WRITER_DATABASE_URL",
    "SPEECHNEXUS_PRONUNCIATION_READER_DATABASE_URL",
    "SPEECHNEXUS_PRONUNCIATION_WRITER_DATABASE_URL",
    "SPEECHNEXUS_PRONUNCIATION_PUBLISHER_DATABASE_URL",
    "SPEECHNEXUS_BINDING_WRITER_DATABASE_URL",
    "SPEECHNEXUS_BINDING_DEFAULTS_WRITER_DATABASE_URL",
    "SPEECHNEXUS_BINDING_ADMIN_READER_DATABASE_URL",
    "SPEECHNEXUS_BINDING_LIFECYCLE_DATABASE_URL",
    "SPEECHNEXUS_QUARANTINE_RELEASE_DATABASE_URL",
)

RETIRED_INSPECTOR_KEYS = (
    "SPEECHNEXUS_AUDIO_INSPECTION_RUNTIME_ROOT",
    "SPEECHNEXUS_AUDIO_INSPECTION_COMMAND",
    "SPEECHNEXUS_AUDIO_INSPECTION_LAUNCHER_PYTHON",
    "SPEECHNEXUS_AUDIO_INSPECTION_BUBBLEWRAP",
    "SPEECHNEXUS_AUDIO_INSPECTION_PRLIMIT",
)

AUDIO_ONLY_LIMIT_KEYS = (
    "SPEECHNEXUS_AUDIO_INSPECTION_MAX_DURATION_MS",
    "SPEECHNEXUS_AUDIO_INSPECTION_MAX_REFERENCE_DURATION_MS",
    "SPEECHNEXUS_AUDIO_INSPECTION_MAX_INPUT_BYTES",
    "SPEECHNEXUS_AUDIO_INSPECTION_WALL_SECONDS",
    "SPEECHNEXUS_AUDIO_INSPECTION_MAX_RECEIPT_BYTES",
)

TRANSCRIPTION_KEYS = (
    "SPEECHNEXUS_TRANSCRIPTION_RECIPE_FILE",
    "SPEECHNEXUS_TRANSCRIPTION_INPUT_BUCKET",
    "SPEECHNEXUS_EXECUTION_GATEWAY_BASE_URL",
    "SPEECHNEXUS_EXECUTION_GATEWAY_COMPONENT_CREDENTIAL",
    "SPEECHNEXUS_OBJECT_STORAGE_ACCESS_KEY",
    "SPEECHNEXUS_OBJECT_STORAGE_SECRET_KEY",
)

SPEECH_KEYS = (
    "SPEECHNEXUS_SPEECH_RECIPE_FILE",
    "SPEECHNEXUS_SPEECH_DEFAULT_LANGUAGE",
    "SPEECHNEXUS_SPEECH_SETTLEMENT_DATABASE_URL",
    "SPEECHNEXUS_EXECUTION_GATEWAY_BASE_URL",
    "SPEECHNEXUS_EXECUTION_GATEWAY_COMPONENT_CREDENTIAL",
    "SPEECHNEXUS_OBJECT_STORAGE_ACCESS_KEY",
    "SPEECHNEXUS_OBJECT_STORAGE_SECRET_KEY",
)

GATEWAY_FILE_KEYS = tuple(
    f"SPEECHNEXUS_GATEWAY_FILE_{engine}_{suffix}"
    for engine in ("ASR", "TTS", "VOICE_DESIGN", "VOICE_CLONE")
    for suffix in ("CONFIG_FILE", "OBJECT_ACCESS_KEY", "OBJECT_SECRET_KEY")
)

WORKBENCH_SAVED_ASSET_KEYS = (
    "SPEECHNEXUS_WORKBENCH_SPEECH_BASE_URL",
    "SPEECHNEXUS_WORKBENCH_OBJECT_ENDPOINT",
    "SPEECHNEXUS_WORKBENCH_OBJECT_BUCKET",
    "SPEECHNEXUS_WORKBENCH_OBJECT_ACCESS_KEY",
    "SPEECHNEXUS_WORKBENCH_OBJECT_SECRET_KEY",
)

WORKBENCH_LLM_KEYS = (
    "SPEECHNEXUS_WORKBENCH_LLM_ENDPOINT",
    "SPEECHNEXUS_WORKBENCH_LLM_MODEL",
    "SPEECHNEXUS_WORKBENCH_LLM_API_KEY",
    "SPEECHNEXUS_WORKBENCH_LLM_QUALIFICATION_REFERENCE",
)


@dataclass(frozen=True)
class Row:
    keys: tuple[str, ...]
    prerequisite: str
    behaviour: str
    disposition: str
    evidence: str
    names_keys: bool = True


ROWS: tuple[Row, ...] = (
    Row(
        keys=("SPEECHNEXUS_JOB_ADMISSION_DATABASE_URL",),
        prerequisite="SPEECHNEXUS_JOB_WRITER_DATABASE_URL（admission 池由 JobRuntime.open() 创建）",
        behaviour=NAMED,
        disposition="已修 #660",
        evidence=f"{UNIT_PAIRS}::test_a_job_companion_dsn_without_the_writer_names_both_keys",
    ),
    Row(
        keys=("SPEECHNEXUS_VOICE_PUBLISHER_DATABASE_URL",),
        prerequisite="SPEECHNEXUS_JOB_WRITER_DATABASE_URL（publisher 池由 JobRuntime.open() 创建）",
        behaviour=NAMED,
        disposition="已修 #660",
        evidence=f"{UNIT_PAIRS}::test_a_job_companion_dsn_without_the_writer_names_both_keys",
    ),
    Row(
        keys=(
            "SPEECHNEXUS_VOICE_PUBLISHER_DATABASE_URL",
            "SPEECHNEXUS_JOB_WRITER_DATABASE_URL",
        ),
        prerequisite="同一个 database（host / port / database 一致）",
        behaviour=NAMED,
        disposition="已修 #660",
        evidence=f"{JOB_PG}::test_a_publisher_on_another_database_is_refused_naming_both_keys",
    ),
    Row(
        keys=(
            "SPEECHNEXUS_JOB_ADMISSION_DATABASE_URL",
            "SPEECHNEXUS_JOB_WRITER_DATABASE_URL",
        ),
        prerequisite="同一个 database（host / port / database 一致）",
        behaviour=NAMED,
        disposition="已修 #660",
        evidence=f"{JOB_PG}::test_an_admission_on_another_database_is_refused_naming_both_keys",
    ),
    Row(
        keys=API_ROLE_IDENTITIES,
        prerequisite="SPEECHNEXUS_PROCESS_ROLE=api（每个运行体构造期校验）",
        behaviour=NAMED,
        disposition="无需：既有拒绝（按身份名拒绝，本卡不改写既有消息）",
        evidence=f"{UNIT_PAIRS}::test_a_non_api_role_refuses_every_identity_dsn",
        names_keys=False,
    ),
    Row(
        keys=(
            "SPEECHNEXUS_PROFILE_WRITER_DATABASE_URL",
            "SPEECHNEXUS_PROFILE_PUBLICATION_EVIDENCE_FILE",
        ),
        prerequisite="两者互为前提（writer 必须有已评审证据文档，反之亦然）",
        behaviour=NAMED,
        disposition="无需：既有拒绝（消息点名两个键）",
        evidence=f"{UNIT_PAIRS}::test_the_profile_writer_and_evidence_document_name_each_other",
    ),
    Row(
        keys=("SPEECHNEXUS_OBJECT_STORAGE_ACCESS_KEY", "SPEECHNEXUS_OBJECT_STORAGE_SECRET_KEY"),
        prerequisite="两者必须成对出现",
        behaviour=NAMED,
        disposition="无需：既有拒绝（概念级消息，只点名身份）",
        evidence=f"{UNIT_PAIRS}::test_a_partial_object_storage_pair_is_refused",
        names_keys=False,
    ),
    Row(
        keys=(
            "SPEECHNEXUS_EXECUTION_GATEWAY_BASE_URL",
            "SPEECHNEXUS_EXECUTION_GATEWAY_COMPONENT_CREDENTIAL",
        ),
        prerequisite="两者必须成对出现",
        behaviour=NAMED,
        disposition="无需：既有拒绝（概念级消息，只点名身份）",
        evidence=f"{UNIT_PAIRS}::test_a_partial_execution_gateway_pair_is_refused",
        names_keys=False,
    ),
    Row(
        keys=(
            "SPEECHNEXUS_BINDING_WRITER_DATABASE_URL",
            "SPEECHNEXUS_BINDING_DEFAULTS_WRITER_DATABASE_URL",
        ),
        prerequisite="两种 binding 写入模式只能选一种",
        behaviour=NAMED,
        disposition="无需：既有拒绝（概念级消息，只点名身份）",
        evidence=f"{UNIT_PAIRS}::test_both_binding_writer_modes_are_refused",
        names_keys=False,
    ),
    Row(
        keys=("SPEECHNEXUS_ASSET_INSPECTION_URL", "SPEECHNEXUS_ASSET_INSPECTION_TOKEN"),
        prerequisite=(
            "SPEECHNEXUS_ASSET_INSPECTOR=audio 且资产运行时已装配"
            "（SPEECHNEXUS_OBJECT_STORAGE_ACCESS_KEY/SECRET_KEY 非空）"
        ),
        behaviour=NAMED,
        disposition="无需：既有拒绝（消息点名键）",
        evidence=f"{INSPECTOR}::test_text_mode_names_every_configured_inspection_setting",
    ),
    Row(
        keys=AUDIO_ONLY_LIMIT_KEYS,
        prerequisite="SPEECHNEXUS_ASSET_INSPECTOR=audio 且资产运行时已装配（文本模式下无消费者）",
        behaviour=NAMED,
        disposition="已修 #660",
        evidence=f"{INSPECTOR}::test_text_mode_rejects_a_narrowed_audio_only_limit",
    ),
    Row(
        keys=RETIRED_INSPECTOR_KEYS,
        prerequisite="无（容器拥有这些输入，本进程不再消费）",
        behaviour=NAMED,
        disposition="无需：既有拒绝（消息点名键与替代键）",
        evidence=f"{INSPECTOR}::test_text_mode_rejects_a_retired_local_prerequisite",
    ),
    Row(
        keys=("SPEECHNEXUS_ARTIFACT_READER_DATABASE_URL",),
        prerequisite="SPEECHNEXUS_OBJECT_STORAGE_ACCESS_KEY/SECRET_KEY（资产运行时装配）",
        behaviour=DOCUMENTED,
        disposition="无需：既有文档声明（无对象存储凭据时业务端点 503）",
        evidence="docs/components/production-assets.md::With neither key, business endpoints",
    ),
    Row(
        keys=("SPEECHNEXUS_WORKSPACE_WRITER_DATABASE_URL",),
        prerequisite="资产运行时已装配（成对的 SPEECHNEXUS_OBJECT_STORAGE_* 凭据）",
        behaviour=DOCUMENTED,
        disposition="无需：既有文档声明（只设置本变量不会装配写端口）",
        evidence="deploy/README.md::只设置本变量而关闭资产运行时不会装配写端口",
    ),
    Row(
        keys=TRANSCRIPTION_KEYS,
        prerequisite="整套同步转写输入（缺任一项即不装配）",
        behaviour=WARNED,
        disposition="无需：既有启动告警点名全部缺失键",
        evidence=f"{UNIT_PAIRS}::test_partial_transcription_configuration_warns_with_every_missing_key",
    ),
    Row(
        keys=SPEECH_KEYS,
        prerequisite="整套同步 TTS 输入（缺任一项即不装配）",
        behaviour=WARNED,
        disposition="无需：既有启动告警点名全部缺失键",
        evidence=f"{UNIT_PAIRS}::test_partial_speech_configuration_warns_with_every_missing_key",
    ),
    Row(
        keys=(),
        prerequisite="所属运行体已装配（对应 DSN/URL/凭据非空）",
        behaviour=DOCUMENTED,
        disposition="无需：本卡补充文档声明（数值旋钮无法区分 Compose 透传默认值）",
        evidence=f"{DOC}::## 数值与并发旋钮",
    ),
    Row(
        keys=("SPEECHNEXUS_JOB_DISPATCH_DATABASE_URL",),
        prerequisite="启用 job-dispatch profile 并启动其入口",
        behaviour=NAMED,
        disposition="无需：既有启动闸门点名拒绝",
        evidence=f"{GATE}::test_a_blank_dispatch_dsn_names_the_environment_variable",
    ),
    Row(
        keys=("SPEECHNEXUS_GATEWAY_CONTROL_QUERY_DATABASE_URL",),
        prerequisite="SPEECHNEXUS_GATEWAY_COMPONENT_TOKEN（执行控制启用）",
        behaviour=NAMED,
        disposition="已修 #660",
        evidence=f"{GATEWAY_PAIRS}::test_a_query_dsn_without_execution_control_names_both_keys",
    ),
    Row(
        keys=(
            "SPEECHNEXUS_GATEWAY_COMPONENT_TOKEN",
            "SPEECHNEXUS_GATEWAY_INGRESS_LOCK_DIRECTORY",
            "SPEECHNEXUS_GATEWAY_RECOVERY_CONFIG_FILE",
            "SPEECHNEXUS_GATEWAY_RECOVERY_NODE_COMPONENT_TOKEN",
            *GATEWAY_FILE_KEYS,
        ),
        prerequisite="执行控制启用、配置为绝对路径且每类引擎成对配置",
        behaviour=NAMED,
        disposition="无需：既有拒绝（概念级消息，只点名身份）",
        evidence=f"{GATEWAY_PAIRS}::test_a_partial_file_engine_configuration_is_refused",
        names_keys=False,
    ),
    Row(
        keys=("SPEECHNEXUS_CONTROLLER_ENGINE_AUTH_ROOT_KEY",),
        prerequisite="至少一个获批模板声明 engine_auth（否则凭据永不被读取）",
        behaviour=NAMED,
        disposition="已修 #660",
        evidence=f"{CONTROLLER_PAIRS}::test_a_root_key_without_an_authenticated_template_names_both",
    ),
    Row(
        keys=WORKBENCH_LLM_KEYS,
        prerequisite="四个键同配；缺任一项即不装配内网 LLM",
        behaviour=NAMED,
        disposition="已修 #660（`LLM_API_KEY` 单独配置此前被静默忽略）",
        evidence=f"{WORKBENCH_PAIRS}::test_a_partial_llm_configuration_is_refused",
    ),
    Row(
        keys=WORKBENCH_SAVED_ASSET_KEYS,
        prerequisite="五个键同配；缺任一项即拒绝而不是静默关闭保存",
        behaviour=NAMED,
        disposition="无需：既有拒绝（概念级消息，只点名身份）",
        evidence=f"{WORKBENCH_PAIRS}::test_a_partial_saved_asset_configuration_is_refused",
        names_keys=False,
    ),
)

# Optional selector fields that are deliberately not rows above. They select a
# capability on their own, so "configured but ineffective" cannot happen without a
# second key: they either take effect or fail their own startup self-check.
EXEMPT: dict[tuple[str, str], str] = {
    (
        WORKBENCH,
        "cleanup_database_url",
    ): "独立开关：缺省即不重放删除账本，显式配置后由启动身份校验拒绝越权；无配对前置",
    (
        BACKEND,
        "asset_default_ttl_seconds",
    ): "数值旋钮，由「数值与并发旋钮」组声明",
}
