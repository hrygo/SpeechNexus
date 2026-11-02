# Agent 运行事故与历史排障

从[军团工作流](../paperclip.md)迁出的历史记录，原文保留当时的失败、命令与后续证伪；其中“当前”“当日”及运行限制均指原记录时点。仅在相关故障排查时读取，不作为每次派工的必读材料。

当前预检、等待、任务续行和隔离恢复以[交付 SOP](../agent-delivery-sop.md)及实际平台控制为准。下文直接修改平台数据库、清空会话、更换权限模式或新建卡的历史处置不是当前授权；必须先核实时版本、操作归属、旧进程停止与未决结果，并由获准操作者经支持入口处理。原失败和修正不改写为当前成功。

以下规则来自实际执行事故，不是设计推演。每条都附当次可见后果，便于判断是否仍适用。

### 派工前先确认管道能跑

派真任务前先用一张一分钟的探针卡确认该岗能启动。2026-10-27 当天，某岗的 AI 账号在 16:02 被平台判定不可用（`ConfigurationIncompleteFailure`），随后两张真任务卡直接变成 `blocked`，其中一张在 20 分钟后才被发现完全没有产出。

### 单岗并发 1，建卡前先查 `in_progress`

同一岗位的第二张卡不会并发执行，只会排队，但界面仍显示 `in_progress`。当天因此出现"第二张卡看起来在跑、实际在等"的空转。建卡前查目标岗的在办卡数量，忙就等，不要靠卡片状态判断。

### 不要在已完成卡上补评论

给已完成任务补评论会唤醒该岗重新执行，产生一次无意义运行。验收结论写在卡片状态变更时一并提交，或写进 issue / 文档，不要事后追加。

### 时限按套件实际规模设，不按本机经验设

同一套 `tests/unit/gateway` 在本机数分钟跑完，在 Linux aarch64 执行机上 7 分钟仍未结束。跨主机任务卡应给出"单文件 + 明确分钟数"，或先 `--collect-only` 看规模，不要用本机速度估。

### 上游模型改名会被误判成鉴权失败

本地推理代理把上游已下架的模型名继续映射时，上游返回 `410 Gone`，平台侧把它归因为凭据失效，于是整个 AI 账号被标记为需要重新授权，全队停摆。代理侧 health 检查必须跑一次真实推理，模型别名保持稳定，只改到上游真实模型名的映射。

### 跳过不是通过

集成测试的 `skip` 不计入证据；`fake` 只证明覆盖范围；作者自测不得当作独立验收。独立验收要求不同主机、不同数据库大版本或不同取码路径，同一环境重跑不构成独立证据。

### 唤醒看起来"只有新建卡有效"，真因通常是队列里的一条僵尸唤醒

2026-10-27 的记录：改卡片状态、改指派人、事后补评论都不触发派工，而新建一张卡立刻进入 `in_progress`。当时据此写下的规则是"唤醒只有一个可靠方式：新建卡"。

**这条规则在 2026-10-28 被证伪**：同一环境下把 `blocked` 卡改回 `todo` 后岗立即执行了。真正的阻塞不是"改状态无效"，而是队列里有一条**永远投递不出去的唤醒**占着该岗的唤醒位。

例：一条 `agent_wakeup_requests` 记录指向某张已 `cancelled` 卡上的评论，状态长期停在 `deferred_issue_execution`，自愈循环每 30 秒尝试一次并失败：

```
WARN failed to deliver saved legacy comment after recovery stopped {"queueId":"<wake-id>"}
```

当次一天内累积了 1224 条以该 `queueId` 命名的 `queued-comment-delivery:<wake-id>` 子记录。

**处置**：找到 `id` 等于日志中 `queueId` 的那条唤醒（**不是**它派生的子记录），改为终态：

```bash
psql "$PAPERCLIP_DSN" -c "UPDATE agent_wakeup_requests
  SET status='skipped', reason='issue_cancelled_superseded', updated_at=now()
  WHERE id='<queueId>'"
```

改完约 90 秒内不再产生新的 `queued-comment-delivery` 记录，被占住的岗随即恢复。**只删子记录无效**，父记录会继续派生。

因此排查顺序是：先查 `agent_wakeup_requests` 里该岗是否有非终态唤醒，再判断"平台唤不醒"；直接新建卡只是绕过，僵尸唤醒仍在。

### `paused` 是可改状态字段，不是不可变约束

当日五个岗长期停摆，被当成外部阻塞上报了两轮。实际上平台只是 `agents.status='paused'` 与 `lifecycle_state='paused'`，改回 `idle` / `ready` 即可恢复。遇到归类为"阻塞"的状态，先问一句"这个字段我能不能改"，再决定是否升级为需要他人决策的事项。

### `opencodePermissionMode=ask` 的岗无法无人值守运行

某个岗的适配器是 `opencode` + 模型 `paperclip/omlx-*` + `opencodePermissionMode: ask`，启动即失败，平台报 `original assignee is not invokable`。可用组合是 `codex` 适配器 + `codexPermissionMode: never`，与正常工作的岗对齐。改配置后仍不接单，因为唤醒只在建卡时发生（见上一条）。

### `maxTurnsPerRun` 决定单卡上限，大卡必然耗尽

同一岗的轮次预算是适配器配置项。当天一个岗在 60 轮上限下把预算全部用于探索，worktree 里零提交零改动，只在错误里留下 `error_max_turns`。跨多个模块的卡要么提高轮次上限，要么拆小；两者都不做就会安静地烧完。

### 检查点必须写"到点必须报"，不能写"卡住就报"

"卡住就报"是无效条款：卡住的岗不会认为自己卡住。同一批卡里，写了"第 30 分钟在卡里报一次进度"的岗准时报了进展与"没有卡住"；只写"卡住就报"的岗安静烧完 60 轮、零产出，四十分钟后才发现。

### 执行机只读检出时，按补丁交付并由集成方落地

有执行机的检出是只读的（`.git/index` 不可写、无 `gh`），永远开不了 PR。可行的交付方式是补丁 + 清单，由具备推送权限的一方落地：核对补丁 SHA-256 与执行机自报值一致、`git apply --check` 通过、逐文件与清单吻合、跑门禁与格式检查，再提交并说明哪些是集成方改的。当日据此落地的两个 PR 均一次全绿。

集成时**必须自己跑一遍格式检查**：执行机交付的 6 个新文件全部不合规，原样提交会直接挂在格式门禁上。这是集成方的责任，不是执行机的疏忽。

### 新增表或路由必须同步四类横切断言

以下断言在任何新增之后都会红，且都不在新增者自然会跑的测试里：路径全集（`tests/contract/test_asset_http_contract.py`、`test_operational_contract.py`）、数据库表全集（`tests/integration/test_coordination_foundation.py`）、迁移 head 字面量（现集中到 `tests/support/migrations.py` 的 `HEAD`）、授权列清单。当日一个新表就让六分片里的集成分片全红，而作者只跑了本卡测试文件。

派工时直接在卡面列出这四处，或要求跑一次相关集成分片。

### 两个并行 PR 会撞在同一个装配点

路由装配文件（`app.py`）是每个新切片的必经之处。当日两个 PR 各自基于旧主干，合并第一个后第二个立刻冲突。冲突通常两边都是新增，正确解法是**两边都保留**（两个 router 并存），而不是取一边；取一边不会立刻红，但会静默丢掉一个切片。合并前先看它与最近合并的 PR 是否触碰同一文件。

### 会话损坏时换新会话，不要修补

某岗的 native 会话在一次成功运行后损坏，报 `HarnessReconciliationError: persisted active turn ... is missing from thread/read`，此后新运行被拒（`New work must wait while a native run recovers its unfinished accounting`），卡片被反复置为 `blocked`，而该岗显示 `idle`。

处理顺序：清理卡住的 finalization 状态、清空该岗的历史 session id 强制新会话、**新建一张卡**（不是改旧卡状态）。修旧会话没有成功过。

### 单次运行有硬时限，且是**按岗可配**的

执行适配器配置里有 `timeoutSec`（`adapter_config.timeoutSec`，缺省不限）。某岗的实际值是 3600，一张"三处修复 + 全量测试"的卡跑到整数一小时被砍，报 `Timed out after 3600s`，**worktree 里 77 KB 改动一次都没提交**。

判断口径：**卡的规模要按"含环境搭建的整段耗时"估**，不是按"改几行代码"。新依赖、新 venv、模型下载都算在内。两种处理都可接受——把上限调大，或把卡拆小；什么都不做就会周期性丢掉一小时。

### 运行被砍后，先抢救工作区，再决定要不要重派

运行结束时工作区**不会被删除**，未提交的改动还在原处：

```bash
# 执行机上：导出已跟踪改动 + 未跟踪文件
cd <run_workspace>
git diff > /tmp/<run>.tracked.patch
tar cf - $(git status --porcelain | awk '/^\?\?/ && !/\.paperclip-runtime/ {print $2}') \
  | base64 -w0 > /tmp/<run>.untracked.b64
```

拉回本地后在**干净 worktree**（基于当前主干）上复原：

```bash
git worktree add /tmp/salvage -b <branch> origin/main
git apply --3way /tmp/<run>.tracked.patch
tar xf <decoded>.tar -C /tmp/salvage
```

然后**必须自己跑测试**：当日抢救回来的 ASR 实现有 9 个测试红，根因是运行层填 `"auto"`、域校验只认 BCP-47 的哨兵不一致——一次默认请求 100% 503。抢救不等于可用，抢救只等于"不必重做一遍"。

顺序上：**先抢救再决定**。直接重派会得到一个"已经做过一遍"的活，且新运行拿不到旧工作区的分支。

### CI 全绿不等于部署可用：按默认装配实测每个入口

当日 12 个 PR 全部 14/14 绿之后，用生产入口的默认装配（`create_app(settings=...)`，不注入任何服务）实测：

```
POST /v1/audio/speech         -> 503 backend_unavailable
POST /v1/audio/transcriptions -> 503 backend_unavailable
```

原因是这两个服务**只接受注入**，而 `create_app` 没有对应的 runtime 装配模块（其它服务如 `job_runtime` / `artifact_runtime` 都有）。同一轮还查出：`POST /v1/nexus/jobs` 只写 `speech_jobs` 一族，**不写任何派发意图**，网关也不读该表，`advance()` 在生产代码里零调用点——队列有生产者、没有消费者。

判据很简单，每次切片合并后做两件事：

1. **装配实测**：用默认 settings 建 app，逐个打新入口，记录状态码；503 要能解释成"确实缺配置"而不是"没人接线"。
2. **成对搜索**：新写一张表/一个队列，同时搜"谁读它"和"谁写它"。只有写没有读，就是没有消费者。

分层交付的固有盲区是：**每层测试只验证自己那层**，两层之间那段线没人写的时候，没有任何测试会红。

### SQL 字符串里的转义会变成真实字节

迁移 DDL 里写 `storage_version_id !~ '[\x00-\x1f\x7f]'`，Python 先把 `\x00` 变成真正的 NUL 字节；PostgreSQL 的 C 字符串在 NUL 处截断，报 `unterminated quoted string at or near "'["`，六个集成分片全在 setup 阶段失败。

改用 POSIX 字符类 `'[[:cntrl:]]'`（等价覆盖 0x00–0x1F 与 0x7F）。**单元测试发现不了这类问题**——正确性取决于"字符串里的字节是什么"，只有真实数据库解析才暴露。新增迁移一律在真实 PG 上跑一次 `alembic upgrade head` 再交。

### 并行分支的 diff 里会"删除"别人刚合并的东西

两个 agent 改同一片区域、先后合并时，晚提交的那个分支基线早于对方合并。rebase 到最新 `main` 后常常**没有文本冲突**，但 `git diff` 会把"我的分支里本来就没有那段新内容"呈现成**删除**。

实例：分支 base 是 `057fbfd`，`main` 已到 `1bcbf12`（新增了 ASR 结果字段与它的测试）。直接合并该分支会删掉 `test_vllm_asr.py` −96 行、`test_file_asr_result.py` −164 行——两个文件的改动都不属于这个分支的工作范围。

合并前必须做两件事：

```bash
git fetch origin main
git rebase origin/main
git diff --name-status origin/main HEAD | grep '^D'   # 每一行都要能解释
```

`^D` 里出现**本任务没打算删的文件**时，不要合，先问是谁的。rebase 成功后还要重跑受影响层的测试——冲突解决得干净不代表语义还在。

判据：**"能快进/无冲突"不等于"没有删除"。** 只有逐条看过删除项，才知道别人的成果还在不在。
