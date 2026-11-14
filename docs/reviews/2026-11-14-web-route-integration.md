# 非作者审查：纯 Web 路由接入 Web 能力道集成分片（PR #758 / issue #757）

- 被审对象：PR [#758](https://github.com/hrygo/SpeechNexus/pull/758)，分支 `ci/757-web-integration`，head `e4c05cabdc83cf45b1497d00394dc57b82d29ff1`，baseline `origin/main`=`fe165d8a`，merge-base `26fd8b39`
- 审查分支：`review/767-web-routing`，基于 `origin/main` 的独立 worktree `/private/tmp/sn767`；被审内容以 detached 快照 `/private/tmp/sn767-pr`（`e4c05cab`）读取与运行
- 审查时间：2026-11-14（Asia/Shanghai），运行证据核验时刻见文末时间线
- 审查性质：非作者、只读；未修改工作流/路由脚本/测试；所有变异只发生在临时快照副本，未 push

## 结论

**通过，无阻塞项。** 方案堵住的洞是真实的：`web` 路由在 `Integration 4`/`Integration 5` 两片执行的 nodeid 集与 `full` 路由**完全相同**（我下载两条真实运行的分片制品逐条比对，并用真实分区器独立复算，三者一致）；六片证据门禁 `coordination` 与合并覆盖率判定**未被放宽**（作业结构级相等、判定脚本零改动），最终门禁反而**收紧**了一个维度。

三条反例中反例 1、反例 3 均已核实为真实运行且结论如作者所述；反例 3 尤其证明新方案能在 PR 阶段抓住 #745 那类缺陷（`Integration 4` 因 Playwright strict mode violation 失败，最终门禁 `integration=failure` 变红）。临时 draft PR #759–#763 全部 CLOSED、未合并、分支已删、mutant 提交不在 `main`。

遗留问题只有两条文案/实现细节（守卫只查 `browser` 而注释说「browser 或 Node 契约」；`ci-routing.py` 注释声称「规则只在一处」，实际复制了 `max(1, count // 3)`），都不通向假绿，列为建议修，不阻塞合并。

## 本卡核心问题一：`web` 路由在 4/5 两片执行的 nodeid 集是否与 `full` 相同

**相同。三重独立证据。**

### 证据 1：两条真实运行的分片制品逐条比对

| 运行 | 路由 | head | shard 4 `selected` | shard 5 `selected` | `collection` |
| --- | --- | --- | --- | --- | --- |
| `43577755898` | `web` | `f39c9150` | 392 | 392 | 2754 |
| `43577692527` | `full` | `db14fa86` | 392 | 392 | 2754 |

```
shard 4: web selected == full selected : True
shard 5: web selected == full selected : True
collection_digest 两侧相同 : sha256:0dc6d8b87ab18...
outcomes 分布相同 : shard4 391 passed + 1 skipped；shard5 389 passed + 3 skipped
```

两侧 `collection_digest` 相同说明完整收集集一致，`selected` 逐元素相同说明分片选择一致——不是「另一个更小的子集」，也不是「另一个选择逻辑」。

### 证据 2：真实分区器在本机独立复算

用 PR head 的 `scripts/dev-env/integration-shards.py` 对真实 `pytest -m integration tests/integration --collect-only` 收集集复算：

```
collection=2754 web_class=50
shard 0: 493 nodeids; web_consumers=0
shard 1: 493 nodeids; web_consumers=0
shard 2: 492 nodeids; web_consumers=0
shard 3: 492 nodeids; web_consumers=0
shard 4: 392 nodeids; web_consumers=25
shard 5: 392 nodeids; web_consumers=25
union all = 2754 collection = 2754
union(4,5) = 784 ; web consumers covered = 50 / 50
web consumers missing from 4/5 = 0
disjoint lanes = True
web_route_shard4_identical= True web_route_shard5_identical= True
not_run_under_web = 1970 nodeids (lanes 0-3)
not_run_contains_web_consumer = False
```

这份复算同时回答了两个问题：4/5 两片的集合与 `full` 相同；**且 0–3 片一个 Web 消费者都不含**（`web_consumers=0`），所以排除 0–3 不会漏掉任何浏览器/Node 契约消费者。本地复算的 392 例/片也与作者文档一致。

### 证据 3：机制层面

`partition(nodeids, index, count)` 只读取 `(collection, index, count)` 三个参数，不读取「本次哪些片会跑」；`web_lane_count = max(1, count // 3)` 与 `--count 6` 都未改动。因此矩阵少展开四行不会改变剩下两行的选择结果——这是纯函数性质，不是巧合。

**需要说清的边界**：`web` 路由不执行 0–3 片上的 1970 条普通 DB 用例，因此它对「完整集合」不构成证据，也不声称是。完整集合 + 同提交互斥 + 合并覆盖率仍只在 `full` 的 `coordination` 判定。

## 本卡核心问题二：Gate 是否被放宽

**没有放宽，是收紧。** 分三个层面核。

### 1. `coordination`（六片证据与合并覆盖率门禁）逐字段未变

对 baseline 与 head 的 `ci.yml` 做 YAML 结构级对比：

```
coordination job identical: True
name : Gate: six-shard integration evidence and merged coverage
if   : ${{ always() && needs.changes.result == 'success' && needs.changes.outputs.mode == 'full' }}
needs: ['changes', 'python_checks', 'integration_shards']
timeout-minutes: 10
serialized equal: True
```

配套脚本与判定参数都不在本次改动清单内：

```
changed files: ['.github/workflows/ci.yml', 'docs/validation/ci-validation.md',
 'scripts/dev-env/ci-routing.py', 'tests/ci/test_ci_routing.py',
 'tests/ci/test_node_controller_image_ci.py', 'tests/contract/test_ci_workflow.py',
 'tests/unit/test_integration_shards.py']
  scripts/dev-env/integration-shards.py untouched: True      # 分区算法/同提交互斥集合校验
  scripts/dev-env/python-coverage.py untouched: True         # 覆盖率合并 + fail_under 判定
  scripts/dev-env/workbench-pg-probe.py untouched: True      # 分片执行与 25 min 内层上限
  scripts/dev-env/download-integration-evidence.py untouched: True   # --count 6 证据下载
  pyproject.toml untouched: True                             # fail_under = 80
```

即分区算法、`--index`、`--count 6`、30 分钟作业预算、六片必然成功、同提交完整互斥集合、合并后 `fail_under=80` 全部原样。

### 2. 最终门禁新增的是必须匹配的新维度

逐模式对比 baseline 与 head 的期望表，只有新增、没有改动：

| 模式 | `integration` 期望 | 相对 baseline |
| --- | --- | --- |
| `docs` | `skipped` | 新增键；`checks`/`coordination`/`web`/`deployment`/`acceptance`/`docs_format` 期望不变 |
| `web` | `success` | 新增键，且是本改动唯一放开的作业；其余期望不变 |
| `no-db` | `skipped` | 新增键；其余不变 |
| `full` | `success` | 新增键，与 `coordination` 要求重合而不冲突 |

同时 `integration_shards` 被加入最终门禁的 `needs`，门禁多等一个结果。旧期望一条都没有从 `success` 降为 `skipped`，因此不存在放宽方向。

实证（三条真实运行的门禁日志）：

```
失败方向  43577800599 → ##[error]Scope=web; Python=success; PG/InnoDB=skipped; integration=failure; ...
通过方向  43577755898 → Verified scope=web; inapplicable lanes are not application-test evidence.
通过方向  43578978352 → Verified scope=docs; inapplicable lanes are not application-test evidence.
```

第一行正是 #745 类缺陷在 PR 阶段被新门禁拦住的直接证据：改回旧门禁时这一行不会出现（见下文反例 1 的运行仍 success）。

### 3. 变异验证：这些断言是真约束

在临时快照里做三处变异（均未 push），确认门禁断言会被咬住：

| 变异 | 结果 |
| --- | --- |
| M1：`integration_shards` 的条件改回 `mode == 'full'` | `tests/contract/test_ci_workflow.py` 2 条红（`test_a_web_route_runs...`、`test_required_workflow_is_never_filtered_out...`） |
| M2：删掉 `matrix.exclude` 一行 | `tests/contract` 1 条 + `tests/unit/test_integration_shards.py` 4 条红 |
| M3：把最终门禁 `web` 的 `integration=success` 改回 `skipped`（即放宽） | `tests/contract/test_ci_workflow.py` 8 条红 |

M3 是关键：如果把门禁放宽，现有测试会立刻变红，说明「收紧」这一状态被钉住了，不是靠作者自述。

## 十三条逐条判定

| # | 项 | 判定 | 关键证据 |
| --- | --- | --- | --- |
| A1 | `web` 下 4/5 片 nodeid 集是否与 `full` 相同 | **通过（核心）** | 真实制品逐条相同（392/392，`collection_digest` 相同）；独立复算相同；`partition` 为纯函数；0–3 片 Web 消费者数为 0 |
| A2 | `excluded_shards` 与 `strategy.matrix.exclude` 接法 | **通过** | 官方语义「excluded configuration only has to be a partial match」；本矩阵只有 `shard` 一个键，部分/全量匹配无歧义；`web` 运行只展开 4/5 两行、`full` 展开 6 行、`docs`/`no-db` 整体 skipping 均实测 |
| A3 | `web` 能力道失败关闭守卫的可靠性与判据 | **通过（附文案偏差）** | 判据是「真实被选集合里是否有 `*_browser.py`/probe 模块」，不是声明式白名单；M5 变异下 `browser=false` 守卫红 + tests/ci 红；缺口是注释说「browser 或 Node 契约」但只查 `browser`（方向为 fail-closed） |
| B4 | 量化数字复核 | **通过** | 见下方「代价复核」，三行独立复算与作者表一致（29.5 / 14.6 / 67.9 runner 分钟，16/15/21 行） |
| B5 | `docs` 路由仍不跑集成分片 | **通过** | run `43578978352`：15 行，Integration 行 skipped、`coordination` skipped、`docs_format` success、最终门禁 success |
| C6 | 反例 1（条件改回 `mode=='full'`） | **通过** | run `43577811710` conclusion=success，`integration_shards` 一行 skipping（未展开），`coordination` skipping，最终门禁 success；路由日志 `{"mode": "web", "reason": "...complete Web lane."}`（旧文案，确认是基线工作流） |
| C7 | 反例 3（复现 #745） | **通过（最关键）** | run `43577800599`：`web` 作业 success、`Integration 4` failure、`Integration 5` success、最终门禁 failure；失败即 `strict mode violation: getByLabel('新单元文本') resolved to 2 elements`，具体为 `test_workbench_real_runtime_chapter_create_browser.py::test_a_chapter_created_in_the_production_bundle_is_stored_and_readable`（`workbench-chapter-create-probe.mjs:94`） |
| C8 | 临时 draft PR #759–#763 | **通过** | 五个全部 `CLOSED`、`mergedAt=null`、`mergeCommit=null`；远端分支已不存在；四个 head 提交均非 `origin/main` 祖先 |
| D9 | 六片门禁语义未放宽 | **通过（核心）** | 作业结构级相等；判定脚本与 `pyproject.toml` 零改动；条件/依赖/超时逐字段打印 |
| D10 | 最终门禁新增结果核对是收紧还是放宽 | **通过（收紧）** | 逐模式期望只增不改；门禁 `needs` 多一个结果；实证三条日志 + 变异 M3 |
| D11 | 超范围两个测试文件是否只新增断言 | **通过** | `tests/contract`：期望表 +4 键、`needs` 精确集合 +1、env 精确字典 +1、新增 1 条结构测试与 2 条参数化测试（24 例）、2 处旧断言更新为新的精确字符串；`tests/unit`：`strategy` 字典新增 `exclude` 精确表达式、新增 `if` 精确断言。无删断言、无 `<=`/subset 化 |
| E12 | `tests/ci` 350→354 与「会不会静默变空」 | **通过** | 本地复算 baseline 350 / head 354；4 条新方法；M4（router 声称全部片都是 Web 能力道）2 条红，M5（分区器不再把任何用例判为 Web）`test_web_lanes_are_exactly...` 红，`assertTrue(web_lanes)` 挡空集 |
| E13 | 是否引入新的假绿（`docs` 误判可否整层跳过） | **通过（残留风险已说明）** | `docs` 判据只接根 README/AGENTS、`docs/**.md`、`.agents/skills/<skill>/{md,json,yaml,yml}`；任何 Web 源文件路径都落不到 `docs`（已知 Web 路径→`web`，其余→`full`）；未知路径一律 `full`，失败方向是「多跑」 |

## 代价复核（B4）

用 `gh run view <id> --json jobs` 取每个作业的 `startedAt`/`completedAt` 求和复算（不含未展开的 skipping 行；口径与作者一致，为作业时长之和）：

| 路由 | 运行 | 作业行 | 实际执行 | 分片 | 墙钟（首尾作业） | runner 分钟（我算） | 作者值 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `web` 前 | `43572881930`（PR #756 head `eaf172c0`） | 15 | 9 | 0 | 08:49:31→08:58:35 = 544 s | 876 s = **14.6** | 14.6 / 9:04 |
| `web` 后 | `43577755898` | 16 | 11 | 2 | 08:51:12→09:02:03 = 651 s | 1769 s = **29.5** | 29.5 / 10:51 |
| `full` | `43577692527` | 21 | 20 | 6 | 08:50:22→09:02:15 = 713 s | 4075 s = **67.9** | 67.9 / 11:53 |

墙钟以路由步开始到最终门禁结束计；作者表以 run 创建时刻或缺省口径计，二者相差数秒，不影响结论（`web` 增量 +107 s、+14.9 runner 分钟、+1 作业行）。三行全部复算一致，不是抽查一行的结果。

顺带核实了文档里一处容易被当成叙述的事实：**修复 PR #756 自己的 PR 运行（`43572881930`，head `eaf172c0`）确实没有集成分片证据**——`mode=web`、`Integration` 一行 skipping、最终门禁仍 success。这正是 #757 要修的洞。

## `exclude` 语义核实（A2）

- 官方文档（`jobs.<job_id>.strategy.matrix.exclude`）：**"An excluded configuration only has to be a partial match for it to be excluded."** 且 "All `include` combinations are processed after `exclude`."（<https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax>，2026-11-14 核验）
- 本处 `exclude` 行只写 `{"shard": N}`，而矩阵只有 `shard` 一个键，因此「部分匹配」与「全量匹配」两种理解给出同一结果——作者依赖的部分键语义在这里没有歧义风险。
- 类型：路由用 `json.dumps` 输出 JSON 数字（`{"shard": 0}`），矩阵值也是数字；实测匹配成功。
- 已知限制不适用于本写法：把整个 `matrix` 写成 `${{ fromJSON(...) }}` 时不能再并列 `exclude`（社区讨论 #25227 的报错即此类）；这里 `matrix` 是静态映射、只有 `exclude` 的值是表达式，实际运行接受。
- 空矩阵风险：`web` 至少保留 2 行；`full`/`docs`/`no-db` 均为 `[]`；若 `excluded_shards` 输出缺失，`fromJSON('')` 会让矩阵求值报错、作业红（fail-closed，不会静默变绿）。
- 生产实证：`web` 运行只有 `Integration 4`/`Integration 5` 两行（0–3 未出现）；`full` 运行六行；`docs` 运行一行未展开的 skipping。

## 守卫判据核实（A3）

`tool_requirements()` 的 `browser` 来自 `any(requires_browser(nodeid) for nodeid in selected)`，即**真实被选 nodeid 里是否真有 `*_browser.py` 模块或 `BROWSER_PROBE_MODULES`**，不是读一份「本片应该有消费者」的声明表。因此它无法被声明与实际不符骗过：

- 分类被改坏 → `web` 集合为空 → 分区退化为哈希 → 两片都选不到浏览器用例 → `browser=false` → 守卫 `exit 1`。
- 我用变异 M5 复现了这一层：`requirements --index 4 --count 6` 输出 `browser=false`，同时 `tests/ci` 的交叉核对测试红。

两点偏差（不阻塞）：

1. 注释与步骤名说「browser 或 Node-contract consumer」，代码只检查 `browser`（`node_contracts` 未参与判定）。方向是**多红不假绿**：如果将来所有 `*_browser.py` 消失、只剩 Node 契约消费者，`web` 会误红而不是误绿。
2. Node 契约一侧另有漂移钉：`tests/unit/test_integration_shards.py` 用 AST 从真实测试文件推导 `NODE_CONTRACT_TESTS` 并要求相等，且确认它们在真实收集集里存在。所以「契约用例被改名后悄悄不再被 web 覆盖」这条路被单测挡住。

守卫位置在 `Select external tools` 之后、Node/pnpm/Chromium 安装之前，属于快速失败，不会先烧 3 分钟构建再报错。

## 超范围测试改动逐条判定（D11）

### `tests/contract/test_ci_workflow.py`（+64/-2）

| 改动 | 判定 |
| --- | --- |
| `_EXPECTED` 四个模式各新增 `INTEGRATION_RESULT` | 收紧：门禁多一个必须匹配的维度 |
| `set(_GATE["needs"])` 期望集合新增 `integration_shards` | 收紧：精确集合相等，少一个或多一个都红 |
| 门禁 step `env` 期望字典新增 `INTEGRATION_RESULT` 映射 | 收紧：精确字典相等 |
| 新增 `test_a_web_route_runs_the_partitions_that_select_the_browser_consumers` | 新增 12 条断言：`if` 精确字符串、`shard` 列表仍是 `[0..5]`、`exclude` 精确表达式、`changes.outputs.excluded_shards` 接线、守卫 `if`/`env`/`run` 文本、守卫无 `continue-on-error`、守卫在 `integration_tools` 之后 |
| 新增 `test_final_gate_requires_the_route_own_integration_lanes`（20 例） | 收紧：`web`/`full` 必须 `success`，`docs`/`no-db` 必须 `skipped`，其余状态全红 |
| 新增 `test_final_gate_rejects_missing_integration_result`（4 例） | 收紧：缺该 env 时每个模式都红 |
| 两处旧断言由旧字符串改为新精确字符串 | 同步，不放松（旧值已不成立，若不改会红） |

### `tests/unit/test_integration_shards.py`（+11/-2）

| 改动 | 判定 |
| --- | --- |
| `strategy` 期望字典新增 `matrix.exclude` 精确表达式 | 收紧：此前不校验 `exclude`，现在形状偏离即红 |
| 新增 `shards["if"]` 精确字符串断言 | 收紧 |

两个文件都**没有**删除断言、没有把精确相等改成 subset/`in`、没有加 `skip`。载荷验证见 M1/M2/M3：其中 M1/M2 只有这两个文件（与 `tests/unit`）会红，`tests/ci` 全绿——说明 workflow 形状的钉子确实在这两层，而这两层在 `web` 路由下由 `python_checks`/`python_contracts` 执行，不会漏检。

## `tests/ci` 354 条与「静默变空」评估（E12）

- `python3 -m unittest discover -s tests/ci`：baseline（`origin/main`）**350 条 OK**，PR head **354 条 OK**，与作者自报一致。
- 新增 4 条是 `IntegrationLaneTests` 的四个方法：
  1. `test_web_lanes_are_exactly_the_partition_indices_that_select_web_consumers` —— 用**真实分区器**在合成集合上算出「哪些片选到 Web 消费者」，要求恰好等于 router 的 `web_lanes(6)`；并断言所有 Web 消费者都在这两片内被执行。
  2. `test_only_the_web_route_removes_lanes_and_it_keeps_its_own_web_lanes` —— `full`/`docs`/`no-db` 排除集为空；`web` 的排除集恰好是「总数 − web 能力道」；`web_lanes` 非空、排除数小于总数。
  3. `test_router_and_workflow_agree_on_the_partition_count` —— 工作流里 `--shard-count 6`、`--count 6`、`shard: [0..5]` 与 router 常量一致。
  4. `test_static_lanes_stay_inside_the_declared_partition_count` —— `web_lanes(3)=(2,)`、`web_lanes(6)=(4,5)`、`web_lanes(16)=(11..15)`。
- **「分区器没选中任何片，这 4 条会不会静默变空」→ 不会。** 变异 M5（把 `requires_browser` 恒 False 并把 `NODE_CONTRACT_TESTS` 清空，使分区退化为纯哈希）下第 1 条立刻红（`selected_web_lanes` 为空集 vs `{4,5}`）；第 2 条的 `assertTrue(web_lanes)` 另挡一层。变异 M4（把 `web_lanes` 改成「所有片都是 Web 能力道」）下 2 条红。
- 需要点明的一个覆盖边界：这 4 条**不检查** workflow 的 `if` 条件与 `exclude` 接线（M1/M2 下 `tests/ci` 仍 354 全绿）。这部分由 `tests/contract` 与 `tests/unit` 承担，而它们在 `web` 路由下同样执行（`python_checks`/`python_contracts`），所以整体没有缺口——但如果只跑 `tests/ci` 就以为门禁形状被覆盖，会误判。

## 是否引入新的假绿（E13）

- `docs` 路由下 `integration_shards` 仍 skipped，最终门禁期望 skipped，与改造前一致（run `43578978352` 实测全绿、分片 skipped）——不是新增的假绿，是既有语义。
- 能否把 `web` 改动误判成 `docs` 从而整层跳过？`classify` 的 `docs` 分支只接受根 `README.md`/`README.en.md`/`AGENTS.md`、`docs/**.md`、`.agents/skills/<skill>/` 下的 md/json/yaml/yml。任何 `web/` 源文件路径都进不了 `docs`：已知 Web 路径判 `web`，`web/` 下非 `src|public|index.html` 或非 Web 后缀的路径（如 `web/README.md`、`web/workbench/tests/x.ts`）一律 **`full`**。未知道路同样 `full`。失败方向是「多跑」而非「少跑」。
- 残留风险（非本改动引入，也不阻塞）：门禁校验的是「路由结论 ↔ 各 lane 结果」的一致性，不是路由结论本身是否正确。若 `classify` 本身被改错（改它必然走 `full` 之外的路径 → 需改 `scripts/`，而 `scripts/**` 变更判 `full`），错误路由会带着整层一起跳过而门禁仍绿。这是路由作为信任锚的普遍前提，`docs` 判据没有额外扩大这个面。

## 必须修清单

**阻塞项：0 条。** 没有发现会让 #745 那类缺陷再次以绿合并的路径，也没有发现门禁放宽。

建议修（不阻塞，可在本 PR 或后续单独改）：

1. **守卫文案与实现不一致**（`ci.yml` 守卫步骤名/注释 vs 判定）：注释写「selected no browser or Node-contract consumer」，实际只检查 `browser`，`node_contracts` 不参与。二选一——要么把 `node_contracts` 也纳入判定并重写注释，要么把注释收窄为「browser 消费者」。当前方向安全（fail-closed），但不一致会让下一位维护者误判守卫覆盖面。
2. **`ci-routing.py` 注释与事实不符**：`INTEGRATION_SHARD_COUNT`/`WEB_LANE_SHARE_DIVISOR` 上方的注释说「keeps the selection rule in one place instead of a second hard-coded copy」，实际它把分区器的 `max(1, count // 3)` 复制了一份；真正防漂移的是 `tests/ci/test_ci_routing.py` 用真实分区器做的交叉核对。建议注释改成「由 tests/ci 交叉核对分区器」，否则下一位读者会以为改分区器不必改这里。
3. **可选**：`web` 路由下 4/5 片的 `coverage-<shard>.data` 目前只作证据留存、无消费者判断。文档已写明，但建议在守卫步骤注释或分片名旁再点一句，避免后续误以为 web 也参与了覆盖率合并。

## 新绿

无。逐条评估后没有发现新增的「看起来绿但没测到」的路径：`web` 路由新增的两片是真实执行（制品 392 例、outcomes 完整、`browser=true` 驱动真实构建与 Chromium）；`docs`/`no-db` 的跳过是既有语义且门禁按路由核对；`full` 的六片与 `coordination` 未动。

## 只能依赖作者陈述、我无法独立验证的点

1. **反例 2（「把 `web` 判成 `full`」的对比运行）**：任务约束禁止新建 draft PR，所以我不能实跑一次「把 web 判成 full」的变异分支。我用同改动的最新 `full` 运行 `43578786173`（PR head `e4c05cab`，21 行、6 片、`coordination` success）与作者引用的 `43577692527`（67.9 runner 分钟，我已独立复算）作为等价证据，但「反例 2 这个特定实验本身」仍属作者叙述。
2. **#745 当时的 PR 运行确实全绿**：我核到 `main` 上 #745 的合并提交 `6e1ed2c5` 与修复 `b521c9c0`（PR #756），并用 PR #756 自己的运行 `43572881930`（`mode=web`、分片 skipping、门禁 success）证明了同类情形，但 #745 那次运行的具体记录未逐条回看。
3. **「作者只能手动 `workflow_dispatch` 才拿到证据」**：手动运行记录未逐一核对（`workflow_dispatch` 恒 `full` 可推知会跑六片，但我没有定位到那次具体 run）。
4. **#759–#763 各承载哪条反例的对应关系**来自 PR #758 评论；PR 状态、合并状态、分支删除与提交归属我已独立核实。

## 我独立复现的证据（命令与输出摘要）

### 1. 独立 worktree 与基线

```
$ git fetch --all --prune
$ git worktree add /private/tmp/sn767 -b review/767-web-routing origin/main
HEAD is now at fe165d8a docs: 记录渲染拒绝词表与错误归因的非作者审查 (#750) (#764)
$ cd /private/tmp/sn767 && git diff origin/main...HEAD --stat
(empty)
```

### 2. 真实分区器复算（A1）

```sh
cd /private/tmp/sn767-pr   # detached e4c05cab 快照，uv sync --locked 后
uv run python /tmp/recompute_757b.py
```

输出见「本卡核心问题一 · 证据 2」。脚本逻辑：`collect_integration_nodeids()` 用与执行相同的收集（`pytest -m integration tests/integration --collect-only`），再对 `index in range(6)` 调 `partition(nodeids, index, 6)`。

### 3. 两条真实运行的分片制品比对（A1）

```sh
gh run download 43577755898 -n integration-shard-4 -D /tmp/art-web/4    # web 路由
gh run download 43577692527 -n integration-shard-4 -D /tmp/art-full/4   # full 路由
# 对 shard 5 同样下载
python3 -c '...'   # 比较 selected / collection_digest / outcomes
```

输出见「证据 1」。

### 4. 路由脚本实跑（含 `excluded_shards`）

在临时 worktree 里把 PR head 的 `ci-routing.py` 覆盖到真实历史 diff 上运行：

```
# 纯 Web diff（eaf172c0 vs cfeee5f8，两个文件都在 web/workbench/src）
{"mode": "web", "reason": "Web sources changed; retain Python consumer/unit checks, the complete Web lane and the Web-capable integration partitions."}
mode=web
excluded_shards=[{"shard": 0}, {"shard": 1}, {"shard": 2}, {"shard": 3}]

# docs diff（fe165d8a vs 26fd8b39，单文件 docs/reviews/*.md）
{"mode": "docs", "reason": "Only prose or skill documents/metadata changed; heavy checks are out of scope."}
mode=docs
excluded_shards=[]
```

### 5. 生产运行核实（C6/C7/B5）

```
43577811710  success   head 1a579b0b  mode=web   Integration 一行 skipping  门禁 success      # 反例 1
43577800599  failure   head 3520ef47  mode=web   Integration 4 failure / 5 success  门禁 failure + integration=failure   # 反例 3
43578978352  success   head aa9e63b8  Integration skipping  docs_format success 门禁 success   # docs 路由
43577755898  success   head f39c9150  mode=web   Integration 4/5 各 392 例  门禁 success
43577692527  success   head db14fa86  mode=full  六片全成功  coordination success
43572881930  success   head eaf172c0  mode=web   Integration skipping（PR #756 head）
```

反例 3 的失败正文（`gh run view 43577800599 --log-failed`）：

```
E   locator.fill: Error: strict mode violation: getByRole('region', { name: '有声书与导出工作台' }).getByLabel('新单元文本') resolved to 2 elements:
E     1) <textarea required="" maxlength="200000" aria-describedby="create-unit-text-bounds"></textarea> aka getByRole('textbox', { name: '新单元文本' })
E     2) <p class="books-status" aria-label="新单元文本 字数" ...> aka getByLabel('新单元文本 字数')
...
tests/integration/test_workbench_real_runtime_chapter_create_browser.py:81: AssertionError
at .../scripts/dev-env/workbench-chapter-create-probe.mjs:94:37
```

反例 3 的分片步骤序列同时说明守卫通过后才真正执行：

```
9  Require the Web lanes to select actual browser consumers   success
10 Set up Node.js ... success
13 Install locked Web dependencies for browser consumers     success
14 Build actual Web bundles for browser consumers            success
15 Install Chromium for browser consumers                    success
17 Run a complete integration partition ...                  failure
```

`full` 运行里同一步骤是 `skipped`（守卫只在 `web` 生效，`full` 行为未变）：

```
9  Require the Web lanes to select actual browser consumers  skipped
```

### 6. 临时 PR 状态（C8）

```
759  CLOSED  ci/757-web-route-green     mergedAt=null  mergeCommit=null
760  CLOSED  ci/757-web-route-aria      mergedAt=null  mergeCommit=null
761  CLOSED  ci/757-web-route-aria-era  mergedAt=null  mergeCommit=null
762  CLOSED  ci/757-web-route-prefix    mergedAt=null  mergeCommit=null
763  CLOSED  ci/757-docs-route-check    mergedAt=null  mergeCommit=null
远端分支：仅 refs/heads/ci/757-web-integration（PR #758 head）与无关的 docs/760-matrix-refresh
四个 head 提交（f39c9150 / 3520ef47 / 1a579b0b / aa9e63b8）均不是 origin/main 祖先
```

### 7. 静态检查（PR head 快照）

```
$ uv run ruff format --check .
1849 files already formatted
$ uv run ruff check .
All checks passed!
$ uv run mypy
Success: no issues found in 1452 source files
$ uv run lint-imports
Contracts: 9 kept, 0 broken.
```

（`mypy` 按 CI 的原命令运行，无 `--platform` 覆盖参数；`pyproject.toml` 的 `[tool.mypy]` 未设 `platform`。）

### 8. `gh pr checks 758`（2026-11-14 17:3x 核验）

21 项全部 `pass`，其中 `Integration 0`–`Integration 5` 六片都实际执行（本 PR 自身走 `full`：路由日志 `{"mode": "full", ...}`），`Gate: six-shard integration evidence and merged coverage` pass，`Gate: required checks for this change` pass，`Docs: markdown code block formatting` skipping。

### 9. 变异清单（全部在 `/private/tmp/sn767-pr` 临时快照，已 `git status` 确认还原干净，未 push）

| 变异 | 目的 | 结果 |
| --- | --- | --- |
| M1 `if` 改回 `mode=='full'` | 验证反例 1 的机制被测试钉住 | contract 2 红；tests/ci 全绿 |
| M2 删 `matrix.exclude` | 验证矩阵接线被钉住 | contract 1 + unit 4 红；tests/ci 全绿 |
| M3 门禁 `web` 改回 `integration=skipped` | 验证收紧状态被钉住 | contract 8 红 |
| M4 `web_lanes` 返回全部片 | 验证「片数恰好」被钉住 | tests/ci IntegrationLaneTests 2 红 |
| M5 分区器不再分类任何 Web 用例 | 验证守卫/交叉核对不会静默变空 | `requirements` 输出 `browser=false`；tests/ci 1 红 |

## 时间线（过程记录）

- 17:23 建独立 worktree `/private/tmp/sn767`（基于 `origin/main`=`fe165d8a`），确认 `git diff origin/main...HEAD --stat` 为空；建 detached 快照 `/private/tmp/sn767-pr`=`e4c05cab`；建 `/private/tmp/sn757-route`=`eaf172c0`、`/private/tmp/sn-routedocs`=`fe165d8a` 供路由实跑
- 17:25 读 PR diff（7 文件 +299/−18）、`ci.yml` 全文、`ci-routing.py` 全文、`integration-shards.py` 全文
- 17:26 `tests/ci`：baseline 350 OK / head 354 OK；`uv sync --locked` 于快照 worktree
- 17:28 `gh` 核实六条运行存在与结论；`#759–#763` 状态与远端分支
- 17:29 真实分区器复算（2754 收集 / 50 Web 消费者 / 4-5 片各 392）
- 17:30 下载并比对 `web` 与 `full` 运行的分片制品（selected / digest / outcomes 全相同）
- 17:31 路由脚本实跑（web → 排除 0–3；docs → `[]`）；YAML 结构级对比 `coordination` 相等
- 17:32 变异 M1/M2/M3/M4/M5，逐条确认红；还原快照
- 17:33 `ruff format --check`、`ruff check`、`mypy`、`lint-imports`、`gh pr checks 758`
- 17:35 反例 3 失败正文、门禁 `integration=failure` 行、守卫步骤序列取证
- 17:4x 写本报告，`ruff format`（pinned 0.17.2）后提交

## 附：本次审查未做的事

- 未修改任何工作流、路由脚本或测试（除临时快照内的变异，已还原）
- 未 push 变异，未新建 draft PR
- 未合并、未 push `main`
- 未对 `web` 路由的 `+14.9 runner 分钟` 成本提出反对：作者已量化，且相对「判成 `full`」的 +53 runner 分钟仍显著更省
