# 截止时间预算 helper 的非作者审查（PR #945，关联 #944/#938）

**能合。** 没找到阻塞合并的缺陷：`#938` 那条守卫的断言一条没少、一条没弱（§1），`0.9×` 下界与 `2×`
上界都有实测红（§2），两条结果泳道的原有断言全在并各多两条强的（§5）。代价是三个测试的「快路径
≤ 0.3 s」这个从未声明的副作用约束被换成「≤ 2.0 s」且失败信息不体现（§3），以及失败路径单测耗时
从 1.2 s 涨到 10–44 s（§4）；这两条都是可接受但必须写进记录的取舍，不是缺陷。§7 六项里四项是文案与
收敛建议，其中两项指向新文件里照搬过来、与实测不符的事实陈述。

审查对象：`fix/result-deadline-same-shape`，head `222d1401`（父 `f430d546`）。审查分支
`review/945-deadline-budget` 从 `origin/main`（`f430d546`）新建，本报告是该分支唯一改动。

**行号口径：** 下文 `file:line` 按被审查 head `222d1401` 计。环境：macOS 26、Python 3.14.9（`uv sync
--locked --all-packages --all-groups`，所有 `uv`/`pytest` 命令带 `UV_NO_CONFIG=1`），审查 worktree
`/private/tmp/sn947`；对照基线用同一 lock 的另一 worktree `/private/tmp/sn946-base`（`f430d546`）。
所有变异在收尾还原，两个 worktree 的 `git status --porcelain` 为空。

---

## 0. diff 口径

`git merge-base origin/main 222d1401` = `f430d546` = 当前 `origin/main` 顶端，因此两点与三点口径
一致，没有把已合入的其它改动算进来：

| 口径 | 结果 |
| --- | --- |
| `git diff --stat f430d546 222d1401` | 4 文件 +222/−94 |
| `git diff --stat origin/main...222d1401` | 4 文件 +222/−94（相同） |

改动集（`git diff --numstat f430d546 222d1401`）：

| 文件 | +/− |
| --- | --- |
| `tests/support/deadline_budget.py`（新） | +103 / −0 |
| `tests/unit/gateway/test_execution_object_deadline.py` | +17 / −57（常量与 4 条断言搬走） |
| `tests/unit/gateway/test_asr_results_deadline.py` | +52 / −21 |
| `tests/unit/gateway/test_tts_results_minio.py` | +50 / −16 |

三个被测用例的参数化数量与基线一致：执行入口 3 个（`HEAD.headers`/`GET.headers`/`GET.body`）、
asr 3 个（`PUT`/`HEAD`/`GET`）、tts 3 个（同）。

`tests/support/deadline_budget.py` 只被这三个测试导入，产品代码无引用，未触碰导入边界：
`lint-imports` 9 契约全 KEPT（§8）。

---

## 1. `#938` 那条守卫：逐条对比（最重要项）

`git show f430d546 -- tests/unit/gateway/test_execution_object_deadline.py` 的原文版本，其
`test_trickling_input_cannot_extend_total_deadline_and_releases_socket` 里是 4 条断言：

| # | 基线（`f430d546`） | head（`222d1401`） | 强弱 |
| --- | --- | --- | --- |
| 1 | `assert server.started.is_set(), observed` | `deadline_budget.py:97` `assert started, observed` | 同 |
| 2 | `assert fast_path is not None and fast_path < budget, observed` | `:98` 同式 | 同 |
| 3 | `assert not server.trickled.is_set(), observed` | `:100` `assert not trickled, observed` | 同 |
| 4 | `assert budget * DEADLINE_LOWER_RATIO <= elapsed <= budget * DEADLINE_UPPER_RATIO` | `:103` 同式（常量值 0.9/2.0 原样搬入） | 同 |

`observed` 的字段也逐个一致（`budget/elapsed/fast_path/trickled/calls`，`calls` 都是
`[verb for verb, _, _ in server.calls]`）；断言顺序一致，失败时先报「没进慢阶段」再报耗时越界，
可读性不变。调用点 `test_execution_object_deadline.py:172` 之后的 4 条断言（`calls` 序列、
`versionId=v1`、`disconnected.wait(1)`、临时目录为空）一字未动。

**结论：`#938` 那条守卫 0 条删除、0 条新增、0 条由强变弱，是纯搬移。** 唯一的形态变化是断言从
测试函数内搬进 helper（失败 traceback 多一跳 `.venv/...` 之外的 `deadline_budget.py:103`），以及
`fast_read()` 不再自己计时（计时移入 `calibrated_budget()`，`time.monotonic()` 仍是每样本一对），
语义等价。

顺带确认这次搬移没有顺手放大预算：基线 `MINIMUM_BUDGET_SECONDS = 0.5` / `MAXIMUM = 2.0` /
`FAST_PATH_MULTIPLE = 10` / `CALIBRATION_READS = 3` / `TRICKLE_BUDGET_MULTIPLE = 20` /
`TRICKLE_INTERVAL_SECONDS = 0.03` 与 head `deadline_budget.py:25–47` 逐个相等。

---

## 2. 两个边界变异：红/绿证据

变异都做在被测系统一侧（改测试里的预算接线），不改断言，这样红的来源只能是「耗时与预算对不上」。
每次变异后单跑，跑完 `git checkout` 还原。

### 2.1 下界：让 `elapsed` 远小于 `0.9×预算` → 必须红

做法：把实际生效的总截止时间改成 `budget / 5`，而断言仍拿 `budget` 比。这样慢阶段照样进得去
（不会先被 1 号断言拦），读照样以 `*_deadline_exceeded` 结束，只有耗时不匹配。

```
# tests/unit/gateway/test_asr_results_deadline.py:143  value = adapter(server, tmp_path, budget / 5)
$ pytest "…::test_trickle_headers_cannot_extend_total_deadline" -q
>   assert budget * DEADLINE_LOWER_RATIO <= elapsed <= budget * DEADLINE_UPPER_RATIO, observed
E   AssertionError: budget=0.500s elapsed=0.101s fast_path=0.002190291997976601 trickled=False calls=['PUT', 'HEAD', 'GET']
tests/support/deadline_budget.py:103: AssertionError
3 failed in 0.49s
```

执行入口泳道同样复现（`test_execution_object_deadline.py:169` 改 `duration_seconds=budget / 5`）：
`3 failed`，`elapsed=0.101s`，`fast_path=0.00168958…`，同样停在 `:103`。**红。**

**关于任务里建议的「trickle 拉到 100 倍 + 让读提前被别的原因结束」**：我按此试了，**构造不出**，原因
不是手法问题而是该方案的语义不成立——把 `TRICKLE_BUDGET_MULTIPLE` 改成 100 只会让对端写得更久，
并不会让读提前结束（实测把常量改 100 后该用例仍绿，`1 passed in 0.63s`）；要让 `elapsed` 提前，
只能是截止时间本身提前触发，所以唯一有效的构造就是把生效截止时间接到比断言预算小的地方。另外
「读因为正常完成而提前结束」这条路也测不到下界：那会先撞 `pytest.raises` 的 `DID NOT RAISE`
（§4 的 drain 实验即是此形态），失败信息指向不到 `:103`。

### 2.2 上界：让 `elapsed` 超过 `2×预算` → 必须红

做法：把实际生效的总截止时间改成 `budget * 3`，断言仍用 `budget`。此时 1–3 号断言都满足
（`started` 已 set、`fast_path` 很小、trickle 总量 20×预算远未写完），只有耗时会撞上界。

```
$ pytest "…::test_trickle_headers_cannot_extend_total_deadline" -q      # asr: adapter(..., budget * 3)
E   AssertionError: budget=0.500s elapsed=1.501s fast_path=0.0030319999787025154 trickled=False calls=['PUT', 'HEAD', 'GET']
tests/support/deadline_budget.py:103: AssertionError
3 failed in 4.71s
```

tts 泳道同样复现（`test_tts_results_minio.py:265` 改 `bounded_store(server, budget * 3)`）：
`3 failed`，`elapsed=1.501s`，`fast_path=0.00244362…`。**红。**

两条边界都是**由同一条断言 `:103` 承担**，上下界各自有独立的红证据；`0.9`/`2.0` 两个常量没有
形同虚设。

---

## 3. 校准读会不会把「第一次读才慢」变成测不到：会，且这是本 PR 最大的语义代价

读代码即可判定：`calibrated_budget()`（`deadline_budget.py:50`）在预算被使用**之前**跑
`CALIBRATION_READS = 3` 次完整读，并取 `max(samples) × 10`；预算读是**第 4 次**。任何「只在进程
第一次读出现」的成本必被前 3 次吸收，而且吸收量不设上限（`max` 只用于抬预算）。所以：

- 这类成本**不会**让这三个测试变红，只会把该条的预算抬到 `MAXIMUM_BUDGET_SECONDS = 2.0`；
- 抬到天花板后**没有任何诊断输出**：测试是绿的，`observed` 那句只在失败时才打印。

实测（临时探针 fixture，`MinioAsrResults._put` 首次调用注入 1.0 s 延迟，跑完删除）：

| 形态 | 老接线（`origin/main` 的 flat 0.3 s 守卫） | 新接线（head 的校准预算） |
| --- | --- | --- |
| 进程级一次性成本（全局 flag，仅第一次读慢） | **6 failed**（`started` 未 set，正是 `#938` 修的那种红） | **6 passed**，`budget=2.0 elapsed=2.002`，`fast_path=0.0015`（成本已被校准读吃掉） |
| 每实例冷启动成本（每个新 store 第一次读慢） | **6 failed**（同上） | **6 passed**，`budget=2.0 elapsed=2.001`，`fast_path=1.0046`（成本落在预算读自己的快路径里，被 10× 余量兜住） |

也就是说：**「第一次读 1.0 s」这个在 `origin/main` 上是红的形态，在 head 上变成绿**，预算恰好顶到
天花板 2.0 s，单条用例从 0.5 s 变成 2.0 s。

怎么判这件事，我的结论是「不是缺陷，但必须记录，且天花板情形应当发声」：

1. 这三个用例的契约是「截止时间到了就切断、不是被对端写完」，不是「快路径有多快」。把机器/冷启动
   成本从预算里剥离正是 `#944` 的目的，为此付出「看不到冷启动回归」的代价是自洽的。
2. 但这意味着一条**从未被声明**的副作用守卫（「快路径 ≤ 0.3 s 否则红」）被静默替换成「≤ 2.0 s
   否则红」，且新上限不体现在任何失败信息里。若团队确实依赖这三个用例顺手看快路径延迟，应另立
   一条显式延迟断言/基准，而不是指望这里。
3. 余量在天花板处会**名不副实**：`deadline_budget.py:51` 的 docstring 写的是「an order
   of magnitude above this machine's fast path」，但上表第二种形态实测 `fast_path=1.0046`、
   `budget=2.0`，余量只有 2×；再大一点（1.9 s）就只剩 1.05×，正好退回 `#938` 想消灭的「截止时间与
   快路径赛跑」。届时不会红（`started` 仍能 set、耗时仍在 `[1.8, 4.0]` 内），只是余量趋零。建议在
   `calibrated_budget()` 里区分「被 `MAXIMUM` 截断」这一情形并记在失败信息里（或直接断言
   `calibrated <= MAXIMUM`，让「这台机器快路径 > 0.2 s」作为一种 runner 事实被报出来）。

---

## 4. `trickle_frame_count()` 的帧数与最坏运行时间

`trickle_frame_count(budget) = ceil(20 × budget / 0.03)`（`deadline_budget.py:67`）：

| budget | 帧数 | 对端 trickle 总时长（不被切断时） |
| --- | --- | --- |
| 0.5 s（本机实际值，见下） | 334 | 10.02 s |
| 1.0 s | 667 | 20.01 s |
| 2.0 s（`MAXIMUM_BUDGET_SECONDS`） | **1334** | **40.02 s** |

预算 2.0 s 时帧数是 1334，对端要写 40 s —— **但测试本身不会跑 40 s**：读在预算处被切断，客户端
关连接后对端下一次写就 `BrokenPipe`，`Handler` 的循环随之退出。实测把
`MINIMUM_BUDGET_SECONDS` 抬到 2.0（模拟慢机器顶到天花板）：

```
9 passed in 18.60s
2.08s / 2.08s / 2.06s（执行入口 3 条）  2.03s × 3（asr）  2.03s × 3（tts）
teardown 全部 0.01s（没有残留线程把 trickle 写完）
```

即天花板下 9 条用例合计 ≈ **18.6 s**，单条 ≈ `budget + 0.03 ~ 0.08 s`，不是「几十秒」。本机实际
预算落在地板（`fast_path` 只有 1.5–3.7 ms，`10×` 校准值约 0.05 s，被 `MINIMUM = 0.5` 抬起来），
所以常态是每条 0.52–0.61 s。相对 `origin/main` 的同一子集：23 passed 4.57 s → 23 passed 5.94 s
（asr/tts 的 deadline 用例 0.31 s → 0.53 s，6 条共 +1.3 s；执行入口那条 `#938` 本来就是 0.58 s）。

**失败路径**才是真正变长的地方，值得记一笔：若被测代码不再遵守截止时间（读被对端 trickle 拖完），
红之前要先写完整条 trickle。实测把生效截止时间接到 60 s 让读 drain：

| budget | 红之前耗时 | 形态 |
| --- | --- | --- |
| 0.5 s（地板） | **11.58 s** | `DID NOT RAISE AsrResultError` |
| 2.0 s（天花板） | **44.33 s** | 同上 |

对照 `origin/main` 的固定 40 帧 = 1.2 s。最坏情形 9 条 × 44 s ≈ **6.6 min**，单元测试 job 的预算是
15 min、正常 ~4 min，仍在预算内，但「一条真实回归把 job 拖到接近超时」的概率比原来高一个量级。
这是失败路径成本，可接受，前提是知道它来自 `TRICKLE_BUDGET_MULTIPLE = 20` 而不是 percentile 抖动。

---

## 5. 两条结果泳道原有断言核对

`git show origin/main:tests/unit/gateway/test_asr_results_deadline.py` 与新版逐条比：

| 基线断言 | asr 新版 | tts 新版 |
| --- | --- | --- |
| `not server.trickled.is_set()` | 在（helper `:100`） | 在（helper `:100`） |
| `server.started.is_set()` | 在（helper `:97`） | 在（helper `:97`） |
| `calls` 序列 `["PUT"] / ["PUT","HEAD"] / ["PUT","HEAD","GET"]` | 在（`test_asr…:160–164`） | 原本没有，仍未加 |
| `server.bodies[slow_key] == RESULT.body`（未回滚） | 在（`:167`） | 在（`test_tts…:284`） |
| 无 `DELETE` | 在（`:168`） | 原本没有，仍未加 |
| `sum(PUT) == 1` | 原本没有 | 在（`:285`） |
| **新增** `fast_path < budget` | 有（`:98`） | 有（`:98`） |
| **新增** `0.9× ≤ elapsed ≤ 2×` | 有（`:103`） | 有（`:103`） |

**一条不少，各多两条更强的。** 两处形态变化：

1. 断言从 `try:` 内移到 `finally: value.close()` 之后（`test_asr…:152–168`、`test_tts…:274–285`）。
   `close()` 对两者都不持有连接（docstring 明说每次 stage 自开自闭），语义不变，反而让断言不再依赖
   `close()` 是否抛错。
2. 新增 `server.calls.clear()`（`test_asr…:142`、`test_tts…:264`）。因为 `server` 是函数级 fixture，
   清不清在计数上等价；但加了之后 `calls` 类断言明确只覆盖预算读那一次，比原来更严。
   **`server.bodies` 没有跟着清**：校准读的 PUT 已经把 `bodies[slow_key]` 写成 `RESULT.body`，所以
   「body 未被回滚」这条在当前实现下仍成立（预算读的 PUT 会重新赋值；若读坏则是 `b""` → 红；
   `calls.clear()` + `PUT` 计数/序列把「这次 PUT 确实发生过」钉住了），但它不再能单独证明
   「这次请求的 body 被接受」。建议在 `calls.clear()` 旁一并 `server.bodies.clear()`，成本 0，
   断言强度回到字面意思。

---

## 6. PR 自评核实：「相对预算、调大只有变慢、上界由 `MAXIMUM_BUDGET_SECONDS = 2.0` 兜住」

**结论方向对，实验描述按字面复现不了，有一处把两个旋钮混成一个。**

- 「守卫是相对预算的」✅ 实测证实：`MINIMUM = MAXIMUM = 30` 时每条用例 30.03 / 30.03 / 30.05 s、
  3 条代表（各泳道一条）`3 passed in 90.25s`，全绿不红。下界 0.9/上界 2.0 都是 `× budget`，
  `trickled` 的判据（20×）与 `fast_path < budget` 也都是相对的，确实不存在「预算变大就红」。
- 「上界由 `MAXIMUM_BUDGET_SECONDS = 2.0` 兜住」✅ 对**校准出来的**预算成立，且它连「变慢」也一并
  兜住：把 `MINIMUM` 单独改成 30 时，`calibrated_budget()` 的
  `min(MAXIMUM, max(MINIMUM, calibrated))` 会把它静默压回 2.0 —— 实测 `9 passed in 18.58s`，
  每条仍是 2.03 s，不是 30 s。所以「9 passed 但每条约 30 s」只有在**同时**抬高
  `MAXIMUM_BUDGET_SECONDS`（或绕开这行 clamp 直接给测试塞 30）时才能看到；只提「把预算改成 30 s」
  又说「上界由 2.0 兜住」是自相矛盾的，读者会以为改一处就够。
- 边界补充：`MAXIMUM` 只约束校准路径，不约束调用方显式传入的预算；另外 `min(MAXIMUM, max(MINIMUM,
  …))` 在 `MINIMUM > MAXIMUM` 时静默返回 `MAXIMUM`（我实测即此），调参的人拿到的值会与写的常量
  不一致，建议加一句注释或断言。

---

## 7. 必须修清单

阻塞项：**0**。以下按「合并前 / 后续 / 记录」分级，前两项是文案事实修正（不改行为，几分钟），
建议随本 PR 或紧随其后的一条 squash 处理。

| 级别 | 位置 | 问题 | 建议 |
| --- | --- | --- | --- |
| 必须修（文案，非行为） | `deadline_budget.py:51` | 「an order of magnitude above this machine's fast path」在天花板截断后不成立（实测 `fast_path=1.00` / `budget=2.0`，余量 2×） | 改成「unless the ceiling binds」，或在 `calibrated_budget()` 里对被截断的情形留痕 |
| 必须修（文案，非行为） | `deadline_budget.py:39–40` | 「past this ceiling the fast path is a runner problem the failure message shows」不成立：单个校准样本被拉长只会抬预算，测试绿且无任何输出（§3 探针即是此形态） | 删掉「the failure message shows」，或真的把被截断事实写进 `observed` |
| 建议 | `test_asr…:142`、`test_tts…:264` | 只 `calls.clear()` 未 `bodies.clear()`，body 断言可被校准读的残留满足（§5.2） | 一并 `server.bodies.clear()` |
| 建议 | `deadline_budget.py:64` | `min(MAXIMUM, max(MINIMUM, …))` 在 `MINIMUM > MAXIMUM` 时静默返回 `MAXIMUM`，与所写常量不符（§6） | 注释说明或加 `assert MINIMUM <= MAXIMUM` |
| 记录 | `deadline_budget.py:28` | 失败路径单测耗时 1.2 s → 10–44 s（§4），最坏 9 条 ≈ 6.6 min | 已记录在本报告；若将来把 `TRICKLE_BUDGET_MULTIPLE` 再调大需重算 |
| 记录 | §3 | 「第一次读才慢」由红转绿，快路径 ≤ 0.3 s 的隐含约束消失 | 若需要，另立显式快路径延迟断言，不要在本 helper 上加 |

---

## 8. 验证记录

| 检查 | 结果 |
| --- | --- |
| `pytest tests/unit/gateway/{execution_object_deadline,asr_results_deadline,tts_results_minio}.py` | 43 passed，32.63 s（首次含 mp3 编码单测 22.08 s） |
| `pytest tests/unit/gateway` | 1729 passed，17.79 s |
| `pytest tests/unit`（全量） | 7512 passed，3 skipped（`prlimit` 仅 Linux），117.32 s |
| `pytest tests/ci` | 547 passed + 2259 subtests，16.44 s |
| `ruff format --check` / `ruff check`（改动 4 文件） | 4 files already formatted / All checks passed |
| `mypy` | Success: no issues found in 1546 source files |
| `lint-imports` | 9 contracts kept，0 broken |
| 变异还原 | 两个 worktree `git status --porcelain` 空；探针文件已删除 |

探针与变异清单（全部已还原）：asr `budget/5`、asr `budget*3`、exec `budget/5`、tts `budget*3`、
asr `timeout=60`（drain，地板与天花板两轮）、`TRICKLE_BUDGET_MULTIPLE=100`、`MINIMUM=2.0`、
`MINIMUM=30`（单独）、`MINIMUM=MAXIMUM=30`、`_put` 首次调用注入 1.0 s（进程级/每实例两种）。
