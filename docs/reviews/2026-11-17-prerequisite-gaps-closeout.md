# 七类前置收口审计的非作者审查（PR #934 / #877）

审查对象：`fix/prerequisite-gaps-closeout`，head `70a8c892`（单提交），分叉点 `1721ddb2`，即审查期间的
`origin/main` 顶点。被审对象是 `#877` 的**收口审计**本身：作者声称七类前置 **7 项达标 / 0 项不达标**、
产品代码无需修，并声称审计找到**两处判据缺口**（`#877` item 4 的「有实例但无 binding」臂、
`tts_settlement_preflight` 的迁移归类），已各补一条用例。

**口径先记事实。** `git diff 1721ddb2 70a8c892` = **2 文件 +118/−0**（`tests/integration/
test_tts_preflight_unmigrated_pg.py` 新增 84 行、`tests/unit/backend/test_job_precondition_naming.py`
+34 行），与 PR 描述一致；两点口径在本题下同值（分叉点就是 `origin/main` 顶点），仍按分叉点计。
`git merge-tree --write-tree --name-only origin/main 70a8c892` 退出码 0（合并树 `a113439f`），无冲突。
**产品代码、契约、`ci.yml`、`docs/releases/mvp-0.1.0.md` 均未被本 PR 触碰**（§7 逐条核对）。

---

## 结论

**不能合（当前）。** 两条新用例是真守卫（两个方向的变异都实测变红，且比作者声明的更细的漂移也能抓到），
抽样复核的三条七行表判据也全部属实，第 ⑥ 行的「设计选择」有实测与文档双向支撑、不是借口，
`#877` 正文回写没有把本机复现夸大成 MegaCube 实跑。**但「两处判据缺口、已收口」这个结论不成立**：
同一函数 `_unpublished_binding` 里另有两处判据在删掉后全套单测仍绿，其中一处正是 `#877` item 4
要治的那句话；另有一处基线陈述与实测相反。

三处**低成必修**（都是「声称与事实不一致」，不改产品行为）：

- **M1**：零状态 422 文案里的「publish the binding」**没有任何用例**。把它从 `_unpublished_binding`
  的零状态臂删掉 → `tests/unit/backend` **3566 passed**（全绿）。本 PR 新增用例只钉住了另一臂。
  这是 `#877` item 4 的原缺陷句子，也是 `#877` 正文与 PR ①表逐字引用的实测输出，必须有用例。
- **M2**：PR ③「补前基线」那句**不实**。分叉点上把 `except` 收窄（作者所说的「删掉真实归类」）**不是全绿**：
  `tests/unit/backend/test_lease_settlement.py::test_the_preflight_names_a_missing_migration_instead_of_a_driver_error`
  `1 failed, 14 passed`。真缺口是「没有真实驱动证据」与「消息里的共享迁移命令没有用例」，不是「没有任何用例」。
- **M3**：`_unpublished_binding` 的 fail-open 分支（`except PublicJobUnavailableError: states = ()`，
  注释明写「不可读的注册事实不得把 422 换成存储答案」）**没有用例**：删掉整段 → `tests/unit/backend`
  仍 3566 passed。补一条，或把「两处缺口」改写成如实的三处/并列出未覆盖项。

其余为可接受但需记录（§8）。**本 PR 的方向、两条用例的质量、七行表里九成的变异证据都是真的**——
它是一份差一步收口的审计，不是一份错审计。

---

## 0. 复核记录

审查在独立 worktree `/private/tmp/sn938` 完成：分支 `review/934-prereq-closeout` **从 `origin/main`
（`1721ddb2`）新建**，只在阅读时 `git checkout 70a8c892`，本报告是该分支唯一改动（`check-review-pr-boundary.py`
要求只承载 `docs/reviews/**`）。依赖 `UV_NO_CONFIG=1 uv sync --locked --all-packages --all-groups`
加 `pnpm install --frozen-lockfile`（新 worktree 两项都要跑，否则 8 个 `[typescript]` 假红；本次实测正是这 8 条）。
隔离 PG `127.0.0.1:55480/sn892`（`SHOW server_version` = **16.16**，与 PR ①表声明一致）、
MySQL `127.0.0.1:55434`。**所有注入均已还原**，收尾 `git status --porcelain` 空。

| 检查 | 命令 | 结果 |
| --- | --- | --- |
| 分叉点 diff | `git diff --stat 1721ddb2 70a8c892` | 2 文件 +118/−0，与描述一致 |
| 合并不冲突 | `git merge-tree --write-tree --name-only origin/main 70a8c892` | exit 0，树 `a113439f` |
| 全量单测/契约 | `pytest tests/unit tests/ci tests/contract -q` | 11999 passed + 8 个 `[typescript]` 假红 + 3 skipped + 2261 subtests |
| 装好 node 后复跑假红 | `pnpm install --frozen-lockfile` → `pytest ... --lf` | **8 passed**；合计 **12007 passed / 3 skipped / 2261 subtests**，与作者数字一致 |
| 新集成用例 | `pytest tests/integration/test_tts_preflight_unmigrated_pg.py` | 1 passed |
| 前置集成用例 | `pytest tests/integration/test_public_job_prerequisites_pg.py` | 7 passed |
| 闸门单测 | `pytest tests/unit/test_job_startup_gate.py` | 44 passed |
| API 启动形态（进程级） | `pytest tests/integration/test_startup_gate_output_pg.py` | 3 passed（含退出码 3 那条） |
| 启动闸门进程输出 | `pytest tests/unit/backend/test_startup_gate_process_output.py` | 13 passed |
| 表↔文档守卫 | `pytest tests/ci/test_job_prerequisite_map.py tests/unit/backend/test_api_startup_refusal.py` | 10 passed |
| 前置命名单测 | `pytest tests/unit/backend/test_job_precondition_naming.py` | 7 passed（基线 7） |
| schema 清理 | 跑完 5 次后用 `psql` 查 `sn_tts_preflight_%` | 0 行残留 |

---

## 1. 两条新用例是不是真守卫（按卡要求自己重做变异，不照抄作者方向）

### 1.1 `_unpublished_binding` 的 `if states:` 臂 — 是真守卫，且细漂移也挡得住

| 变异（我自己写的） | 与作者声明的差异 | 结果 |
| --- | --- | --- |
| **M1-a 删整臂**：把 `if states:` 那段 `return` 整块删除，控制流落回零状态臂 | 同向（作者：`if False:`） | **`1 failed, 6 passed`** ✓ |
| **M1-b 只删状态列表**：保留「has registered engine instance(s)」与「publish the binding」，只把 `{', '.join(sorted(states))}` 去掉 | 同向（作者：「把实例状态从文案里去掉」） | **`1 failed, 6 passed`** ✓ |
| **M1-c 更细的漂移**：臂与文案都保留，**只列出第一个状态**（`sorted(states)[:1]`） | **作者没做这一向** | **`1 failed, 6 passed`** ✓ — `assert 'stopped' in message` 抓住 |

M1-c 是本卡点名要验的「只删文案里一部分」：红的原因是 `assert "draining" in message and "stopped" in
message` 要求**两个状态都在**，而不是「至少出现一个状态词」。恢复后 `7 passed`。三条断言分别钉住
「两件修复可区分」「状态被点名」「不得声称未注册」，职责不重叠，不是一条断言三处复用。

**同一臂之外**：`assert "publish the binding" in message` 只在这一臂存在，零状态臂**没有**等价断言（见 M1）。

### 1.2 `tts_settlement_preflight` 的迁移归类 — 是真守卫，但增量与作者的说法不同

| 变异 | 结果 | 说明 |
| --- | --- | --- |
| **M2-a 作者的变异**：`except (InvalidSchemaName, UndefinedTable)` 收窄到只 `InvalidSchemaName` | 新集成用例 **`1 failed`**（真 `UndefinedTable` 逃逸）✓ | 作者声明的效果属实 |
| **M2-b 同一变异下谁还红**：`stub` 用例 | `test_lease_settlement.py` **`1 failed, 14 passed`** | 假连接用例**也**红 → 这条变异不是新用例独有 |
| **M2-c 新用例的独有增量**：保留归类，只把消息里的共享命令 `MIGRATION_COMMAND` 换成一句普通话（`run the migration profile`） | 新集成用例 **`1 failed`**；`test_lease_settlement.py` **15 passed**；`tests/ci/test_job_prerequisite_map.py` **8 passed** | **只有新用例红** — 这才是它真正堵住的那一格 |

结论：新集成用例的价值真实，但它堵的是 **「真实驱动确实走到这个归类」+「拒绝消息带共享迁移命令」**，
不是「删掉归类没人管」。作者的 ③ 表把它写成「改成不捕获即 1 failed」是对的，
**但 ③ 的「补前基线」把这件事写成了「仍然全绿」——见 §1.3。**

### 1.3 「补前基线」实测：states 臂的基线成立，preflight 的基线不成立

在**分叉点**（`git checkout review/934-prereq-closeout`，即 `1721ddb2`，`git status` 空）复测：

| 基线变异 | 结果 | 与作者声明（PR ③「两处变异都是绿的」） |
| --- | --- | --- |
| 用分叉点的 6 条用例跑 M1-a（删整臂） | **`6 passed`** | 一致 ✓ 缺口真实 |
| 用分叉点的 6 条用例跑 M1-b（只删状态） | **`6 passed`** | 一致 ✓ 缺口真实 |
| **分叉点**跑 M2-a（收窄 `except`） | **`1 failed, 14 passed`**（`test_the_preflight_names_a_missing_migration_instead_of_a_driver_error`） | **不一致 ✗** |

分叉点的 stub 用例（`git show 1721ddb2:tests/unit/backend/test_lease_settlement.py:262` 与 head 逐行相同）
直接抛 `psycopg.errors.UndefinedTable`，所以「删掉真的归类」在**补这条集成用例之前就已经会红**。
PR ③ 那句「preflight 归类只有假连接证据，删掉真实归类仍然全绿」把两件事混成一件：
**证据强度不足**（假件直接抛驱动异常）是真的；**没有用例挡**是假的。同一句也出现在 `#877` §4
（「只有假连接证据……已补」）——`#877` 那句没有声称基线全绿，是 PR ③ 多写了半句。

---

## 2. 七行表的「达标」有判据背书吗：随机抽 3 条自行变异

抽样方式：对 PR ③「审计同时对七项各做一次行为退回变异复核」的 12 行清单做 `random.seed(20261117)` 的
`random.sample(rows, 3)`，抽到 **R3 / R1 / R4**（未挑作者写得最细的行）。全部自行重写变异、自行跑红、再还原。

| 抽中行 | 我写的变异 | 变红用例 | 结果 |
| --- | --- | --- | --- |
| **R3 闸门迁移台账** | 删 `migration_findings` 里 `alembic_version` 缺失的 `return` 分支 | `test_a_missing_migration_version_table_names_the_profile`、`test_a_missing_schema_is_named_by_the_migration_check_not_a_traceback` | **`2 failed, 42 passed`** ✓ |
| **R1 闸门缺授权** | 删 `probe_findings` 的失败 `return` **且**删 `role_guard_findings` 的 `findings.append` | `test_a_rejected_select_probe_names_the_table_and_stops`、`test_the_role_guard_names_superuser_and_ddl_roles` | **`2 failed, 42 passed`** ✓（与作者点名的两条完全一致） |
| **R4 闸门检查顺序** | 把 `role_guard_findings` 调到 `migration_findings` 之前 | `test_database_findings_runs_the_head_the_probes_and_the_writes`、`test_a_missing_schema_is_named_by_the_migration_check_not_a_traceback` | **`2 failed, 42 passed`** ✓ |

另外**补抽一条非闸门行**（抽样结果全落在闸门里，覆盖太窄）：**`admission DSN 点名`** —— 把
`job_runtime._ADMISSION_DSN_BLANK` 直接改成 `WRITER_DSN.detail`，即把 `#877` 当年的原缺陷（两条 DSN
共用一句话）重新引回来：

```
FAILED tests/integration/test_public_job_prerequisites_pg.py::test_a_blank_writer_dsn_and_a_blank_admission_dsn_answer_differently
FAILED tests/integration/test_public_job_prerequisites_pg.py::test_the_runtime_hands_the_shared_admission_row_to_the_store
2 failed, 5 passed
```

抽到的 4 条判据**都有对应用例**，作者矩阵在这 4 行上属实。**但这不覆盖全部**：同一函数里的 M1、M3
（§结论）说明「逐条都做过分变异复核、均有对应用例」这句话对七行表整体不成立——至少表 ⑦ 引用的那句文案
（零状态臂的「publish the binding」）没有任何用例。

---

## 3. 第 ⑥ 行的双重标准：是不是借口

**不是借口。** 三条独立证据互相咬合，而且主干的守卫比 PR 描述的更强：

1. **主干的 24 行表把它记成「是」**。`tests/ci/startup_gates.py:68` 起 `api_job_schema` 一行：
   `observed` = uvicorn 打印 traceback 与 `Application startup failed. Exiting.` 后退出码 3、无 `FAIL:` 行、
   异常消息即 `schema_not_migrated` 原文；`promise` = §9.1 写「traceback + 退出码 3、不是一行 `FAIL:`」；
   `agrees="是"`；`disposition` 明确「`#924` 已把文档写成实测为真；本卡补进程级用例钉住；若要挪到入口换一行
   `FAIL:`，**须另立卡（启动顺序与二次连接）**」。这是「两种时点、两种机制」的判断，不是没修的借口。
2. **文档三处一致**。`docs/operations/compose-minimal.md` §9.1（`2346`–`2357` 行）把两个时点分开写：
   进程已在跑 → `schema_not_migrated` 原文 `503` 无 traceback；启动期未迁移 → lifespan 抛
   `JobSchemaNotMigratedError`、uvicorn 打 traceback、**退出码 3**；能打一行 `FAIL:` 且退出码 1 的是
   **同步装配**的 `SettlementPreflightUnavailableError`。`docs/operations/async-job-runbook.md` §1 行 2 现在
   写的是同一句（`#929` 修的就是它）。`docs/operations/compose-minimal-from-zero.md` 也引这一节。
3. **我自己跑了进程级用例**（真起 `speechnexus_backend.main:main`，真 PG）：
   `pytest tests/integration/test_startup_gate_output_pg.py` → `3 passed`；其
   `test_api_job_schema_gate_fails_in_the_lifespan_with_exit_three` 断言
   `returncode == 3`、含 `Traceback`、含 `JobSchemaNotMigratedError`、含 §9.1 原文、**不含** `FAIL:` 行。
   我又按它的 env 手起了一次 API，实测输出尾部：

   ```
   ... JobSchemaNotMigratedError: The Job database has not run the migration profile: a required relation is missi…
   ERROR:    Application startup failed. Exiting.
   ```

   一条实测级细节：**异常消息是 traceback 的末行，但不是整个输出的末行**（后面还有 uvicorn 的
   `ERROR: Application startup failed. Exiting.`）。PR ①表与 `#877` 正文写的是「uvicorn traceback 的末行」，
   **措辞准确**；主干的 `startup_gates.py` 写成「末行异常消息」，是更松的说法，属 `#929` 已有文本，
   不在本 PR 范围（§8-A3 记一笔）。
4. 反向也验证了另一条：同步装配那条路径确为 `1` 行 `FAIL:`、退出码 `1`（同文件
   `test_api_settlement_gate_prints_one_fail_line_and_no_traceback`，含在 `3 passed` 内）。

因此第 ⑥ 行的「双标」是**机制差异 + 已有实测 + 文档已对齐**，可以判为设计选择。
**但「文档与实测必须一致」这一条要加个主语**：一致的是 §9.1 与 runbook §1；`#877` 正文没有把
「API 侧仍旧 traceback」写丢（它还专门放在 §3「仍未改善的处置」里），这点是对的。

---

## 4. 第 ⑦ 行：422 文案到底点了几件事

**在 `#877` 实测的那条状态上，两件都点名了；但同一 `422` 的其余出口一件都不点。**

- 零状态臂（无已发布 binding、无实例）：`publish the binding and register the engine (engine-registration
  profile)` —— **两件**，且 `#877` 与 PR ①表逐字引用的正是这句。
- 「有实例但无 binding」臂：只说 `publish the binding for this model (binding lifecycle/publications)` ——
  **只说一件是正确设计**（此状态再让他去注册引擎是错的），新用例钉住了这点。
- **口径缺口**：`public_jobs.py` 里还有 3 处**零文案** `raise ModelNotPublishedError`
  （`:813` 非字符串 model、`:835` 已发布 binding 未声明所需 task、`:867` `catalog_revision < 1`），
  `detail=None` → `job_errors.py:72` 回落到契约默认句 **「The model is not published.」**，
  也就是 `#877` item 4 点名要撤掉的那句「把人送到模型目录」。其中 `:835` 有明确修复动作
  （发布一个声明该 task 的 binding 修订），却仍给出无信息量的默认句。
  这不推翻第 ⑦ 行「空库 → 422 点名两件事」的实测，但**「无可用引擎」这一类的达标只覆盖被测的那一条臂**，
  建议在 `#877` 第 ⑦ 行加限定，或另立卡（§8-A4）。

---

## 5. `#877` 正文的回写准确吗

**准确，没有把「本机复现」写成 MegaCube 实跑。** 逐项核对：

- `#877` 正文（已回写）标题段写明「**收口审计（2026-11-17，基线 `origin/main` `1721ddb2`）**：七类前置逐项在
  **本机隔离 PostgreSQL 16.16 + 真实 MinIO** 上重跑」——与 PR ①表列头、与作者自述一致；
  我核到本机 PG 确为 **16.16**。分叉点也是 `1721ddb2`，与正文声明一致。
- `#877` §5「未验证项」明确保留「三项原达标项在本机真实 PG + MinIO 复现，**未在 MegaCube 重跑**；
  本机无 GPU 引擎，未重跑真实 Job 全链」，与 PR ⑤ 逐条一致。**没有发现把本机写成 MegaCube 的地方。**
- 七行表把「原不达标」的四项（4/5/6/7）与「原达标」的三项（1/2/3）分开标注，`#877` 当时的结论与现在的
  实测分列，「达标」列 7 个「是」——**行为层面的「7 项达标」我抽查的 4 条判据都属实**（§2）。
- 需要修的只有 PR ③ 那半句基线陈述（§1.3 / M2），不在 `#877` 正文里。
- 细节差异（不影响结论，记录）：PR ①表第 7 行写「真实 uvicorn + 已迁移空库 curl 创建」，`#877` 第 7 行
  只写「创建得到 `422`」；`#877` §5 把「`422` 一臂是真实 HTTP 实测」这句省了。两处都更保守，不算夸张。

---

## 6. 新增集成文件会不会成为 flaky 源

**不会，这一格是本次审查里最干净的部分。** 逐项对照 `tests/integration/AGENTS.md`「独立 schema 生命周期」：

| 检查点 | 实测/读码 |
| --- | --- |
| 唯一 schema 名 | `sn_tts_preflight_` + `uuid4().hex[:12]`，满足 `speech_pool` 的 `[a-z][a-z0-9_]{2,62}` 与 30 字符长度 |
| 清理路径 | `try` 内 `CREATE SCHEMA`，`finally` 内 `DROP SCHEMA IF EXISTS … CASCADE` + `admin.close()`；断言失败、创建失败都走同一条 |
| 连接池先关 | 用例自己 `try/finally: pool.close()`，**在 fixture 的 finally 之前**执行，`DROP … CASCADE` 不会与活动连接相撞 |
| 不依赖执行顺序 | 不读共享表、不读 seed、不依赖其他用例；单文件单用例 |
| 并行 | 我起 **4 个进程同时跑**：`1 passed` ×4；跑完 `pg_namespace` 里 `sn_tts_preflight_%` **0 行残留** |
| DSN 缺失语义 | `pytest.fail(...)` 而非 skip，符合本目录强制约定；错误文本「skip is not migration evidence」与目录规则逐字一致 |
| CI 会被选到 | `.py` → `ci-routing.py` 归类 `full`；集成分片对**完整收集**做确定性分割，新文件自动落片，无需登记 |

残余（低风险，§8 记录）：① 进程被 `SIGKILL` 时会留下 `sn_tts_preflight_*`，没有像 `flake_harness`
那样的启动清扫；② `assert len(message) < 400` 把消息上界和驱动首行长度耦合，psycopg 将来若在首行加料
会误红（当前真实首行 `relation "admission_leases" does not exist`，加上前缀/后缀约 230 字符，余量充足）。

---

## 7. 改动规模与写集核对

| 项 | 结果 |
| --- | --- |
| 文件数 / 行数 | **2 文件，+118/−0**（`--stat` 与 `--numstat` 一致） |
| 改动类型 | 1 个新集成用例文件 + 1 个已有单测文件追加 1 条用例；**无产品代码改动** |
| `contracts/**` | 未触碰 ✓ |
| `.github/workflows/ci.yml` | 未触碰 ✓ |
| `docs/releases/mvp-0.1.0.md` | 未触碰 ✓ |
| 部署/迁移/闸门脚本 | 未触碰 ✓（`deploy/job-startup-gate.py` 仅被我临时变异，已还原） |
| 提交粒度 | 单提交 `70a8c892`，父即分叉点 `1721ddb2` |
| 报告 PR 边界 | 本报告分支从 `origin/main` 建，只新增 `docs/reviews/2026-11-17-prerequisite-gaps-closeout.md` |

---

## 8. 必须修 / 可接受但需记录

### 必须修（三处，都是改测试或改陈述，不动产品行为）

- **M1 · 零状态 422 的「publish the binding」没有用例。** 在
  `test_an_unpublished_binding_without_an_instance_names_the_engine` 里加
  `assert "publish the binding" in message`（并对新增用例已有的负向断言保持对称），或补一条同形用例。
  证据：把该句从 `_unpublished_binding` 零状态臂删掉 → `pytest tests/unit/backend -q` **3566 passed**。
  理由：这句正是 `#877` item 4 要治的文案，且被 PR ①表与 `#877` 第 7 行逐字引用为实测输出。
- **M2 · 改掉 PR ③「补前基线」里不实的那半句。** 分叉点上收窄 `except` 会红
  （`test_lease_settlement.py` `1 failed, 14 passed`，见 §1.3）。应写成「stub 已能挡归类删除；
  缺的是真实驱动证据与消息中的共享迁移命令（我实测：后者只有新用例挡）」。`#877` §4 的措辞无需改。
- **M3 · `_unpublished_binding` 的 fail-open 没有用例。** `except PublicJobUnavailableError: states = ()`
  整段删掉后 `tests/unit/backend` 仍 3566 passed。补一条「注册事实不可读时仍是 422、不被存储答案顶掉」的用例；
  若本轮不补，把「两处判据缺口」改为「三处（含 fail-open）」或明确列为未覆盖。

### 可接受但需记录

- **A1** 结论措辞：「7 项达标 / 0 项不达标」在**行为**层面成立（我抽查 4 条判据全红）；但「审计找到两处判据
  缺口、已补齐」这句话在 M1/M3 补上前不成立。两件事最好在 PR 描述里分开写。
- **A2** `#877` 第 7 行的「达标」宜加限定：只覆盖「空库 → 422 点名两件事」与「无 ready 实例 → 429 点名状态」，
  不含 `:813/:835/:867` 三个零文案 `raise`（回落「The model is not published.」）。
- **A3** 主干的 `tests/ci/startup_gates.py` `observed` 写「末行异常消息就是 …」；实测最后一行是
  `ERROR: Application startup failed. Exiting.`，异常消息是 **traceback 的末行**。建议改成与 §9.1 同样的措辞
  （属 `#929` 已有文本，不是本 PR 的必须修项）。
- **A4** 第 ⑥ 行「API 侧仍是 traceback 外壳」的实测依据继承自 `#926` review M1（PR ⑤ 已如实声明未重跑），
  守卫是进程级用例 `test_api_job_schema_gate_fails_in_the_lifespan_with_exit_three`，我复跑绿。
- **A5** 新集成用例的两条残余（§6）：SIGKILL 残留 schema；`len(message) < 400` 与驱动首行耦合。

---

## 附录 A：本次全部变异与实测输出（均已还原，`git status` 空）

```
# 分叉点 1721ddb2 基线（缺口 1）
pytest tests/unit/backend/test_job_precondition_naming.py -q        → 6 passed
  删整臂（if states: 整块删除）                                      → 6 passed   （绿 = 缺口）
  只删状态列表                                                       → 6 passed   （绿 = 缺口）

# head 70a8c892（缺口 1 已补）
pytest tests/unit/backend/test_job_precondition_naming.py -q        → 7 passed
  删整臂                                                             → 1 failed, 6 passed
  只删状态列表                                                       → 1 failed, 6 passed
  只列第一个状态（更细漂移）                                          → 1 failed, 6 passed
  删 fail-open except                                                → 7 passed   （绿 = 新缺口 M3）
  零状态臂删「publish the binding」                                   → tests/unit/backend 3566 passed（绿 = M1）
  message 换成 "The model is not published."                          → 1 failed, 6 passed

# 缺口 2：tts_settlement_preflight 迁移归类
分叉点 收窄 except                                                  → test_lease_settlement 1 failed, 14 passed（非绿！）
head   收窄 except（集成）                                          → 1 failed（真驱动逃逸）
head   收窄 except（stub）                                          → 1 failed, 14 passed
head   消息换成不含 MIGRATION_COMMAND 的普通话                       → 集成 1 failed；stub 15 passed；ci 表 8 passed
head   集成用例 4 进程并行                                          → 1 passed ×4；残留 schema 0

# 七行表抽样 3 条（seed 20261117 → R3, R1, R4）
R3 删 alembic_version 缺失分支                                      → 2 failed, 42 passed
R1 删 probe 失败分支 + 删角色守卫判定                                → 2 failed, 42 passed
R4 角色守卫调到迁移比对之前                                          → 2 failed, 42 passed
补抽：admission DSN 改用 writer 句子                                  → 集成 2 failed, 5 passed

# 第 ⑥ 行形态（真进程、真 PG）
pytest tests/integration/test_startup_gate_output_pg.py             → 3 passed（含 returncode==3、含 traceback、不含 FAIL:）
pytest tests/unit/backend/test_startup_gate_process_output.py       → 13 passed
手起 API（空 schema）实测尾部                                        → exit 3；traceback 末行 = JobSchemaNotMigratedError: …；
                                                                    最后一行 = ERROR:    Application startup failed. Exiting.

# 规模
git diff --stat 1721ddb2 70a8c892                                    → 2 files, +118/-0
git merge-tree --write-tree --name-only origin/main 70a8c892        → exit 0
```

## 附录 B：审查期间改过的文件（全部还原）

`backend/.../application/public_jobs.py`、`backend/.../infrastructure/tts_settlement_preflight.py`、
`backend/src/speechnexus_backend/job_runtime.py`、`deploy/job-startup-gate.py` 各做过变异，
还原后逐文件 `git status --porcelain` 为空；未提交任何产品改动。
