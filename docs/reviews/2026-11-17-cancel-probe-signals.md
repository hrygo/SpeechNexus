# 取消探针负例与授权降级信号的非作者审查（PR #962 / issue #959）

审查对象：PR #962 `fix/959-cancel-probe-signals`，head
`f0a289b3ac66755333ef506d51b0c100170cce0f`（父 `2e07d320`），对应 issue #959。
diff 基线 `767d36e4`（审查 worktree 创建时的 `origin/main`）。审查期间 `origin/main`
前进到 `4f4440bb`：`#961` 已合入（`8b31731a`）并把
`docs/releases/mvp-0.1.0.md` 对齐为四条信号，`#963` 是其审查；本 PR 不碰该文件，
两者不冲突（§2.4、§3.5）。

- 被审 worktree：`/tmp/sn962-subject`（detached HEAD = `f0a289b3`；收尾
  `git status --porcelain` 为空，运行 `.venv` 在 `.gitignore` 内）。
- 审查 worktree：`/tmp/sn962-review`（分支 `review/959-cancel-signals`，自
  `767d36e4` 新建；只新增本文件）。
- 数据库：独立 PG 容器 `sn962-review-pg`（`postgres:16.15-alpine`，
  `127.0.0.1:55486`，库/用户 `sn962review`）；24 条用例、变异与降级探针实测都在其上
  执行，schema/角色由用例 `finally` 清理，容器用后删除。
- 审查人不是作者，未修改被审代码，未合并任何 PR。

## 结论

**能合（无阻断项）。**

- 指定的 CASE 变异把新用例打红、其余 23 条仍绿；还原后 24 passed（§3.1）。
- 新用例同时钉住 detail 与 claim 两侧：把 `_CLAIM` 的两个到期谓词改成
  `IS NOT NULL` 时它同样红在 `before.claimed == 0`（§3.2），不是只测 detail。
- 四类降级信号与代码/隔离 PG 实测一致：旧两列账本授权下 `cancellation_debt` **整体**
  `unavailable (permission)`，不是 Job 级降级；`stop_evidence_debt` 不受这两列影响；
  同 probe 实例重授后下一次查询即恢复（§2.2、§3.3）。
- `deploy/compose.env.example` 只有注释行变化，没有改任何键、值或运行时默认（§2.3）。
- 范围 4 文件；未碰 `docs/releases/mvp-0.1.0.md` 与判定表；`check-doc-refs.py` PASS、
  `terminal problems=0 new=0`，`check-doc-statements.py` OK，`ruff` 通过（§2.4、§3.4）。

非阻断建议 5 条（§5）。其中 §5.1 是唯一影响“自查判据可读性”的事实性措辞：④ 的
“三个账本标记”被写成恒含三项，实际只打印**命中项**（单行 `never claimed` 时只出现
`never claimed: 1`）。它不改变 ③/④ 的判别动作（整体 `unavailable` vs Job 级形态），
故不阻断；建议随本 PR 一行修正。

## 1. 阻断项

无。

审查按“会不会让运维做错动作 / 会不会让回归失去保护 / 是否越过本卡范围”三条判断。
未发现阻断项：核心负例在指定变异下红，四类信号的主判据与实测一致，范围与门禁均通过。
§5.1 的措辞问题属于“预期标记数量写错”，运维的实际动作（整体降级才重跑授权；Job 级
形态说明账本列已生效）仍正确，因此列为 P2 非阻断。

## 2. 逐项核对

### 2.1 负例是否真钉住

新用例 `tests/integration/test_cancel_failure_slot_pg.py:850`
（`test_a_future_check_is_not_due_now_and_not_claimable_yet`）做了三件事：

1. `:865-871` 以 schema owner 写入账本行：`settled_at_us IS NULL`、
   `last_outcome='pending'`、`next_check_at_us = clock_timestamp() + 1h`。
2. `:873-880` 读 `CancellationDebtProbe` 的 detail：计数为 1、不含 `due now`、
   不误报 `never claimed` / `never claimable`，且括号直接是
   `(held leases: execution-unknown=1): `——即该行落在 `scheduled`，没有任何调度标记。
3. `:882-894` 用同一个 `_reconciler(box)` 实例、同一个 `now_us` 调两次 `run_once`：
   到期前 `claimed == 0`；把该行改成 `next_check_at_us = 0` 后 `claimed == 1`。

**变异结果（§3.1）**：把共享 CASE 的
`WHEN r.next_check_at_us <= now_clock.now_us THEN 'due'`
改成 `WHEN r.next_check_at_us IS NOT NULL THEN 'due'`，新用例红在
`:876 assert "due now" not in scheduled`，整套 `1 failed, 23 passed`；还原后
24 passed。`next_check_at_us` 在 schema 中是 `BIGINT NOT NULL`
（`job_cancellation_fence_schema.py:53`），所以该变异等价于“所有未 settled、非
exhausted 的行都算 due”，负例确实钉住了“未来不是 due”。

**断言是否只测 detail**：不是。`:886` 的 `before.claimed == 0` 覆盖 claim 侧；§3.2
的补充变异把 `_CLAIM` 的两处 `next_check_at_us <= now_us` 改成 `IS NOT NULL`，新用例
在 `:886` 红（`assert 1 == 0`），说明 claim 侧也被钉住。issue #959 要求的三条断言
（detail 不含 `due now`、到期前 `claimed=0`、到期后 `claimed=1`）全部落地。

**时钟是否自洽**：两侧各用各的时钟，但各自内部一致。

- detail 侧：INSERT 用数据库 `clock_timestamp()`，探针的 `_NOW_CLOCK`
  （`readiness.py:111-114`）也在查询时读同一个数据库时钟；`+1h` 必然落在未来，确定。
- claim 侧：`run_once(now_us=...)` 的 `now_us` 是测试套件固定的
  `ACCEPTED_AT_US + WINDOW_US + 1 = 20_000_001`（µs，1970 起点），比 2026 年的数据库
  时钟小约 57 年。`before` 与 `after` 用同一个 `now_us`，且两次之间只改
  `next_check_at_us`，因此是一组受控对照；`claimed` 从 0 到 1 只能由该列解释。
- 边界：用例没有证明探针的数据库时钟与 worker 的 `now_us` 是“同一瞬间”，
  只证明两个读面各自对“未来/到期”作出正确反应；`:888` 的“same worker and clock”
  指两次 `run_once` 的同一个 `now_us`，不是指与探针同一个时钟。对 #959 的要求足够；
  可读性建议见 §5.4，非阻断。

### 2.2 四类信号与代码/实测

文档把 ①/② 归给隔离释放角色，③/④ 归给取消债务探针；③ 与 ④ 互斥（账本列未生效
vs 已生效）。逐条核对：

| 信号 | 文档位置 | 事实源 / 实测 | 判定 |
|---|---|---|---|
| ① API 启动被拒并点名表/列 | `deploy/README.md:990-993`、`docs/operations/compose-minimal.md:1224-1225` | `quarantine_release_runtime.py:237` 生成 `Quarantine release startup rejected ({detail}); …`，`:137` 的 detail 点名 `<table>.<column>`；`app.py:683-685` 在 lifespan 调 `quarantine_release_runtime.open()`，校验失败即拒绝装配 | 与代码一致 |
| ② 操作员 CLI 退出码 3、`unavailable: quarantine_release_privilege_missing` | `deploy/README.md:994-996`、`compose-minimal.md:1226-1228` | `completed_release_cli.py:92` `EXIT_UNAVAILABLE=3`；`:382-384`/`:442-444` 把 `QuarantineSurfaceError` 打成 `unavailable: {code}`；`quarantine_release_postgres.py:62` 对 `InsufficientPrivilege` 返回该 code | 与代码一致；本机未起 Compose 栈复现（§4） |
| ③ 旧两列账本授权 → 探针整体 `unavailable (permission)` | `deploy/README.md:997-999`、`compose-minimal.md:1229-1230` | `cancellation_debt_reader_grants`（`job_cancellation_fence_schema.py:140-141`）把账本列从 `job_id/settled_at_us` 扩到四列；完整查询与 Job 级查询共用 `_SCHEDULE_STATE_SQL`（`readiness.py:120-126`），两者都引用 `r.last_outcome`/`r.next_check_at_us`，缺列时都会抛 `InsufficientPrivilege`，`_inspect` 的 Job 级回退也失败，最终 `check()` 归类为 `unavailable (permission)` | 隔离 PG 实测：`cancellation_debt: unavailable (permission)`；不是 Job 级 |
| ④ 四列已授、租约/执行/证据表不可读 → Job 级降级 | `deploy/README.md:1000-1003`、`compose-minimal.md:1231-1233` | `readiness.py:415-421` 拼 `jobs: lease-unread=…` + 三个可能的调度标记；`:422-425` 只对**非空**状态追加标记 | 隔离 PG 实测（撤 `admission_leases` 列权限）：`(jobs: lease-unread=1; never claimed: 1)` + `[lease detail unavailable: permission]`。形态、判据正确；`三个标记` 的措辞与“只打印命中项”不符（§5.1） |

补充核对：

- **`/readyz` 不会因这些降级失败**：`CancellationDebtProbe.check()` 捕获所有异常并返回
  detail，永远返回 `ok`；`ReadinessService._inspect_probe`
  （`speech/application/readiness.py:67-80`）只在 probe 抛异常/超时时标 FAILED。
  代码路径与文档“都不会让 `/readyz` 失败”一致；未发真实 HTTP 请求（§4）。
- **`GRANT` 对下一次查询生效、不需要重启 API**：探针对象在 `create_app` 时按
  `settings.readiness_cancellation_dsn` 构造（`app.py:270-290`），DSN 变化需要重启；
  权限检查是每次查询执行时做的。实测在同一 probe 实例上 `REVOKE` 后得到
  `unavailable (permission)`，重跑 `cancellation_debt_reader_grants` 后下一次
  `check()` 立即恢复完整 detail，未重启进程。
- **只有换 DSN 才需要重启**：同上；探针复用连接，失败时 `_discard()` 丢掉连接，
  下一轮重连，仍然不需要重启 API。
- **隔离释放角色必须重启才通过**：`QuarantineReleaseRuntime.open()`
  （`quarantine_release_runtime.py:193-240`）只在 lifespan 启动期做逐列校验，之后没有
  周期复检；重跑授权后要让校验重新执行，只能重启（或首次启动）。与文档一致。
- **`stop_evidence_debt` 未被混写**：`_STOP_EVIDENCE_DEBT`（`readiness.py:496-529`）
  不读账本表，只读 `speech_jobs`、`admission_leases`、`submit_intents`、
  `execution_stop_evidence`。实测撤销账本两列后，同一身份的
  `stop_evidence_debt` 返回 `None`（查询成功、无此类债），不是
  `unavailable (permission)`。文档把两者分开写，正确。
- **锚点存在、未复制第三份语义**：链接目标
  `docs/components/production-jobs.md` 的标题 `#### 隔离不了的时候：可见面与处置`
  在 `:334`，GitHub 锚点 `#隔离不了的时候可见面与处置` 成立；
  `check-doc-refs.py` PASS。新文档只点名三个标记并链接到组件文档的“证明什么 /
  不证明什么”表，没有重写判定语义，符合“不复制第三份”的要求。

### 2.3 `compose.env.example` 是否只改注释

是。`git diff -U0 767d36e4..HEAD -- deploy/compose.env.example` 的增删行全部以
`#` 开头；逐行检查后非注释增删行集合为空。改动范围是
`deploy/compose.env.example:308-324` 的注释块：删掉旧的
“`cancellation_debt` 不受影响 / `stop_evidence_debt` 整体降级”描述，改为四列账本
授权 + 旧两列整体 `unavailable (permission)`，并指向 `deploy/README.md` 与
`docs/components/production-jobs.md`。没有任何键名、值、默认值或生效行的变化，
运行时风险为零。

### 2.4 范围与门禁

- `git diff --name-only 767d36e4..HEAD` 恰好 4 个文件：
  `tests/integration/test_cancel_failure_slot_pg.py`、`deploy/README.md`、
  `docs/operations/compose-minimal.md`、`deploy/compose.env.example`。
- `docs/releases/mvp-0.1.0.md`、`docs/plans/mvp-release-readiness.md` 均未改。
  测试 diff 是纯新增 47 行，已有 23 条的断言未动。
- `check-doc-refs.py`：`RESULT PASS`，`SCOPE terminal problems=0 new=0`。
- `check-doc-statements.py`：`RESULT OK`（13 条 capability statements）。
- `ruff check`（测试文件与 `readiness.py`）：`All checks passed!`；
  `ruff format --check`（测试文件）：`1 file already formatted`。
- `tests/integration/test_cancel_failure_slot_pg.py`：24 passed（§3.4）；
  `tests/integration/test_completed_release_pg.py`：16 passed。
- 判定表 `docs/plans/mvp-release-readiness.md:41` 仍写旧的“三条”信号；属判定表，
  作者已声明不在本卡范围。`docs/components/production-jobs.md:416` 仍写
  “旧的四列授权（账本只有 `job_id/settled_at_us`）”，与新增的“两列”措辞不一致；
  作者已在 PR 正文声明不改组件文档（§5.2）。

## 3. 变异与实跑

### 3.1 指定的 CASE 变异

环境：

```bash
docker run -d --name sn962-review-pg \
  -e POSTGRES_USER=postgres -e POSTGRES_PASSWORD=sn962review \
  -e POSTGRES_DB=sn962review -p 55486:5432 postgres:16.15-alpine
export SPEECHNEXUS_TEST_DATABASE_URL='postgresql://postgres:sn962review@127.0.0.1:55486/sn962review'
cd /tmp/sn962-subject
UV_NO_CONFIG=1 uv run --all-packages pytest tests/integration/test_cancel_failure_slot_pg.py -q
```

未变异：`24 passed in 17.23s`（冷启动）/`10.34s`/`10.29s`。

变异（`readiness.py:124`）：

```diff
- WHEN r.next_check_at_us <= now_clock.now_us THEN 'due'
+ WHEN r.next_check_at_us IS NOT NULL THEN 'due'
```

单跑新用例：

```
tests/integration/test_cancel_failure_slot_pg.py:876: AssertionError
E  AssertionError: assert 'due now' not in 'cancellatio...ion-unknown)'
E    'due now' is contained here:
E      nknown=1; due now: 1): job-… -> lease-test-stage-main(lease=reserved, execution=executing, subclass=execution-unknown)
1 failed
```

整套：

```
FAILED tests/integration/test_cancel_failure_slot_pg.py::test_a_future_check_is_not_due_now_and_not_claimable_yet
1 failed, 23 passed in 10.32s
```

还原后：`24 passed in 10.34s`，`git status --porcelain` 为空。

### 3.2 补充变异：claim 谓词

为确认新用例不是“只测 detail”，把
`job_cancellation_reconciliation_postgres.py:50` 与 `:65` 的两处
`next_check_at_us <= %(now_us)s` 同时改成 `next_check_at_us IS NOT NULL`（`due` CTE
与 `ON CONFLICT … WHERE` 一起改，否则第二处会挡住更新）：

```
tests/integration/test_cancel_failure_slot_pg.py:886: AssertionError
E  assert 1 == 0
E   where 1 = ReconciliationBatch(claimed=1, settled=0, retried=1, exhausted=0, unavailable=0, blocked=()).claimed
1 failed
```

还原后 24 passed。这说明 `before.claimed == 0` 确实钉住 claim 侧的到期谓词，而不只是
detail 的 CASE。

### 3.3 四类信号的隔离 PG 实测

用 `_sandbox(..., coordination=False, debt_reader=True)` 只授
`cancellation_debt_reader_grants`，再按需 `REVOKE`/`GRANT`：

```
1 FULL      : cancellation_debt: 1 accepted cancellation(s) still hold capacity over 1 held lease(s)
              (held leases: execution-unknown=1; never claimed: 1): job-… -> lease-test-stage-main(…)
2 TWO-COL   : cancellation_debt: unavailable (permission)
3 STOP-DEBT : None
4 REGRANT   : cancellation_debt: 1 accepted cancellation(s) still hold capacity over 1 held lease(s)
              (held leases: execution-unknown=1; never claimed: 1): job-… -> lease-test-stage-main(…)
```

- 2 是撤销账本 `last_outcome, next_check_at_us` 后的形态：**整体**降级，不是
  `(jobs: lease-unread=1)`。
- 3 是同一身份、同一撤销状态下的 `stop_evidence_debt`：`None`，查询成功，未被这次
  扩列拖降级。
- 4 是同一 probe 实例重跑 `cancellation_debt_reader_grants` 后：下一次 `check()` 恢复
  完整 detail，未重启进程。

再撤 `admission_leases` 的三列：

```
5 FULL      : … over 1 held lease(s) (held leases: execution-unknown=1; never claimed: 1): job-… -> …
6 JOB-LEVEL : cancellation_debt: 1 accepted cancellation(s) still hold capacity
              (jobs: lease-unread=1; never claimed: 1): job-… -> lease-unread [lease detail unavailable: permission]
```

6 是文档 ④ 的形态；注意它只有 `never claimed: 1` 一个标记（该行是 never-claimed，
没有 exhausted/due 行），不是三个标记都出现——见 §5.1。

### 3.4 门禁实跑

```
RESULT PASS           # check-doc-refs.py，SCOPE terminal problems=0 new=0
RESULT OK             # check-doc-statements.py，13 条
All checks passed!    # ruff check
1 file already formatted
24 passed             # test_cancel_failure_slot_pg.py
16 passed             # test_completed_release_pg.py
```

### 3.5 与 #961 的交叉状态

审查期间 `#961` 以 `8b31731a` 合入 main，`docs/releases/mvp-0.1.0.md` 已从旧的
“三条”改为“四条”信号（含 `cancellation_debt` 整体 `unavailable (permission)` 与
`stop_evidence_debt` 的独立条目）。本 PR 的基线 `767d36e4` 早于该合入，但不碰该文件，
合并后会保留 #961 的版本，不存在双写或覆盖。审查未把 #961 的内容当作本 PR 的改动来
判分。

## 4. 未验证项

- **真实 Compose 栈的启动拒绝与 CLI 退出码 3**：本审查只做代码核对（§2.2 的
  ①/②），没有在本机 Compose 栈复现“启动被拒”或“CLI exit 3”。作者在 PR 正文已声明
  同一未验证项；本审查确认声明与正文一致，不把它算作本卡的证据。
- **`/readyz` 的真实 HTTP 200**：未发真实请求；结论来自 probe 永不抛异常 +
  `ReadinessService` 的 FAILED 只由异常/超时触发。降级 detail 不参与聚合判定的代码
  路径已核对。
- **同一会话内 `GRANT` 生效**：实测的恢复路径是“失败后探针丢弃连接 → 重连 → 恢复”，
  未单独测“连接不丢弃、同一 idle 会话上 GRANT 后下一次查询”。两者都不需要重启 API；
  PostgreSQL 的权限检查发生在执行期，只是本审查没有把这一细分做成实测。
- **真实部署窗口 / 真实引擎**：不在本卡前提内；本审查与作者均未覆盖。
- **`docs/plans/mvp-release-readiness.md` 与 `docs/components/production-jobs.md`
  的既有旧措辞**：作者已声明不在本卡范围；本审查只记录，未修改。

## 5. 非阻断建议

### 5.1（P2）④ 的“三个账本标记”应改为“命中的标记”

涉及：`deploy/README.md:1000-1003`、`docs/operations/compose-minimal.md:1231-1233`、
`deploy/compose.env.example:321-323`。

三处都写成 Job 级降级“报出三个账本标记”。实际 `_describe_debt`
（`readiness.py:422-425`）只在对应集合非空时追加标记；§3.3 的实测 ⑥ 只有一个
`never claimed: 1`。建议改成“按账本事实打印**命中**的 `never claimed` /
`never claimable (exhausted)` / `due now`（三者按行互斥、只打印非零项）”，并保留
“整个 detail 变成 `unavailable (permission)` 才是账本列未生效”这一判别。这样不会让
运维在只看到一个标记时误以为授权没生效。

### 5.2（P2）组件文档“旧的四列授权”与新增“两列”不一致

`docs/components/production-jobs.md:416` 写“旧的四列授权（账本只有
`job_id/settled_at_us`）”：括号里是两列，标签却是四列。本 PR 的新文档链接到该节并
写“两列”，两处并存会让读者困惑。作者已在 PR 正文声明不改组件文档；建议另开一行
修正（“旧的两列账本授权”），或随本 PR 一并改（不扩大范围）。

### 5.3（P3）`compose.env.example` 的前两条信号缺少“仅在配置了隔离释放角色时适用”

`deploy/compose.env.example:316-321` 把“api startup 拒绝 / CLI exit 3 / 探针整体
降级”并列写成一次漏跑的可见信号；只配 `SPEECHNEXUS_READINESS_CANCELLATION_DSN`、
没有隔离释放角色的部署，前两条不适用。README 的 ② 已有这句限定
（`deploy/README.md:995-996`），example 注释可加半句“when that role is configured”，
或直接指向 README 的 ①/②。

### 5.4（P3）新用例的两个时钟建议在注释里点明

`tests/integration/test_cancel_failure_slot_pg.py:863-864` 说明 INSERT 用的是数据库
时钟，`:884` 的 worker 用固定 `ACCEPTED_AT_US + WINDOW_US + 1`（1970 起点，约
20 s）。两侧各自自洽，但读者容易把 `:888` 的“same worker and clock”误读为与探针
同一时钟。建议补一句“worker 的 `now_us` 是测试固定时钟；探针用数据库时钟；两侧只
需对‘未来/到期’给出一致方向”，或把 claim 侧的 `now_us` 也改成从数据库时钟派生。
不影响本卡断言强度。

### 5.5（P3）`deploy/README.md:985-987` 的“两个函数”可限定到实际配置的角色

“任何配置了 `SPEECHNEXUS_QUARANTINE_RELEASE_DATABASE_URL` 或
`SPEECHNEXUS_READINESS_CANCELLATION_DSN` 的环境，升级时都必须……重跑
`quarantine_release_grants` 与 `cancellation_debt_reader_grants`”对只配探针 DSN 的
部署过宽（没有隔离释放角色时前者无对象）。这是既有措辞、本 PR 只在其后追加了四列
事实；可在同一句里改成“按实际配置的角色重跑对应函数”。`compose-minimal` 新增的
“探针身份不在重放列表内”已部分澄清，非阻断。

## 6. 交付与工作树状态

- 本审查报告：`/tmp/sn962-review/docs/reviews/2026-11-17-cancel-probe-signals.md`；
  审查分支 `review/959-cancel-signals` 只含本文件，commit 后 push 并开审查 PR
  （base `main`）。
- 被审 worktree `/tmp/sn962-subject`：收尾 `git status --porcelain` 为空；变异文件已
  还原（`git diff --stat` 为空）。
- PG 容器 `sn962-review-pg` 在实测后删除；用例自建 schema/角色已由 `finally` 清理。