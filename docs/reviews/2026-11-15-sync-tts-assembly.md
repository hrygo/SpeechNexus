# PR #843 非作者审查：同步 TTS 参考部署装配（#821）

- 审查对象：PR #843 `fix/821-tts-assembly`，head `7669427910a2c686321e364509d07cb986ba4453`，自报修 #821；单提交 `feat(deploy): assemble synchronous TTS in the reference deployment (#821)`
- 基线：审查开始时 `origin/main = 32498ccc`；审查期间 main 前进一个 review-only 提交，现行 `origin/main = 6a4d7184`（`docs/reviews/2026-11-15-readiness-error-code.md`），本报告按 6a4d7184 复核
- PR 分叉点：`git merge-base origin/main 76694279 = c672b6b2`（PR 落后 main 两个提交）
- 审查分支/检出：`/private/tmp/sn845`，`review/845-tts-assembly`（基于 `origin/main`，已 fast-forward 到 6a4d7184；本目录只放本报告）
- 运行检出：`/private/tmp/sn843pr`（detached `76694279`，跑 loader、校验器、单测、`tests/ci`、dry-run）；变异副本只写 `/tmp/sna*` 与被 gitignore 的 `deploy/.runtime/`、`deploy/compose.env`，不写产品代码
- 环境：macOS arm64、Python 3.14.9、uv 0.13.5、Docker 29.9.3、Compose v5.7.0、gh 2.103.2；**未起参考栈**（未起 PostgreSQL/MinIO）
- 审查时间：2026-11-15 16:54–17:2x（Asia/Shanghai）
- 被审内容一律 `git show 76694279:<path>` 读取
- 报告文件名预检：`origin/main` 的 `docs/reviews/` 共 61 个文件（含 6a4d7184 新增的 readiness 审查），无 `2026-11-15-sync-tts-assembly.md`，未见同名或近名报告
- 范围：`git diff origin/main...76694279 --stat` = 13 文件 / +724 / −43，全部位于 `deploy/`、`docs/`、`scripts/dev-env/`、`tests/`，无产品运行代码
- 硬要求：本分支提交后 `git diff origin/main...HEAD --stat` 只有本报告一个文件（见文末）

## 结论

**不通过（必须修 6 项：4 项内容、2 项交付状态）。**

装配方向是对的，而且做对了几件难事：compose 最小插入、`sn_speech_settlement` 在全新与升级两条预置路径上补齐、样例经生产 loader 校验、样例无凭据、四种反例中的三种可独立复现为红、`tests/ci` 与 `tests/unit/backend` 例数对得上、`reference-acceptance --dry-run` 的 pending 活性验证是真实的。顺手修的转写样例漂移也确实修对了。

但本卡最核心的问题恰好没有守住：**校验器会给一个「照它自己测试夹具替换完」的装配报 `replace-ready`，同时样例里仍有 6 类未被点名的占位值，其中多类会让受理阶段失败或让 TTS 静默关闭。** 也就是说，「看起来能装、实际装不上」的风险从样例本身转移到了「校验器盖章」这一步。此外 PR 与当前 main 冲突、该 head 上 **0 个 CI check-run**，两处文档把测试路径写错，`deploy/README.md` 同一文件内仍保留「TTS 未装配」的旧结论。

四个核心问句的短答：

| 问句 | 短答 |
| --- | --- |
| 样例替换后真能装吗 | **能装配（factory 返回非 None），不能保证可受理。** 照 README 替换 + settlement 角色 + 已发布合格声音后，`build_speech` 能起来；但首个请求还需要 TTS `engine_instances` 注册、plan 摘要/预算与注册值逐项相等、`policy_revision_id` 等于声音资格里的 `gateway_policy_revision_id`，这些在样例、`deploy/private/README.md`、`deploy/README.md` 的替换清单里都没有出现（只在 `docs/plans/mvp-release-readiness.md` B4 以「运行 plan 的权威取值」笼统带过）。 |
| 漏挂卷就起不来是好事吗 | **方向是对的（fail-closed 优于静默关闭），但当前实现让代价被低估：** 失败信息是 `Invalid private speech admission recipe`，不点名文件、不保留 `FileNotFoundError`；且 compose 把路径写死，连只要 ASR 的部署也必须提供 TTS 私密文件，没有文档化的退出方式。 |
| 列面只做源码对齐的风险 | **低。** 该列面不是只有源码对齐：`disable_settlement_grants` + `tts_settlement_worker_grants` 这一对已在真实 PG 集成用例里配合 `_verify_worker` 跑过（`tests/integration/test_tts_durable_pg.py:413-432` 创建同一对 grant 并调用 `_verify_worker`，`:446-448` 验证多给一列即红，`:455-480` 让完整 `TtsSettlementRuntime.open()` 跑通）。若真不足，后果是 **API 整体启动失败**而非运行期静默；但底层异常被线程吞掉，只剩 `TTS settlement worker failed to start`，排障成本高。 |
| 半替换能否绕过 | **对校验器已覆盖的身份字段不能**（只改 `model_id` 仍红）；**对未覆盖字段能**：`policy_revision_id`、`stage.instance_id`、`config_digest/topology_digest/qualification_digest`、endpoint `address/port`、已过期 `admission_expires_at_us` 都能在 `replace-ready` 下漏过（实测 4 条绿）。PR 自带的「replaced」测试夹具正是这种形态，等于把 false-green 固化进 CI。 |

## 判定总表

| 项 | 内容 | 判定 | 证据 |
| --- | --- | --- | --- |
| A1 | 样例合成值是否会被当成替换完 | **部分成立**：loader 层字段完整、无凭据、摘要正确；但 6 类占位值未被文档/校验器覆盖，其中多类影响受理、也影响装配 | §A1 |
| A2 | `recipe_digest` 重算流程可执行 | **成立**：runbook §3 脚本原样跑通，两样例摘要与提交值一致；但脚本注释里的错误文案与真实报错不符 | §A2 |
| A3 | 凭据边界 | **成立**：样例无凭据字段；全零 token 被点名；schema 只接受小写 64-hex，无「全零变体」漏网；但 `address/port`、`objects.endpoint` 等端点占位未被检查 | §A3 |
| B4 | 缺文件即中止 vs 键未设即关闭 | **语义成立且方向正确，诊断不足**：已实测两种路径；失败信息不点名文件，compose 无法从 env 关闭该键 | §B4 |
| B5 | `SPEECHNEXUS_ASSET_INSPECTOR=text` 保持默认 | **成立**：`audio` 需要 URL+token 且缺一即拒绝启动，翻默认会让基线拓扑起不来；标注足够显著 | §B5 |
| B6 | settlement 角色两条预置路径 | **成立**：全新/升级路径都创建角色并重放同一对授权；列面风险低（有 PG 用例）；失败代价是整个 API 启动失败、错误被吞 | §B6 |
| C7 | 三条反例独立复现 | **成立**：未替换=10 条点名/EXIT=1；缺字段=拒绝；文件不存在=中止；键未设/过期=警告+关闭 | §C7 |
| C8 | 半替换绕过 | **部分成立（有绕过）**：只改 `model_id` 红；改 checker 认识的字段即可绿，留下 4 类未点名占位 | §C8 |
| D9 | 转写样例原本加载即失败、修正是否正确 | **失败成立、修正正确、原因陈述失实**：主因是 `binding_publication_id != binding_revision_id`，原摘要并未过期；修改在范围内可接受，但应更正叙述并独立提交 | §D9 |
| E10 | diff 范围 | **成立**：无同步 TTS 业务逻辑/契约语义变更，无手改生成产物 | §E10 |
| E11 | 例数与 CI | **例数成立，CI 不成立**：`tests/ci` 399（基线 399）、`tests/unit/backend` 3284（3273+11）、新用例 11/11 通过；但该 head **0 个 check-run、0 个 Actions run**，`gh pr checks 843` 为 `no checks reported` | §E11 |
| E12 | dry-run PASS 与 pending 活性 | **成立**：`DRY-RUN PASS steps=7 pending=2`；移走 recipe 后 `pending=3` 且出现「缺少同步 TTS recipe」，恢复后回到 2 | §E12 |
| — | PR 可合并性 | **不成立**：与当前 main 在 `docs/plans/mvp-release-readiness.md` 冲突（`git merge-tree` 报 CONFLICT） | §交付状态 |

## A. 样例可装配性

### A1 逐字段：照 README 替换后到底能不能装

`deploy/private/speech-recipe.example.json`（59 行，任务书写的 60 行不影响结论）通过 `load_tts_recipe`，`recipe_digest` 与 RFC 8785 重算一致；无任何凭据字段。问题不在 schema，而在「替换清单 → 运行时事实」之间：

| 字段 | 必须匹配什么（源码事实） | 文档是否点名 | checker 是否点名 | 不改的后果 |
| --- | --- | --- | --- | --- |
| `model_id` | gateway `served_model` | 是（private/README + runbook） | 是 | 启动可过，受理 `reconciliation_required` |
| `binding_revision_id` / `manifest_id` | `runtime_plan.binding` 同值 + endpoint target | 是 | 是（recipe 内） | loader 拒绝或受理失败 |
| `policy_revision_id` | **声音资格/审批里的 `gateway_policy_revision_id`**（`builtin_voice_qualification.py:54`） | **否**（连 runbook 都只说「同样要求」，没说从哪取） | **否** | 启动期 `no current qualified voice`，同步 TTS 静默关闭 |
| `stage.instance_id` | endpoint `target.instance_id` + 已注册 `engine_instances` | runbook 泛提「instance_id」 | **否** | 启动可过；请求期 503 `backend_unavailable` |
| `runtime_plan.binding.config_digest` | `engine_instances.config_digest`（`runtime_sql.py:163`） | 泛提「digest」 | 只查 `-compose-example` 字样，样例 `sha256:…0004` **不被点名** | 受理 `reconciliation_required` |
| `runtime_plan.topology_digest` / `qualification_digest` | readiness 收据/实例拓扑（`runtime_observation_codec.py:190-191`） | **否** | **否**（样例 `…0005`/`…0006`） | 受理 `reconciliation_required` |
| `runtime_plan.peak/steady/per_slot`、`max_slots`、`loading_policy`、`memory_architecture` | 注册的实例向量（`runtime_sql.py:153-183`） | **否** | **否** | 受理 `reconciliation_required` |
| `admission_expires_at_us` | 必须在未来；启动期过期 → 工厂返回 None；运行期过期 → 每次受理 `policy_or_resource_disabled`（`admission.py:313`） | 是（private/README + runbook 均写「过期即关闭」） | **否** | 启动静默关闭，或运行中突然 503 |
| `recipe_digest` | RFC 8785 重算 | 是（runbook §3） | 是（loader） | 启动即失败 |
| endpoint `address`（`10.0.0.11`）、`port`（18101） | 部署网络内真实引擎 | runbook 第 5 条泛提 | **否** | `replace-ready` 后请求连不上引擎 |
| gateway `objects.endpoint`（`172.28.0.10:9000`） | 部署实际 MinIO | private/README 专门说明可改 | **否** | 对象存储不可达 |

样例的 `admission_expires_at_us=4000000000000000` = **2096-10-02T07:06:40Z**，本身不会过期，「过期即不装配而非报错」的语义在 `deploy/private/README.md` 与 runbook §3 里都写清了——这一点做得对。缺的是校验器不验证它（§C8 实测）。

**装配层结论（`build_speech` 能否返回非 None）**：能。`speech_runtime.py:50-67` 的七个必填项 + 已发布且合格的内置声音；样例替换后这些都可以满足。**受理层结论**：不能只靠本 PR 文档完成——还缺 TTS `engine_instances` 注册（参考部署 §8 的 `engine-register` 只吃 `SPEECHNEXUS_TRANSCRIPTION_RECIPE_FILE`，`deploy/compose.yaml:579`；手动模式 `register-engine.md` §「手动模式」可以注册但没有与 TTS 装配文档交叉引用）、已发布 binding 与合格声音。PR 用「真实引擎、已发布且合格的内置声音版本、一次 200」概括，未点出「实例注册 + plan 字段来源」，这是「装完仍 503」最可能的实际卡点。

### A2 `recipe_digest` 重算流程：实跑

按 runbook §3 原样执行（`uv run python`，非 `--all-packages`），两条 recipe 都成功重算，且与提交值一致：

```text
transcription-recipe.json recipe_digest: sha256:de85ebca291f1c7ed660a155092bfd74327d956201a0ba0b4bf61001d28d08f2
speech-recipe.json recipe_digest: sha256:3a553bbedc78190f86a0b333115be40fdc7fd51580fdb7f256ae2ad00c34ca67
```

另用 `rfc8785` 对提交文件直接重算：两处 `match=True`。脚本可执行、幂等、保留 0600。**但 runbook 的说明「否则 API 启动时报配置摘要不符」与真实错误不符**：`load_tts_recipe` 把所有 `ValueError/OSError/…` 统一成 `Invalid private speech admission recipe`（`tts_recipe.py:80-81`），不会说「摘要不符」。这正是本卡警告的「错误信息可能指向别处」：摘要错、字段缺、文件不存在三者报同一句话。

同一类问题还有一处：`check-tts-assembly.py` 用相对目录调用时，会把「相对路径」误报成内容问题：

```text
$ uv run --all-packages python scripts/dev-env/check-tts-assembly.py --private-dir deploy/.runtime/private
tts assembly problem: speech-recipe.json rejected by the production loader: Invalid private speech admission recipe   # exit=1
```

根因：`_private_directory` 支持相对值（`:44-70`），但 `load_tts_recipe` 要求绝对路径（`tts_recipe.py:43`），脚本没有 `.resolve()`。规范流程（runbook §2 生成 `deploy/compose.env`，写入绝对 `SPEECHNEXUS_PRIVATE_CONFIG_DIR`）下文档命令可用；但用 `deploy/compose.env.example` 的相对值、或直接传相对 `--private-dir` 时，操作者会以为文件内容错了。建议一行修复。

### A3 凭据边界

- 样例与新 README **没有**真实凭据/私有路径：`speech-recipe.example.json` 无任何 `token/credential/password` 字段；新测试 `test_the_examples_hold_no_real_credential_or_host_path` 扫 `TTS_RECIPE/TTS_ENDPOINT/TTS_GATEWAY` 的 `/Users/`、`/opt/`、非零 64-hex。
- `bearer_token` 全零占位：endpoint schema 是 `re.fullmatch(r"[0-9a-f]{64}", …)`（`vllm_tts.py:44-45`），所以大写形式 schema 直接拒绝；「合法但无效」的全零形态只有 `"0"*64` 一种，checker 精确匹配（`check-tts-assembly.py:38,114`）能抓到。**没有别的全零变体漏网。**
- 漏的是**非 token 的端点占位**：endpoint 的 `address: "10.0.0.11"`、`port: 18101`，gateway 的 `objects.endpoint: "http://172.28.0.10:9000"`，以及 `tts_policy.language: null`。这些不在「凭据」范围，但会让 `replace-ready` 的装配指向不可达引擎；private/README 对 172.28.0.10 有专门说明，对 10.0.0.11 没有。

## B. 装配语义与失败模式

### B4 「键设了但文件读不出 → 中止」vs「键未设 → 关闭」

实测（stub settings 调生产 `build_speech`）：

```text
--- case 1: key set, file absent ---
RAISED: ValueError -> Invalid private speech admission recipe
--- case 2: key unset ---
WARNING … Synchronous speech unavailable; missing configuration: SPEECHNEXUS_SPEECH_RECIPE_FILE
RESULT: None
--- case 3: key set, valid file, expired ---
WARNING … Synchronous speech unavailable; private recipe expired
RESULT: None
```

`compose.yaml` 把 `SPEECHNEXUS_SPEECH_RECIPE_FILE` 写成字面路径，因此参考部署里不存在「键未设」分支：漏挂卷/漏 copy 一律走 case 1，`create_app_from_env` 不捕获，API 起不来。

**结论：fail-closed 是对的。** 理由：如果漏文件变成「TTS 静默关闭」，一个只验证了 `/healthz` 的部署会看起来健康，而 TTS 在请求时 503/关闭，正是 #821 要消除的「装配不出来却不知道」；显式中止能把问题锁定在启动日志。参考部署的最小面本来就是同步 ASR+TTS，TTS 私密文件成为必需项与其产品定位一致。

但代价要记清、并在必须修里收口：

1. 失败信息不点名文件、路径或 `FileNotFoundError`，与「摘要算错/字段缺失」不可区分；
2. 只要 ASR 的部署也必须提供 TTS 私密文件（compose 字面量，无法从 env 关掉）；`deploy/README.md` 没有说「如何显式关闭同步 TTS」，也没有 preflight 在 `up` 前点名文件；
3. 与下一条并列时容易误读：settlement DSN 的空白确实是「软关闭」，recipe 缺失是「硬中止」，两者在 compose env 注释里写清了，但 `deploy/README` 的标题段只说了「a missing or malformed recipe stops API startup」，没有说「连 ASR-only 也一样」。

### B5 `SPEECHNEXUS_ASSET_INSPECTOR=text` 默认与显著标注

理由成立，源码逐条对上：

- `TextAssetInspector.inspect` 对非 `text_input` 返回 `Inspection(available=False)`（`application/assets.py:87-92`）；
- `reserve_asset` 对不可用检查器直接抛 `AssetError("asset_inspection_unavailable")`（`domain/assets.py:276-277`），HTTP 映射 503（`asset_errors.py:40-43`）；
- 选 `audio` 必须同时给 URL 与 token，缺一在启动期点名拒绝（`asset_runtime.py:150-180`）；选 `text` 却显式配置 audio 前置项也会启动失败（`asset_runtime.py:216-223`）。所以把默认改成 `audio` 会让「没起 asset-inspector 容器、没 token」的基线拓扑直接起不来，确实是把 fail-closed 契约反过来。

标注足够显著：`deploy/README.md` 在 `## Local endpoints` 之后用**加粗独立段**说明「shipped default makes audio uploads fail」并给出 503 与选择路径；runbook §9 失败表也新增一行。没有藏在段尾。唯一小瑕疵：`docs/operations/compose-minimal.md` 的标题仍是「仍不可用的能力」，而同表 TTS 行已改为「已装配，未在本机验证」，建议改名或调行。

### B6 settlement 角色：两条预置路径与列面风险

两条路径都补齐了（逐行核对 runbook）：

- 全新路径 §6：`roles` dict 加入 `sn_speech_settlement`（18 个角色，与正文「十八个」一致），创建后对 `sn_api` 重放 `speech_synthesis_reader_grants`，对 `sn_speech_settlement` 重放 `disable_settlement_grants` + `tts_settlement_worker_grants`；口令导出、DSN 写回（15 个身份、本段新增 11 个）同步；
- 升级路径：`new_roles` 创建同一角色，并重放同一对授权函数；
- 静态守卫 `tests/ci/test_reference_deploy_dsn_coverage.py` 加了 anchor 与 role 绑定（`sn_api` 2 处、`sn_speech_settlement` 2 处），`tests/ci` 399/399 通过。

**列面风险判断：低，而且作者自述「只做源码对齐」是低估了证据。** 这一对授权函数在 `tests/integration/test_tts_durable_pg.py` 里用真实 PG 创建角色、应用同一对 grant、调用 `_verify_worker` 并让 worker 跑完一次结算（`:413-432`），还验证了「多给一列就拒绝」（`:446-448`），另有 `test_worker_lifecycle_with_distinct_restricted_pg_identity`（`:455-480`）验证完整 `TtsSettlementRuntime.open()`。`_verify_worker` 对 schema 内**每一列**做精确比对（多一列少一列都失败，`tts_settlement_worker.py:89-150`），因此「入册列面 = 两函数输出」这一点有 PG 证据，不是只有源码对齐。

若列面真的不足，后果**不是运行期局部错误，而是 API 整体起不来**：`_verify_worker` 抛 `ValueError` → `_thread_main` 捕获后只置 `_failed=True`、不记日志（`tts_settlement_worker.py:248-258`）→ `open()` 抛 `TTS settlement worker failed to start`（`:264-268`）→ `build_speech` 的 `except BaseException` 关闭资源并 `raise`（`speech_runtime.py:154-174`）→ `create_app_from_env` 上抛 → API 容器退出。排障成本在于底层异常被吞，日志只有一句泛化信息。这是既有代码问题，不是本 PR 引入，但本 PR 把「两条路径」作为卖点，建议在 runbook 或启动信息里补一句「失败时看 settlement 线程/授权声明」的定位提示。

## C. 反例与断言强度

### C7 三条反例（独立复现）

1. **占位身份未替换 → 10 条逐条点名，EXIT=1**：

```text
tts assembly problem: model_id='tts-compose-example' is still the committed example's synthetic value
tts assembly problem: runtime_plan.binding.approved_template_id='template-tts-compose-example' is still …
tts assembly problem: binding_revision_id='binding-revision-tts-compose-example' is still …
tts assembly problem: manifest_id='capacity-manifest-tts-compose-example' is still …
tts assembly problem: gateway-file-tts-endpoint.json bearer_token is the example's all-zero placeholder; …
tts assembly problem: endpoint target.binding_revision_id='binding-revision-tts-compose-example' is still …
tts assembly problem: served_model='tts-compose-example' is still …
tts assembly problem: gateway tts_policy.config_revision='policy-tts-compose-example' is still …
tts assembly problem: gateway builtin_voice_binding.voice_id='voice-compose-example' is still …
tts assembly problem: gateway builtin_voice_binding.voice_revision_id='voice-revision-compose-example' is still …
exit=1
```

2. **文件不存在 → 启动中止**：`build_speech` 抛 `ValueError: Invalid private speech admission recipe`（上面 B4 case 1）；`load_tts_recipe(absent)` 同样拒绝，测试 `test_a_recipe_the_api_is_pointed_at_must_exist` 固定了这一点。

3. **删必需字段 → 拒绝**：删掉 `stage` 并用 RFC 8785 重算摘要后仍 `REJECTED: Invalid private speech admission recipe`，证明不是摘要兜底而是字段必需。

4. **键未设 / 已过期 → 警告 + None**（B4 case 2/3），与 compose 注释和 private/README 的「expired leaves TTS switched off」一致。

### C8 半替换：能红，也能绕过

用 PR 自带测试的替换夹具（只替换 checker 认识的字段）构造变体：

| 变体 | 结果 |
| --- | --- |
| 只改 `model_id` + `served_model`（其余占位） | **红**：manifest/binding/token/voice 逐条点名，EXIT=1 |
| 按测试夹具「替换完」，保留 `config_digest=…0004`、`topology_digest=…0005`、`qualification_digest=…0006` | **绿**：`tts assembly replace-ready` + EXIT=0 |
| 再保留 `policy_revision_id='policy-revision-tts-compose-example'` | **绿**：EXIT=0（启动后 `no current qualified voice`，TTS 关闭） |
| 再保留 `stage.instance_id='instance-tts-1'` 而 endpoint target 改成新实例 | **绿**：EXIT=0（受理期实例不匹配） |
| 全替换但 `admission_expires_at_us=1`（已过期） | **绿**：EXIT=0（启动时工厂直接返回 None） |
| `--env-file` 里写有 settlement DSN，但 shell 未 export | 输出 `settlement DSN present: False`，仍 EXIT=0 |
| `--env-file` 里 settlement DSN 为空 | 仍 `replace-ready` + EXIT=0 |

最后两条是另一个独立缺陷：`check-tts-assembly.py:185` 用 `os.environ` 而不是解析 `--env-file`，且 settlement DSN 缺失只打印不计数——脚本开头说「Validate the synchronous TTS assembly … four inputs」（含 settlement DSN role），实际只验证了其中三个文件，第四个无论有无都 `replace-ready`。

**结论：半替换对「身份字段」有效，对「plan/资格/端点/时间字段」无效。** 最严重的是 PR 自己的 `AssemblyCheckerTests._assembly(replace=True)` 恰好构造了「绿但 plan 摘要仍是占位」的形态，`test_a_replaced_assembly_passes` 把它固化成通过用例；后续修 checker 时必须同步改此夹具，否则测试会反向保护 bug。

## D. 顺手修的转写样例漂移（#821 范围外）

`origin/main` 的 `transcription-recipe.example.json` 确实加载即失败（运行检出 `sn843pr` 的 loader，读到的是 main 的字节）：

```text
asr-main.json REJECTED ValueError Invalid private transcription admission recipe
asr-pr.json   OK ApprovedAdmissionRecipe(binding_revision_id='binding-revision-compose-example', …)
```

但**原因不是 PR 叙述的「摘要过期」**：

```text
main committed digest: sha256:8d520c7de6dcf516809f483141f8eaa183df86c00a5695d74d285f61f6bd9366
main computed digest : sha256:8d520c7de6dcf516809f483141f8eaa183df86c00a5695d74d285f61f6bd9366   match: True
redigest-only (只重算、不改 publication id) -> REJECTED
```

真实原因是 `TranscriptionRecipe.__post_init__` 要求 `binding_publication_id == binding_revision_id`（`transcription_recipe.py:216-218`）；原样例写的是 `binding-publication-compose-example`，所以 loader 拒绝。PR 把该字段改成与 revision 同值后加载通过，这是**正确且最小**的修法：`binding_publication_id` 在受理时来自发布 binding 的 `publication.revision_id`（`application/transcription.py:312`），仓库既有约定就是与执行 binding revision 同值（`public_job_admission.py:670,825` 也这么写）。摘要变化是改字段后的必然重算，不是修复「过期摘要」。

**是否该在本卡做**：可以接受。本 PR 新增的 `RecipeExampleLoadsTests` 同时加载两份 recipe，若不复原转写样例，这条新用例在 CI 必红；修法是 2 个字段 + 重算摘要，无行为变更，属于同一类「仓库里的私有样例与实际 loader 漂移」。但应：(a) 在 PR 正文/issue 更正原因，不要写「摘要过期」（`docs/plans/mvp-release-readiness.md:44,82` 也重复了这个错误说法）；(b) 独立成一个 commit 或至少在提交说明里点明范围，便于回退与追溯。

## E. 范围、例数与 CI

### E10 范围

`git diff --stat` 13 文件：`deploy/README.md`、`deploy/compose.env.example`、`deploy/compose.yaml`、`deploy/private/README.md`、`deploy/private/speech-recipe.example.json`、`deploy/private/transcription-recipe.example.json`、`docs/components/synchronous-speech.md`、`docs/operations/compose-minimal.md`、`docs/plans/mvp-release-readiness.md`、`scripts/dev-env/check-tts-assembly.py`、`scripts/dev-env/reference-acceptance.py`、`tests/ci/test_reference_deploy_dsn_coverage.py`、`tests/unit/backend/test_private_recipe_examples.py`。

- 无 `backend/`、`execution-gateway/`、`web/`、`contracts/` 或生成产物改动；没有同步 TTS 业务逻辑或契约语义变更；
- `reference-acceptance.py` 只加了私有文件存在性检查，不改变验收判定；
- 新增三个文件均为 644/普通文件，无模式漂移；
- 与 #839 的关系：两条分支**互不冲突**。`git merge-tree --write-tree origin/fix/828-readiness-expiry 76694279` 无冲突；`compose.yaml` 的插入点不同（本 PR 在 api 服务 environment 的 transcription 段之后，注释 `:143-150`、键 `:151-153`；#828 改 `x-engine-registration-env`、`engine-register`/`engine-readiness` 服务与 gateway 注释，且 `git diff origin/main...origin/fix/828-readiness-expiry -- deploy/compose.yaml | grep -c "api:"` 为 0），`compose.env.example` 本 PR 在 `:53-58` 与 `:127-128`，#828 的 `SPEECHNEXUS_READINESS_WARNING_SECONDS` 插在 `:81` 附近，hunk 不重叠。只需注意两条分支都写同一文件，后合入者需重跑 `docker compose config`，本 PR 无需改动。

### E11 例数与 CI

- `python3 -m unittest discover -s tests/ci`：PR head **399 tests OK**；`origin/main` 基线同样 **399**（任务书里的 408 是 #828 分支的数，本 PR 未变）；
- `uv run --all-packages pytest --collect-only -q tests/unit/backend`：PR head **3284**；`--ignore=tests/unit/backend/test_private_recipe_examples.py` 得 **3273**，差 11 = 新文件 11 条，全部通过（`11 passed in 0.64s`）；
- `gh pr checks 843 --repo hrygo/SpeechNexus` → **`no checks reported on the 'fix/821-tts-assembly' branch`**；`gh api …/commits/76694279/check-runs` → `total_count: 0`；combined status → `pending / total_count 0`；`gh api …/actions/runs?head_sha=76694279` → `total_count: 0`。**该 head 上从未跑过 CI**，PR 正文的「例数/全绿」只能是本地或别的分支结果，不能作为合并门禁证据。
- 代码质量：`ruff check` + `ruff format --check` 对 4 个改动 Python 文件通过；`mypy` 对两个新 Python 文件 `Success: no issues found`。

### E12 dry-run 与活性

按 runbook §2 生成 `deploy/compose.env`（绝对私有目录），再补 4 个验收必需键后：

```text
reference acceptance: DRY-RUN PASS steps=7 pending=2
待输入: gateway-file-asr.json 的 vad_model_file 不存在: …/silero_vad.onnx
待输入: gateway 未 healthy；job-dispatch-worker 的 network_mode: service:gateway 需要它先运行
```

把 `speech-recipe.json` 临时移走后重跑：

```text
待输入: 缺少同步 TTS recipe: …/deploy/.runtime/private/speech-recipe.json
reference acceptance: DRY-RUN PASS steps=7 pending=3
```

恢复后回到 `pending=2`。**活性验证真实**：新增检查确实接在 `stack_private_inputs`（`reference-acceptance.py:1229-1241`）并被 dry-run 调用；`pending 未增加` 不是检查没接上。注意它只验证文件存在，不验证内容（内容由 `check-tts-assembly.py` 负责，而后者见 §C8 的 false-green）。

## 交付状态

- `gh pr view 843`：`state OPEN`，`headRefOid 76694279`，基线 `main`；审查开始时 `mergeable: CONFLICTING`，main 前进后为 `UNKNOWN`（GitHub 重算中）。静态复核 `git merge-tree --write-tree origin/main 76694279` 稳定报：

```text
CONFLICT (content): Merge conflict in docs/plans/mvp-release-readiness.md
```

冲突原因：PR 基于 `c672b6b2`，main 的 `32498ccc` 把该文件第 12 行的 `deployment` 作业名改成 `compose`，而本 PR 重写了同一行的 ③ 列（`#821 已修正…`），两侧都动了这一行。**不 rebase 无法合并。**

## 必须修清单

1. **MF-1 交付状态：rebase 到当前 main，解决 `docs/plans/mvp-release-readiness.md` 冲突，并在该 head 上跑出 CI 绿。**
   - 证据：`git merge-tree` 冲突；check-runs/actions runs 均为 0；`gh pr checks 843` 无记录。
   - 影响：冲突与「无 CI」任一都使本 PR 不能被评审为可合并。

2. **MF-2 `deploy/README.md:91` 旧结论与新章节自相矛盾。**
   - 现在同一文件既写 `## Synchronous TTS assembly`（「assembled by this file」），又保留「public TTS remains blocked by the missing public dispatch path; this Compose only prepares the gateway's private file-TTS references」。这是 PR 自己新增章节后遗留的旧结论，必须改成「已装配、未在本机复跑 200」或明确限定。

3. **MF-3 `docs/plans/mvp-release-readiness.md` 两处事实错误。**
   - `:44` 与 `:82` 把新测试路径写成 `tests/ci/test_private_recipe_examples.py`，实际是 `tests/unit/backend/test_private_recipe_examples.py`（`tests/ci` 下不存在该文件）；
   - 同两处「摘要过期」失实（见 §D9：main 摘要与内容一致，失败原因是 publication/revision 不一致）；`:44` 的「1730 行」也已是旧值（PR head 为 1974 行）。
   - 影响：读者会去错误的 CI 分片找测试、会按错误的修复原因理解漂移。

4. **MF-4 `scripts/dev-env/check-tts-assembly.py` 的 false-green 必须收口（至少把承诺缩小到它真正检查的范围）。**
   - 未检查却会决定成败的字段：`policy_revision_id`（启动即 TTS 关闭）、`stage.instance_id`（请求期 503）、`config_digest`/`topology_digest`/`qualification_digest`（受理 `reconciliation_required`）、endpoint `address/port`、`admission_expires_at_us` 过期；
   - `settlement DSN` 缺失仍 `replace-ready`，且 `--env-file` 里的 DSN 不生效（`os.environ` 读取），输出 `settlement DSN present: False` 会误导；
   - 相对 `--private-dir`/相对 env 值被误报成「production loader 拒绝」（应 `.resolve()`）；
   - 修复时同步改 `AssemblyCheckerTests._assembly(replace=True)`：当前夹具只替换 checker 认识的字段，测试会保护 false-green。
   - 影响：本卡的核心交付物是「把未替换变点名失败」；现状会把 6 类占位值盖章为 replace-ready。

5. **MF-5 文档补「替换完仍差什么才能受理」，或明确 `replace-ready` 只保证文件级替换。**
   - 至少写清：`config_digest`/`topology_digest`/`qualification_digest` 与预算向量必须取自注册的实例（`register-engine.md` 手动模式）；`policy_revision_id` 必须等于声音资格的 `gateway_policy_revision_id`；TTS 实例需要单独注册（参考部署 §8 只注册 ASR）。
   - 影响：「照 README 替换」与「能发 200」之间目前没有可执行的桥。

6. **MF-6 转写样例修复的叙述更正与提交边界。**
   - 把「摘要过期」改成「`binding_publication_id` 与 `binding_revision_id` 不一致」；最好拆成独立 commit 或在提交说明中点明这是 #821 范围外的同类漂移修复（另见 MF-3）。

## 建议修（不阻塞）

1. `load_tts_recipe` 对缺失文件与内容非法返回同一句 `Invalid private speech admission recipe`；fail-closed 保留，但启动失败日志应带路径与 `FileNotFoundError` 语义（或在 `up` 前加 preflight），否则「漏挂卷」与「摘要算错」不可区分。
2. compose 把 `SPEECHNEXUS_SPEECH_RECIPE_FILE` 固定为字面路径，连 ASR-only 部署也被强制；若这是有意，请在 `deploy/README.md` 明说「即使不用 TTS 也必须提供该文件」，并给出显式关闭方式（或允许 env 覆盖为空）。
3. 运行期过期语义没写：`admission.py:313` 会在每次受理检查 `admission_expires_at_us`，过期后请求变 503 `backend_unavailable`，不只是启动期关闭；private/README 目前只描述启动期。
4. runbook 升级路径正文「共十四个角色」与该段 `new_roles` 的 15 项（漏列 `sn_artifact_retention`）不一致；这是既有口径问题，本 PR 改了数量，建议顺手把 retention 写进列举。
5. `docs/operations/compose-minimal.md` 的「仍不可用的能力」标题与其 TTS 行「已装配」并存，建议改标题或移行。
6. endpoint 的 `address: 10.0.0.11`/`port: 18101` 在 private/README 中没有像 172.28.0.10 那样说明，建议补一句「引擎地址/端口必须替换」。
7. `check-tts-assembly.py` 成功时打印 `settlement DSN present` 的语义（shell 环境 vs env 文件）建议与 `--env-file` 行为统一，避免误读。

## 只能依赖作者陈述 / 未验证项

- **未验证**：参考部署上的真实 `POST /v1/audio/speech` `200`、已发布且合格的内置声音版本、真实引擎、TTS 实例注册与完整 settlement 结算。这些需要 GPU 引擎窗口与本机 PG/MinIO 栈；我按授权未起参考栈，只做了源码、生产 loader、本地脚本与既有集成用例的核对。
- **未验证**：`sn_api` 的 `speech_synthesis_reader_grants` 在真实角色上重放后的端到端（runbook 两条路径的真实执行）。我用的是静态锚点测试（`tests/ci`）+ 既有 PG 集成用例（同一对 settlement grants），没有以迁移 owner 跑 §6 脚本。
- **作者自述**：PR 正文给出的是本地结果（`tests/ci` 399 passed/1357 subtests、`ruff check/format`、`mypy` 1474 files、`lint-imports` 9 kept、`compose config` 9 profile），没有声称 GitHub CI 已跑；`tests/ci` 399 与 `compose config` 我独立复核一致。但这些本地结果不能替代该 head 的 CI 门禁（0 check-run），已列入 MF-1。PR 正文「四条反例全红」中资产检查器默认未改动一条已由 diff 证实，其余三条已独立复现。
- **环境限制**：`tests/integration` 需要隔离双库 DSN，本轮未提供，故引用的是仓库既有 PG 用例的存在与内容，而非本轮执行结果；这不改变「列面有 PG 用例保护」的判断，但「本轮重跑」未做。

## 审查过程（时间线）

| 时点 | 动作 | 结果 |
| --- | --- | --- |
| T0 16:54 | `pwd`/`git status`/`git fetch`；确认 `origin/main=32498ccc`、`fix/821=76694279`；`docs/reviews/` 无同名报告 | 基线确定；主工作区只有他人的 `uv.lock`/`.workbuddy/` 未提交改动，未触碰 |
| T1 16:55 | `git worktree add /private/tmp/sn845 -b review/845-tts-assembly origin/main`；读 SKILL 与 `deploy/README.md` | 硬要求起点：diff 为空 |
| T2 16:56 | 读 13 文件 diff；确认范围只含 deploy/docs/scripts/tests | 范围成立 |
| T3 16:57 | 读生产装配：`speech_runtime.build_speech`、`tts_recipe`、`transcription_recipe`、`tts_settlement_worker`、`asset_runtime`、授权函数 | 建立失败路径与列面事实 |
| T4 16:58 | `/tmp/sn843pr` detached 检出 76694279；`tests/ci` 399 OK；unit/backend collect 3284 | 例数初步 |
| T5 16:59 | runbook §3 复制 + 摘要重算脚本原样执行；两样例 loader 通过、摘要 match | A2 成立 |
| T6 17:00 | 复现 C7 三条：未替换 10 条/EXIT=1、删字段拒绝、缺文件中止；键未设/过期=警告+None | C7 成立 |
| T7 17:00 | 构造替换/半替换/过期/相对路径/env-file DSN 变体，跑 checker | 4 条 false-green + 相对路径误报 |
| T8 17:03 | `git merge-tree` 检测与 #828 及 main 的合并性；查 `gh pr checks`/check-runs/actions runs | #828 无冲突；main 冲突；CI 0 次 |
| T9 17:04 | `reference-acceptance --dry-run`（补 4 个合成必需键）→ PASS pending=2；移走 recipe → 3 | E12 活性真实 |
| T10 17:06 | `origin/main` 转写样例 loader 失败证明 + 只重算摘要仍失败 + PR 修复通过 | D9 原因更正 |
| T11 17:07 | 期间 main 前进到 6a4d7184；`git merge --ff-only` 更新审查分支；复核 merge-tree 冲突仍在 | 基线更新 |
| T12 17:08 | `ruff check/format --check`、`mypy`、new tests 11/11、baseline 3273 | 质量门禁 |
| T13 17:12 | 写报告；清理 `/tmp/sna*`、`/tmp/sn*.env`、运行检出与生成目录；提交、push、开 PR | 见文末 diff |

## 硬要求自检

本报告提交后：

```text
$ git diff origin/main...HEAD --stat
 docs/reviews/2026-11-15-sync-tts-assembly.md | 335 +++++++++++++++++++++++++++
 1 file changed, 335 insertions(+)
```

除本报告外，本审查分支没有产品代码、测试、脚本或部署文件改动；所有变异都在 `/private/tmp/sn843pr` 的 `/tmp` 副本与 gitignore 的 `deploy/.runtime/`、`deploy/compose.env` 中，未 push 到被审分支。
