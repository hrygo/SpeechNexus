# 参考部署 DSN 覆盖与角色预置的非作者审查（#654 / #572）

- **审查对象**：`1e9f2b12`（`fix: 补齐参考部署角色预置覆盖并加守护 (#572) (#654)`），PR #654，已合入 `origin/main`。
- **基线**：`origin/main` head `8281c2bb`（含 #653）。被审的四个文件在 `1e9f2b12..8281c2bb` 之间**没有**再改动（`git log 1e9f2b12..8281c2bb -- <四个路径>` 为空），审查期间 main 也未前移；因此本报告的逐条结论对当前 main 成立。
- **审查分支 / 工作区**：`review/662-dsn-coverage`，独立 worktree `/private/tmp/sn662`。事实取自该 worktree 与 `git show origin/main:<path>`，没有读主检出（停在旧分支）的工作树。
- **审查者**：非作者。产品代码零改动；只新增本报告。`uv.lock` 未被改动（`git status --porcelain uv.lock` 为空）。
- **审查方式**：静态审查 + 27 个透传键的消费端独立枚举 + 文档 §6 逐块实跑（隔离 Compose project `sn662`、PostgreSQL 16.15-alpine、迁移 head `0053_voice_draft_publication`）+ 7 个身份的启动校验 + 两条撤权/错身份反证 + 新守卫 8 组测试级变异 + 与 `deploy/compose.yaml`、`docs/operations/reference-acceptance.md`、`tests/ci` 的逐项关系核对。
- **隔离环境**：project `sn662`；`postgres:16.15-alpine` 绑定 `127.0.0.1:55449`（与既有 `sn633-*`、`sn656a-*` 及宿主 59000/8000 隔离）；卷 `sn662_postgres-data`；迁移镜像 `sn662-migrate:latest`。口令只存在于被 gitignore 的 `deploy/compose.env`，本报告不记录任何真实值。收尾时 `docker compose -p sn662 ... down -v` 并移除 worktree。
- **审查日期**：2026-11-13。

> **结论针对的「事实」**：本报告把「实测」与「作者陈述」分开。凡标「实跑」的都有隔离环境输出；凡只做了代码路径推导的，在第六节单独列出。

---

## 时间线

| 时点 | 动作 | 结果 |
| --- | --- | --- |
| T0 | `git worktree add /private/tmp/sn662 -b review/662-dsn-coverage origin/main` | 独立于主检出与既有容器 |
| T1 | `git show 1e9f2b12 --stat`、四个文件全文、PR #654 描述 | 改动面：`compose.env.example`＋`compose-minimal.md`（+450 行）＋旧测试例外＋新守卫 562 行 |
| T2 | 机器枚举 `deploy/compose.yaml` 的透传键与容器侧名字 | 27 个透传键；5 个 `:?` 必填、22 个 `:-` 空默认；API/JOB_DISPATCH/REGISTRATION 三键容器内同名 `SPEECHNEXUS_DATABASE_URL` |
| T3 | 逐键核对消费端（settings 字段、`validation_alias`、`env_prefix`、`os.environ` 字面、启动闸门） | 27/27 有运行时消费者，没有发现死键 |
| T4 | 独立起 `sn662` 的 `postgres` 服务并跑迁移 | 16.15-alpine，head `0053_voice_draft_publication` |
| T5 | 抽出文档全部 ```bash 块（`block-00..37`），按 §6 顺序单进程执行 11（口令 export）→12（建角色与授权）→13（DSN 写回） | `roles ready: …`（14 个角色）；写回 11 条 §6 DSN；无异常 |
| T6 | 执行 21–24 验证 SQL | 表级 40 行、只读写入检查 0 行、单行 `speech_binding_families\|latest_revision`、列级 6 行——与文档预期逐行一致 |
| T7 | 执行 30（既有部署授权升级） | 11 个角色幂等重放、`grants refreshed` |
| T8 | 用写回的真实 DSN 直接构造 7 个 runtime 并 `open()` | 7/7 `startup accepted` |
| T9 | 撤 `sn_catalog_reader` 的 `SELECT speech_publication_heads`、撤 `sn_binding_writer` 的 `INSERT speech_binding_commands`；再把两个 DSN 指向 `sn_api` | 两次都 `REJECTED` 且报文点名角色面；错身份同样 `REJECTED` |
| T10 | 复现 `9398627f` 的 `NameError` | 旧 §6 脚本调用 `quarantine_release_grants` 但未 import，脚本在建完角色后抛 `NameError`；`1e9f2b12` 补 import 后通过 |
| T11 | 新守卫 8 组变异（4 红、4 绿） | 见第五节；红=按设计变红，绿=未守住的属性 |
| T12 | 跑 `python3 -m unittest discover -s tests/ci` | `Ran 173 tests ... OK`（基线共 173 条，新增文件 15 条） |

---

## 结论

**有条件通过。**

这份改动解决的是真问题：27 个透传键此前有 11 个在 runbook 里没有名字，6 个「设置里有键、端点 fail-closed、参考部署却没有预置步骤」，`quarantine_release_grants` 的 `NameError` 让 §6 脚本在真实新库上直接跑不完。作者把 27 键逐行过了一遍、把 7 个身份接进 §6、把 5 个判为故意不预置并写出判断方式，又加了一份真能变红的机器守卫；我独立跑 §6 到「7 个 runtime 全部 accepted」，验证 SQL 与文档预期逐行一致，`NameError` 也在旧提交上复现、在新提交上消失。核心主张「没有死键」独立枚举成立。

但仍有两处**文档事实错误/不完整**，都落在「故意不预置」这张表本身，而这正是本片要根治的类：

- **B1（阻塞）**：覆盖表 `SPEECHNEXUS_BINDING_DEFAULTS_WRITER_DATABASE_URL` 行的「未配置时的实际行为」写的是「空值 → 与未配置 binding writer 相同：写入端点 `503 binding_unavailable`」。参考部署里 binding writer 是配置好的，`POST /v1/nexus/admin/model-bindings` 正常写入、不是 503；defaults producer 的 `GET` 路由在 producer 关闭时根本不注册（404），也不是 503。表头声明「每行给稳定失败」，这一行给的是不存在的失败。
- **B2（阻塞）**：`sn_voice_publisher` 的「启用需要什么」不完整：只按文档配 `SPEECHNEXUS_VOICE_PUBLISHER_DATABASE_URL` 不会启动失败、也不会启用——`JobRuntime` 只在 `SPEECHNEXUS_JOB_WRITER_DATABASE_URL` 配置时才构造，voice publisher 池与「必须与 Job writer 同库」的约束都在它内部。按任务给的判据（不预置后能否知道「没启用」以及「启用需要什么」），这一项只满足前半。

修掉 B1/B2（纯文档）后可转**通过**；其余非阻塞项建议同轮清掉，尤其是守卫只查名字/计数的地方——否则下一次「表格看着对、行为已漂移」仍会绿。

---

## 一、审查对象与数字核对

`1e9f2b12` 只改四个文件（`git show --stat`）：`deploy/compose.env.example`（+19/−）`docs/operations/compose-minimal.md`（+450）`tests/ci/test_compose_minimal_role_provisioning.py`（+14/−）新增 `tests/ci/test_reference_deploy_dsn_coverage.py`（562 行）。`deploy/compose.yaml` 未改。

逐项核对数字：

| 声明 | 核对结果 |
| --- | --- |
| 27 个透传 `*_DATABASE_URL` | 成立：`deploy/compose.yaml` 中 `SPEECHNEXUS_*_DATABASE_URL` 的 host 键去重后 27 个 |
| 5 个渲染期必填（`${KEY:?}`） | 成立：API、GATEWAY_CONTROL、GATEWAY_CONTROL_QUERY、MIGRATION、REGISTRATION |
| 22 个空默认（`${KEY:-}`） | 成立 |
| 覆盖表 27 行 | 成立（`tests/ci` 集合比较） |
| 「故意不预置」5 项 | 成立：`BINDING_DEFAULTS_WRITER`、`PRONUNCIATION_READER/WRITER/PUBLISHER`、`VOICE_PUBLISHER` |
| §6 新建 14 个非 owner 角色 | 成立：实跑 `roles ready:` 行 14 个；`api/gateway/gateway_query` 为基线，其余 11 个 |
| 升级脚本重放 11 个角色 | 成立：实跑输出 `grants refreshed` |
| §6 DSN 写回 11 个身份 | 成立（实际写 11 行：4 个此前已有写回的 `workspace/audit/model_instance/quarantine` ＋ 7 个新身份；Job 三键仍在异步 Job 段自己的写回块） |
| 新增 7 个身份 | 成立：`sn_catalog_reader`、`sn_artifact_reader`、`sn_resource_pool_reader`、`sn_binding_list_reader`、`sn_binding_writer`、`sn_binding_admin_reader`、`sn_binding_lifecycle` |
| 设置里 `*_database_url` 字段 20 个 | 成立（`backend/src/speechnexus_backend/settings.py` 类体内，排除基字段 `database_url`） |
| compose − 设置 = 8 个键 | 成立：`API`、`DISABLE`、`DISABLE_SETTLEMENT`、`GATEWAY_CONTROL`、`GATEWAY_CONTROL_QUERY`、`JOB_DISPATCH`、`MIGRATION`、`REGISTRATION` |
| 新守卫「14 条用例」 | **不成立**：实际 15 条（`def test_` 计数 15 行，`python3 -m unittest` 收集 15 条）。非阻塞记账误差 |
| 新守卫自身行数 562 | 成立 |

**对任务书括述的一处校正**：任务说「11 个此前无文档的 DSN 键全部补齐预置步骤，5 个判为故意不预置」——「11 个此前无文档」成立（`git show 1e9f2b12^:docs/operations/compose-minimal.md` 对比，恰好 11 个此前未出现的键），但其中只有 **6 个**补了预置步骤（ARTIFACT、BINDING_ADMIN_READER、BINDING_LIST_READER、BINDING_WRITER、MODEL_CATALOG_READER、RESOURCE_POOL_READER），另 **5 个**是故意不预置（包括 PRONUNCIATION 三键与 VOICE_PUBLISHER、BINDING_DEFAULTS_WRITER）。第 7 个新身份 `sn_binding_lifecycle` 的键在旧文档里被提到过（旧 §6 只说「如果启用生命周期 API，须用 `binding_lifecycle_grants` 与 `binding_publication_grants` 配置其既有非 owner 身份」），但没有创建角色的步骤。所以准确的说法是：**11 个此前无文档的键全部被分类，其中 6 个补步骤；新增 7 个身份进 §6**。PR 描述本身写对了（「改动后为 0」指分类；「补齐的预置步骤（7 个身份）」另列），是任务书转述合并了两句。

---

## 二、两条核心主张

### 2.1「27 个透传键全部有运行时消费者，没有死键」——成立

独立枚举方式：对每个 host 键，先按 `deploy/compose.yaml` 的 `容器名: ${HOST_KEY...}` 找到容器侧 env 名（三处塌缩为 `SPEECHNEXUS_DATABASE_URL`），再在 `backend/src`、`execution-gateway/src` 内查四类读取：`BaseSettings` 类体字段（含 `env_prefix` 组合）、`validation_alias`、`os.environ[...]`/`os.environ.get(...)` 字面、以及启动闸门/worker 的显式常量。结论（全部有消费端，摘录非平凡的几条）：

- `MODEL_CATALOG_READER` → `ModelCatalogRuntime`（`app.py` 仅当 DSN 非空构造）；`ARTIFACT_READER` → `ArtifactRuntime`（还要求 `object_storage_access_key`，§2 已必填）；`RESOURCE_POOL_READER` → `ResourcePoolRuntime`；`BINDING_LIST_READER` → `BindingListRuntime`；`BINDING_WRITER`/`BINDING_ADMIN_READER`/`BINDING_LIFECYCLE` → 三个对应 runtime。
- `BINDING_DEFAULTS_WRITER` → `BindingDefaultsRuntime`，只在「默认参数 producer 模式」独占时构造；与 `BINDING_WRITER` 同时存在时 `app.py` 在构造路由前抛 `Configure one explicit binding writer mode`（`app.py` 的 producer 判定与冲突检查）。
- `VOICE_PUBLISHER` → `JobRuntime._open_publisher_pool()`（仅当 `JOB_WRITER` 已配置、`JobRuntime` 被构造时读取）；这是「有条件消费者」，也是 B2 的根源。
- `GATEWAY_CONTROL_QUERY` 在源码里**从不出现全名**：由 gateway 的 `env_prefix="SPEECHNEXUS_GATEWAY_"` ＋ 字段 `control_query_database_url` 组合出来。这正是「字面扫描不够」的理由，守卫专门处理了这一形态。
- `JOB_DISPATCH` 容器内名是 `SPEECHNEXUS_DATABASE_URL`；`deploy/job-startup-gate.py` 用 `RUNTIME_DSN_ENV = "SPEECHNEXUS_DATABASE_URL"` 连接，报错文案却点名 host 键 `SPEECHNEXUS_JOB_DISPATCH_DATABASE_URL`（表格「空值 → 启动闸门点名拒绝启动」即指此，实测文案一致）。
- `MIGRATION` → `speech/infrastructure/migrations/env.py` 的 `os.environ.get("SPEECHNEXUS_MIGRATION_DATABASE_URL")`；`REGISTRATION` → 引擎注册 profile 读容器名 `SPEECHNEXUS_DATABASE_URL`。

没有发现死键。守卫的读取覆盖对**当前**代码也够：源码里没有单引号 DSN 字面、没有 `AliasChoices`、没有 `f"SPEECHNEXUS_…_DATABASE_URL"` 动态拼接；`os.getenv` 未用于 DSN。残留在第五节作为盲区记录，不影响结论。

### 2.2「11 个此前无文档的 DSN 键全部补齐处置；5 个判为故意不预置且给了启用判断」——分类成立，5 项判断见第三节

11 个未分类键在新表里全部有行、有身份、有失败描述、有处置；`tests/ci` 的集合比较保证未来也一一对应。5 个「故意不预置」的独立判断见下。

---

## 三、「故意不预置」5 项的独立判断

判断标准用任务给的两条：①不预置后，部署方能否从文档知道它没启用；②能否知道启用需要什么；并额外问一句：它是不是把「该补的步骤」包装成了「故意不预置」。

### 3.1 `BINDING_DEFAULTS_WRITER`：理由站得住，但表格行为列写错（B1）

- **理由成立**：它与 `sn_binding_writer` 是互斥的 producer 模式，不是叠加能力；`docs/components/binding-defaults-producer.md` 明说「separate capability profile」，且两者同时配置时 API 拒绝启动。参考部署选 writer 模式，是部署决策，不是没人写步骤。附带核实：`binding_defaults_grants` 返回 `(*binding_draft_grants(schema, role), GRANT SELECT, INSERT ON defaults 表…)`，确实是 superset，文档「重放 `binding_defaults_grants` 即可」正确。
- **站不住的部分（B1）**：覆盖表的失败列与代码不符。参考配置下 `POST /v1/nexus/admin/model-bindings` 由 writer provider 正常处理，不是 `503 binding_unavailable`；`GET` defaults 快照路由只在 `if producer:` 时 `add_api_route`（`speech/interfaces/http/bindings.py` 末尾），producer 关闭时该路径不存在（404），也不是 503。也就是说「默认参数 producer 关闭」在参考部署的可观察事实是「写入照常、defaults 快照端点不存在」，而不是表里的 503。表头承诺「每行给稳定失败」，这一行是这条承诺唯一的反例。
- 启用路径本身可执行：清空 writer DSN → 建角色 → 重放 `binding_defaults_grants` → 启动自检；迁移在 head 上已包含 defaults 表。没有建议改成预置。

### 3.2 `PRONUNCIATION_READER / WRITER / PUBLISHER`：站得住

- **理由成立**：发音词典是独立能力，不在参考部署声明的同步 ASR/TTS 面上；`docs/operations/compose-minimal.md` 的「仍不可用的能力」明确点名，`docs/components/pronunciation-drafts.md` 有「Identities, grants and startup verification」整节与可直接跳转的锚点。实读代码：reader 只允许 `speech_pronunciation_families/revisions/publications` 的 SELECT，writer 只到 drafts＋命令表，publisher 才拿发布账本；组件文档的三身份与其一致。
- **启用所需可推导**：三个键分别对应只读、草稿、原子发布；`publish: true` 在 publisher 缺失时返回 `422 pronunciation_publication_not_supported`，先于 writer 检查（`_create` 里 `if command.publish and publisher is None:` 在前），所以文档「读取 503、发布 422、草稿 503」在「三个键全空」下也成立——这一条我原本怀疑「publish:true 会先撞 writer 的 503」，实读后确认文档是对的。
- 没有发现「本该可用」的证据：即便配了三个身份，参考部署也没有 TTS/Job 消费者去使用已发布的词典（TTS REST 明确拒绝 `pronunciation_revision`），预置反而是造死路径。

### 3.3 `VOICE_PUBLISHER`：理由成立，但启用路径不完整（B2）

- **理由成立**：voice 发布需要 `voice_design`/`voice_validation` 车道产生的测量证据；`deploy/job-startup-gate.py` 的 `GATEWAY_CONFIGS` 只有 `asr`、`tts` 两个 lane，`gateway_lane_findings` 会以「assembles no gateway lane」点名拒绝其余 kind；参考部署也没预置 `JOB_WRITER`，voice 草稿本身都创建不了。配了角色确实是「写入即失败的发布路径」，理由不是包装。
- **不完整（B2）**：只配 `SPEECHNEXUS_VOICE_PUBLISHER_DATABASE_URL` **不会有任何启动报错**，能力仍然 503。`app.py` 只有 `settings.job_writer_database_url is not None` 时才构造 `JobRuntime`；publisher 池、`voice_publications` provider 以及 `Public Job writer and voice publisher must share one database` 的检查都在 `job_runtime.py` 内部。文档的启用清单只写「装配 gateway voice 车道配置与 `SPEECHNEXUS_JOB_DISPATCH_KINDS` → 创建角色重放 `voice_publication_grants` → 配置该键」，漏了「先启用 Job writer 身份（三键）并与 voice publisher 同库」这一步。按判据②，这一项尚未完全站住。
- 其余表述正确：空值时 `503 voice_draft_unavailable`、Job 创建与读取不受影响（实读 provider 装配与 `create` 路由）。

---

## 四、逐条清单

### 4.1 判 5 个「故意不预置」

见第三节：4 项站得住（`PRONUNCIATION_*` 三键算一组、`VOICE_PUBLISHER` 理由、`BINDING_DEFAULTS_WRITER` 理由），其中 `BINDING_DEFAULTS_WRITER` 的行为列与 `VOICE_PUBLISHER` 的启用路径各有缺陷（B1/B2）。没有发现「该能力本该在参考部署可用、只是没人写步骤」的包装：三项都对应参考部署明确不装配的消费面（默认参数 producer、发音词典、voice 证据链），不是「漏了步骤」。

### 4.2 独立枚举死键

见 2.1：27/27 有消费者。补充两条边界：

- `SPEECHNEXUS_VOICE_PUBLISHER_DATABASE_URL` 是「条件消费者」：Job writer 未配置时它被读也无效果（不报错、不生效），文档只说「空值→503」，没说「只配它也没用」（并入 B2）。
- 表外两个键的说法核实成立：`SPEECHNEXUS_SPEECH_SETTLEMENT_DATABASE_URL` 只被未装配的 `tts_settlement_worker` 读取；`SPEECHNEXUS_PUBLICATION_WRITER_DATABASE_URL` 属于 host 侧 `publish_binding.py` CLI 的 `validation_alias`，都不经过 compose，都不是死键。

### 4.3 §6 能否从零照抄、7 个身份启动是否真的通过

**能。** 实跑（单进程、按文档顺序 11→12→13）：`roles ready: sn_api sn_gateway … sn_binding_lifecycle`（14 个）；写回 11 条 §6 DSN；升级脚本（30）`grants refreshed`。随后用写回的 DSN（仅把容器名 `@postgres:5432/` 换成本次隔离端口）构造 7 个 runtime：

```
sn_catalog_reader OK
sn_artifact_reader OK
sn_resource_pool_reader OK
sn_binding_list_reader OK
sn_binding_writer OK
sn_binding_admin_reader OK
sn_binding_lifecycle OK
```

reverse 探针（证明校验不是形式）：撤 `sn_catalog_reader` 对 `speech.speech_publication_heads` 的 SELECT → `Model catalogue reader startup rejected; verify migration and reader role`；撤 `sn_binding_writer` 对 `speech.speech_binding_commands` 的 INSERT → `Binding writer startup rejected; verify migration and minimum role`；把两个 identity 的 DSN 指向 `sn_api` 的连接串 → 两者都 `REJECTED`（不是「只要连得上就过」）。

两点说明：①`ArtifactRuntime.open()` 在本次不依赖对象存储可达（MinIO 未起也 accepted），这只证明启动校验本身，不证明对象存储链路；②我把 7 个 runtime 直接构造，而不是起完整 `speechnexus-api` 进程（完整进程需要 gateway 私有配置，不在 §6 范围内）。

### 4.4 `NameError` 是否存在、修在哪

**存在，且是本次改动修掉的真缺陷。** 在 `9398627f`（#653 合并前的基线）上，§6 fresh-install 脚本建完角色后调用 `quarantine_release_grants(schema, "sn_quarantine_release")`，但脚本没有 import 该函数（升级脚本有 import）；`NameError` 只在执行到调用时发生，所以现象是「角色建好了、授权没重放」。`1e9f2b12^`（`0af59018`）仍是同一形态，`1e9f2b12` 在 fresh 脚本的 import 列表补上 `quarantine_release_grants` 后，脚本一次跑通。修复位置正确（import 在同一个 heredoc 顶部），没有引入重复。

### 4.5 守卫是否真能变红、哪些性质没守

**能变红（4 组）**：

| 变异 | 结果 |
| --- | --- |
| M1 删掉 §6 里 `"sn_catalog_reader": os.environ[...]` | 红：`sn_catalog_reader: missing from the fresh-install role set` |
| M2 给 compose 加一行未分类透传 `SPEECHNEXUS_UNPROVISIONED_READER_DATABASE_URL` | 红：集合差 `['SPEECHNEXUS_UNPROVISIONED_READER_DATABASE_URL']` |
| M3 删掉 `VOICE_PUBLISHER` 的豁免段落 | 红：`missing exemption marker` |
| M4 把 inline GRANT 里的 `resource_pools` 改成 `resource_pool` | 红：`sn_resource_pool_reader: GRANT [...] != READABLE [...] in resource_pool_runtime.py`，且 READABLE 的常量已解析（不是字面串） |

**没守住的（4 组绿）**：

| 变异 | 结果 | 缺的守卫 |
| --- | --- | --- |
| M5 把某行失败码换成另一个真实存在的 503 码（`audit_service_unavailable`） | 绿 | 行为列只匹配 `\d{3} [a-z_]+`，不核对该码是否由该端点/该身份产生 |
| M6 把 `BINDING_LIST_READER` 行的身份与端点改成 `sn_wrong_identity` / `GET /wrong/path` | 绿 | 身份、端点两列完全没有校验 |
| M7 把 DSN 写回里 `sn_binding_writer` 的口令变量换成 `SN_BINDING_ADMIN_READER_ROLE_PASSWORD` | 绿 | 写回块的「角色↔口令变量」配对无守卫（`ROLE_PASSWORD_USE` 只查建角色块） |
| M8 把验证 SQL 的预期输出「六行」改成「五行」 | 绿 | 文档里「预期输出」类断言全部无守卫 |

因此**未守卫的性质**清单（建议后续补）：①表行的身份/端点；②每行真实失败码（而非「像码的字符串」）；③故意不预置理由的事实性；④启用路径的链接与前置条件；⑤验证 SQL 的预期输出（行数、列名、值）；⑥DSN 写回的 password 变量与角色配对；⑦升级脚本口令变量名（同上，变异交换不被发现）；⑧`deploy/compose.yaml` 的「不再需要改」声明（本次成立，但无守卫）；⑨「每个 runtime 启动报文与文档一致」（守卫只查文档里有 `启动` 字样）；⑩死键判定是名字出现即算（见 4.6）；⑪prose 计数（如「十一个」「十四个」）无守卫。

### 4.6 断言强度

- **15 条**用例，不是 PR 描述的 14 条；其中强断言：27 键集合双向相等、22 个空默认的 compose 文本、11 个写回键在两条路径的存在、`ROLE_GRANT_BINDINGS` 的 grant 调用计数、inline GRANT 与 runtime READABLE 的集合相等（**常量解析，不是字面**：`HEAD_TABLE`/`RETIREMENT_TABLE` 由 `table_constants()` 解析后比较，已核对三组 inline reader 全等）。
- **弱断言**：`test_the_runbook_names_this_guard` 只查文件名出现；`test_every_row_states_…` 的行为列只做正则；`test_every_provisioned_row_keeps_its_step` 与 `test_every_deliberate_exemption_is_explained` 是「doc 里出现某段文字」的锚点；`test_role_grant_bindings_are_replayed_in_both_provisioning_paths` 只数出现次数；`test_no_reader_grant_function_is_invented` 只查发明出来的函数名不存在。这些都按设计如此（防删除、防漂移），但**不能**当作事实正确性的证明。
- **死键守卫是名字级的**：`readable_env_names()` 把任何 `"SPEECHNEXUS_…_DATABASE_URL"` 字面都算作「有人读」。实测：新建一个 backend 模块只放 `README = "SPEECHNEXUS_TOTALLY_FAKE_READER_DATABASE_URL"`（从不当配置读），该名字即被计入 readable；再把同名 host 键加进 compose，`test_no_forwarded_dsn_is_a_dead_key` 仍是绿的。能挡住「彻底没人提这个名字」，挡不住「有人把名字写在注释/文档串里充数」。（当前 27 键没有这种情况。）
- **旧测试里的 pronunciation 例外比注释宽**：`test_documented_unavailable_codes_exist_in_the_http_handlers` 把 pronunciation 的组成码拆成前缀＋裸码，在**全部 backend 源码拼接文本**里分别找 `"pronunciation_" + code` 与 `"bare"`；注释声称「同模块、同错误表」，实现并不校验同模块，也不校验该 bare 码真的用于 pronunciation。且它只覆盖 `503`（`UNAVAILABLE_CODE` 形态），文档里的 `422 pronunciation_publication_not_supported` 完全不在任何码校验范围内。
- `test_role_grant_bindings_are_replayed_in_both_provisioning_paths` 只数 `binding_lifecycle_grants` 2 次，不验证 `binding_publication_grants` 是否也在两条路径被重放（文档要求两份声明一起重放）。

### 4.7 文档可执行性

- **§6 新增子节**（`### 公开目录、Artifact、资源池与 binding 身份`，锚点可跳）：建角色 → 写回 DSN → 验证 SQL 三段都能「从零照抄」；只做了隔离适配（`-p sn662`、容器名 `sn662-postgres-1`、端口 55449），没有改写 SQL。验证 SQL 输出与文档逐行一致：表级 40 行（artifact 2、admin_reader 6、lifecycle 14、list 5、writer 6、catalog 5、resource_pool 2）、只读身份写入为 0 行、单行 `speech_binding_families|latest_revision`、列级 6 行（`speech_asset_references|released_at_us`、`speech_catalog_state|generated_at_us`、`speech_catalog_state|revision`、`speech_publication_heads|revision|revision_id|updated_at_us`）。
- **升级段**：11 个角色、`binding_lifecycle_grants` + `binding_publication_grants` 一起重放、inline 三个只读面显式 GRANT——脚本实际包含这些；重复执行幂等。
- 小坑（非阻塞）：§6 的 DSN 写回使用 `os.environ[...]` 读口令，要求 operator 在**同一个 shell** 里执行「口令 export」与「写回」（文档按顺序写在同一节，但没有一句「必须同一 shell」）；我第一遍分 shell 执行时得到 `KeyError: 'SPEECHNEXUS_WORKSPACE_WRITER_ROLE_PASSWORD'`。这是操作提示，不是脚本缺陷。
- 另一个措辞小问题（非阻塞）：写回段的「把十一个新角色的连接地址写回」——写回块确实写 11 行，但其中 4 个（workspace/audit/model_instance/quarantine）是此前就有写回的既有身份，只有 7 个是本片新增角色。建议改成「十一个非基线角色」。

### 4.8 与 `deploy/compose.yaml`、`reference-acceptance.md` 的关系

- `compose.yaml` 本片未改，**也不需要改**：27 个 host 键在父提交已全部透传；11 个写回键都已带 `:-` 空默认；三个新身份不需要独立 service；Job 三键的容器名不变。`compose.env.example` 新增的 7 个 `*_ROLE_PASSWORD` 与 §6 的 `export` 名一一对应（守卫的 `test_each_provisioned_role_has_a_password_source` 也覆盖了）。
- 与 #653 的 `reference-acceptance.md` **没有直接矛盾**：验收入口仍只管 3 个基线角色＋Job 三键，明确「不重做 §6 的撤权与基线角色」；§6 新增的 11 个角色不会让验收的环境要求变多，也不改变 `roles` 步的探针。唯一需要读者自己拼的仍是「§6 是参考部署的角色真相、验收脚本只验 Job 侧」——两边各自成立。
- 「仍不可用的能力」新增行与代码一致：发音词典读取 `503`、`publish: true` `422`、voice 发布 `503`，且 Job 创建/读取不受 voice publisher 影响。

---

## 五、缺陷清单

### 阻塞

**B1** `docs/operations/compose-minimal.md` 覆盖表 `BINDING_DEFAULTS_WRITER` 行「未配置时的实际行为」与代码不符：参考部署下 binding 写入正常（writer 模式），defaults 快照 `GET` 路由不注册（404），不存在 `503 binding_unavailable`。同一段落「表中每行给稳定失败」和「故意不预置的身份」开头的「未配置时返回表中那个稳定错误」因此对会计失真。修法：该行改成「键为空即默认参数 producer 关闭；legacy binding 写入不受影响；defaults 快照端点不存在（404）；两键同配时 API 拒绝启动（`Configure one explicit binding writer mode`）」，并同步开头那句的适用范围。

**B2** §「故意不预置的身份」的 `sn_voice_publisher` 启用路径缺前置：只配 `SPEECHNEXUS_VOICE_PUBLISHER_DATABASE_URL` 不报错也不生效——必须先配置 Job writer 身份（`JOB_WRITER`/`JOB_ADMISSION`/`JOB_DISPATCH` 与应用侧 Job writer）并保证与 voice publisher 同库（`job_runtime.py` 的 `Public Job writer and voice publisher must share one database`），否则 `JobRuntime` 根本不构造。修法：把「先启用 Job writer 三键并与 publisher 同库」写成启用清单第一步，并说明「只配该键时仍稳定 503、无启动报错」。

### 非阻塞

- **N1** PR #654 描述写「14 条用例」，实际 15 条（新增文件 15 个 `test_`）。
- **N2** 新守卫的弱断言面：行内身份/端点（M6）、行为码真伪（M5）、DSN 写回口令配对（M7）、验证 SQL 预期输出（M8）、prose 计数全部可漂移而绿；`test_role_grant_bindings_are_replayed_in_both_provisioning_paths` 只数次数，不验证 `binding_publication_grants` 也被重放。
- **N3** 死键判定是「名字出现即算 readable」：注释/文档串即可让一个真正的死键通过（已用 `README = "SPEECHNEXUS_…_DATABASE_URL"` 证明）。当前 27 键无此情况，但守卫强度低于其声明的「没有死键」。
- **N4** 旧测试的 pronunciation 例外实现宽于注释：全部 backend 源码拼接匹配，不保证同模块/同错误表；`422 pronunciation_publication_not_supported` 不在任何码校验内；`UNAVAILABLE_CODE` 只匹配 503。
- **N5** 文档措辞：写回段「十一个新角色」实际是「十一个非基线身份」（4 个既有＋7 个新增）；§6 DSN 写回未明说「与口令 export 同 shell」。
- **N6** 守卫扫描范围只有 `backend/src`、`execution-gateway/src`：若将来某个 DSN 只被 `scripts/` 或 `deploy/*.py` 消费，会被误判死键（当前所有消费者都在扫描范围内，`MIGRATION` 也在 backend 的 migrations/env.py）。
- **N7** 覆盖表「守护」列对「故意不预置」行也写「覆盖+角色」，但角色守卫并不核对豁免项；且「每行至少由覆盖检查」的「检查」对 identity/端点/真码并不成立（同 N2）。
- **N8** 表头「守护」列把 `test_reference_deploy_job_prereqs.py`、`test_reference_deploy_profile_publication.py` 当作该行事实的守卫，这两个文件确实存在并覆盖 Job/profile 键，但**不覆盖**本表新增的 7 个身份（表内行也没有引用它们，实际不冲突，只是列义偏宽）。

---

## 六、独立复现的反例与探针（可重复）

均在 `/private/tmp/sn662` 内完成；变异只改被审文档，跑完立即从内存恢复原文（`git status --porcelain` 每次为空）。

1. **M1 删角色步骤**（`"sn_catalog_reader": os.environ[...]`）：`python3 -m unittest tests.ci.test_reference_deploy_dsn_coverage` → 红，`['sn_catalog_reader: missing from the fresh-install role set'] != []`。
2. **M2 加未分类透传键**（compose 加 `SPEECHNEXUS_UNPROVISIONED_READER_DATABASE_URL: ${…:-}`）：红，`['SPEECHNEXUS_UNPROVISIONED_READER_DATABASE_URL'] != []`。
3. **M3 删例外段落**：红，`SPEECHNEXUS_VOICE_PUBLISHER_DATABASE_URL: missing exemption marker …`。
4. **M4 改 inline GRANT 关系**（`resource_pools`→`resource_pool`）：红，`sn_resource_pool_reader: GRANT [...] != READABLE [...] in resource_pool_runtime.py`。
5. **M5 换真实但错误的 503 码**（`model_binding_service_unavailable`→`audit_service_unavailable`）：**绿**（盲区）。
6. **M6 改行内身份与端点**（→`sn_wrong_identity`、`GET /wrong/path`）：**绿**（盲区）。
7. **M7 DSN 写回口令变量对调**（writer 行用 admin reader 的口令变量）：**绿**（盲区）。
8. **M8 验证 SQL 预期输出「六行」→「五行」**：**绿**（盲区）。

行为探针：

- 撤权：`REVOKE SELECT ON speech.speech_publication_heads FROM sn_catalog_reader` → catalog `REJECTED: Model catalogue reader startup rejected; verify migration and reader role`；恢复 `model_catalog_reader_grants` 后 accepted。
- 撤权：`REVOKE INSERT ON speech.speech_binding_commands FROM sn_binding_writer` → `REJECTED: Binding writer startup rejected; verify migration and minimum role`；恢复后 accepted。
- 错身份：catalog/binding_writer 用 `sn_api` 的 DSN → 两者 `REJECTED`（不是「能连就过」）。
- 死键名字级：临时文件 `backend/src/speechnexus_backend/zz_probe_deadkey.py` 只含 `README = "SPEECHNEXUS_TOTALLY_FAKE_READER_DATABASE_URL"` → `readable_env_names()` 包含该名字（已删除探针文件）。说明「名字出现」不等于「有消费者」。
- 全量测试：`python3 -m unittest discover -s tests/ci` → `Ran 173 tests ... OK`。

---

## 七、只能依赖作者陈述的点

1. **作者称「7 个 runtime 用写回的真实 DSN 启动校验全部通过」**：我以同一 `Settings` 直接构造 7 个 runtime 并 `open()`，7/7 accepted；但这不是完整 `speechnexus-api` 组合根（需要 gateway 私有配置与完整 env），作者若声称起过完整进程，属其陈述，我未复现。
2. **作者称「settings.py 的 20 个 `*_database_url` 字段」**：我以类体字段正则独立计数得 20，独立成立。
3. **作者称「在隔离 PostgreSQL 16.15 上跑过 §6」**：我的复现使用同一镜像 `postgres:16.15-alpine`，但环境、口令与 base 不同，属等价复现而非复跑同一实例。
4. **voice 发布/发音词典的完整启用链（422 资格、gateway lane 私有配置、Kind 装配）**：只做了代码路径与 gate 拒绝逻辑的核对，没有做端到端启用实验——参考部署本来也不装配这些面，端到端需要额外基础设施。
5. **`binding_defaults_writer` 模式的端到端可用性**：只核对了互斥检查、`binding_defaults_grants` 的 superset 关系与组件文档；未起默认参数 producer 模式实跑。
6. **PR 描述中「改动前 27 键中 16 个已有文档」等记账**：我按 `1e9f2b12^` 的文档独立比对得 11 个未出现，与 PR 的 11 一致；其余计数已在上表核对。

---

## 八、收尾

隔离资源在报告完成后删除：`docker compose -p sn662 --env-file deploy/compose.env -f deploy/compose.yaml down -v`（含 `sn662_postgres-data` 卷与 `sn662-migrate:latest` 镜像），临时目录 `/private/tmp/sn662-proc` 与本报告所在 worktree `/private/tmp/sn662` 在提交、push、开 PR 后移除；`review/662-dsn-coverage` 分支保留在仓库供复核。产品代码未改；`uv.lock` 未改；`git status` 在工作树内保持干净。
