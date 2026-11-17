# 启动闸门失败输出形态审计的非作者审查（PR #929）

审查对象：`fix/startup-gate-output`，head `aa5e33a8`（父 `c33891ab`，即与 `origin/main` 的分叉点）。

**口径前提先记事实。** 审查期间的 `origin/main` 已从 `c33891ab` 前进到 `acf633ff`（#924、#926 落主干）。
本报告全部数字按 `git diff c33891ab aa5e33a8` = **11 文件 +1109/−6** 计，与 PR 描述一致；两点
`git diff origin/main aa5e33a8` 会把 #924/#926 的 20 个文件反向计成删除，不是本 PR 的 delta。
合并层面 `git merge-tree --write-tree --name-only origin/main aa5e33a8` 退出码 0（合并树 `9a4e666a`），
自动合并后 `pytest tests/ci` = 528 passed / 2249 subtests，**无冲突、无回归**。合并不成问题，结论只由内容决定。

**结论：不能合（当前）。** 三处**低成本必须修**，全部是「声称与事实不一致」，不需要返工设计：

- **M1**：表中唯一的「不一致」行（`api_job_schema`）在 main 上**已经过期**——它引用的 `#924` §9.1 在主干上
  已经把启动期写成「traceback + 退出码 3、不是一行 `FAIL:`」，与行内那句引用**正好相反**；真正没被修的是
  `async-job-runbook.md` §1 行 2。改一格 `promise` 与一格 `disposition` 即可，但这正是本 PR 存在的意义所在。
- **M2**：`_connection_failure_class` 的分类在真实 psycopg 上**不可达**（连接失败时 `Error.sqlstate` 恒为
  `None`），而新增的两条单测用自造异常喂了一个真实驱动永远不会有的 `sqlstate`，断言了**真实入口不可能输出的
  文本**——本 PR 要治的 #920 病（用比真实协作对象更强的假件作证据）在 PR 内部复发了。
- **M3**：「22 个单元」不完备：`job-dispatch-worker` 容器真正的入口 `speechnexus-job-dispatch-worker`
  自己的 `main()` 有两条具名拒绝（退出 `1`/`2`），完全没进表；`asset-inspector` 行的「真实输出」漏了第三条。

其余为可接受但需记录（§8）。本 PR 的**测量、两处修复与两个守卫方向都是对的**：泄露修复实测有效（§1）、
`database_findings` 的新顺序没有误报（§5）、四条反例全部复现为红（§7）。

---

## 0. 复核记录

审查在独立 worktree `/private/tmp/sn930` 完成：分支 `review/929-startup-gates` 从 `origin/main`
（`acf633ff`）新建后仅检出被审 head 阅读；本报告是该分支唯一改动。依赖
`pnpm install --frozen-lockfile` + `UV_NO_CONFIG=1 uv sync --locked --all-packages --all-groups`
（新 worktree 两项都要跑，否则 8 个 `[typescript]` 假红）。隔离 PG `127.0.0.1:55480/sn892`、
MySQL `127.0.0.1:55434`。未改产品代码、契约、迁移或部署文件；所有注入均已还原。

| 检查 | 命令 | 结果 |
| --- | --- | --- |
| fork 点 diff | `git diff --stat c33891ab aa5e33a8` | 11 文件 +1109/−6，与 PR 描述一致 |
| 合并不冲突 | `git merge-tree --write-tree --name-only origin/main aa5e33a8` | exit 0，树 `9a4e666a` |
| 合并树 CI 守卫 | `pytest tests/ci`（合并后） | 528 passed, 2249 subtests |
| 表↔文档守卫 | `pytest tests/ci/test_startup_gate_output_map.py` | 6 passed |
| 进程级用例 | `pytest tests/unit/backend/test_startup_gate_process_output.py` | 11 passed（2.9 s） |
| 闸门单测 | `pytest tests/unit/test_job_startup_gate.py` | 47 passed |
| API 结算闸门（真 PG） | `pytest tests/integration/test_startup_gate_output_pg.py` | 1 passed（真起 `speechnexus_backend.main:main`） |
| 泄露面全扫 | 22 个单元逐个起入口 + 固定正则集 | 见 §1 |
| 表口径计数 | `GATES` 分类统计 | 22 行：20「是」（5 承诺形态 + 15 无格式承诺）、1「否」、1「未验证」 |

作者自述的数字（「16 形态一致、其中 5 个承诺了输出形态、另有 4 个无格式承诺」）与代码不符；
`GATES` 实际是 5 个承诺形态 + 15 个无格式承诺 = 20 个一致。表内自洽，只是自述口径把 16 说成了「一致」的
总数。记在 §8-A5。

---

## 1. 泄露面全扫：改动的那两行确实关上了，但「其余本来就干净」不完全成立

扫描方式：对 22 个单元逐个构造失败环境、以 `subprocess` 起**真实入口**，用同一组正则扫 stdout+stderr：
`postgresql://`、`mysql://`、`@`、IPv4（含 `:port`）、`(?i)password|secret|token|passwd`、构建机绝对路径
（`/Users|/private|/opt|/run|/tmp` 等）、登录名与库名、DSN 口令字面量。

### 1.1 一行结论类（文档承诺「一行」的单元）：干净

| 单元 / 失败路径 | 实测输出 | 判定 |
| --- | --- | --- |
| `job-startup-gate.py` 空白环境（5 条 `FAIL:`） | 只点名键名、桶名、`/run/speechnexus-private` 容器内路径 | 干净 |
| `job-startup-gate.py` 非法 schema | 只点名 `SPEECHNEXUS_DATABASE_SCHEMA` | 干净 |
| `job-startup-gate.py` 连不上（`:1`） | `… cannot connect to PostgreSQL (OperationalError); …` | 干净 |
| `job-startup-gate.py` 口令错（真服务器、真 `28P01`） | 同上，`(OperationalError)` | 干净（但见 M2） |
| `workbench-startup-gate.py` 空白 / 不可达 | 只点名键名；运行期拒绝为固定句 | 干净 |
| `workbench/migrate.py` 三种 DSN 形态（拒绝、畸形 scheme、口令错） | `(OperationalError)` / 固定句 | 干净 |
| `asset-inspector` 空白 | `asset inspector configuration error: <键规则>` | 干净 |
| `binding-disable-worker` / `runtime-delivery-worker` / `voice-evidence-worker` | 固定句，退出 `1`/`1`/`2` | 干净 |
| `engine-readiness`（不可达、畸形 scheme） | `error: database_unavailable: OperationalError` / `ProgrammingError` | 干净 |
| `engine-register` | `error: <code>: <message>`，无取值回显 | 干净 |

这一栏里没有任何一处回显 DSN 片段、主机、端口、库名或登录名。**作者修的那两行是真的关上了**：
改动前的 `reason = str(error).strip().splitlines()[0][:300]` 在真实口令错下会打印
`connection failed: connection to server at "127.0.0.1", port 55480 failed: FATAL:  password authentication failed for user "hrygo"`
——我按原样还原后由 `assert_no_leak` 抓到 `['127.0.0.1']`（§7-c）。

### 1.2 traceback 类：同一段文本仍在别处打印（可接受，但必须记录）

本 PR 从 job 闸门移走的那段 psycopg 原文，**在 API 自己的 lifespan 上原样打印**。实测（真 PG、真口令错、
`SPEECHNEXUS_DATABASE_URL` 指向 `sn_api_runtime`）：

```
rc = 3
error connecting in 'speech-coordination': connection failed: connection to server at "127.0.0.1",
port 55480 failed: FATAL:  password authentication failed for user "sn_api_runtime"   （打印两次）
… psycopg_pool.PoolTimeout: couldn't get a connection after 3.00 sec
ERROR:    Application startup failed. Exiting.
```

即：主机、端口、登录名与 `FATAL: password authentication failed` 全部落在容器日志里。同类单元
（`gateway`、`artifact-retention`、`asset-maintain`、`job-migrate`、`minio-init`、`node-controller`）的 traceback
还带构建机绝对路径（`/private/tmp/sn930/backend/src/…`、`/Users/hrygo/.local/share/uv/…`）。

这**不构成本 PR 的缺陷**：文档 §读法「不泄露」条款的原文是「**给出一行结论的单元**不得在输出里带 DSN 片段、
主机、库名、用户名」，口径本身是对的，traceback 类单元从来不在该条款内。但由此得到两条必须记录的事实：

1. 「泄露面只关了那一处」这个判断要按**类别**读：一行结论类全干净；traceback 类**按定义会打印**驱动原文。
   任何声称「启动失败输出不泄露凭据」的通稿式表述都是假的。
2. `#924` 在主干 §9.1 已经把该闸门写成「traceback + 退出码 3」，所以它不能也不该同时声称启动期不泄露。
   这一条在主干上是自洽的，只需在 #929 rebase 时不要把它写回去。

### 1.3 一处仍无构造性保证的驱动回显（无实证泄露）

`deploy/job-startup-gate.py` 的 `write_privilege_findings` 失败路径仍是同一个模式：

```python
reason = str(error).strip().splitlines()[0][:300]
f"the post-claim write columns cannot be read from {schema}: {reason} …"
```

我用假连接把该分支打通，输出为
`the post-claim write columns cannot be read from speech: relation "speech.speech_jobs" does not exist (server at 10.1.2.3, user sn_job_dispatch); …`
——机制上无约束。但换回真 psycopg，这个调用点是**已连上之后**的查询，抛出的只能是服务端错误
（`UndefinedTable` / `InvalidSchemaName` / `InsufficientPrivilege`），文本里只有 schema、表与角色名，
而这三类恰好是文档明确允许点名的部署面。所以：**没有实证泄露，但保证来自异常类型的巧合，不来自构造**，
且该分支**没有任何用例覆盖**（需要「半迁移」状态）。记 §8-A4。

---

## 2. 文档逐字比对测试**不读产品源码**——注入证明

`tests/ci/startup_gate_output_map.py` 的唯一数据源是 `tests/ci/startup_gates.py` 里的 `GATES` 元组
（`render_table()` 就是 `GATES` 的 `f-string` 渲染）。它读的文件只有三类：`tests/ci/startup_gates.py`
（import）、`docs/operations/startup-gate-output.md`、以及 `compose-minimal.md`/`async-job-runbook.md`/
`operations/README.md` 三份只为断言「链接到本表」的文档；另加两个**测试模块**，且只为用正则
`^def (test_[a-z0-9_]+)\(` 收集用例名。它**从不打开** `deploy/*.py`、`backend/src/**`、
`asset-inspector/src/**`。文档自己写的是「`tests/ci/startup_gates.py` 是这张表的唯一来源」——这句话是诚实的，
但它意味着「文档 vs 源码」退化成了「文档 vs 测试副本」。

**注入验证（改产品行为，不改表）。** 我改了 `asset-inspector` 真入口里被本表**逐字引用**的那一行：

```python
- raise SystemExit(f"asset inspector runtime root is missing: {RUNTIME_ROOT}")
+ raise SystemExit(f"asset inspector runtime root not found (set it in the image): {RUNTIME_ROOT}")
```

结果：`test_startup_gate_output_map.py` 6 passed、`test_startup_gate_process_output.py` 11 passed、
`pytest -k "asset_inspector or assetinspector"` 56 passed / 143 subtests passed。**六条文档守卫、十一条进程
用例、五十六条单元测试全绿**，而文档表格里那句 `asset inspector runtime root is missing: <path>` 已经变成假的。

补充注入（表述漂移）：给 `job-startup-gate.py` 的一条 `FAIL:` 文案追加一个子句，同样全绿。

所以这张表的**自洽性有机械保障，真实性没有**。这不是「作者撒谎」——文档没有声称它校验产品代码——但
「唯一来源表」这个名字会让读者以为改产品文案会被抓，实测不会。记 §8-A1（给了低成本硬化方案）。

---

## 3. 「非一致行禁止带用例」是真判据，但只对表自洽

`test_an_unreconciled_row_cannot_carry_evidence` 遍历 `inconsistent_rows()`（即 `agrees == "否"` 的行），
逐行 `assertIsNone(gate.case)` 并要求 `disposition` 里带 `#`。实测**是真判据**：

给 `api_job_schema`（唯一 `否` 行）挂上模块里确实存在的用例名 →

```
FAILED test_an_unreconciled_row_cannot_carry_evidence
AssertionError: 'test_job_dispatch_gate_names_missing_prerequisites_without_a_traceback' is not None :
    api_job_schema promises something the measured output does not do; …
```

红。这条防「证据提前编造」的**方向**是对的。

但它守的是「表不能自相矛盾」，不是「证据是真的」。我把同一行改成 `agrees="是"` 并挂一个空壳用例：

```python
def test_api_job_schema_stub_that_starts_nothing() -> None:
    """Evidence placeholder: no subprocess, no entry point, no assertion."""
    assert True
```

再从表重新渲染文档表格 → **6 条守卫全部通过**。原因是 `registered_cases()` 只做名字正则匹配：

```python
CASE_PATTERN = re.compile(r"^def (test_[a-z0-9_]+)\(", re.MULTILINE)
```

它证明的是「这个名字的函数在一个测试模块里出现过」，不是「这个用例起过入口」。
`tests/ci/test_startup_gate_output_map.py:11-13` 写着「a case that starts the real entry point, not a
mocked function」、`tests/ci/startup_gates.py:34-35` 写着「A test that calls the entry function under a
monkeypatched collaborator is not evidence (#920)」——这两句现在是**注释级约定**，不是机械判据。
记 §8-A1（同一个硬化点可以一起覆盖）。

---

## 4. 进程级用例质量：11 条**全部**真起子进程，质量合格

`test_startup_gate_process_output.py` 共 8 个测试函数 + 1 个 3 参数化 = 11 条收集项，**每一条都走
`run_entry()` → `subprocess.run([sys.executable, *arguments], env=只透传 PATH/HOME/TMPDIR/LANG/LC_ALL)`**，
没有 `CliRunner`、没有函数内调用、没有 `monkeypatch`。逐条核对：

| 用例 | 起的入口 | 断言强度 |
| --- | --- | --- |
| `test_asset_inspector_reports_one_line_and_no_traceback` | `-m speechnexus_asset_inspector.main` | 首行**逐字相等** |
| `test_job_dispatch_gate_names_missing_prerequisites_without_a_traceback` | `deploy/job-startup-gate.py` | `>=3` 条 `FAIL:`、唯一性、点名的键、拒绝句中 `FAIL:` 不出现、无泄露 |
| `test_job_dispatch_gate_refuses_an_unsafe_schema_instead_of_crashing` | 同上 | 恰 1 条 schema 行 + 无 traceback |
| `test_job_dispatch_gate_connect_failure_names_no_server_or_login` | 同上 | 恰 1 条 `cannot connect` + `assert_no_leak` |
| `test_workbench_migrate_names_missing_inputs_without_a_traceback` | `-m …workbench.migrate` | 恰 3 条，`stdout == ""` |
| `test_workbench_migrate_names_an_unreachable_owner_without_echoing_the_dsn` | 同上 | 恰 1 条 + `(OperationalError)` + 无泄露 |
| `test_workbench_gate_names_missing_inputs_without_a_traceback` | `deploy/workbench-startup-gate.py` | 恰 5 条，`stdout == ""` |
| `test_workbench_gate_refuses_a_runtime_check_failure_in_one_named_line` | 同上 | 前缀逐字 + 无泄露 |
| `test_every_gate_failure_is_cheap_enough_for_a_startup_case`×3 | 三个闸门 | 退出码 `1`、无 traceback、`timeout=30` |

`tests/integration/test_startup_gate_output_pg.py` 也是真的：`subprocess.run([sys.executable, "-c",
"from speechnexus_backend.main import main; main()"])`，并且缺 DSN 时 `pytest.fail` 而不是 skip。
我在隔离 PG 上实跑：真建 schema、真起进程、真 `DROP SCHEMA`，1 passed（0.50 s）。

**唯一不足**：这些用例断言的是子串或首行，除 asset-inspector 那条外都不钉「逐字」。所以文案漂移不会被抓
（§2 的第二次注入就是靠这一点全绿的）。这解释了 §2 的漏洞，属同一类问题。

---

## 5. `database_findings` 的新顺序：没有把「连不上」误报成「schema 键非法」

新顺序是 `dsn_findings → _identifier(schema) → connect → 迁移头 → 角色守卫 → SELECT 探针 → 写权限`。
四种组合实测：

| 场景 | 实测（退出码 1） |
| --- | --- |
| 合法 schema + 可连但未迁移 | `FAIL: sn_review_empty.alembic_version is missing: run the migration profile …` |
| 合法 schema + 连不上 | `FAIL: … cannot connect to PostgreSQL (OperationalError); …` |
| 非法 schema + 连不上 | `FAIL: SPEECHNEXUS_DATABASE_SCHEMA is not a safe lowercase SQL identifier …` |
| 非法 schema + 可连 | 同上一行 |

结论：**「连不上」永远不会被报成「schema 键非法」**——`IDENTIFIER = [a-z][a-z0-9_]{2,62}` 对
`speech`/`workbench`/`sn_review_empty` 全部通过，只有真非法值才走新分支；`#877`/#924 关心的
「未迁移必须先于角色守卫被点名」也没有回归（第 1 行）。唯一的语义变化是：**两者同时坏时只报 schema**
（连不上被掩盖）。这可以直接接受：非法 schema 是确定的配置缺陷，且修复它必须先于排查连通性；
但它是一处**故意的新优先级**，值得在文档「读法」里留一句，否则运维会以为库是通的。

另外一个既有小瑕疵（不是本次引入）：`IDENTIFIER` 要求至少 3 个字符，所以 1–2 字符的 schema 名会被
报成「不是安全的标识符」，文案对短名不精确。

---

## 6. 22 个单元的完备性：漏了一个单元的两条拒绝，漏了一行输出

我按「容器入口 + `sys.exit`/`SystemExit` + `FAIL:`/`error:` 打印点」反查，覆盖面是
`deploy/compose.yaml` 的全部 17 个服务（含 `postgres`/`minio`/`minio-init`）、`backend/pyproject.toml` 的
19 个 console script、`asset-inspector`、`execution-gateway`、`node-controller` 的入口，以及两个 `deploy/*-startup-gate.py`。

**没进表、且确实是部署单元启动拒绝的**：

1. **`job-dispatch-worker` 自己的 `main()`**（`backend/src/speechnexus_backend/job_dispatch_worker.py:116-133`）。
   容器的 `command` 就是 `speechnexus-job-dispatch-worker`；闸门 `exec` 之后，这个进程还有两条具名拒绝：

   ```python
   except DispatchDependenciesUnavailableError:
       parser.exit(2, "Job dispatch unavailable: planner and result publisher are not assembled; no lease was claimed.\n")
   except Exception:
       parser.exit(1, "Job dispatch unavailable; no lease was claimed.\n")
   ```

   实测（空白环境）：退出 `1`，stderr 恰一行 `Job dispatch unavailable; no lease was claimed.`，无 traceback、
   无泄露。这是标准的「一行结论 + 非零退出」，正是本表该有的行，而表里 `job_dispatch_worker` 行的
   `stage` 只写「入口脚本 `deploy/job-startup-gate.py`」，覆盖不到它。

**进了表但输出列不完整**：

2. `asset-inspector` 行写了两条输出（configuration error / runtime root missing），漏了第三条：
   `raise SystemExit(f"asset inspector prlimit is missing: {PRLIMIT}")`（`asset-inspector/src/speechnexus_asset_inspector/main.py:32`）。

**按阶段覆盖、但只有「未验证」行的**：

3. `gateway` 的 `_build_control` 在 lifespan 阶段的非 VAD 拒绝（控制面）。表里唯一相关行
   `gateway_vad_lifespan` 的 `stage` 写的是「ASGI lifespan（`_build_control` / `runtime.open`）」，`observed`
   是「未验证」。我用真入口、真失败测了**同一 stage 的非 VAD 分支**（控制面查询权威被拒）：

   ```
   rc = 3
   File "…/speechnexus_gateway/app.py", line 317, in <lambda>
     control, cancelled = await _join_lifecycle_call(lambda: _build_control(resolved_settings))
   ValueError: invalid execution query reader authority
   ERROR:    Application startup failed. Exiting.
   ```

   这**正面支持**了该行「按 lifespan 归属推断为 traceback + 退出码 3」的推断（同一 stage、同一 uvicorn
   `sys.exit(3)`），虽然仍不能替 VAD 那句具体文案作证。建议把这半条实测证据写进 `disposition`，让「未验证」
   的边界更精确。

**判定为合理排除的**：`admin`/`workbench` 是 nginx 镜像（envsubst/`:?` 配置失败不是代码里的「判定」）；
`tts_settlement_worker.__main__`（一行固定拒绝，退出 `1`）没有容器入口，只在 API 线程内使用；
`deploy/compose-network.py`、`deploy/images/verify-asset-inspector-image.py` 是构建/CI 工具；
`engine-readiness-renew` 与 `engine-register` 是同一个可执行文件的子命令。

结论：文档正文那句「**每一处「拒绝启动」的判定**」是全域断言，但集合的定义规则没写下来，而实测至少漏一处。
按 M3 修（补一行最省）。

---

## 7. 反例重做：作者列的三条全部复现为红，我另加一条

每条都从被审 head 的干净树出发展开，完成即 `git checkout`/备份还原，收尾 `git status --porcelain` 为空
（§0）。断言都是「跑真入口的进程级用例」，不是单元桩。

| # | 作者声称的反例 | 我的还原方式 | 结果 |
| --- | --- | --- | --- |
| a | workbench 闸门换回 lifespan → 红 | 把 `deploy/workbench-startup-gate.py` 里 `asyncio.run(check(settings))` 换成 `pass`（运行期检查不再于 `exec` 之前跑） | **红**：`test_workbench_gate_refuses_a_runtime_check_failure_in_one_named_line` → `assert 0 == len(fail_lines)`，FAIL 行数 0 |
| b | 删 `workbench/migrate.py` 的 `except psycopg.Error` → 红（出现 `Traceback`） | 整段删除该 `except` 分支 | **红**：`test_workbench_migrate_names_an_unreachable_owner_without_echoing_the_dsn` → `'Traceback' is contained here` |
| c | 连接失败行加 DSN → 泄露断言红 | 把 `str(error).strip().splitlines()[0][:300]` 加回 `cannot connect to PostgreSQL` 那行 | **红**：`assert_no_leak` → `the gate echoed a deployment input: ['127.0.0.1']` |
| d | （我加）删掉提前的 schema 标识符守卫 | 删除 `_identifier(schema)` 的 try/except 块，恢复「逃逸成 traceback」 | **红**：`test_job_dispatch_gate_refuses_an_unsafe_schema_instead_of_crashing` → `assert 0 == len(schema_lines)` |

四条反例都成立，说明**这些用例对它们各自的失败面是有效的**（与 §2 的「文案漂移抓不到」并不矛盾：
它们是逐点钉住的，只是钉的点不等于表里 22 行的全部字面量）。

---

## 8. 必须修 / 可接受但需记录

### 必须修（三处，都是改文案或补一行）

**M1（严重，且是本 PR 的核心价值被合并顺序反噬）** — `tests/ci/startup_gates.py` 的 `api_job_schema` 行
与正文「两处与本表不一致的地方」第 1 条，引用的 `#924` §9.1 在主干上已经改成相反的事实。
主干 `docs/operations/compose-minimal.md` §9.1 现在写的是：

> **启动期未迁移**（库从未迁移过）：…… 但它发生在 ASGI lifespan 里，uvicorn 在启动失败时打印该异常及其
> traceback……以**退出码 3** 结束进程。因此启动期**不是**「一行 `FAIL:`、无 traceback」。

而本行 `promise` 写「`async-job-runbook.md` §1 行 2 与 `#924` 的 §9.1 承诺一行 `FAIL: … migration profile`
且「不再 traceback」」，`disposition` 写「由 `#924` 把文档改成实测为真」。主干上：§9.1 **已经**改完了
（而且改成了 traceback），`async-job-runbook.md` §1 行 2 **原样没动**，仍然写着
「API 启动则以 `FAIL: ... migration profile` 拒绝（都不再是 traceback）」。于是：

- 该行 `agrees` 的**理由**现在只剩 runbook §1 行 2 一条，§9.1 那条已经站在测量这一边；
- 本 PR 若照现状合入，会在「让承诺与事实一致」的表里新增一句**关于 §9.1 的假陈述**，并把运维指向错的文档；
- 主干上 runbook §1 行 2 与 §9.1 **互相矛盾**这个事实，本表最有资格点名它，但现在的文案点错了对象。

修法（不需要返工设计）：rebase 到 `acf633ff` 后，把 `promise` 改成只剩 runbook §1 行 2（并注明 §9.1 已由
`#924` 改为实测为真），`disposition` 改成点名 runbook §1 行 2 的修正是剩余动作及其归属。渲染守卫仍会绿
（它比对的是表↔文档两侧），否则就是 §2 说的那个漏洞。

**M2（严重：新增单测断言了真实驱动不可能产生的输出）** — `deploy/job-startup-gate.py` 的
`_connection_failure_class` 依赖 `getattr(error, "sqlstate", None)`，但 psycopg 的**连接失败**永远不带
`sqlstate`。实测四种真实失败（口令错 `28P01`、不可达、库不存在、角色不存在）：

```
auth refused   type=OperationalError   sqlstate=None   diag.sqlstate=None
unreachable    type=OperationalError   sqlstate=None   diag.sqlstate=None
no such db     type=OperationalError   sqlstate=None   diag.sqlstate=None
no such role   type=OperationalError   sqlstate=None   diag.sqlstate=None
```

真实入口的口令错输出因此是
`FAIL: SPEECHNEXUS_JOB_DISPATCH_DATABASE_URL cannot connect to PostgreSQL (OperationalError); …`，
而 `_REFUSED_LOGIN_PREFIXES`/`_UNREACHABLE_PREFIXES` 两个分支在生产的 psycopg 上**恒不可达**，
注释里「the operator-relevant difference instead (refused login vs. unreachable server vs. anything else,
by exception type)」是假的。更值得修的是证据面：`tests/unit/test_job_startup_gate.py` 新增的
`test_a_connect_failure_never_echoes_the_driver_message` 与 `test_an_unreachable_server_is_classified_without_the_address`
用手写的 `RefusedConnectionError(..., sqlstate="28P01")` / `sqlstate="08006"` 喂了真实驱动不会有的属性，
于是断言了 `(the login was refused)` / `(the server is unreachable)` 这两种**真实入口不可能打印**的文本。
这正是本 PR 要治的 #920 病（证据比真实协作对象更强）在 PR 内部复发。

修法二选一，都便宜：(i) 用**不打印**的方式判别——检查 `str(error)` 是否含 `password authentication failed`
等标记再映射成固定句（只读、不回显）；(ii) 删掉两个前缀元组、注释与那两条断言，只保留 `type(error).__name__`，
并把注释改成「驱动类型名是唯一可安全转述的分量」。

**M3（中：集合不完备，且文档是全域断言）** — 补 `job-dispatch-worker` 自己 `main()` 的两条拒绝
（退出 `2`：`Job dispatch unavailable: planner and result publisher are not assembled; no lease was claimed.`；
退出 `1`：`Job dispatch unavailable; no lease was claimed.`）为一行，或把 `job_dispatch_worker` 行的 `stage`
扩到「闸门脚本 + worker 自身 `main()`」；顺带补 `asset-inspector` 行的第三条输出
`asset inspector prlimit is missing: /usr/bin/prlimit`。若不想扩表，至少把正文「每一处「拒绝启动」的判定」
改成明确的取舍规则（例如「只收 Compose 入口脚本与各 `main()` 里 `exec`/服务循环之前的拒绝」），
否则读者会把它当完备清单用。补行成本最低，建议补。

### 可接受但需记录

- **A1 表↔产品代码的真实性无人守。** §2 的注入证明：改一行被表格逐字引用的产品文案，6 条文档守卫 +
  11 条进程用例 + 56 条单元测试全绿。`registered_cases()` 只做名字正则，空壳用例即可充当「证据」（§3）。
  低成本硬化：对 15 条「无格式承诺」行里被引用的字面量（`invalid gateway file ASR configuration`、
  `asset inspector runtime root is missing`、`Disable worker failed; …`、`Runtime delivery unavailable; …`、
  `invalid voice evidence worker configuration`、`error: invalid_settings`、`RuntimeStartupError` 的固定句等）
  加一条 `grep` 式守卫，要求每个引号内字面量在对应产品文件里存在；再把「用例存在」升级为「用例体内出现
  `subprocess`」。这是一条新守卫，不是返工。
- **A2 「唯一来源」是测试文件本身。** 文档与 PR 描述都诚实这么写，不算误导；但建议在「读法」里加一句
  「本表是**承诺与实测的登记册**，不是产品行为的派生断言；产品文案漂移不由本表负责」，避免下一个读者误判。
- **A3 traceback 类仍打印驱动原文与构建机路径。** §1.2 实测：API lifespan 口令错会打印
  `connection to server at "127.0.0.1", port 55480 … password authentication failed for user "sn_api_runtime"`。
  文档「不泄露」条款只约束一行结论类单元，口径正确，不需要改；但**任何后续通稿**不得声称启动失败输出不泄露
  凭据，`#924` 改 §9.1 时也不能加这种话（主干当前没有，保持即可）。
- **A4 `write_privilege_findings` 的 `reason` 回显机制无保证、无用例。** §1.3。真 psycopg 下只能带 schema/表/
  角色名（文档允许），但保证来自异常类型巧合。建议顺手改成 `type(error).__name__` + schema，与相邻两处一致。
- **A5 自述数字与代码不符。** 实际 22 行 = 20「是」（5 承诺形态 + 15 无格式承诺）+ 1「否」+ 1「未验证」；
  自述的「16 形态一致 / 4 无格式承诺」不是这个切分。表内自洽，只影响描述准确性。
- **A6 `gateway_vad_lifespan` 的「未验证」可以收窄。** §6-3 已实测同一 stage 的非 VAD 分支为
  traceback + 退出码 `3`，建议把这条证据写进 `disposition`：未验证的只剩「该 stage 里 VAD 那句具体文案」，
  而不是整个 stage 的形态。

### 明确判为可以保留、不需要改的

- 两处修复本身（schema 守卫提前、连接失败行去回显）：实测有效，无泄露，反例可复现。
- `database_findings` 新顺序：无误报、无 #877/#924 回归（§5）。唯一要记录的是「两者同时坏时只报 schema」这一
  新优先级。
- 11 条进程级用例全部真起子进程（§4）。
- 「非一致行不得带用例」的方向（§3），只是强度需要 A1 补强。
- 与 `origin/main` 的合并：文本零冲突、合并树 `tests/ci` 全绿（§0）。
