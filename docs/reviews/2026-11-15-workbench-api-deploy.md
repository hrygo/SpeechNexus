# PR #833 非作者审查：参考部署的独立 Workbench API 部署单元

- **审查对象**：PR [#833](https://github.com/hrygo/SpeechNexus/pull/833) `feat(deploy): 参考部署补 workbench-api 部署单元 (#832)`，head `99f4ca4fbd420b5d723e0b351d0d9c6061365c3e`，base `origin/main` @ `bfafc894`（2 个提交：`7fb9efa2` 装配 + `99f4ca4f` 判定表）。
- **被审 issue**：[#832](https://github.com/hrygo/SpeechNexus/issues/832)（`WEB_WORKBENCH_UPSTREAM` 指向不存在的主机；唯一真实删除实现与工作台业务后端都在 Workbench API）。
- **审查方式**：非作者独立审查。独立工作树 `/private/tmp/sn835`（`review/835-workbench-api-deploy`，基于 `origin/main`）；被审内容一律读 `git show 99f4ca4f:<path>`；行为复现使用临时 PostgreSQL 容器（`postgres:16.15-alpine`，本机回环端口）、本机 uvicorn 起真实 ASGI 工厂、仓库 `deploy/web/nginx.conf` 起 nginx 容器。临时栈与临时库在审查结束前全部 `down`/删除，未触碰共享 GPU 上的既有引擎。
- **审查日期**：2026-11-15（Asia/Shanghai）。
- **结论**：**修改后合并（不通过当前 head）**。部署单元、启动闸门、迁移步与删除三段在真实 PG + 真实 HTTP 上成立，四条反例中三条复现为真；但本卡最重要的身份边界有一条可复现缺口——**新补的"运行身份不得持有 speech 侧关系权限"校验只作用于服务身份，删除权威 `sn_workbench_cleanup` 完全不走这条校验**，把清理 DSN 指向共用宽身份 `sn_api` 时迁移与闸门都打印 `OK:`；另有 `NOINHERIT` 成员关系可绕过该校验，与测试名/PR 自述不符。另有若干"证据/文档陈述强于实测"的点，见必须修清单。

## 1. 过程时间线

| 时间（本地） | 动作 | 结果 |
| --- | --- | --- |
| 14:09 | `git fetch`；确认 `docs/reviews/2026-11-15-workbench-api-deploy.md` 不存在；创建 `/private/tmp/sn835` | 报告文件名可用；15 个改动文件，+1597/−36 |
| 14:10–14:13 | 读 `docs/operations/compose-minimal.md`、`deploy/README.md`、`docs/plans/mvp-release-readiness.md` 及 PR 版本差异；读 `migrate.py`、`runtime.py`、`workbench-startup-gate.py`、`runtime_schema.py`、各 `*_schema.py` 授权声明 | 形成 A1 逐条判定底稿 |
| 14:13–14:20 | `gh pr checks 833`；Docker COPY 集合模拟 + `tsc`（缺/含 `tests/fixtures/contracts` 对照） | CI 全绿；前端镜像构建失败断言成立，实测 35 个引用文件（非 33） |
| 14:20–14:26 | 起临时 PG 容器；`speechnexus-workbench-migrate` 先 FAIL（角色不存在）后 OK；重跑幂等 | 迁移行为与自报一致 |
| 14:26–14:30 | 转储两角色真实 ACL；`check_database` 逐项反例（PUBLIC/INHERIT/NOINHERIT/序列/owner/同名） | 见 §3.2；发现 NOINHERIT 与 cleanup 越界两条绕过 |
| 14:30–14:33 | 本机 uvicorn 起 `create_app_from_env`；`projects → deletion-plans → deletions → succeeded`；错 revision/错 digest 各 409 | 删除三段与定点拒绝复现 |
| 14:33–14:35 | 含内容项目（transcript 2 个 revision，含文本）级联删除；查 `deletion_ledger`、行数与残留文本 | 内容擦除证据成立 |
| 14:35–14:37 | 仓库 `nginx.conf` + `19-configure-upstreams.envsh`：不存在上游 → 502（日志点名 upstream）；真实服务 → 200；缺 upstream 键 → 启动即失败 | 无静默降级 |
| 14:37–14:40 | `tests/ci`、`tests/unit/backend`、`tests/integration/test_startup_authority_561.py`；`workbench-pg-probe.py` | 378 / 3272 / **55**；探针 422 passed + 36 条 MinIO 环境型失败 |
| 14:41–14:45 | 清理临时容器、网络、uvicorn、临时工作树与临时库 | 无 `sn835*` 残留 |

## 2. 结论

1. **部署单元本身成立**：`workbench-migrate`（owner 一次性迁移 + 授权 + 复核）与 `workbench-api`（闸门 → 真实 ASGI 工厂）在真实 PG 上跑通；迁移先 FAIL 后 OK、重跑幂等；闸门三条 `OK:` 与真实 `/v1/workbench/status` `200` 复现。`WEB_WORKBENCH_UPSTREAM` 现在指向被装配的服务，缺键/坏上游都失败关闭，没有静默降级成"无数据"。
2. **删除三段是完整实现，不是骨架**：预检（依赖快照 + `plan_revision` + `plan_digest` + 300 s 过期）→ 确认（固定 revision/digest/`confirm_targets`，重算快照）→ 清理（tombstone 精确校验、outbox、擦除回执、持久账本）。错版本/错摘要定点 `409 deletion_plan_changed`，固定值同 key 重提 `202` 幂等重放。
3. **身份边界未达自述强度**：服务身份最小化实测成立；但"共用宽身份被拒"只覆盖服务身份，**清理身份可以被指向 `sn_api` 而迁移与闸门都说 OK**；`NOINHERIT` 成员关系也能绕过新校验。这两点是本卡 A 项的核心缺口。
4. **范围与例数基本可信，个别数字有误**：diff 只含 `deploy/`、`docs/`、工作台侧装配/闸门/迁移、授权声明与测试，未动语音侧与机器契约，未手改生成产物；`tests/ci` 378（基线 370）、`tests/unit/backend` 3272 与自报一致；`test_startup_authority_561.py` 实测 **55**（基线 53，新增 2 条），自报"53（含新增 2 条）"算术不成立。

## 3. 逐条判定

### 3.1 A1 专用身份是否真的最小

**授权声明来源**（读 `99f4ca4f`）：`runtime_grants_sql` / `cleanup_grants_sql` 组合 11 个 `*_schema.py` 的授权函数；迁移在 owner 事务里重放并逐项复核（`RUNTIME_PROBE`/`CLEANUP_PROBE`）。

**实测 ACL**（迁移后直接查 `information_schema.role_table_grants` / `column_privileges`）：

```
runtime: 0 条 DELETE/TRUNCATE；无表级 UPDATE；UPDATE 全部是列级（_UPDATE_COLUMNS 精确集合）
runtime: SELECT/INSERT 覆盖 TABLES；deletion_ledger / erased_mutation_receipts / runtime_schema_revision 只读
cleanup: DELETE 仅 projects, project_versions, transcripts, transcript_revisions,
         mutation_receipts, meetings（6 张业务表）
cleanup: INSERT 仅 deletion_ledger, erased_mutation_receipts；UPDATE 仅 deletion_outbox
         的 (completed_at, objects_erased_at) 与 projects/transcripts 的 (id)
cleanup: 不能删账本、墓碑、擦除回执（无 DELETE）；saved_assets/books/chapters 等靠外键级联
```

**判定**：

- `sn_workbench_runtime` **是最小**：不能 DELETE 任何业务行，不能 TRUNCATE，不能做表级 UPDATE，账本只读，且启动闸门对 `REFERENCES`/`TRIGGER`/只读表 INSERT（表级与列级）逐一拒绝（`test_startup_authority_561.py` 的 `WORKBENCH_MISGRANTS` 六例）。
- `sn_workbench_cleanup` 的**变更面最小**，但**读面偏宽**：它能读 `books.source_text`、`chapters.text`、`transcript_revisions` 全文与 `content_write_intents.intent`，而实际擦除目录（`postgres_owned_deletions.py`）只读 id/revision/status/object_key 一类元数据列。这不是越界（都在自己的工作台命名空间内，且该角色本就是删除权威），但用列级 `SELECT` 可以收窄；见必须修 M6。
- **cleanup 能读任意表内容？** 不能"任意"：`assistant_turns`、`minutes_tasks.result_text`、`teleprompter_states` 都不在其 SELECT 集合内；但 transcripts/books/chapters 的内容体确实可读。

### 3.2 A2 新校验能否被绕过（独立造例，用完撤销）

被审校验：`_refuse_foreign_relation_privileges`（`runtime.py:252-301`），在 `check_database` 末尾（`runtime.py:407`）对**配置 schema 之外**的任何关系/序列权限（表级、列级、序列）拒绝启动；`SET ROLE NONE` 后按登录身份判断。

| 尝试 | 结果 | 实测输出（摘要） |
| --- | --- | --- |
| 默认 `INHERIT` 成员关系：`GRANT wide_reader TO sn_workbench_runtime` | **被拒（正确）** | `FAIL: ... runtime role must not hold privileges on speech.admission_attempts; ... revoke the foreign grant instead of sharing a wider role` |
| `PUBLIC` 授权：`GRANT SELECT ON speech.admission_attempts TO PUBLIC` | **被拒（正确）** | 同上，点名 `speech.admission_attempts` |
| 外部序列：`GRANT USAGE ON SEQUENCE speech.foreign_seq` | **被拒（正确）** | `runtime role must not hold privileges on speech.foreign_seq` |
| `pg_read_all_data` + `INHERIT` | **被拒（正确）** | 点名 `speech.admission_attempts` |
| **`NOINHERIT` 成员关系**：`GRANT wide_reader TO sn_workbench_runtime WITH INHERIT FALSE` | **放行（绕过）** | 闸门 `OK:`×3、`exec=True`；随后以该登录 `SET ROLE wide_reader; SELECT ... FROM speech.admission_attempts` 成功读到数据 |
| **`pg_read_all_data` + `NOINHERIT`** | **放行（绕过）** | 闸门 `OK:`×3；`SET ROLE pg_read_all_data` 后读到 speech 表 |
| **清理身份指向共用宽身份**：运行 DSN = `sn_workbench_runtime`，清理 DSN = `sn_api` | **放行（绕过）** | 迁移：`OK: granted the runtime surface to sn_workbench_runtime and the cleanup surface to sn_api`；闸门：`OK: runtime and cleanup identities hold exactly their declared surface`、`exec=True` |
| 两个 DSN 指向同一运行身份 | 闸门放行；迁移点名 | 闸门 `OK:`×3；迁移 `FAIL: ... name the same role sn_workbench_runtime; ...` |

**判定**：

- 新校验挡住了 `PUBLIC`、`INHERIT` 成员关系、`pg_read_all_data`（默认继承）、外部序列、列级授权、owner 身份与 `options=-c role=` 型 DSN 切换（业务连接每个事务 `SET ROLE NONE`，`postgres_versions.py:265`），方向正确。
- **绕过路径一（严重）**：`check_cleanup_identity` 只判断"不是 owner/不是特权角色/有 schema USAGE"，**没有调用** `_refuse_foreign_relation_privileges`。删除权威恰恰是权限更大的那个身份，把清理 DSN 指向 `sn_api` 时迁移会主动把 cleanup 授权装到 `sn_api` 上并复核通过，闸门随后打印"两个身份恰好持有其声明面"——这句话此时是假的。`deploy/README.md` 写的 "Pointing either DSN at `sn_api` therefore fails closed" 对清理 DSN 不成立。
- **绕过路径二（中低）**：`NOINHERIT` 成员关系不在登录时生效，`has_table_privilege(current_user, ...)` 看不到，于是校验放行；但该登录可以 `SET ROLE` 到宽角色，形成休眠权威。触发它需要有人显式 `WITH INHERIT FALSE` 授权，正常路径不会产生，但**测试名与自述声称覆盖了它**：`test_workbench_startup_also_refuses_inherited_foreign_privileges` 的 docstring 写 "`NOINHERIT` or a membership grant is not a way to share the API's role"，而用例只做了默认 `INHERIT` 的 `GRANT "wider" TO role`，没有 `WITH INHERIT FALSE` 分支。PR 正文"含角色成员继承路径"据此不成立。

### 3.3 A3 共用广权限身份被拒（独立复现）

运行 DSN 换成 API 的 `sn_api`（并按工作台声明给它授权）后，真实闸门输出：

```
FAIL: the workbench runtime database was refused at startup: Workbench runtime database is unavailable or unsafe. runtime role must not hold privileges on speech.admission_attempts; the workbench identity is scoped to its own schema, so revoke the foreign grant instead of sharing a wider role
RESULT shared-sn_api rc=1 exec=False
```

**判定**：服务身份这一侧**成立**，失败信息点名冲突关系（`speech.admission_attempts`）而不是泛泛报错，且未 `exec` 服务。但同一张卡的另一半（清理身份）不成立，见 §3.2。

另测两条"失败但不点名"的情形（与文档承诺不符，见必须修 M3）：

```
运行 DSN 指向 owner(postgres)   -> FAIL: ... Workbench runtime database is unavailable or unsafe.（无具名条件）
清理 DSN 指向 owner(postgres)   -> 同上（无具名条件）
schema revision 改为 10          -> 同上（无具名条件）
```

迁移步对 owner/同名会点名（`FAIL: role ... is a cluster-wide authority` / `name the same role`），所以"按文档先跑迁移再起 API"的路径仍能拦住；但闸门本身对这三类只给泛化消息。

### 3.4 A4 迁移 owner 权限与运行期 DDL

- **owner 只在迁移里**：`workbench-migrate` 是 `restart: "no"` 的一次性容器，命令 `speechnexus-workbench-migrate`，跑完退出；`workbench-api` 服务的 environment **不含** `SPEECHNEXUS_MIGRATION_DATABASE_URL`（`grep -c` = 0），运行容器拿不到 owner DSN。owner 不需要"降权"，因为它不常驻。
- **运行期不做 DDL**：`runtime.py` 只从 `runtime_schema` 导入 `SCHEMA_REVISION`/`TABLES`，`create_runtime_app` 与各适配器无建表/改表；`check_database` 在 `BEGIN READ ONLY` 里只读（`runtime.py:323`）。实测运行身份：

```
CREATE TABLE workbench.probe(x int) -> ERROR: permission denied for schema workbench
DROP TABLE workbench.projects       -> ERROR: must be owner of table projects
CREATE SCHEMA probe_schema          -> ERROR: permission denied for database
has_schema_privilege(workbench,'CREATE') = f；'USAGE' = t
```

- **版本闸门**：`runtime_schema_revision` 必须等于本构建的 `SCHEMA_REVISION=11`（实测改成 10 后闸门拒绝启动）；迁移对既有命名空间只核对版本、不就地改写（重跑输出 `OK: workbench schema workbench already at revision 11`）。
- **次要**：`workbench-migrate` 同时拿到两个角色的**完整 DSN（含口令）**，而它只读取登录名；可改为传角色名以缩小口令暴露面（必须修 M6）。

### 3.5 B5 反例是否真红（至少三条，独立复现）

用仓库 `deploy/web/nginx.conf` + `deploy/web/19-configure-upstreams.envsh` 起 `nginxinc/nginx-unprivileged:1.31-alpine`：

1. **上游改回不存在主机**（`WEB_WORKBENCH_UPSTREAM=http://workbench-api-absent:8000`）：

   ```
   http=502
   [error] ... upstream: "http://198.18.5.130:8000/v1/workbench/status", host: "127.0.0.1:18081"
   ```

   502，日志点名 upstream。注意本机 Docker Desktop 把不存在主机解析成代理地址，日志点名的是解析后的地址而非主机名；Linux 内嵌 DNS 下会报 "could not be resolved"。无论哪种，都是显式失败，没有回退成空数据。

2. **上游指向真实服务**（`http://host.docker.internal:8082` → 本机真实 workbench API）：

   ```
   http=200
   {"object":"nexus.workbench_status","workbench_enabled":true,"llm_enabled":false,
    "capabilities":[{"name":"transcription","status":"disabled"},...,{"name":"teleprompter","status":"available"}]}
   ```

   代理层证据成立。

3. **缺必需 env**：`WEB_WORKBENCH_UPSTREAM` 为空时 `19-configure-upstreams.envsh` 直接 `invalid WEB_WORKBENCH_UPSTREAM` 并以退出码 1 结束（容器 `exited`，ExitCode=1），不启动 nginx；闸门侧去掉 `SPEECHNEXUS_WORKBENCH_CURSOR_KEY`：

   ```
   FAIL: SPEECHNEXUS_WORKBENCH_CURSOR_KEY is blank: list cursors cannot be signed without a key
   RESULT missing-cursor-key rc=1 exec=False
   ```

4. **共用宽身份**：见 §3.3（服务身份被拒；清理身份未被拒，见 §3.2）。

**判定**：B5 四条中三条（502、缺 env、共用宽身份-服务侧）成立，且"不静默降级"成立（nginx `location` 无 fallback，envsh 校验失败即退出）。第四条"共用宽身份"只成立一半。

### 3.6 B6 删除确认定点拒绝

真实 HTTP（本机 uvicorn + 真实 PG）：

```
POST /v1/workbench/deletion-plans  -> 200  plan_revision="1"  plan_digest="sha256:f103..."
POST /v1/workbench/deletions  expected_plan_revision="2"        -> 409 deletion_plan_changed
POST /v1/workbench/deletions  plan_digest="sha256:000...0"      -> 409 deletion_plan_changed
POST /v1/workbench/deletions  固定 revision+digest+confirm      -> 202 queued/pending
POST 同一 body 同一 Idempotency-Key（重提）                     -> 202 同一条记录（幂等重放）
GET  /v1/workbench/deletions/{id}                               -> 200 succeeded / cleanup_state=complete
GET  /v1/workbench/projects/{id}                                -> 404
```

**判定**：拒绝是**定点**的（`digest != self._digest(plan)` 或 `expected_revision` 与当前目标不符才 409），固定值同 key 重提仍 `202`；不是一刀切。补充语义：接受成功后换新 key 再提会因目标已消失而 409，这符合"确认必须绑定已存在的快照"，不属缺陷。

### 3.7 C7 账本字段与擦除证据

作者自报的运行是**空项目**（只有 `projects` 一行），其 `deletion_ledger.deleted` 只有项目项，**没有内容擦除证据**。我另造了含内容项目（1 个 project + 1 个 transcript + 2 个 revision，文本体 `confidential manuscript body one/two`）后级联删除，实测：

```
deletion_ledger.deleted = [["workbench","project","7d14a881...","1"],
                           ["workbench","transcript","947c245d...","2"]]
deletion_plans.target_revision = 1；plan_digest = sha256:8d5c...
deletion_records.plan_digest   = sha256:8d5c...（与 plan 一致，运行/清理身份都不能改它）
resource_tombstones            = (project,...,1), (transcript,...,2)
deletion_outbox                = completed_at 与 objects_erased_at 均已写
行数                           = projects 0, project_versions 0, transcripts 0, transcript_revisions 0
残留文本                        = SELECT count(*) FROM transcript_revisions WHERE text LIKE '%confidential%' = 0
```

**判定**：

- **版本/摘要固定成立**：预检把 `target_revision` 与 `plan_digest` 固化在 `deletion_plans`；确认记录把 `plan_digest` 固化在 `deletion_records`；账本行通过 `deletion_id` 外键指回不可变的记录，`deleted` 数组逐项带 revision。账本行本身不复制 digest，但链路不可变、可复核，满足"版本/摘要固定的预检 + 明确范围确认 + 持久账本"。
- **内容擦除**：不只是元数据行消失——transcript 正文所在行被实际删除，残留查询为 0。作者贴的证据只覆盖空项目，这一点需要在 PR 正文/文档补上（我替它补齐了）。
- **未覆盖的对象阶段**：本单元不配置对象区；带 saved assets/有声书对象前缀的项目在清理时 `objects_owed=True` 而 `self._objects is None`，会抛 `ObjectZoneUnavailableError`，删除会一直停在待结清。这是"窄口径"的真实边界，文档应显式写明（必须修 M4）。

### 3.8 C8 删除三段是否完整 / #819 前提

- **代码层面是完整三段**：`preview`（依赖快照、300 s 过期、digest）→ `accept`（固定 revision/digest、显式 `confirm_targets`、原子重算快照）→ `PostgresDeletionCleanup`（精确 tombstone/版本校验、先删行再擦对象、outbox 两阶段完成、`erasure_receipts`、`deletion_ledger`），并由启动期 `recovery.run()` 先重放账本再服务（ADR 0005）。不是骨架。
- **实测三段全部有真实 HTTP + 真实 PG 证据**（§3.6、§3.7）。
- **#819 前提**：workbench 侧删除只覆盖 `workbench` 命名空间；语音侧 `/v1/nexus/deletions*` 仍是恒 503 占位（`speech/interfaces/http/unimplemented.py:72`），本 PR 未动语音侧。所以"优先选删除"目前只能落在工作台侧，`#819` 的实施范围与必要性没有变化；MVP 判定表把 F-DELETE 记为"工作台侧达标（窄口径）/ 语音侧不在首发范围"与实测一致，但这是**决策**而非"删除已整体完成"的证据。

### 3.9 D9 `deploy/images/web.Dockerfile` 断言

- `web.Dockerfile` 的 COPY 集合只有 `package.json / pnpm-lock.yaml / pnpm-workspace.yaml / tsconfig.base.json / web / packages/contracts / scripts/dev-env/validate-design-tokens.*`，**没有** `tests/fixtures/contracts`；本 PR 对 Dockerfile 的 diff 为空，`web/` 源码也不在 diff 内。
- 实测引用面：`web/` 下 **35 个文件、35 条 import**（admin 11、workbench 24），指向 4 个 JSON fixture；其中 6 个是非测试产品/开发源码（`profile-test-fixtures.ts`、`*-fixtures.ts`、`dev/*-transport.ts`），其余为 `*.test.ts(x)`。
- 构建链：两个 web 应用的 `build` 都是 `tsc -p tsconfig.json && vite build`，`tsconfig.json` 的 `include: ["src"]` 会把 `src` 下的测试与 fixture 模块一并类型检查。
- 按 Docker COPY 集合做隔离模拟（`git archive` 只取 COPY 集合，`node_modules` 用本机安装）：

  ```
  缺 tests/fixtures/contracts：workbench tsc 57 个 error（其中 TS2307 缺 fixture + 由此派生的隐式 any）；
                              admin tsc 25 个 error（11 个 fixture TS2307 + 派生）
  补上 tests/fixtures/contracts：workbench tsc EXIT=0；admin tsc EXIT=0
  ```

  **判定：断言准确**——前端镜像构建确实会失败，且 `origin/main` 上同样如此；本 PR 没有顺手改 Dockerfile 掩盖它，也没有放大它。唯一偏差是引用数量：实测 35 个文件（35 条 import），不是自报/复核时的 33；不影响结论，建议 #834 按 35 记。

### 3.10 E10 范围与例数

- **diff 范围**（15 个文件）：`deploy/`（compose、env 示例、README、启动闸门）、`docs/`（runbook、判定表）、workbench 侧装配（`workbench/migrate.py` 新增、`workbench/runtime.py` 仅加越界校验、`environment.py` 只加 `_ROLE_PASSWORD` 后缀、`backend/pyproject.toml` 只加 console script）、`tests/`（2 个新单测文件、1 个新 CI 守卫、2 个既有测试文件增例）。
- **未改**：`contracts/`、`packages/contracts`、任何语音侧文件、任何生成产物、锁文件、`deploy/images/web.Dockerfile`。业务契约语义未动；`runtime.py` 的 55 行新增全部是越界校验与调用点。
- **例数**（实测）：

  | 项 | 自报 | 实测 | 判定 |
  | --- | --- | --- | --- |
  | `tests/ci` | 378（基线 370） | 378 passed，816 subtests；去掉新文件后 370 | 一致 |
  | `tests/unit/backend` | 3272（新增 27） | 3272 passed；新文件 15+12=27 | 一致 |
  | `tests/integration/test_startup_authority_561.py` | 53（含新增 2） | **55 passed**（基线 53，+2） | 自报数字错 |
  | `gh pr checks 833` | — | 全部 pass（含 integration 6 分片、Python/Web、compose config、dry-run） | 一致 |
  | `workbench-pg-probe.py` | 422 passed / 36 条 MinIO 失败 | 422 passed / **35 errors + 1 failed**，全部同一断言 | 实质一致 |

### 3.11 E12 `workbench-pg-probe.py` 的 36 条失败

```
1 failed, 422 passed, 35 errors in 32.65s
AssertionError: isolated MinIO service is required for object evidence
assert (None)
```

失败集中在 exports/render-units/saved-assets/owned-deletions/export-worker/recovery 等对象相关用例；探针 pytest 日志里 `isolated MinIO service is required` 出现 72 次（36 个用例 × traceback+summary 各一次），全部同一原因。**判定：环境型失败，不是掩盖真失败**；作者描述基本准确（1 条是测试内断言失败而非 fixture error，措辞上"跳过式失败"略不精确）。

## 4. 三个核心问题的直接回答

1. **专用身份是否真最小？**
   - 服务身份 `sn_workbench_runtime`：**是**。实测 0 条 DELETE/TRUNCATE、无表级 UPDATE、账本/回执/版本标记只读，破坏性权限靠列级授权精确到列，且启动闸门逐项复核。
   - 清理身份 `sn_workbench_cleanup`：**变更面最小、读面偏宽**。它只能删 6 张业务表、只能向账本/擦除回执 INSERT、只能改 outbox 两列；但能读 transcripts/books/chapters 的正文列（擦除目录并不需要），且**它的"是否共用宽身份"完全没有被校验**。
2. **新校验能否绕过？**
   - **能，两条独立路径**：(a) 清理身份不经过该校验，把清理 DSN 指向 `sn_api` 时迁移与闸门都打印 `OK:`（已端到端复现）；(b) `NOINHERIT` 成员关系（含 `pg_read_all_data`）不体现在 `has_table_privilege(current_user, …)` 上，闸门放行，登录仍可 `SET ROLE` 取得该权威（已复现读取 speech 表）。
   - 不能绕过的方式（实测被拒）：`PUBLIC` 授权、默认 `INHERIT` 成员关系、外部序列、列级授权、owner 身份、DSN `options=-c role=` 切换（业务连接每事务 `SET ROLE NONE`）。
3. **删除三段是否完整？**
   - **是完整三段**（预检/确认/清理+账本），有真实 HTTP + 真实 PG 证据，定点 409 与幂等重放成立，内容行确实被擦除。
   - 边界：本单元未装配对象区，带 saved assets 的项目删除无法结清（会停在待结清）；语音侧仍是占位 503，`#819` 前提未变。

## 5. 必须修清单

| 级别 | 项 | 位置 | 修法 |
| --- | --- | --- | --- |
| **P0（安全边界）** | 清理身份不走越界校验：清理 DSN 指向 `sn_api` 时迁移与闸门都说 OK | `workbench/runtime.py` `check_cleanup_identity`；`deploy/README.md`；runbook | 在 `check_cleanup_identity` 里对清理登录身份调用同一 `_refuse_foreign_relation_privileges`（并复核它恰好持有 cleanup 声明面）；或在文档中明确"清理身份越界不校验"并给出替代保证。至少要让 `deploy/README.md` 的 "Pointing either DSN at `sn_api` therefore fails closed" 与实现一致。 |
| **P1（校验可绕过 + 自述不符）** | `NOINHERIT` 成员关系可绕过；测试名/docstring 声称覆盖了 `NOINHERIT`，实际只测默认 `INHERIT` | `tests/integration/test_startup_authority_561.py::test_workbench_startup_also_refuses_inherited_foreign_privileges`；PR 正文"含角色成员继承路径" | 二选一：加 `WITH INHERIT FALSE` 用例并让校验遍历 `pg_auth_members` 拒绝"成员角色持有越界权限"；或删掉 `NOINHERIT` 声称、把测试改名为只覆盖 `INHERIT`，并在文档写清休眠成员关系不在拦截范围。 |
| **P1（文档强于实测）** | runbook 写"缺键、DSN 指向 owner、两个身份同名…都以 FAIL: 点名"，实测闸门对 owner/同名/版本不符只给泛化消息，且闸门不拒绝同名（只有迁移拒绝） | `docs/operations/compose-minimal.md` §6 新增小节；`deploy/README.md` | 按实测改写：闸门点名的是缺键与越界关系；owner/同名由迁移步点名（`depends_on` 保证先跑）；闸门对这三类只失败关闭不点名。或补代码让闸门点名。 |
| **P2（证据强度）** | F-DELETE 的 A 档证据只覆盖空项目，没有内容擦除；对象阶段（saved assets 字节）未装配且未说明"带对象内容的删除无法结清" | `docs/plans/mvp-release-readiness.md` F-DELETE/B6 行；PR 正文 | 把含 transcript 文本的级联删除与"无对象区时对象阶段不结清"写进证据与限制；不要把"工作台侧达标"读成"任意内容都能删干净"。 |
| **P2（例数）** | `test_startup_authority_561.py` 自报 53，实测 55（基线 53 + 新增 2） | PR 正文 | 改为 55。 |
| **P3（次要）** | 迁移容器持有两个角色完整 DSN（含口令），实际只用登录名；cleanup 读面含未用到的内容列；闸门对 owner/同名/版本不符的泛化消息 | `deploy/compose.yaml` `workbench-migrate`；`cleanup_grants_sql`；`runtime.py` | 迁移改传角色名；内容表改列级 SELECT；失败消息补具名条件（至少区分 owner / 版本不符 / 同名）。 |

## 6. 只能依赖作者陈述的点（未独立复现）

1. **Compose 容器栈本身**：作者的 `sn832` 真实容器栈（`docker compose --profile workbench-api up`、`workbench-api Up (healthy) 127.0.0.1:8092->8000`、`docker compose ps` 输出）未复现。我复现的是同一 ASGI 工厂 + 同一迁移 + 同一 PG 行为（本机 uvicorn + 临时 `postgres:16.15-alpine`），Compose 的依赖顺序、健康检查、`read_only`/`cap_drop`/`no-new-privileges`、published 端口只按文本与 CI `docker compose config` 校验。
2. **`docker compose config` 与 `--services` 输出**：按 CI 的 "Compose: configuration validation" 通过推断，未逐条比对作者贴的渲染结果。
3. **真实镜像构建**：前端镜像构建失败结论来自 COPY 集合模拟 + `tsc`，未跑真实 `docker build`（需网络与时间）；后端镜像亦未构建。
4. **CI 的 integration 分片**：`gh pr checks 833` 全部 pass，未本地跑 6 个 integration 分片；本地只跑了 `tests/ci`、`tests/unit/backend`、`test_startup_authority_561.py` 与 workbench 探针。
5. **作者的四条反例**中"上游不存在 → 502 且日志点名 upstream"我复现了，但本机 Docker Desktop 的日志点名解析后 IP；作者环境（若为 Linux）可能点名主机名，属环境差异。
6. **`workbench-pg-probe` 的 36 条**：我复现为 35 errors + 1 failed（同一 MinIO 断言），未逐条核对是否还有非 MinIO 的隐藏失败；日志中该断言出现 72 次，未发现其他失败原因。

## 7. 复现命令（关键步骤）

```bash
# 迁移：角色缺失先 FAIL，创建角色后 OK，重跑幂等
SPEECHNEXUS_MIGRATION_DATABASE_URL=postgresql://postgres:***@127.0.0.1:55441/sn835 \
SPEECHNEXUS_WORKBENCH_DATABASE_URL=postgresql://sn_workbench_runtime:***@127.0.0.1:55441/sn835 \
SPEECHNEXUS_WORKBENCH_CLEANUP_DATABASE_URL=postgresql://sn_workbench_cleanup:***@127.0.0.1:55441/sn835 \
  uv run speechnexus-workbench-migrate

# 闸门（真实 check_database / check_cleanup_identity，exec 记录而不真启动）
uv run python /tmp/sn835-verify/gate_probe.py <case>

# 删除三段
curl -X POST :8082/v1/workbench/deletion-plans -H 'Idempotency-Key: ...' -d '{"target":{...},"mode":"cascade"}'
curl -X POST :8082/v1/workbench/deletions -H 'Idempotency-Key: ...' -d '{"plan_id":...,"expected_plan_revision":...,"plan_digest":...,"confirm_targets":[...]}'

# 前端镜像 COPY 集合模拟（缺/含 fixtures 对照）
git archive 99f4ca4f web packages/contracts ... | tar -x -C /tmp/ctx
node_modules/.bin/tsc -p web/workbench/tsconfig.json   # 缺 fixtures: 57 errors；补 fixtures: 0
```

---

**审查结论**：部署单元与删除三段可以进 MVP；身份边界必须先补 P0（清理身份越界校验）与 P1（`NOINHERIT`/文档一致性），并把证据从"空项目"补到"含内容 + 对象阶段边界"。这些修完后本 PR 可合并。
