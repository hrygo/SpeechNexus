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
    The classification: ``点名拒绝`` (startup refusal), ``静默无效`` (accepted and
    inert -- no row may keep this after #660), ``文档已声明`` (an operator document
    states the dependency) or ``启动告警点名`` (a warning lists the missing keys
    and the process starts).
``display``
    The per-row "当前表现" text in the operator table. It defaults to
    ``behaviour`` and only differs when the behaviour is conditional: the audio
    inspection keys are refused only while the asset runtime is composed, and the
    dispatch DSN is only consumed by the dispatch worker entry.
``disposition``
    What this repository did about the row: ``已修 #660`` (fixed here), ``已开
    issue #NNN`` (tracked elsewhere), or ``无需…`` with the reason it needs no
    change. ``静默无效`` rows must carry an open issue: claiming a fix while the
    row is still silent is a contradiction.
``evidence``
    ``<path>::<test function or document sentence>``. The meta-guard verifies the
    path exists and the sentinel is present, so a renamed test or a reworded
    paragraph turns the guard red instead of silently keeping a stale citation.
    Evidence is what proves the *behaviour today*; it is not the guard's proof
    that a judgment exists (see ``probes``).
``names_keys``
    ``True`` when the refusal message contains the environment variable names.
    Legacy refusals that only name the identity (``Configure both object-storage
    keys together``) are ``False`` and are listed as such rather than upgraded
    here: this card only converts *silent* configurations into named refusals.
``probes``
    ``<path>::<symbol>[|<symbol>]`` evidence that the judgment itself exists in
    the product source, or ``<path>::text:<fragment>`` for script-level checks.
    Symbols may be functions, methods (``Class.method``), classes or module
    assignments. The meta-guard resolves them with ``ast`` and requires every
    ``message_keys`` value in the union of their source ranges, so deleting the
    judgment, emptying it, or replacing the message with a generic sentence turns
    the guard red without running any test.
``message_keys``
    Environment variable names the refusal/warning message must contain. Empty
    for ``names_keys=False`` rows (the message names the identity concept) and for
    ``文档已声明`` rows.
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
COMPOSE = "deploy/compose.yaml"
COMPOSE_ENV = "deploy/compose.env.example"
JOB_RUNTIME = "backend/src/speechnexus_backend/job_runtime.py"
APP = "backend/src/speechnexus_backend/app.py"
ASSET_RUNTIME = "backend/src/speechnexus_backend/asset_runtime.py"
PROFILE_RUNTIME = "backend/src/speechnexus_backend/profile_runtime.py"
TRANSCRIPTION_RUNTIME = "backend/src/speechnexus_backend/transcription_runtime.py"
SPEECH_RUNTIME = "backend/src/speechnexus_backend/speech_runtime.py"
GATEWAY_SETTINGS = "execution-gateway/src/speechnexus_gateway/settings.py"
CONTROLLER_APP = "node-controller/src/speechnexus_node_controller/app.py"
JOB_GATE = "deploy/job-startup-gate.py"

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

JOB_WRITER = "SPEECHNEXUS_JOB_WRITER_DATABASE_URL"
JOB_ADMISSION = "SPEECHNEXUS_JOB_ADMISSION_DATABASE_URL"
VOICE_PUBLISHER = "SPEECHNEXUS_VOICE_PUBLISHER_DATABASE_URL"
JOB_DISPATCH = "SPEECHNEXUS_JOB_DISPATCH_DATABASE_URL"
PROFILE_WRITER = "SPEECHNEXUS_PROFILE_WRITER_DATABASE_URL"
PROFILE_EVIDENCE = "SPEECHNEXUS_PROFILE_PUBLICATION_EVIDENCE_FILE"
OBJECT_ACCESS = "SPEECHNEXUS_OBJECT_STORAGE_ACCESS_KEY"
OBJECT_SECRET = "SPEECHNEXUS_OBJECT_STORAGE_SECRET_KEY"
GATEWAY_URL = "SPEECHNEXUS_EXECUTION_GATEWAY_BASE_URL"
GATEWAY_CREDENTIAL = "SPEECHNEXUS_EXECUTION_GATEWAY_COMPONENT_CREDENTIAL"
ASSET_INSPECTOR = "SPEECHNEXUS_ASSET_INSPECTOR"
INSPECTION_URL = "SPEECHNEXUS_ASSET_INSPECTION_URL"
INSPECTION_TOKEN = "SPEECHNEXUS_ASSET_INSPECTION_TOKEN"
GATEWAY_QUERY = "SPEECHNEXUS_GATEWAY_CONTROL_QUERY_DATABASE_URL"
GATEWAY_TOKEN = "SPEECHNEXUS_GATEWAY_COMPONENT_TOKEN"
CONTROLLER_ROOT_KEY = "SPEECHNEXUS_CONTROLLER_ENGINE_AUTH_ROOT_KEY"

# Identity DSNs each runtime constructs only under ``SPEECHNEXUS_PROCESS_ROLE=api``.
API_ROLE_IDENTITIES = (
    JOB_WRITER,
    PROFILE_WRITER,
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

# The judgment that each identity-bearing runtime refuses under a non-API role.
API_ROLE_PROBES = (
    (JOB_RUNTIME, ("JobRuntime.__init__",)),
    (PROFILE_RUNTIME, ("ProfileRuntime.__init__",)),
    ("backend/src/speechnexus_backend/model_catalog_runtime.py", ("ModelCatalogRuntime.__init__",)),
    ("backend/src/speechnexus_backend/resource_pool_runtime.py", ("ResourcePoolRuntime.__init__",)),
    (
        "backend/src/speechnexus_backend/binding_runtime.py",
        (
            "_BindingRuntime.__init__",
            "BindingRuntime.__init__",
            "BindingDefaultsRuntime.__init__",
            "BindingAdminReaderRuntime.__init__",
            "BindingLifecycleRuntime.__init__",
        ),
    ),
    ("backend/src/speechnexus_backend/audit_reader_runtime.py", ("AuditReaderRuntime.__init__",)),
    (
        "backend/src/speechnexus_backend/model_instance_runtime.py",
        ("ModelInstanceRuntime.__init__",),
    ),
    (
        "backend/src/speechnexus_backend/workspace_writer_runtime.py",
        ("WorkspaceWriterRuntime.__init__",),
    ),
    (
        "backend/src/speechnexus_backend/pronunciation_runtime.py",
        ("_PronunciationRuntime.__init__",),
    ),
    (
        "backend/src/speechnexus_backend/quarantine_release_runtime.py",
        ("QuarantineReleaseRuntime.__init__",),
    ),
)

RETIRED_INSPECTOR_KEYS = (
    "SPEECHNEXUS_AUDIO_INSPECTION_RUNTIME_ROOT",
    "SPEECHNEXUS_AUDIO_INSPECTION_COMMAND",
    "SPEECHNEXUS_AUDIO_INSPECTION_LAUNCHER_PYTHON",
    "SPEECHNEXUS_AUDIO_INSPECTION_BUBBLEWRAP",
    "SPEECHNEXUS_AUDIO_INSPECTION_PRLIMIT",
)

# Budgets whose consumer moved into the inspector container. Their API-side
# defaults must equal the defaults forwarded by Compose (see the meta-guard's
# default-consistency check) or a legitimate deployment is refused as drifted.
MIGRATED_LIMIT_KEYS = (
    "SPEECHNEXUS_AUDIO_INSPECTION_MEMORY_BYTES",
    "SPEECHNEXUS_AUDIO_INSPECTION_CPU_SECONDS",
    "SPEECHNEXUS_AUDIO_INSPECTION_OUTPUT_BYTES",
)

AUDIO_ONLY_LIMIT_KEYS = (
    "SPEECHNEXUS_AUDIO_INSPECTION_MAX_DURATION_MS",
    "SPEECHNEXUS_AUDIO_INSPECTION_MAX_REFERENCE_DURATION_MS",
    "SPEECHNEXUS_AUDIO_INSPECTION_MAX_INPUT_BYTES",
    "SPEECHNEXUS_AUDIO_INSPECTION_WALL_SECONDS",
    "SPEECHNEXUS_AUDIO_INSPECTION_MAX_RECEIPT_BYTES",
)

# API-side field names and their Compose-forwarded defaults. The meta-guard binds
# these to settings.py, asset_runtime.py, deploy/compose.yaml and
# deploy/compose.env.example rather than trusting four hand-kept copies.
AUDIO_LIMIT_FIELDS = (
    "audio_inspection_max_duration_ms",
    "audio_inspection_max_reference_duration_ms",
    "audio_inspection_max_input_bytes",
    "audio_inspection_wall_seconds",
    "audio_inspection_max_receipt_bytes",
)
MIGRATED_LIMIT_FIELDS = (
    "audio_inspection_memory_bytes",
    "audio_inspection_cpu_seconds",
    "audio_inspection_output_bytes",
)

TRANSCRIPTION_KEYS = (
    "SPEECHNEXUS_TRANSCRIPTION_RECIPE_FILE",
    "SPEECHNEXUS_TRANSCRIPTION_INPUT_BUCKET",
    GATEWAY_URL,
    GATEWAY_CREDENTIAL,
    OBJECT_ACCESS,
    OBJECT_SECRET,
)

SPEECH_KEYS = (
    "SPEECHNEXUS_SPEECH_RECIPE_FILE",
    "SPEECHNEXUS_SPEECH_DEFAULT_LANGUAGE",
    "SPEECHNEXUS_SPEECH_SETTLEMENT_DATABASE_URL",
    GATEWAY_URL,
    GATEWAY_CREDENTIAL,
    OBJECT_ACCESS,
    OBJECT_SECRET,
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
    probes: tuple[str, ...] = ()
    message_keys: tuple[str, ...] = ()
    display: str = ""


def probe(*symbols: tuple[str, tuple[str, ...]]) -> tuple[str, ...]:
    return tuple(f"{path}::{'|'.join(names)}" for path, names in symbols)


def message_marker(row: Row) -> str:
    """The operator-table "消息点名" cell for a row."""
    if row.behaviour != NAMED:
        return "—"
    return "键名" if row.names_keys else "身份（不含键名）"


ROWS: tuple[Row, ...] = (
    Row(
        keys=(JOB_ADMISSION,),
        prerequisite=f"{JOB_WRITER}（admission 池由 JobRuntime.open() 创建）",
        behaviour=NAMED,
        disposition="已修 #660",
        evidence=f"{UNIT_PAIRS}::test_a_job_companion_dsn_without_the_writer_names_both_keys",
        probes=probe(
            (APP, ("_JOB_COMPANION_DATABASES", "_refuse_job_companion_database_without_writer"))
        ),
        message_keys=(JOB_ADMISSION, JOB_WRITER),
    ),
    Row(
        keys=(VOICE_PUBLISHER,),
        prerequisite=f"{JOB_WRITER}（publisher 池由 JobRuntime.open() 创建）",
        behaviour=NAMED,
        disposition="已修 #660",
        evidence=f"{UNIT_PAIRS}::test_a_job_companion_dsn_without_the_writer_names_both_keys",
        probes=probe(
            (APP, ("_JOB_COMPANION_DATABASES", "_refuse_job_companion_database_without_writer"))
        ),
        message_keys=(VOICE_PUBLISHER, JOB_WRITER),
    ),
    Row(
        keys=(VOICE_PUBLISHER, JOB_WRITER),
        prerequisite="同一个 database（host / port / database 一致）",
        behaviour=NAMED,
        disposition="已修 #660",
        evidence=f"{JOB_PG}::test_a_publisher_on_another_database_is_refused_naming_both_keys",
        probes=probe((JOB_RUNTIME, ("JobRuntime.open",))),
        message_keys=(VOICE_PUBLISHER, JOB_WRITER),
    ),
    Row(
        keys=(JOB_ADMISSION, JOB_WRITER),
        prerequisite="同一个 database（host / port / database 一致）",
        behaviour=NAMED,
        disposition="已修 #660",
        evidence=f"{JOB_PG}::test_an_admission_on_another_database_is_refused_naming_both_keys",
        probes=probe((JOB_RUNTIME, ("JobRuntime.open",))),
        message_keys=(JOB_ADMISSION, JOB_WRITER),
    ),
    Row(
        keys=API_ROLE_IDENTITIES,
        prerequisite="SPEECHNEXUS_PROCESS_ROLE=api（每个运行体构造期校验）",
        behaviour=NAMED,
        disposition="无需：既有拒绝（按身份名拒绝，本卡不改写既有消息）",
        evidence=f"{UNIT_PAIRS}::test_a_non_api_role_refuses_every_identity_dsn",
        names_keys=False,
        probes=probe(*API_ROLE_PROBES),
    ),
    Row(
        keys=(PROFILE_WRITER, PROFILE_EVIDENCE),
        prerequisite="两者互为前提（writer 必须有已评审证据文档，反之亦然）",
        behaviour=NAMED,
        disposition="无需：既有拒绝（消息点名两个键）",
        evidence=f"{UNIT_PAIRS}::test_the_profile_writer_and_evidence_document_name_each_other",
        probes=(
            *probe((PROFILE_RUNTIME, ("ProfileRuntime.__init__", "_publication_authority"))),
            # The constant lives in a module using PEP 758 syntax, which the CI
            # interpreter (3.12) cannot parse; pin its exact source line as text.
            "backend/src/speechnexus_backend/speech/infrastructure/profile_publication_evidence.py"
            '::text:EVIDENCE_FILE_VARIABLE = "SPEECHNEXUS_PROFILE_PUBLICATION_EVIDENCE_FILE"',
        ),
        message_keys=(PROFILE_WRITER, PROFILE_EVIDENCE),
    ),
    Row(
        keys=(OBJECT_ACCESS, OBJECT_SECRET),
        prerequisite="两者必须成对出现",
        behaviour=NAMED,
        disposition="无需：既有拒绝（概念级消息，只点名身份）",
        evidence=f"{UNIT_PAIRS}::test_a_partial_object_storage_pair_is_refused",
        names_keys=False,
        probes=probe((BACKEND, ("Settings.asset_credentials_are_paired",))),
    ),
    Row(
        keys=(GATEWAY_URL, GATEWAY_CREDENTIAL),
        prerequisite="两者必须成对出现",
        behaviour=NAMED,
        disposition="无需：既有拒绝（概念级消息，只点名身份）",
        evidence=f"{UNIT_PAIRS}::test_a_partial_execution_gateway_pair_is_refused",
        names_keys=False,
        probes=probe((BACKEND, ("Settings.asset_credentials_are_paired",))),
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
        probes=probe((BACKEND, ("Settings.asset_credentials_are_paired",))),
    ),
    Row(
        keys=(INSPECTION_URL, INSPECTION_TOKEN),
        prerequisite=(
            "SPEECHNEXUS_ASSET_INSPECTOR=audio 且资产运行时已装配"
            "（SPEECHNEXUS_OBJECT_STORAGE_ACCESS_KEY/SECRET_KEY 非空）"
        ),
        behaviour=NAMED,
        disposition="无需：既有拒绝（消息点名键）",
        evidence=f"{INSPECTOR}::test_text_mode_names_every_configured_inspection_setting",
        probes=probe((ASSET_RUNTIME, ("_INSPECTOR_PREREQUISITES", "_select_asset_inspector"))),
        message_keys=(INSPECTION_URL, INSPECTION_TOKEN),
        display="点名拒绝（仅资产运行时已装配时；未装配时整块检查面关闭，见 §已知非拒绝形态）",
    ),
    Row(
        keys=AUDIO_ONLY_LIMIT_KEYS,
        prerequisite=("SPEECHNEXUS_ASSET_INSPECTOR=audio 且资产运行时已装配（文本模式下无消费者）"),
        behaviour=NAMED,
        disposition="已修 #660",
        evidence=f"{INSPECTOR}::test_text_mode_rejects_a_narrowed_audio_only_limit",
        probes=probe((ASSET_RUNTIME, ("_AUDIO_ONLY_LIMITS", "_select_asset_inspector"))),
        message_keys=AUDIO_ONLY_LIMIT_KEYS,
        display="点名拒绝（仅资产运行时已装配时；未装配时整块检查面关闭，见 §已知非拒绝形态）",
    ),
    Row(
        keys=MIGRATED_LIMIT_KEYS,
        prerequisite="无（解码预算由检查器容器拥有，本进程不再消费）",
        behaviour=NAMED,
        disposition="无需：既有拒绝（消息点名键与检查器侧替代键）",
        evidence=f"{INSPECTOR}::test_a_migrated_resource_limit_fails_startup_naming_both_keys",
        probes=probe((ASSET_RUNTIME, ("_MIGRATED_LIMITS", "_refuse_retired_inspector_settings"))),
        message_keys=MIGRATED_LIMIT_KEYS,
    ),
    Row(
        keys=RETIRED_INSPECTOR_KEYS,
        prerequisite="无（容器拥有这些输入，本进程不再消费）",
        behaviour=NAMED,
        disposition="无需：既有拒绝（消息点名键与替代键）",
        evidence=f"{INSPECTOR}::test_text_mode_rejects_a_retired_local_prerequisite",
        probes=probe(
            (ASSET_RUNTIME, ("_RETIRED_PREREQUISITES", "_refuse_retired_inspector_settings"))
        ),
        message_keys=RETIRED_INSPECTOR_KEYS,
    ),
    Row(
        keys=("SPEECHNEXUS_ARTIFACT_READER_DATABASE_URL",),
        prerequisite=f"{OBJECT_ACCESS}/{OBJECT_SECRET}（资产运行时装配）",
        behaviour=DOCUMENTED,
        disposition="无需：既有文档声明（无对象存储凭据时业务端点 503）",
        names_keys=False,
        evidence="docs/components/production-assets.md::With neither key, business endpoints",
    ),
    Row(
        keys=("SPEECHNEXUS_WORKSPACE_WRITER_DATABASE_URL",),
        prerequisite="资产运行时已装配（成对的 SPEECHNEXUS_OBJECT_STORAGE_* 凭据）",
        behaviour=DOCUMENTED,
        disposition="无需：既有文档声明（只设置本变量不会装配写端口）",
        names_keys=False,
        evidence="deploy/README.md::只设置本变量而关闭资产运行时不会装配写端口",
    ),
    Row(
        keys=TRANSCRIPTION_KEYS,
        prerequisite="整套同步转写输入（缺任一项即不装配）",
        behaviour=WARNED,
        disposition="无需：既有启动告警点名全部缺失键",
        evidence=f"{UNIT_PAIRS}::test_partial_transcription_configuration_warns_with_every_missing_key",
        probes=probe((TRANSCRIPTION_RUNTIME, ("build_transcription",))),
        message_keys=TRANSCRIPTION_KEYS,
    ),
    Row(
        keys=SPEECH_KEYS,
        prerequisite="整套同步 TTS 输入（缺任一项即不装配）",
        behaviour=WARNED,
        disposition="无需：既有启动告警点名全部缺失键",
        evidence=f"{UNIT_PAIRS}::test_partial_speech_configuration_warns_with_every_missing_key",
        probes=probe((SPEECH_RUNTIME, ("build_speech",))),
        message_keys=SPEECH_KEYS,
    ),
    Row(
        keys=(),
        prerequisite="所属运行体已装配（对应 DSN/URL/凭据非空）",
        behaviour=DOCUMENTED,
        disposition="无需：文档声明作用域（数值旋钮无法区分 Compose 透传默认值）",
        names_keys=False,
        evidence=f"{DOC}::## 数值与并发旋钮",
    ),
    Row(
        keys=(JOB_DISPATCH,),
        prerequisite="启用 job-dispatch profile 并启动其 worker 入口",
        behaviour=NAMED,
        disposition="无需：既有启动闸门点名拒绝",
        evidence=f"{GATE}::test_a_blank_dispatch_dsn_names_the_environment_variable",
        probes=(f"{JOB_GATE}::text:{JOB_DISPATCH}",),
        message_keys=(JOB_DISPATCH,),
        display=(
            "点名拒绝（仅 worker 入口；配在 API 进程上被 extra=ignore 静默忽略，见 §拼写与未知键）"
        ),
    ),
    Row(
        keys=(GATEWAY_QUERY,),
        prerequisite=f"{GATEWAY_TOKEN}（执行控制启用）",
        behaviour=NAMED,
        disposition="已修 #660",
        evidence=f"{GATEWAY_PAIRS}::test_a_query_dsn_without_execution_control_names_both_keys",
        probes=probe((GATEWAY_SETTINGS, ("Settings.validate_control_configuration",))),
        message_keys=(GATEWAY_QUERY, GATEWAY_TOKEN),
    ),
    Row(
        keys=(
            GATEWAY_TOKEN,
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
        probes=probe((GATEWAY_SETTINGS, ("Settings.validate_control_configuration",))),
    ),
    Row(
        keys=(CONTROLLER_ROOT_KEY,),
        prerequisite="至少一个获批模板声明 engine_auth（否则凭据永不被读取）",
        behaviour=NAMED,
        disposition="已修 #660",
        evidence=(
            f"{CONTROLLER_PAIRS}::test_a_root_key_without_an_authenticated_template_names_both"
        ),
        probes=probe((CONTROLLER_APP, ("_build_dependencies",))),
        message_keys=(CONTROLLER_ROOT_KEY,),
    ),
    Row(
        keys=WORKBENCH_LLM_KEYS,
        prerequisite="四个键同配；缺任一项即不装配内网 LLM",
        behaviour=NAMED,
        disposition="已修 #660（LLM_API_KEY 单独配置此前被静默忽略）",
        evidence=f"{WORKBENCH_PAIRS}::test_a_partial_llm_configuration_is_refused",
        probes=probe((WORKBENCH, ("WorkbenchSettings.llm_configuration",))),
        message_keys=WORKBENCH_LLM_KEYS,
    ),
    Row(
        keys=WORKBENCH_SAVED_ASSET_KEYS,
        prerequisite="五个键同配；缺任一项即拒绝而不是静默关闭保存",
        behaviour=NAMED,
        disposition="无需：既有拒绝（概念级消息，只点名身份）",
        evidence=f"{WORKBENCH_PAIRS}::test_a_partial_saved_asset_configuration_is_refused",
        names_keys=False,
        probes=probe((WORKBENCH, ("WorkbenchSettings.complete_saved_asset_configuration",))),
    ),
)

# Optional selector fields deliberately not rows above. They select a capability on
# their own, so "configured but ineffective" cannot happen without a second key:
# they either take effect or fail their own startup self-check.
EXEMPT: dict[tuple[str, str], str] = {
    (
        WORKBENCH,
        "cleanup_database_url",
    ): "独立开关：缺省即不重放删除账本，显式配置后由启动身份校验拒绝越权；无配对前置",
}

# Classes whose optional selector fields the inventory check must see. The guard
# discovers the settings classes itself; this mapping pins the expected name and
# environment prefix so a renamed class or prefix cannot silently drop coverage.
SETTINGS_CLASSES: tuple[tuple[str, str, str], ...] = (
    (BACKEND, "Settings", BACKEND_PREFIX),
    (WORKBENCH, "WorkbenchSettings", WORKBENCH_PREFIX),
    (GATEWAY, "Settings", GATEWAY_PREFIX),
    (CONTROLLER, "Settings", CONTROLLER_PREFIX),
)
