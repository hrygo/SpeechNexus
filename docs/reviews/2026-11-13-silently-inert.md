---
title: "静默无效配置升级为启动点名的非作者审查（#660 / #664）"
status: active
version: "0.1.0"
date: 2026-11-13
---

# 把静默无效的配置升级为启动点名的非作者审查（#660 / #664）

- **审查对象**：`dd0fe3d6`（`fix: 把静默无效的配置升级为启动点名 (#660) (#664)`），PR #664，issue #660，已合入 `origin/main`。改动面 17 个文件、+1537/−25。
- **基线**：审查开始时 `origin/main` head 就是被审提交 `dd0fe3d6`。审查期间 main 前移到 `8e36179d`（#666 审查记录、#662 预热时序判据）；`git diff --name-only dd0fe3d6..8e36179d` 只含 `docs/reviews/2026-11-13-ci-guard-script.md`、`tests/unit/backend/test_tts_durable_intent.py`、`tests/unit/controller/test_warmup_deadline.py`，**未触及被审的 17 个文件**，所以逐条结论对当前 main 仍成立。
- **工作区**：独立 worktree `/private/tmp/sn672`，分支 `review/672-silently-inert`（`git worktree add /private/tmp/sn672 -b review/672-silently-inert origin/main`）。事实取自该 worktree 与 `git show origin/main:<path>`；主检出停在 `a7508635 docs/tracker-batch12`，未读其工作树。
- **审查者**：非作者。产品代码零改动；变异实验临时改过 `app.py`、两个 `settings.py`、`runtime_settings.py`、扫描表模块、文档和一条被引用用例，每次实验后 `git checkout --` 还原并确认 `git status --short` 为空。`uv.lock` 未提交、未改动（提交前核对）。
- **隔离环境**：`postgres:16.15-alpine`，容器 `sn672-pg` 绑定 `127.0.0.1:55440`（与既有 `sn633-*` 隔离），跑完删除。口令只出现在本报告命令中为演示用的合成值。
- **审查方式**：静态审查 + 独立枚举 62 个可选选择键 + 跑 5 个新增/修改测试文件（82 条）+ 3 条真实 PG 集成用例 + 进程级探针（`create_app_from_env`、`uvicorn --factory`、workbench 出厂函数、gateway `Settings`）+ 8 组元守护变异 + 2 个「合法但会被拒」配置的前后对照 + `ruff check/format`。
- **平台限制（非本提交问题）**：在 `dd0fe3d6` 上 `tests/unit/controller/test_warmup_deadline.py` 本机 7 条失败（`OSError: [Errno 49] Can't assign requested address`，用例绑定 `127.0.0.2`，macOS 默认不启用 127/8 全段）。这是既有用例的平台差异，不计入本报告证据；审查期间 main 的 #662 已重写这些用例（`dd0fe3d6..8e36179d` 改了该文件），不影响本报告结论。

> **事实分层**：凡标「实跑」的都有本机命令输出；只做代码路径推导的，在「只能依赖作者陈述/未实测」一节单列。

---

## 时间线

| 时点 | 动作 | 结果 |
| --- | --- | --- |
| T0 | 建 worktree、`git fetch origin main`、`git show --stat dd0fe3d6` | head `dd0fe3d6`；17 文件 +1537/−25；#663 是其父提交 |
| T1 | 读六个改动源文件（`app.py`、`job_runtime.py`、`asset_runtime.py`、workbench `runtime_settings.py`、gateway `settings.py`、node-controller `app.py`） | 5 处新判定：job 伴生 DSN、跨库、文本模式音频预算、workbench LLM key、gateway query DSN、controller 根密钥 |
| T2 | 读扫描表 + 元守护 + 新文档 + 9 个新增/修改测试文件 | 表 23 行（点名拒绝 18、文档已声明 3、启动告警点名 2）；守护 5 条用例；evidence 全部指向新测试 |
| T3 | `uv sync --locked --all-packages`；跑 5 个新增/修改测试文件 | `82 passed in 12.64s` |
| T4 | 起 `sn672-pg`；`SPEECHNEXUS_TEST_DATABASE_URL=… uv run pytest -q tests/integration/test_job_database_pair_pg.py` | `3 passed in 6.69s`（同库 open、跨库 publisher、跨库 admission） |
| T5 | 进程级已知样本：无 job DSN 基线 → 只加 publisher → `uvicorn --factory` | 基线 `STARTED`；只加 publisher 时工厂抛 `ValueError` 并点名两个键，uvicorn 退出码 1 |
| T6 | 独立枚举四个 settings 模块的可选选择键（AST，34+10+17+1=62）与表逐个比对 | 62/62 在表或被 `EXEMPT`；但守护自己的扫描器对 workbench 返回 0 个字段 |
| T7 | 元守护变异 M1–M8 | 5 红 3 绿；详见「元守护变异」 |
| T8 | 构造两个「合法但会被拒」的配置，并用父提交文件跑前后对照 | 工作台空 `LLM_API_KEY`、网关空 query DSN 都从「能启动」变成「启动拒绝」 |
| T9 | 复现两处仍静默形态：资产运行时未装配时的音频检查配置；API 进程上的 `JOB_DISPATCH_DATABASE_URL` | 两者都 `STARTED`，配置无消费者 |
| T10 | `python3 -m unittest discover -s tests/ci` | `Ran 221 tests … OK`（#663 与 #664 的守护一起跑） |
| T11 | `uv run ruff check` 改动的 13 个文件、`uv run ruff format --check .` | `All checks passed!`、`1796 files already formatted` |
| T12 | 写本报告、`uv run --no-project ruff format` 报告、跑 `ruff format --check docs/reviews/` | 见文末校验记录 |

---

## 结论

**有条件通过。**

#660 的已知样本被真正修掉了，而且是在正确的层（组合根）：只配 `SPEECHNEXUS_VOICE_PUBLISHER_DATABASE_URL` 时进程启动失败并同时点名两个键（实测 uvicorn 工厂退出码 1），不再是 503 也不是静默；同库双配在真实 PostgreSQL 上能 `open()`；跨库被点名两个键与「must connect to the same database」，且 `_SplitJobDatabaseError` 不再被 `from None` 掩成通用句。跨库用例覆盖了 pool 清理（`runtime._pool is None`），这一点比只看消息更硬。5 处新判定里，消息质量全部达标（点名键 + 该怎么做），没有出现「只拒绝不点名」的通用句。

但这份改动的两个方向风险都命中了：

1. **误伤可用部署**：两处新判定把「空值」当成「已配置」，把此前能正常启动的配置变成启动拒绝。根因是后端 `Settings` 有 `empty_profile_writer_is_disabled` 把空白归一成 `None`，而 workbench / gateway 的 `Settings` 没有这层归一，新判定却直接测 `is not None`。这恰好违反 #660 自己写的边界「配置项语义不变（只把静默无效变成点名拒绝）」——空白 env 值的语义被改了。构造与前后对照见「误伤面清单」。
2. **「扫描表 + 元守护」这个系统性收尾本身不够硬**：`WorkbenchSettings` 的可选键清单守护是空转的（扫描器只认类名叫 `Settings` 的类），文档却声称「四个 settings 模块」都被扫描；表漏了 3 个已经在点名拒绝的键；「点名拒绝」的图例与 6 行 `names_keys=False` 自相矛盾；把判定逻辑删掉、只留表格行时元守护全绿（只有被引用的那条用例会红）。

修掉 B1–B3 后可转通过；N1–N6 建议同轮清掉。

**必须修**

- **B1（误伤）**：workbench `SPEECHNEXUS_WORKBENCH_LLM_API_KEY=` 与 gateway `SPEECHNEXUS_GATEWAY_CONTROL_QUERY_DATABASE_URL=` 的空白值被判为「已配置」并拒绝启动。要么在模块里补空白→`None` 归一，要么判定改用 `raw.strip()`；与后端 `_configured()` 的既有语义对齐。
- **B2（元守护空转）**：`tests/ci/test_configuration_prerequisites.py` 的 `selector_variables()` 只扫 `ClassDef(name="Settings")`，`WorkbenchSettings` 恒返回 0 个字段（实跑），所以该模块新增可选键不会变红；文档 §元守护第 1 条与 `configuration_prerequisites.py` 模块注释（「scanned settings modules」）都高估了覆盖。
- **B3（表不完整/图例不自洽）**：（a）`_MIGRATED_LIMITS` 的 `SPEECHNEXUS_AUDIO_INSPECTION_MEMORY_BYTES`、`_CPU_SECONDS`、`_OUTPUT_BYTES` 已有点名拒绝（含替代键）却不在表里，而「数值与并发旋钮」段说本表不把这类升级为启动拒绝；（b）图例写「点名拒绝 = 启动失败，消息含键名与该怎么做」，但 6 行标 `names_keys=False`（API 身份、对象存储对、执行网关对、binding 写模式、gateway 执行控制、workbench 保存资产）的消息只有概念名、没有环境变量名。

**建议修**

- N1 元守护只校 evidence 的**存在**，不校它**当前是否真的断言点名**；把判定逻辑删掉、保留表格行时它仍绿（M4a）。issue #660 的 AC「删掉检查 → 对应用例必红」由被引用单测满足，不是由元守护满足；文档「元守护」段不宜再暗示它能守住行为。
- N2 `_AUDIO_ONLY_LIMITS` 的五个默认值在 `settings.py`、`asset_runtime.py`、测试三处重复硬编码，没有一致性守护；默认值一改，Compose 透传的默认值会被判成「操作者漂移」而拒绝合法部署。
- N3 五个音频预算行的「点名拒绝」只在资产运行时装配时才成立（无对象存储凭据时静默），「当前表现」列没有「条件性」取值。
- N4 新文档只被 `docs/operations/compose-minimal.md` 链接，未进 `docs/README.md` 索引；扫描表最后一行与 `## 数值与并发旋钮` 之间缺空行。
- N5 两条用例名/断言不符：`test_a_root_key_without_an_authenticated_template_names_both` 只断言一个键名加 `template` 字样；`test_a_non_api_role_refuses_every_identity_dsn` 只断言通用前缀 `Configure an explicit API`，且参数化来源与表行是同一个常量，常量漏项不会被发现。
- N6 `test_the_profile_writer_and_evidence_document_name_each_other` 的后半只断言 evidence 键，没断言 writer 键。

---

## 一、已知样本（issue #660 三条 AC）

| AC | 结论 | 证据（实测） |
| --- | --- | --- |
| 只配 publisher → 启动失败并点名两个键（不是 503、不是静默） | 通过 | 基线（三键全不配）`create_app_from_env()` → `BASELINE STARTED; job_runtime = None`；只加 `SPEECHNEXUS_VOICE_PUBLISHER_DATABASE_URL` 后同一工厂抛 `ValueError`：`… is configured but SPEECHNEXUS_JOB_WRITER_DATABASE_URL is blank; … so set SPEECHNEXUS_JOB_WRITER_DATABASE_URL or unset …`；`uv run uvicorn speechnexus_backend.transcription_runtime:create_app_from_env --factory` 退出码 1 |
| 同库双配 → 正常启动且发布可用 | 部分通过 | 组合层 `create_app` 接受（`tests/unit/backend/test_startup_key_pairs.py::test_a_job_companion_dsn_is_accepted_when_the_writer_is_configured`）；真实 PG `JobRuntime.open()` 通过（`tests/integration/test_job_database_pair_pg.py::test_a_publisher_on_the_same_database_opens`，`3 passed`）。**「发布可用」未实测**：只断言 `voice_publications is not None`，没有一次真实发布调用；完整 API 进程 + 真实 writer 角色的启动也没有跑 |
| 跨库 → 点名拒绝并说明同库要求 | 通过 | 真实 PG 两条用例：`test_a_publisher_on_another_database_is_refused_naming_both_keys`、`test_an_admission_on_another_database_is_refused_naming_both_keys`，消息含两个键、`must connect to the same database`、`unset …`；并且 `_pool`/`_publisher_pool`/`_admission_pool` 都被清理 |

补充确认（代码 + 测试）：`_SplitJobDatabaseError` 在 `JobRuntime.open()` 的 `except BaseException` 里被单独 `raise`，不再落进 `Public Job writer startup rejected; verify migration and writer role`；权限/迁移类失败仍走通用句（`tests/integration/test_startup_authority_561.py` 期望未变）。两处 `self._admission_pool = admission_pool` / `self._publisher_pool = publisher_pool` 提前赋值，使共享清理路径能关掉已开的池，测试断言了对应用例。

## 二、误伤面（本审查重点）

### 先给结论

5 处新判定里，**2 处能构造出「本来能启动、合并后被拒」的合法配置**，根因同一个：空白 env 值在 workbench / gateway 模块里不是 `None`。其余 3 处（五个音频预算、网关只配 query DSN、node-controller 只配根密钥、job 伴生 DSN）在**非空**配置下我没能构造出误报，理由逐条写在后面。

### FP-1 工作台：空白 `SPEECHNEXUS_WORKBENCH_LLM_API_KEY`（合法但被拒）

`.env` / k8s env 模板把可选键写成 `KEY=`（空值）是常见写法，语义就是「不配置」。合并前它是 LLM 关闭状态并正常启动；合并后拒绝启动：

```bash
# 合并后（dd0fe3d6）
SPEECHNEXUS_WORKBENCH_LLM_API_KEY= \
  … uv run python -c "from speechnexus_backend.workbench.runtime_settings import WorkbenchSettings; WorkbenchSettings()"
# ValidationError: SPEECHNEXUS_WORKBENCH_LLM_API_KEY is configured but
#   SPEECHNEXUS_WORKBENCH_LLM_ENDPOINT, …_MODEL and …_QUALIFICATION_REFERENCE are blank
```

```bash
# 合并前（把 runtime_settings.py 换成 923157c7 的版本再跑同一命令）
# api_key repr: SecretStr('')   llm enabled: False   → 启动正常
```

关键机制：`backend/src/speechnexus_backend/workbench/runtime_settings.py:175` 用的是 `if self.llm_api_key is not None:`，而空白 env 值经 pydantic 得到 `SecretStr('')` 而非 `None`（实跑确认）。这与后端 `settings.py` 的 `empty_profile_writer_is_disabled`（空白→`None`）以及 `asset_runtime._configured()` 注释里写的「a supplied blank counts as unconfigured；Compose 会为未设置的变量转发空值」是同一类问题的两套语义。`deploy/compose.yaml` 目前不转发 workbench 的 LLM 键，所以命中的是运维自维护的 env 文件；一旦有人照后端习惯写空占位，就会起不来。

### FP-2 网关：空白 `SPEECHNEXUS_GATEWAY_CONTROL_QUERY_DATABASE_URL`（合法但被拒）

```bash
# 合并后
SPEECHNEXUS_GATEWAY_CONTROL_DATABASE_URL=postgresql://gw@127.0.0.1:5432/db \
SPEECHNEXUS_GATEWAY_CONTROL_QUERY_DATABASE_URL= \
  uv run python -c "from speechnexus_gateway.settings import Settings; Settings()"
# ValidationError: …CONTROL_QUERY_DATABASE_URL is configured but …COMPONENT_TOKEN is blank
```

```bash
# 合并前（换回 923157c7 的 settings.py）
# PRE-COMMIT STARTED OK; query dsn repr: SecretStr('')
```

`execution-gateway/src/speechnexus_gateway/settings.py:72` 的判定同样测 `is not None`，而 gateway 的 `Settings` 也没有空白归一。官方 `deploy/compose.yaml` 两键都用 `:?` 必填，所以 compose 路径不会命中；命中面是手写 env 文件 / 平台注入的 env 列表（把可选键留空）。这仍然是「空值被当成配置」的语义错误：操作者没有配置这个键，消息却说他配置了。

### 其余三处为什么构造不出误报

- **文本模式下的五个音频预算**：逐个核对消费者——`max_duration_ms`、`max_input_bytes`、`wall_seconds`、`max_receipt_bytes` 只被 `HttpAudioAssetInspector` 读；`max_reference_duration_ms` 被塞进 `AssetPolicy`，但 `AssetPolicy.max_reference_duration_ms` 的唯一读者是 `audio_inspection_http._duration_budget()`，文本检查器 `TextAssetInspector.inspect()` 完全不读它。所以文本模式下五个键确实无消费者，拒绝不误伤。
- **网关只配 query DSN（非空）**：`control_query_database_url` 只在 `execution_control_enabled`（即 `component_token is not None`）的 `_build_control` 与 readiness 里被读；没有 token 的网关不会打开这个池。真实配置形态下没有合法用法。
- **node-controller 只配根密钥（非空）**：`EngineCredentials` 只在模板 `engine_auth != NONE` 时被读，`EngineAuthKind` 只有 `NONE`/`VLLM_API_KEY_V1` 两值，判定与读取点一致。有一个**顺序性约束**可以争论：先下发 secret、后把模板切到鉴权（滚动/分阶段上线）会被拒绝；但 `deploy/images/node-controller.md` 本来写「仅批准模板声明 native engine authentication 时提供」，所以按仓库自己的部署口径这不算合法配置。空白根密钥更不构成新误伤——`EngineCredentials('')` 在合并前就会以「32-byte lowercase hex」拒绝。
- **job 伴生 DSN 空白**：后端 `Settings` 已把空白归一成 `None`，`_refuse_job_companion_database_without_writer` 只看 `is not None`，所以 `VOICE_PUBLISHER_DATABASE_URL=` 不会误报（实跑：基线就是空值，能启动）。

## 三、扫描表与代码的一致性、完整性

独立枚举（AST，`SecretStr`/`Path`/`AnyHttpUrl`/`str` 且带 `None`）：后端 34、workbench 10、gateway 17、controller 1，共 62 个可选选择键。62/62 在表里或在 `EXEMPT` 里——按「表行集合」看是完整的。但表与守护的**判断口径**有问题：

1. **workbench 清单守护空转（B2）**：`tests/ci/test_configuration_prerequisites.py:87` 只接受 `node.name == "Settings"`；`selector_variables(WORKBENCH, "SPEECHNEXUS_WORKBENCH_")` 返回 `{}`（实跑：四个模块分别是 34/0/17/1）。表里的 10 个 workbench 键是人工写进去的，不是因为守护看见了它们。新增一个 workbench 可选键，守护不会红（变异 M2）。
2. **表漏了 3 个既有点名拒绝（B3a）**：`asset_runtime._MIGRATED_LIMITS` 的 `SPEECHNEXUS_AUDIO_INSPECTION_MEMORY_BYTES`、`_CPU_SECONDS`、`_OUTPUT_BYTES` 非默认值会在启动时点名该键与替代键（实跑：`SPEECHNEXUS_AUDIO_INSPECTION_MEMORY_BYTES is owned by the inspector container now, set SPEECHNEXUS_INSPECTION_MEMORY_BYTES`），且已有测试 `test_a_migrated_resource_limit_fails_startup_naming_both_keys`。它们既没进「点名拒绝」行，又被「数值与并发旋钮」段的「本表不把这种情况升级为启动拒绝」错误覆盖。
3. **图例与实际分类不自洽（B3b）**：文档「当前表现」表把「点名拒绝」定义为「消息含键名与该怎么做」，但 6 行 `names_keys=False` 的消息只有概念名（`Configure both object-storage keys together`、`execution control requires both a component token and a lock directory` 等）。模块 docstring 用 `names_keys` 区分了，操作文档没有。
4. **「点名拒绝」的条件性（N3）**：五个音频预算与 `ASSET_INSPECTION_URL/TOKEN` 的拒绝发生在 `AssetRuntime.__init__`，而 `create_app` 只在对象存储凭据存在时才装配它。没有对象存储凭据时，同样的配置能启动、全部静默（见 S1）。
5. **`JOB_DISPATCH_DATABASE_URL` 只覆盖了 worker 侧**：表行以 `tests/unit/test_job_startup_gate.py` 为证据，说的是「启用 profile 并启动其入口」；把它配到 API 进程上仍然静默（S4）。
6. **逐行核对**：16 个 `API_ROLE_IDENTITIES` 与代码里的 role 判定一一对应（`binding_defaults_writer` 经 `_BindingRuntime` 基类共用）；retired 五键的消息确实点名键与替代键；`ARTIFACT_READER`/`WORKSPACE_WRITER` 两条「文档已声明」的断言与代码一致（`ArtifactRuntime` 需要对象存储凭据；`workspace_writer_runtime` 需要资产运行时）；两条「启动告警点名」的测试确实在 caplog 里逐键断言。这部分没有发现错误分类。

## 四、元守护变异（在 worktree 临时改，逐次还原）

| 变异 | 预期 | 实测 | 结论 |
| --- | --- | --- | --- |
| M1 后端 `Settings` 加一个未分类 `SecretStr \| None` 键 | 红 | `FAILED (failures=1)` | 符合声称 |
| M2 `WorkbenchSettings` 加同类键 | 红 | `OK` | **空转**（B2） |
| M3 把 `CONTROLLER_ENGINE_AUTH_ROOT_KEY` 行从「点名拒绝」改成「静默无效」但保留「已修 #660」处置（文档同步改） | 红 | `OK` | **自相矛盾行被容忍**：`静默无效` + `已修` 同时成立 |
| M4a 删掉 `app.py` 的判定逻辑、保留表格行与用例 | 红 | 元守护 `OK`；被引用用例 `2 failed` | 守护只看 evidence 存在，不看行为 |
| M4b 再掏空被引用用例正文（保留函数名） | 红 | 元守护 `OK` | **最危险形态：表格、evidence、守护全绿** |
| M5 新增一行「静默无效」+「无需：保持现状」 | 红 | `FAILED (failures=2)` | 符合声称 |
| M6 改一个 evidence 的 sentinel | 红 | `FAILED (failures=1)` | 符合声称 |
| M7 只改文档某行处置、不动模块 | 红 | `FAILED (failures=1)` | 符合声称 |
| M8 把 publisher 拒绝消息改成通用句 `invalid job configuration` | 红 | 元守护 `OK`；被引用用例 `2 failed` | 点名只由用例守、不只由表守 |

即：守护抓得住「未分类键（仅 3 个 Settings 类）」「失效 evidence」「未处置的静默行」「文档漂移」；抓不住「判定被删」「消息变通用」「workbench 新键」「静默+已修 的自相矛盾」。issue #660 的 AC「删掉检查 → 用例必红」「消息改通用句 → 必红」由**被引用的单测**满足，这点要如实区分，否则「元守护」会被当成行为守护来依赖。

## 五、点名消息质量

| 判定 | 消息是否点名键 | 是否说清怎么做 | 备注 |
| --- | --- | --- | --- |
| job 伴生 DSN | 是（两个键） | 是（set 或 unset） | 最好的一条 |
| 跨库（publisher / admission） | 是（两个键） | 是（point both at one database 或 unset） | 另说明业务原因（一个事务同时写） |
| 文本模式音频预算 | 是（漂移的键） | 部分（select audio 或 keep default） | 没给默认值，操作者要自己查 |
| workbench LLM key | 是（四个键） | 是 | 但空白误伤（B1） |
| gateway query DSN | 是（两个键） | 是 | 但空白误伤（B1） |
| node-controller 根密钥 | 是（一个键） | 是（declare authenticated template 或 unset） | 没点名模板文件变量，措辞可接受 |

没有出现「只拒绝不点名」的通用句。

## 六、仍静默的缺陷清单

- **S1（实测）**：对象存储凭据为空时 `AssetRuntime` 不装配，`SPEECHNEXUS_ASSET_INSPECTOR=audio` + URL/TOKEN + 窄化预算全部静默。实跑：`create_app_from_env()` 返回 `STARTED`，资产运行时不装配。表行把前置写进了「生效前置」列，但「当前表现」仍写「点名拒绝」，对按表判断的操作者是误导。
- **S2**：五个音频预算的默认值三处重复（`settings.py`、`asset_runtime.py` 的元组、`test_asset_inspector_wiring.py`），没有守护把它们绑在一起；默认值变更会让「Compose 透传的默认值」变成「操作者漂移」。这是一处未来会静默漂移的判定。
- **S3（守护层）**：判定逻辑被删、被引用用例被掏空但函数名保留时，表与守护都绿（M4b）。
- **S4（实测）**：`SPEECHNEXUS_JOB_DISPATCH_DATABASE_URL` 配到 API 进程（`extra="ignore"`）时静默忽略，API 正常启动；表的 gate 行只覆盖 worker 入口。属于本卡「配置了却不生效」类，但未列出、未拒绝。
- **S5（范围外但同源）**：四个服务都用 `extra="ignore"`，拼错键名的形态（例如 `…_DATABASE_URLS`）既不会被 Settings 拒绝，也不会进扫描表——这仍是「运维以为配置生效了」的静默面。本卡未声称覆盖，登记在此供后续决定。

## 七、与 #663（`923157c7`）的关系

- #663 改的是 `scripts/dev-env/ci-entry-guard.py`、`tests/ci/test_ci_entry_guard.py`、`.github/workflows/ci.yml`、`pyproject.toml`（`"scripts/dev-env/ci-entry-guard.py" = "py312"`）与 `docs/operations/reference-acceptance.md`/`ci-validation.md`；#664 新增 `tests/ci/configuration_prerequisites.py` 与 `test_configuration_prerequisites.py`。**没有共享文件**，职责不重叠：#663 守参考部署入口脚本的结构与判定站点，#664 守配置键与操作文档的一致性。
- 两条守卫都被 `changes` 作业的 `python3 -m unittest discover -s tests/ci` 执行（该步骤是 #663 改后的形态），#664 的守护只用标准库（`ast`/`re`/`unittest`/`pathlib` + 表模块），符合「装依赖前先跑」的约束。合并后一起跑 `Ran 221 tests … OK`，没有互相干扰。
- 唯一的间接耦合：#664 的表行引用了 `tests/unit/...` 与 `tests/integration/...`，这些用例不在 `tests/ci` 的轻量作业里；若只跑 `changes` 作业，判定行为是否仍在只能靠人。

## 八、文档能否让部署方判断「我的配置会不会被拒」

大部分能：表给出键、生效前置、当前表现、处置，逐行与模块同步（M7 会红），扫描表的「点名拒绝」行都能在代码里找到实现。但有四处会让判断出错：B3a（漏 3 个已拒绝的键，且旋钮段反过来声明不拒绝）、B3b（图例要求键名，6 行没有）、N3（音频预算只在资产运行时装配时拒绝）、B1（空白值的实际语义与「unset」的建议相反）。另外新文档没有进 `docs/README.md` 索引，只有 `compose-minimal.md` 一处链接指向它。

## 九、测试强度

新增/修改共 9 个测试文件，5 个可直接跑（82 条全绿），集成 3 条在真实 PG 上全绿。

- **扎实**：`test_a_job_companion_dsn_without_the_writer_names_both_keys`（参数化两个伴生键、断言两个键名与 `set`/`unset` 修复）、`test_text_mode_rejects_a_narrowed_audio_only_limit`（五个键逐个参数化、断言键名与 `SPEECHNEXUS_ASSET_INSPECTOR=audio`）、`test_a_query_dsn_without_execution_control_names_both_keys`、`test_a_partial_llm_configuration_is_refused`（四个键名）、两条真实 PG 用例（消息 + 池清理）。
- **与名字不符**：`test_a_root_key_without_an_authenticated_template_names_both` 只断言一个键名；`test_a_non_api_role_refuses_every_identity_dsn` 只断言 `Configure an explicit API` 通用前缀，且参数化常量与表行同源。
- **只查拒绝、不查点名**：没有；所有拒绝用例都至少断言了被拒绝键（上面两条除外，行本身标了 `names_keys=False`）。
- **缺**：空白值语义没有任何用例（B1 就是因此没被发现）；「同库双配」没有一次完整 API 生命周期启动；「发布可用」没有真实发布调用；`transaction`/`ordering` 注释声称「拒绝发生在打开任何池之前」，但没有用例证明无连接被打开。

## 十、只能依赖作者陈述 / 未实测的点

- **「发布可用」**：只证明 `voice_publications` 已装配、同库能 `open()`；没有一次真实发布写入。
- **完整 API 进程 + 真实 writer 角色启动**：需要迁移 + 全套授权，本审查只到 `JobRuntime.open()` 与组合层 `create_app`；`uvicorn` 只在「拒绝」方向跑过。
- **元守护在 CI 里是否必然被执行**：`changes` 作业会跑 `tests/ci`，但本机无法核对分支保护是否把该作业设为必需；按日志它存在且绿。
- **#660 的「扫描表如实，未处置项写明理由」整体判断**：我核了 62 个可选键与 6 类判定，未逐个复跑每个 worker 入口（`job-dispatch`、`binding-disable`、`voice-evidence` 等）的静默形态。
- **提交信息第二段**：声称「格式化审查记录中的 Python 代码块」，但合并后的 diff 里没有任何 `docs/reviews/` 文件（`git show --name-only` 为 0 个）；这部分由 #665（`9d887a86`）完成，提交信息与 diff 不一致，不影响结论。
- **平台无关性**：所有实跑都在 macOS + Python 3.14.9 + `postgres:16.15-alpine`；Linux CI 的 socket/绑地址差异未复验（controller warmup 的 7 条失败就是平台差异，已排除为本提交影响）。

---

## 校验记录

```text
uv run pytest -q tests/unit/backend/test_startup_key_pairs.py tests/unit/backend/test_workbench_prerequisites.py \
  tests/unit/gateway/test_settings_pairs.py tests/unit/controller/test_composition_prerequisites.py \
  tests/unit/backend/test_asset_inspector_wiring.py
→ 82 passed in 12.64s

SPEECHNEXUS_TEST_DATABASE_URL=… uv run pytest -q tests/integration/test_job_database_pair_pg.py
→ 3 passed in 6.69s

python3 -m unittest discover -s tests/ci
→ Ran 221 tests in 3.945s — OK

uv run ruff check <13 changed files>       → All checks passed!
uv run ruff format --check .               → 1796 files already formatted
uv run --no-project ruff format docs/reviews/2026-11-13-silently-inert.md
uv run ruff format --check docs/reviews/   → 见提交前记录
```
