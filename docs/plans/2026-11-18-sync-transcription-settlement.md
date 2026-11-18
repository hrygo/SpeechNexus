---
title: 同步转写结算实施计划
status: planned
version: "1.0.0"
date: 2026-11-18
---

# 2026-11-18 同步转写结算实施计划

状态：**计划，未实施**。本文按依赖顺序拆出提交单元、用例、反例、本地门禁与真机项；设计判据见
[同步转写的租约结算](../design/sync-transcription-settlement.md)。相关工作项：[#974](https://github.com/hrygo/SpeechNexus/issues/974)
（成功后不结算，已实测）、[#338](https://github.com/hrygo/SpeechNexus/issues/338)（停止证据协议）。

范围：让 `POST /v1/audio/transcriptions` 的成功执行把容量归还，并让归还由一个持久、可重放的
结算运行时负责。不改执行网关、不改公开契约、不改引擎路径、不新增容器。

非目标：同步 ASR 的 abort/drain、`unknown_quarantined` 的隔离推进者、多节点/跨网关迁移、
失败结果的自动归属（只做可见面）。

## 1. 依赖顺序

```text
U0 文档（本切片）
 └─ U1 迁移 + 授权声明
     ├─ U2 准入事务内写 root
     └─ U3 领域/应用 worker + 存储 + 一次性入口      ← Slice A 结束（能释放）
         └─ U4 运行时装配 + 开关 + fail-closed
             ├─ U5 preflight + 身份自检
             └─ U6 参考部署与运维文档                ← Slice B 结束（自动周期）
                 └─ U7 债探针（可见面）
                     └─ U8 真机验收（不是提交单元）
```

Slice A = U1–U3；Slice B = U4–U7。U0 已由本切片完成。U2 与 U3 可在 U1 合入后并行，但 U3 的集成
用例依赖 U2 的 root 才能端到端跑；先落 U2 再落 U3 可以减少一次测试脚手架改动。

## 2. 提交单元

### U0 设计文档（本切片）

- 文件：`docs/design/sync-transcription-settlement.md`、本文件、`docs/design/README.md`。
- 用例：无代码用例；文档门禁见 §5。
- 反例：本单元不引入逻辑，反例落在下游：删掉设计 §4.4 的 A-REL-1，U3 的实现就没有「worker 不得直接更新
  `admission_leases`」这条可验判据；删掉 §4.2 的任一行，对应单元的反例失去依据。
- 真机：无。

### U1 迁移与授权声明

- 文件：
  - `backend/src/speechnexus_backend/speech/infrastructure/migrations/versions/0057_asr_settlement.py`
    （`revision="0057_asr_settlement"`，`down_revision="0056_debt_probe_live_state_index"`；建表、两个
    部分到期索引、历史回填；`downgrade` 拒绝，理由同 `0031_tts_settlement.py`）。
  - `backend/src/speechnexus_backend/speech/infrastructure/asr_settlement_schema.py`
    （`asr_settlement_writer_grants`、`asr_settlement_worker_grants`）。
  - `tests/integration/test_asr_settlement_migration_pg.py`。
- 用例：
  1. 迁移后表、主键、外键、部分到期索引存在；`down_revision` 接在 `0056` 后。
  2. 迁移前已持有、`kind='asr_file'`、`operation-…` 的租约被回填一条 root，`next_check_at_us=0`；
     已释放租约、`job-…` 公开 Job、资格探针租约不被回填。
  3. 回填对同一租约重复执行不产生第二行（迁移只跑一次，测试用重复 INSERT 证明幂等 SQL）。
  4. 无对应 `admission_leases` 行的 root 插入被外键拒绝。
- 反例：删掉回填的 `WHERE l.lease_state <> 'released'`，`test_migration_skips_released_leases` 变红；
  删掉 `kind='asr_file'` 过滤，`test_migration_ignores_public_jobs_and_probes` 变红。
- 本地门禁：`UV_NO_CONFIG=1 uv run pytest tests/integration/test_asr_settlement_migration_pg.py -q`；
  `UV_NO_CONFIG=1 uv run pytest tests/ci -q`（迁移头与授权名被 CI 的部署覆盖测试引用时）。
- 真机：在参考部署的迁移身份上跑一次 `alembic upgrade head`，核对回填行数等于迁移前仍被持有的
  同步 ASR 租约数（`#974` 的部署上为 1），且不触碰已释放租约。

规模：3 个文件，约 200–240 行（迁移 80、授权 30、集成用例 110）。

### U2 准入事务内写 root

- 文件：
  - `backend/src/speechnexus_backend/speech/infrastructure/asr_settlement_sql.py`
    （`insert_asr_settlement(cursor, request)`：只接受单 stage 的 `asr_file` 命令；`ON CONFLICT (lease_id)
    DO NOTHING` 后在同一事务回读并逐列比对；冲突抛 `AdmissionRejectedError("reconciliation_required")`。
    同文件放 `PostgresTranscriptionAdmissionTransactions(PostgresAdmissionTransactions)`：覆写
    `_record_settlement`，`asr_file` 走 root 插入，其它 kind 调 `super()`，基类与 TTS 分支不动）。
  - `backend/src/speechnexus_backend/transcription_runtime.py`（把 `AdmissionService` 的
    `PostgresAdmissionTransactions` 换成上面的子类；`PostgresTranscriptionAdmission` 的字段不变）。
  - `tests/unit/backend/test_asr_settlement_root.py`、`tests/integration/test_transcription_admission_pg.py`
    追加用例。
- 用例：
  1. 成功准入后 root 的 `lease_id/operation_id/attempt_id/gateway_id/host_node_id` 与租约、stage 一致。
  2. 同一租约已存在不同身份的 root → 准入返回 `reconciliation_required`，租约、配额、实例计数、Job
     全部回滚（用事务后计数断言）。
  3. 非 `asr_file`（公开 Job TTS、资格探针）不写 ASR root。
  4. 准入重放（同 `request_id`）不产生第二条 root，也不因 root 已存在而失败。
  5. 提交前在同一事务连接内同时可见 root 与租约；事务回滚后两者都不存在（证明原子性，不靠事后对账）。
- 反例：删掉插入后的回读比对，`test_conflicting_root_rolls_back_admission` 变红（`DO NOTHING` 会静默
  成功并留下错 root）；把插入从准入事务里移到提交之后，`test_root_and_lease_share_one_commit` 变红
  （提交前的同一连接里看不到 root）。
- 本地门禁：`UV_NO_CONFIG=1 uv run pytest tests/unit/backend/test_asr_settlement_root.py
  tests/integration/test_transcription_admission_pg.py -q`。
- 真机：无独立项；随 U8 的准入成功项一起观察（root 行存在且与租约同身份）。

规模：4 个文件，约 240–280 行（SQL 模块 90、hook 40、单元用例 70、集成用例 60）。

### U3 领域/应用 worker + 存储 + 一次性入口（Slice A）

- 文件：
  - `backend/src/speechnexus_backend/speech/domain/asr_settlement.py`（`AsrSettlementRoot`：lease_id、
    operation_id、attempt_id、gateway_id、host_node_id；`identifier` 校验；不含终态/证据字段）。
  - `backend/src/speechnexus_backend/speech/application/asr_settlement.py`（`AsrSettlementWorker.run_once`：
    取 due root → 路由校验 → 读租约 → 已释放则 mark settled → 查网关 → 终态则 `LeaseService.release`
    （版本取自刚读的租约）→ 已释放则 mark settled → 其余 defer；逐 root 捕获异常，单条失败不阻塞整批）。
  - `backend/src/speechnexus_backend/speech/infrastructure/asr_settlement_postgres.py`（`due` 只读、
    `intent(lease_id)` 读取本租约唯一的 `submit_intent_id`、`defer`、`mark_settled`；全部用
    `clock_timestamp()`，`SET TRANSACTION READ ONLY` 用于只读查询）。
  - `backend/src/speechnexus_backend/asr_settlement_worker.py` 的 `--once` 入口（此单元只放一次性运行所需
    的设置读取与连接池，周期循环留到 U4）。
  - `tests/unit/backend/test_asr_settlement_worker.py`（照 `test_tts_settlement_worker.py` 的 fake 形态）。
- 用例：
  1. 成功终态 + 停止证据：一轮扫描 → 释放一次、标记一次、defer 零次。
  2. 网关 `succeeded` 但租约 `execution_cleanup_pending`：零释放、零标记、defer 一次。
  3. 网关 `unknown_quarantined`：不调用 `release`、不调用 `quarantine`、defer。
  4. 网关不可用/超时/`not_found`：不调用 `release`、defer。
  5. 租约已 `released`（运维先释放）：`release` 调用次数 0，仅 `mark_settled`。
  6. 释放返回 `unknown` 后重读为已释放：标记一次；重读仍 reserved：defer，下一轮用新版本重试。
  7. 释放返回 `version_conflict`（另一写者先提交）：重读一次；已释放则标记，否则 defer，不重试同一版本。
  8. root 路由与 worker 不匹配：不调用网关、不释放、不标记。
  9. 过期 deadline 但成功终态：仍然释放（授权不看 deadline）。
  10. `--once` 在空表上退出码 0，在有 root 时打印计数（released/pending）。
- 反例：删掉「租约已 released → 只标记」分支，用例 5 变红（会再调一次 `release`，在真实适配器上得到
  `replayed` 但计数器被错误计为释放）；删掉终态判断，用例 3/4 变红；删掉路由校验，用例 8 变红。
- 本地门禁：`UV_NO_CONFIG=1 uv run pytest tests/unit/backend/test_asr_settlement_worker.py -q`；
  `UV_NO_CONFIG=1 uv run ruff format --check backend/src tests`。
- 真机：在参考部署上对 `#974` 的残留租约跑一次 `--once`，核对 `lease_state='released'`、
  `cleanup_state='complete'`、`budget_released_at_us` 非空、实例 `held_slots=0`、
  `workspace_quotas.held_attempts` 回到 0，随后第二次 `POST /v1/audio/transcriptions` 返回 `200`。
  这是 Slice A 的验收边界：容量可以被归还，但需要一次显式命令，周期扫描尚未启用。

规模：5 个文件，约 360–420 行（领域 40、应用 100、存储 80、一次性入口 60、用例 120）。

### U4 运行时装配 + 开关 + fail-closed

- 文件：
  - `backend/src/speechnexus_backend/asr_settlement_worker.py` 追加 `AsrSettlementWorkerSettings`、
    `AsrSettlementRuntime`（线程 + 事件循环，照 `TtsSettlementRuntime` 的形态）、`run` 循环与
    `__main__`。
  - `backend/src/speechnexus_backend/transcription_runtime.py`：在 `build_transcription` 成功链里打开
    settlement 运行时；缺少结算 DSN 或 `SPEECHNEXUS_ASR_SETTLEMENT_ENABLED=false` 时返回 `None` 并点名键；
    运行时启动失败则让 `build_transcription` 抛出（API 启动失败）。
  - `backend/src/speechnexus_backend/settings.py`：新增 `asr_settlement_enabled`（`Literal["true","false"]`，
    未设置按 `true`）；结算 DSN 复用现有 `speech_settlement_database_url`，不新增键。
  - `tests/unit/backend/test_transcription_production.py` 追加开关/缺配置用例。
- 用例：
  1. 结算 DSN 为空 → `build_transcription` 返回 `None`，HTTP 路由 `503`，日志包含
     `SPEECHNEXUS_SPEECH_SETTLEMENT_DATABASE_URL`。
  2. `SPEECHNEXUS_ASR_SETTLEMENT_ENABLED=false` → 同上，worker 未启动。
  3. 结算 DSN 已配置但身份/表不可用 → `build_transcription` 抛出，API 启动失败，异常文本不含 DSN。
  4. 正常运行 → worker 线程启动，`close()` 在超时内停止；重复 `close()` 幂等。
- 反例：删掉开关判断，用例 2 变红；把运行时启动失败降级为 warning + `return None`，用例 3 变红。
- 本地门禁：`UV_NO_CONFIG=1 uv run pytest tests/unit/backend/test_transcription_production.py -q`；
  真机项：空 DSN 起栈，转写 `503`，`/readyz` 正常；恢复 DSN 后转写恢复。
- 规模：4 个文件，约 260–300 行（运行时 140、装配 30、设置 20、用例 70）。

### U5 preflight + 身份自检

- 文件：
  - `backend/src/speechnexus_backend/speech/infrastructure/asr_settlement_preflight.py`
    （`require_complete_asr_roots`：held sync-ASR lease 必须有 root；settled root 必须对应 released lease；
    缺表时抛命名错误并给出迁移命令。复用 `SettlementPreflightUnavailableError` 与 `MIGRATION_COMMAND`；
    不改 `require_complete_tts_roots` 的两个分支）。
  - `backend/src/speechnexus_backend/tts_settlement_worker.py`：把 `_verify_worker` 泛化为两个 worker
    共用的 `_verify_settlement_worker`；允许集合扩为三张表
    `{speech_tts_settlement, speech_tts_cancellation, speech_asr_settlement}`，ASR 表只允许
    `SELECT` 与列级 `UPDATE (next_check_at_us, settled_at_us)`，其余列/表/破坏性权限仍逐列拒绝。
  - `backend/src/speechnexus_backend/asr_settlement_worker.py`：打开两个池后核对同库同 schema、不同角色、
    无 `SET ROLE`/角色管理/预定义角色；调用共用的列核对与 preflight。
  - `tests/integration/test_asr_settlement_preflight_pg.py`。
- 用例：
  1. 未迁移数据库 → 启动错误点名迁移命令，不含驱动 traceback。
  2. held `asr_file` 租约缺 root → 拒绝启动，detail 点名租约。
  3. root `settled` 但租约仍 `reserved` → 拒绝启动。
  4. root `settled` 且租约 `released` → 允许启动。
  5. 结算角色与准入角色相同 → 拒绝启动。
  6. 结算角色多出 root 表之外的写权限 → 拒绝启动（逐列核对）。
- 反例：删掉「settled 但未释放」检查，用例 3 变红；删掉逐列核对（改成只看表级 `SELECT`），用例 6 变红。
- 本地门禁：`UV_NO_CONFIG=1 uv run pytest tests/integration/test_asr_settlement_preflight_pg.py -q`。
- 真机：在参考部署上故意缺一条 `asr_settlement_worker_grants` 起栈，核对启动被拒绝且错误只含类别；
  恢复授权后正常启动。
- 规模：4 个文件，约 320–380 行（preflight 120、自检 80、接线 20、用例 120）。

### U6 参考部署与运维文档（Slice B 的部署面）

- 文件：
  - `docs/operations/compose-minimal.md`：两条 provisioning 路径（fresh install 与已有部署升级）都加
    `asr_settlement_writer_grants(schema, "sn_api")` 与 `asr_settlement_worker_grants(schema,
    "sn_speech_settlement")`；§3 的 DSN 说明从「同步 TTS」改为「同步 TTS/ASR」；同步 ASR 行更新为
    「成功后自动结算」，并删除「没有可用释放出口」。
  - `docs/operations/synchronous-transcription.md`：删除「成功后租约不自动释放」的已知限制，改成
    「成功后一个扫描周期内归还」与对应的核对读数。
  - `deploy/README.md`：`sn_speech_settlement` 的 “two grants” 改为 three；DSN 条目说明空值同时关闭
    TTS 与 ASR 的同步入口。
  - `scripts/dev-env/check-doc-statements.py`：`sync-asr-compose` 的必含串从「成功后租约不自动释放」
    改为新的自动结算表述，否则 U6 合入后文档门禁必红。
  - `tests/ci/test_reference_deploy_dsn_coverage.py`：`SPEECHNEXUS_SPEECH_SETTLEMENT_DATABASE_URL` 的期望
    增加 `asr_settlement_worker_grants`；`ROLE_GRANT_BINDINGS` 的 `sn_speech_settlement` 与 `sn_api` 元组
    各加一个新 grant 标记（两条 provisioning 路径的计数仍为 2），并同步该表上方「two worker declarations」
    的注释。
- 用例：CI 覆盖测试即用例；新增断言「两条 provisioning 路径都出现新 grant」。
- 反例：只在 fresh install 路径加 grant，`test_reference_deploy_dsn_coverage` 的计数断言变红；
  漏改 `check-doc-statements.py`，`check-doc-statements.py` 因必含串消失而红。
- 本地门禁：`UV_NO_CONFIG=1 uv run pytest tests/ci -q`；`python3 -m unittest discover -s tests/ci`；
  `UV_NO_CONFIG=1 uv run python scripts/dev-env/check-doc-statements.py`。
- 真机：按更新后的 runbook 从已有部署升级，核对 `sn_speech_settlement` 的列级 `UPDATE` 恰好包含
  `speech_asr_settlement(next_check_at_us, settled_at_us)`，`sn_api` 对新表只有 `SELECT`/`INSERT`。
- 规模：5 个文件，约 110–140 行（三处文档、门禁声明、测试期望）。

### U7 债探针（可见面）

- 文件：
  - `backend/src/speechnexus_backend/speech/infrastructure/readiness.py`：新增 `asr_settlement_debt` 前缀，
    统计「root 未 settled 且租约仍被持有且已过 `deadline_at_us`」的条数与形状
    （`succeeded`/`unknown`/`other`），以及「settled 但租约未 released」的异常条数；探针始终 `ok`，
    detail 有界，不使 `/readyz` 失败。
  - `backend/src/speechnexus_backend/speech/interfaces/http/diagnostics.py`（或现有 diagnostics 路由）：
    暴露同一 detail。
  - `tests/unit/backend/test_readiness_asr_settlement.py` 与 `tests/integration/test_asr_settlement_debt_pg.py`。
- 用例：
  1. 空表 → `asr_settlement_debt: 0`。
  2. 一条成功未结算 → 计数 1，形状 `succeeded`。
  3. 一条 `unknown_quarantined` 未结算 → 形状 `unknown`，不与成功混淆。
  4. settled 但未 released → 异常计数 1，detail 点名租约。
  5. 数据库不可用 → `asr_settlement_debt: unavailable (<category>)`，不抛。
- 反例：删掉 `settled_at_us IS NULL` 过滤，用例 4 的异常计数被正常行淹没而变红；删掉形状分类，
  用例 3 与 2 无法区分而变红。
- 本地门禁：`UV_NO_CONFIG=1 uv run pytest tests/unit/backend/test_readiness_asr_settlement.py
  tests/integration/test_asr_settlement_debt_pg.py -q`。
- 真机：`unknown_quarantined` 的同步 ASR 租约在 detail 里可见，且 `/readyz` 仍 `ok`。
- 规模：3 个文件，约 180–220 行（探针 110、用例 90）。

### U8 真机验收（不是提交单元）

按设计 §9.2 执行，全部通过后 Slice B 才算完成：

1. 升级后 `--once` 结算 `#974` 残留租约（`held_slots=0`、`held_attempts=0`），第二次同步转写 `200`。
2. 启用周期后，一次成功转写在一个扫描周期内 `held_slots` 回 0，无需命令。
3. 结算 DSN 置空重启：转写 `503`，容量不再增长；恢复后继续结算。
4. 停 API 再起：未结算 root 继续，已结算 root 不重复扣账。
5. 同一实例连续两次成功转写，第二次不被容量拒绝。

## 3. 总量估计

| 单元 | 文件数 | 行数量级 | 迁移 | 契约/网关改动 |
| --- | --- | --- | --- | --- |
| U0 文档 | 3 | 680–760 | 无 | 无 |
| U1 迁移 + 授权 | 3 | 200–240 | 1 个（`0057`） | 无 |
| U2 准入写 root | 4 | 240–280 | 无 | 无 |
| U3 worker + 存储 + once | 5 | 360–420 | 无 | 无 |
| U4 运行时 + 开关 | 4 | 260–300 | 无 | 无 |
| U5 preflight + 自检 | 4 | 320–380 | 无 | 无 |
| U6 部署文档 + CI | 5 | 110–140 | 无 | 无 |
| U7 债探针 | 3 | 180–220 | 无 | 无 |
| **合计（U1–U7）** | **约 28 个文件**（含新增与修改，测试约占一半） | **约 1,730–1,980 行** | **1 个迁移** | **0** |

诚实边界：这是按现有 TTS 同类模块的行数外推的量级，不含 review 返修；测试行数按现有
`test_tts_settlement_worker.py` / `test_tts_durable_pg.py` 的体积估算。没有迁移回滚（历史必须保留，
与 `0031_tts_settlement.py` 同口径）。

## 4. 两片的可验边界

### Slice A：能释放（U1–U3）

- 已具备：root 持久化、历史回填、一次性结算命令。
- 可验：在参考部署上跑一次 `--once`，`#974` 的残留租约被结算，第二次同步转写 `200`。
- 不可验/仍存在：没有自动推进者；200 之后若无人运行命令，容量仍会占用。发布口径必须写成
  「有界操作员出口」，不能写成「已自动结算」。
- 风险：预置授权未加到 `sn_api` 时，准入在写 root 处回滚，转写整体失败；Slice A 的部署必须同时
  应用 U6 的 `sn_api` 授权部分（或先手工授予），否则会把「不释放」换成「不可用」。

### Slice B：自动周期（U4–U7）

- 已具备：API 进程内周期扫描、启动 fail-closed、preflight、可见面。
- 可验：成功转写后一个扫描周期内 `held_slots` 回 0（无命令）；空 DSN/开关关闭时转写 `503`；
  缺授权/表时启动失败；`unknown_quarantined` 在债探针可见。
- 不可验/仍存在：`unknown_quarantined` 与执行失败形状没有自动推进者（只有可见面）；没有 abort/drain；
  没有多节点/跨网关结算。
- 风险：worker 与准入共用热行，极端并发下出现死锁；两边都只在确认回滚后重试，最终表现为 defer，
  不产生部分释放。真机验收第 5 项（连续两次成功）覆盖这一路径。

## 5. 全局门禁（每个提交单元都跑）

```bash
UV_NO_CONFIG=1 uv run pytest tests/ci -q
python3 -m unittest discover -s tests/ci
UV_NO_CONFIG=1 uv run python scripts/dev-env/check-doc-refs.py
UV_NO_CONFIG=1 uv run python scripts/dev-env/check-doc-statements.py
UV_NO_CONFIG=1 uv run ruff format --check .
```

改动 `backend/src` 的单元追加 `UV_NO_CONFIG=1 uv run lint-imports` 与目标测试；改动迁移的单元追加
`alembic upgrade head` 的空库演练；改动部署文档的单元必须让 `tests/ci` 的 DSN 覆盖测试通过。
`uv run` 一律带 `UV_NO_CONFIG=1`，否则本机镜像会写进 `uv.lock`。

## 6. 未决问题

1. **同步 ASR 的隔离推进者**：`unknown_quarantined` 的租约目前既没有 dispatcher 隔离，也不满足隔离释放
   的 `quarantined` 前置。本计划只让它在债探针可见；是否新增同步 ASR 的隔离路径需要单独裁决（涉及
   `LeaseService.quarantine` 的调用者与 runbook）。
2. **失败形状的自动归属**：`failed`（非 `not_required`）与 `aborted` 目前只有共享释放规则的判据，
   没有专门的触发者。本计划让 worker 把终态交给共享规则，因此有证据的失败会被结算、无证据的保持占用；
   是否要为「失败但无证据」再建一条隔离路径，属于 #338 的范围。
3. **开关的产品语义**：`SPEECHNEXUS_ASR_SETTLEMENT_ENABLED=false` 同时关闭转写路由，是本计划的建议
   （避免「服务但不结算」）；如果产品要求保留转写，需要接受容量泄漏并另行设计只读告警。
4. **`/readyz` 的债探针是否进首发**：U7 是可见面的最小实现；如果首发范围收紧，可以先只留运维 SQL，
   但必须把「同步 ASR 的未知租约无人推进」写进已知限制。

## 7. 回滚

- 代码回滚：停用 `SPEECHNEXUS_ASR_SETTLEMENT_ENABLED` 或回退 API 镜像；root 表与已释放账目保留。
- 迁移不回滚（`downgrade` 拒绝），与 TTS 结算迁移同口径；回退版本后 root 表成为未被读取的历史。
- 已释放容量不能重算，理由与处置见设计 §8.2。