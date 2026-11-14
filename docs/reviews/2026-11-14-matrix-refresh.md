# 交付矩阵刷新的非作者审查（PR #726）

> 过程记录文档：保留时间线、独立复现的反例与命令输出。结论面向合并决定，不修改产品代码与 `docs/plans/` 任何文件。
> 审查对象：`docs/726-matrix-refresh`，head `aa04d5a1f7cd6be096a1e5fcde1c4532a036002d`（下称 head），基线 `origin/main` = `cef394336929e4f520d11ebe962034ffc7dd02e2`（下称 main）。
> 审查分支 `review/727-matrix` 基于 `origin/main` 拉出，非基于被审 head；`git diff origin/main --stat` 只含本报告文件。

## 结论

**有条件通过**：可合并，但需先修一条文档一致性缺陷——F-ASSET 的 `gap` 仍把「`audio` 部署关停漏关检查器监督器（#599）」当作开放债务，而同一行的实现正文已写明关停顺序正确、`#599` 又渲染为 `closed / completed`。这正是本 PR 自称修掉的那一类「注记与正文自相矛盾」，且落在本 PR **确实改过**的那一行里（F-ASSET 的 evidence 块被重写），属范围内漏修，纯文本一行即可闭合。

除此之外本卡的实质结论**全部成立**，且我把作者的自述逐条落到可执行判据上复核：

- 它声称的三处「矩阵与真实代码不符」手工更正，**逐条为真**，含最容易含糊的那条——`#406` 的关闭依据是代码交付，**没有**被偷换成验收。
- 9 条证据链接确实全部改指现存文件；我自扫 189 条 blob 证据 URL + 5 个 issue + 16 个 PR 引用，**dead = 0**。
- 快照增量为 `+696/+702/+703/+704/+705/+723` 与 `#406 open→closed`，**没有夹带**任何其它变动；我又把全部 78 条与 GitHub 实时值逐字段对照（`state`/`state_reason`/`title`/`labels`/`parent_issue_url`），**0 处不一致**。
- 越界改动：**无**，只有 `docs/plans/` 三个文件；`delivery-issues.json` 的人工事实字段不存在被脚本覆盖的可能（脚本根本不写该文件），我用反例分别证明了两侧边界。
- CI `delivery_tracking` gate 本地逐字节复现通过；`check-doc-refs` 的「基线即红」归因成立（main 与 head 的问题集**完全相同**）。
- `ruff format --check .`、`ruff check .` 均绿。

## 审查对象与方法

- 独立 worktree：`git fetch && git worktree add /private/tmp/sn727 -b review/727-matrix origin/main`（`HEAD` = `cef39433`），被审内容一律用 `git show aa04d5a1:<path>` 或另开的 detached 快照 worktree `/private/tmp/sn727-pr`（`git worktree add --detach /private/tmp/sn727-pr aa04d5a1`）读取，**不在审查分支上检出被审改动**。
- 事实核验以 **main 的源码/契约/Compose** 为准，历史归属以 `git log`/`git show <sha>` 为准，GitHub 实时值以 `gh` 为准（读取时刻 2026-11-14T01:5xZ / 本地 09:5x–10:0x）。
- 反例只在临时副本（`/private/tmp/sn727-pr`）内做，做完 `git checkout --` 还原，**未 push**。
- 本 PR 只改文档（`git diff --name-status main head` 仅 3 个 `docs/plans/` 文件），因此不跑产品测试；需要实跑的是文档工具链与矩阵 gate。

范围与文件清单：

```text
$ git diff --stat cef39433 aa04d5a1
 docs/plans/delivery-issues.json        | 60 ++++++++++++++++++-------------
 docs/plans/delivery-tracking.md        | 27 ++++++++------
 docs/plans/github-issues-snapshot.json | 66 ++++++++++++++++++++++++++++++++--
 3 files changed, 114 insertions(+), 39 deletions(-)

$ git diff --name-status cef39433 aa04d5a1
M	docs/plans/delivery-issues.json
M	docs/plans/delivery-tracking.md
M	docs/plans/github-issues-snapshot.json
```

## 七条逐条判定

### 1. 事实更正的逐条核实 —— **成立**

见下一节《它声称的事实更正是否逐条成立》的完整命令与输出。四条全部为真。

### 2. 越界改动与「脚本是否覆盖人工事实」 —— **无越界；边界成立**

- 改动面：只有 `docs/plans/` 三个文件（上列 `--name-status`），无产品代码、无脚本、无 CI 配置。
- `delivery-issues.json` 的字段级 diff 显示：`schema_version`/`repository`/`program`/`facts_as_of`/`epics` **全部未变**；65 个需求的 `issues` 关联数组**一个都没动**（6 个新 open issue 因此正确地落到「未登记的 open issue」，而不是被顺手登记）；变化的键只有 `evidence`（10 个需求）、`verification`（F-TTS）、`gap`/`implementation`/`integration`（O-MEDIA）——都是人工事实字段。
- 关键结构事实：`scripts/dev-env/refresh-implementation-tracker.py` **从不写 `delivery-issues.json`**。它的两处写入是 `write_snapshot()`（写 `--refresh-snapshot` 指定的快照）与 `args.output.write_text()`（写 `delivery-tracking.md`）。因此「脚本覆盖人工事实字段」在本架构下不可能发生，人工字段的校验**只能靠人读**——这一点用第 4 条的反例证明。

### 3. 快照是否可信 —— **可信，增量与真实变化精确相等**

snapshot 逐字段 delta（base → head）：

```text
base fetched_at: 2026-11-13T02:16:29Z
pr   fetched_at: 2026-11-14T09:43:25+08:00
base count: 72  pr count: 78
ADDED  : [696, 702, 703, 704, 705, 723]
REMOVED: []
--- field-level changes for common issues ---
406 {"state_reason": [null, "completed"], "state": ["open", "closed"]}
```

与作者声称的「新增 `#696/#702/#703/#704/#705/#723`、`#406` 状态变化」**完全一致，零夹带**。独立交叉验证（快照 vs GitHub 实时）：

```text
real state/title mismatches: 0
live open: 20 snapshot open: 20
open missing from snapshot: []
snapshot open but live closed: []
label/title/state issues: 0 | checked: 78
parent_issue_url mismatches: 0
```

6 个新 issue 的创建时间均晚于 base 快照时刻，证明是**真实新增**而非 base 漏收：

```text
696 created=2026-11-13T20:09:22Z  state=OPEN
702 created=2026-11-13T20:50:19Z  state=OPEN
703 created=2026-11-13T20:50:21Z  state=OPEN
704 created=2026-11-13T21:00:22Z  state=OPEN
705 created=2026-11-13T21:00:23Z  state=OPEN
723 created=2026-11-14T01:21:29Z  state=OPEN
--- base snapshot fetched_at: 2026-11-13T02:16:29Z ---
```

`fetched_at` 由 `2026-11-13T02:16:29Z` 变为 `2026-11-14T09:43:25+08:00` 的**格式漂移**不是手改：脚本 `current_time()` 正是 `datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")`。两版都能被 `parse_time()` 接受，且 gate 用同一个字面量回填，不影响字节复现。

### 4. 反例（我自己造） —— **两侧边界均如作者所述**

见《我独立复现的反例》。

### 5. CI gate 本地复现 —— **通过，逐字节一致**

见《我独立复现的反例》中的 gate 段。

### 6. `check-doc-refs` 归因 —— **成立：基线即红，本 PR 未新增**

该脚本**不在 CI 中**（`docs/validation/ci-validation.md` 明确写「当前 CI 工作流未调用该脚本，它不属于必需检查、作业门禁或路由范围」），所以「基线即红」不是发布门禁问题，只是本地报告工具红。作者把它归为「越界未修」是正确的。

### 7. F-ASSET 行号漂移故意不改 —— **取舍可接受，但建议在本 PR 一并修或落一张后续卡**

作者自述属实，我逐条复算：

| 正文引用 | head 实际位置 | 该行当前内容 | 写入时是否正确 |
| --- | --- | --- | --- |
| `asset_runtime.py:194` | `def build_asset_inspector` 在 **240** | `wall_seconds=wall_seconds,` | 是（`8c023545` 时 194 正是 `def build_asset_inspector`） |
| `audio_inspection_http.py:105` | `class HttpAudioAssetInspector` 在 **118** | `value[key] = item` | 是（`8c023545` 时 105 正是 `class HttpAudioAssetInspector`） |

即两处都是**写入时正确、之后文件增长导致漂移**，不是编造。同一行里的第三条行号引用 `asset_runtime.py#L115` 我特意复核，它是**钉在 `6544a934` 的永久链接**，该 commit 第 115 行正是 `def build_asset_inspector`，**没有问题**——作者没有把这一条混进「漂移」清单，是准确的。

判定：可接受，但**不建议就这样留着**。理由是这类漂移对工具是隐形的：`check-doc-refs.py` 只查「文件是否存在 / 行号是否越界 / 裸名是否歧义」，240 行的文件里写 194 是合法行号，永远报 OK；因此它不会被任何门禁捡起来，只会等下一个人再花一遍时间核对。作者以「避免噪声」为由不改，但这个理由在本 PR 里偏弱——F-ASSET 这一行的 evidence 块本来就被本 PR 重写了，多改两处行号不构成额外噪声面。**建议**：要么顺手改成 `:240` / `:118`，要么开一张后续卡把「矩阵内联 `路径:行号` 引用定期对表」固化下来（后者更有长期价值）。

## 它声称的事实更正是否逐条成立

### 更正 1：O-MEDIA「存在 Linux 音频检查进程」→ 独立 `asset-inspector` 部署单元 —— **成立**

文件在 main 与 head 都不存在：

```text
$ git cat-file -e origin/main:backend/src/speechnexus_backend/speech/infrastructure/audio_inspection_process.py
fatal: path '...audio_inspection_process.py' does not exist in 'origin/main'
ABSENT (exit 128)
$ git cat-file -e aa04d5a1:backend/.../audio_inspection_process.py
fatal: path '...' does not exist in 'aa04d5a1'
ABSENT
```

`8c023545` 确实是删除它的那个提交，且「5 个单测」的数字精确：

```text
$ git log -1 --format='%H%n%ad%n%s' 8c023545
8c0235455557252ade79684a74b348f06191ecf6
Thu Nov 12 17:07:57 2026 +0800
chore: 删除已无引用的 bwrap 进程检查器路径 (#589) (#614)

$ git show --stat --format='' 8c023545
 backend/src/speechnexus_backend/settings.py        |   9 +-
 .../infrastructure/_audio_inspection_launcher.py   | 231 --------
 .../speech/infrastructure/audio_asset_inspector.py | 154 -----
 .../infrastructure/audio_inspection_process.py     | 556 ------------------
 tests/unit/backend/test_audio_asset_inspector.py           | 210 -------
 tests/unit/backend/test_audio_inspection_http.py           | 107 ++++
 tests/unit/backend/test_audio_inspection_parent_death.py   | 167 ------
 tests/unit/backend/test_audio_inspection_policy.py         |  66 ---
 tests/unit/backend/test_audio_inspection_process.py        | 622 ---------------------
 tests/unit/media/test_inspection_process.py                | 115 ----
 19 files changed, 164 insertions(+), 2164 deletions(-)
```

删除的单测恰为 5 个（`test_audio_asset_inspector.py`、`test_audio_inspection_policy.py`、`test_audio_inspection_process.py`、`test_audio_inspection_parent_death.py`、`tests/unit/media/test_inspection_process.py`）。

替代实现所指的三个文件全部存在：

```text
EXISTS  asset-inspector/src/speechnexus_asset_inspector/service.py
EXISTS  asset-inspector/Dockerfile
EXISTS  contracts/internal/asset-inspection.openapi.yaml
EXISTS  deploy/compose.yaml
EXISTS  tests/unit/backend/test_asset_inspector_wiring.py
EXISTS  tests/ci/test_asset_inspector_image_ci.py
EXISTS  backend/src/speechnexus_backend/speech/infrastructure/audio_inspection_http.py
EXISTS  tests/unit/backend/test_audio_inspection_http.py
```

`HttpAudioAssetInspector` 在组合根被装配的说法成立（`asset_runtime.py:21/147/190`）。退役键「显式设置即启动失败并点名替代键」成立：

```text
backend/src/speechnexus_backend/asset_runtime.py:42:    ("audio_inspection_runtime_root", "SPEECHNEXUS_AUDIO_INSPECTION_RUNTIME_ROOT"),
backend/src/speechnexus_backend/asset_runtime.py:43:    ("audio_inspection_command", "SPEECHNEXUS_AUDIO_INSPECTION_COMMAND"),
backend/src/speechnexus_backend/asset_runtime.py:44:    ("audio_inspection_launcher_python", "SPEECHNEXUS_AUDIO_INSPECTION_LAUNCHER_PYTHON"),
backend/src/speechnexus_backend/asset_runtime.py:45:    ("audio_inspection_bubblewrap", "SPEECHNEXUS_AUDIO_INSPECTION_BUBBLEWRAP"),
backend/src/speechnexus_backend/asset_runtime.py:46:    ("audio_inspection_prlimit", "SPEECHNEXUS_AUDIO_INSPECTION_PRLIMIT"),
```

替代键消息：`"audio inspection runs in the asset-inspector deployment unit; configure SPEECHNEXUS_ASSET_INSPECTION_URL and SPEECHNEXUS_ASSET_INSPECTION_TOKEN"`，并有 `tests/unit/backend/test_asset_inspector_wiring.py` 断言 `pytest.raises(ValueError, match="SPEECHNEXUS_AUDIO_INSPECTION_RUNTIME_ROOT")`。

Compose 侧的四条隔离声明我逐条对照 `deploy/compose.yaml`，**全部为真**：

- `profiles: ["asset-inspection"]`（opt-in，默认 `up` 不启）；
- `image` 由 `asset-inspector/Dockerfile` 构建（`build.dockerfile: asset-inspector/Dockerfile`）；
- 容器项：`read_only: true`、`cap_drop: [ALL]`、`security_opt: [no-new-privileges:true]`、**无 `volumes`**、**无 Docker socket**、无 `ports`；
- 网络：`asset-inspection: internal: true`（`internal: true` 即无默认网关、无出站），且实测**只有 `api` 与 `asset-inspector` 接入**：

```text
$ python3 -c "...遍历 compose services 的 networks..."
api {'default': None, 'asset-inspection': None}
asset-inspector ['asset-inspection']
--- all services: 12
```

因此「解码进程触达不到 PostgreSQL、对象存储与网关」在 Compose 层面成立（三者都在 `default`）。作者同时保留「真实部署的跨平台解码与端到端联调未实测」「镜像构建与接线测试不证明真实容器运行资格」——这是诚实的负面声明，与我在仓库内**找不到**真实部署证据一致。

### 更正 2：F-TTS「#406 保持开放」→ `closed/completed`，且不得把「关闭」偷换成「验收」 —— **成立，区分被保住**

GitHub 实时值：

```text
{"closedAt":"2026-11-13T14:30:27Z","number":406,"state":"CLOSED","stateReason":"COMPLETED",...}
$ gh api repos/hrygo/SpeechNexus/issues/406/timeline ...
{"actor":"hrygo","commit_id":null,"created_at":"2026-11-13T14:30:28Z","event":"closed"}
```

关闭时刻的评论（`2026-11-13T14:30:26Z`）原文：

> 已交付：PR #413 已合并（主干 `7182911f`），并已由后续切片持续覆盖。核对当前 main：`settings.speech_max_output_audio_ms`、`SpeechSynthesis.synthesize(max_output_audio_ms=...)` 与 `_limits.max_output_audio_ms` 的收紧链路均在主干；契约字段与生成消费者同源。关闭本卡。

**关键点：这个关闭依据确实是「代码交付」，不是「验收通过」。** 而 issue 在 2026-11-11 的核对评论明确写着「代码已实现并合入 `origin/main`；真实引擎/资格证据仍未取得，故完整 AC 未验收」。作者改写后的 `verification` 把这两层都写出来了：

> 真实引擎验收按用户要求暂缓；#406 已于 2026-11-13 依代码交付证据关闭（`settings.speech_max_output_audio_ms`、`SpeechSynthesis.synthesize(max_output_audio_ms=...)` 与 `_limits.max_output_audio_ms` 的收紧链均在主干），**真实引擎与资格证据仍未取得**；短输入同步成功不代表长文本或 Base 模型资格。

三处代码依据我逐条在 main 上验证存在：

```text
backend/src/speechnexus_backend/settings.py:207:    speech_max_output_audio_ms: int = Field(default=120_000, ge=100, le=3_600_000)
backend/src/speechnexus_backend/speech/application/speech_synthesis.py:201:        max_output_audio_ms: int | None = None,
backend/src/speechnexus_backend/speech/application/speech_synthesis.py:101:    max_output_audio_ms: int = 120_000
```

证据注记同步改为「#406 已关闭但资格证据仍未取得」，`gap` 保持「请求级上限真实引擎证据暂缓」，**关闭与验收的区分在正文、注记、缺口三处都保住了**。

需要说明的一处模板噪声（**非本 PR 引入，也不是作者新增的措辞**）：矩阵状态列对**所有** closed 项统一渲染 `closed / completed；验收见需求证据`，来自 `delivery_tracker.py:184` 的固定模板（`meaning = "验收见需求证据" if reason == "completed" else "不计完成"`），58 个 closed 项共用同一句。它读起来像「已验收」，但指向的是证据而不是结论；F-TTS 的正文已明确否定「已取得资格证据」。若要把模板改成 `关闭依据见需求证据`，那是全表口径变更，不属于本卡，**建议另卡**处理，不阻断本 PR。

### 更正 3：9 条证据链接腐烂 / 5 个已不存在文件 —— **实质成立；「腐烂」用词不准确（不影响结论）**

9 条改写我逐条数出，与声称的数量精确相符：F-ASSET(1) + F-DELETE(1) + O-BACKUP(1) + O-MEDIA(1) + V-REFERENCE(2) + X-ADMIT(1) + X-RECOVER(1) + X-REDACT(1) = **9**。

5 个被弃用的目标文件在 main 上**全部不存在**：

```text
MISSING  backend/src/speechnexus_backend/speech/infrastructure/audio_asset_inspector.py
MISSING  backend/src/speechnexus_backend/speech/infrastructure/audio_inspection_process.py
MISSING  docs/architecture/production-assets.md
MISSING  docs/architecture/production-coordination-storage.md
MISSING  tests/unit/backend/test_audio_asset_inspector.py
```

替换后的 9 个目标在 `cef39433` 上**全部存在**（同上「NEW (replacement) evidence targets」段，5/5 抽查 + 189/189 全量见下）。

**一处措辞纠正**：这 9 条旧链接并不是「腐烂/404」。它们是**钉了 commit 的 blob URL**（`blob/6544a934/...`、`blob/3ac2bfc/...`），旧 commit 上的文件确实还在，URL 能正常打开：

```text
=== OLD (removed) evidence targets ===
  EXISTS at 6544a934: backend/src/speechnexus_backend/speech/infrastructure/audio_asset_inspector.py
  EXISTS at 3ac2bfc: docs/architecture/production-assets.md
  EXISTS at 3ac2bfc: docs/architecture/production-coordination-storage.md
  EXISTS at 6544a934: backend/src/speechnexus_backend/speech/infrastructure/audio_inspection_process.py
  EXISTS at 6544a934: tests/unit/backend/test_audio_asset_inspector.py
```

准确的说法是「证据指向**已从主干删除或迁移**的文件，读者按当前树核对不到」，而非「链接腐烂」。这不改变修的必要性（甚至更该修：钉死的历史链接会让读者以为那是现行实现），只影响 PR 描述的准确性，**建议在 PR 描述里改述，不需要改文档**。

### 更正 4：V-LEXICON / V-REFERENCE 注记与正文自相矛盾 —— **成立，现已不矛盾**

- **V-LEXICON**：正文写「公开列表与修订创建已合入（#484，PR #525）；原子发布已合入（#520，PR #552）」；旧注记却写「私有草稿实现及**尚未交付的公开生命周期**」——直接矛盾。新注记「私有不可变草稿；公开列表与原子发布已另交付，执行侧消费仍待验收」与正文、`gap`（「语音执行消费者接线与回滚语义仍待交付」）三方一致。
- **V-REFERENCE**：正文写 `voice_reference` 走与 `asr_input` 同一 HTTP 适配器、同一容器，仅预算不同；旧注记却写「voice_reference 落入 `available=False` 分支，不回退文本检查」，另一条写「是否纳入音频检查**待定**」——两条都在否定正文。新注记改为「voice_reference 走同一适配器但使用独立参考音频预算（`_duration_budget`）」与「#592 已关闭：voice_reference 纳入音频检查」，与正文一致。

正文这些技术断言我另做了代码复核，**全部为真**：

```text
backend/src/speechnexus_backend/speech/infrastructure/audio_inspection_http.py:186:            raise AssetError("asset_inspection_purpose_unsupported")
backend/src/speechnexus_backend/speech/infrastructure/audio_inspection_http.py:207:    def _duration_budget(self, purpose: AssetPurpose, policy: AssetPolicy) -> int:
backend/src/speechnexus_backend/speech/infrastructure/audio_inspection_http.py:270:                "X-Nexus-Max-Duration-Ms": str(budget_ms),
backend/src/speechnexus_backend/settings.py:92:    audio_inspection_max_reference_duration_ms: int = Field(default=60_000, ge=1_000, le=60_000)
```

`_duration_budget()` 按 purpose 取 `policy.max_reference_duration_ms` / `self._max_duration_ms`，并把同一数值作为 `X-Nexus-Max-Duration-Ms` 下发、再用同一数值复核回执；`asset_inspection_purpose_unsupported` 在接口层被投影为 503 `asset_inspection_unavailable` + `param: purpose`（`asset_errors.py` 的 `_REFUSALS`），与「检查器完全未装配」的 `param: null` 区分——与正文逐字相符。正文点名的 `tests/unit/backend/test_voice_reference_upload.py` 存在，`test_audio_inspection_http.py:400` 确实断言该错误码。

## 必须修清单

1. **F-ASSET `gap` 与同一行的实现及 `#599` 状态自相矛盾（范围内，纯文本一行）。**
   `delivery-issues.json` 中 F-ASSET `gap` 现文：「…`audio` 部署的关停与 `open()` 失败清理漏关检查器监督器（#599）。」
   但同一需求的 `implementation` 写的是「`AssetRuntime.close()` **先关闭检查器、再关闭**对象存储客户端与 PG 连接池」；`#599` 在快照与 GitHub 上均为 `closed / completed`（`closedAt=2026-11-12T04:21:22Z`）；而 `#599` 所指的「本机进程监督器」已在 `8c023545` 连同 `audio_inspection_process.py` 一起删除。于是矩阵同一行同时出现「关停漏关（开放债务）」与「#599：closed / completed」。
   这属于本 PR 自称修掉的那一类矛盾（对照 V-LEXICON / V-REFERENCE 已修），且发生在**本 PR 改过的** F-ASSET 行内，属范围内漏修。修法二选一：把该句改成已闭合的历史结论，或删除该句（`gap` 前半句「默认装配仍无法创建音频类 Job；正向闭环未在真实部署验收」仍然成立，可保留）。
   附注：同类但**不在本 PR 改动行内**的一处——V-DESIGN `gap` 仍把「校验报告到账本的自动入账与自愈（#555）」列为开放债务，而 `#555` 早在本 PR 之前就是 `closed / completed`。这属于历史遗留，建议另卡，不阻断本 PR。

## 建议修（不阻断）

2. 第 7 条的两处行号漂移：要么改成 `asset_runtime.py:240` / `audio_inspection_http.py:118`，要么开后续卡固化对表机制（后者更有长期价值，因为 `check-doc-refs.py` 对这类「行号合法但指向错符号」的漂移天生隐形）。
3. PR 描述里「9 条证据链接腐烂」改述为「9 条证据指向已从主干删除或迁移的文件」（旧 URL 钉了 commit，仍可打开，见更正 3）。
4. 矩阵状态列 `closed / completed；验收见需求证据` 的模板口径（58 项共用，读起来近似「已验收」）：建议单独一张卡评估是否改为「关闭依据见需求证据」，不在本卡内做。

## 我独立复现的反例

### 反例 A：把 `implementation` 改成明显谎言 → 脚本原样照抄（人工事实字段零校验）

在临时副本 `/private/tmp/sn727-pr` 内把 O-MEDIA 的 `implementation` 替换为：

> 【反例·明显的谎言】Linux 媒体检查由 macOS CoreML 内核在浏览器里完成，代码在 nothing_here/fake_module.py。

然后按 README 的离线渲染命令跑：

```text
$ python3 scripts/dev-env/refresh-implementation-tracker.py \
    --snapshot docs/plans/github-issues-snapshot.json \
    --snapshot-at "2026-11-14T09:43:25+08:00" \
    --output docs/plans/delivery-tracking.md
Refreshed 65 requirements: docs/plans/delivery-tracking.md
render exit=0

$ grep -c "macOS CoreML 内核在浏览器里完成" docs/plans/delivery-tracking.md
1

$ git diff --stat
 docs/plans/delivery-issues.json | 4 ++--
 docs/plans/delivery-tracking.md | 2 +-
 2 files changed, 3 insertions(+), 3 deletions(-)
```

渲染出的矩阵行里逐字出现「【反例·明显的谎言】Linux 媒体检查由 macOS CoreML 内核在浏览器里完成，代码在 nothing_here/fake_module.py。」——**退出码 0、无警告、无校验**。结论：`implementation`/`integration`/`verification`/`gap`/`evidence` 这类人工事实字段**没有任何机器校验**，脚本只做无脑搬运，矩阵的可信度**完全等于人工审查的可信度**。作者说「做过」，我复现一致。

（用完后 `git checkout -- docs/plans/delivery-issues.json docs/plans/delivery-tracking.md` 还原，`git status` 为空。）

### 反例 B：把 GitHub 派生字段改坏 → 刷新必须改回（派生字段可自愈）

在临时副本里把快照中 `#409` 的 `state` 从 `closed` 改坏成 `open`、`state_reason` 置 `null`，再跑实时刷新：

```text
BEFORE: #409 closed completed
broken state injected: #409 -> open

$ python3 scripts/dev-env/refresh-implementation-tracker.py \
    --refresh-snapshot docs/plans/github-issues-snapshot.json \
    --output /tmp/scratch-tracking.md
Snapshot written: docs/plans/github-issues-snapshot.json
Refreshed 65 requirements: /tmp/scratch-tracking.md
exit=0
fetched_at now: 2026-11-14T10:01:25+08:00
AFTER : #409 closed completed
```

`#409` 被改回 `closed / completed`；与提交版快照的 diff 中**除 `fetched_at` 外无任何差异**——顺带再次证明提交版快照的派生字段与 GitHub 实时值一致。派生字段（`state`/`state_reason`/`status:*`/`title`/`html_url`/`parent_issue_url`）确实由脚本独占，人工改会被覆盖；人工事实字段则相反（反例 A）。两侧边界与作者描述一致。

### CI gate 复现（`delivery_tracking`）

在 head 的 detached 快照 worktree 内，逐字照抄 `.github/workflows/ci.yml` 的命令：

```text
$ SNAPSHOT_AT="$(python3 -c 'import json; print(json.load(open("docs/plans/github-issues-snapshot.json", encoding="utf-8"))["fetched_at"])')"
$ echo "$SNAPSHOT_AT"
2026-11-14T09:43:25+08:00

$ python3 scripts/dev-env/refresh-implementation-tracker.py \
    --snapshot docs/plans/github-issues-snapshot.json \
    --snapshot-at "$SNAPSHOT_AT" \
    --output docs/plans/delivery-tracking.md
Refreshed 65 requirements: docs/plans/delivery-tracking.md
render exit=0

$ git diff --exit-code -- docs/plans/delivery-tracking.md
GATE_DIFF_EXIT=0
=== full status ===
(空)
```

**`CI GATE: PASS (byte-identical offline render)` 复现成立**：离线重渲染后 `docs/plans/delivery-tracking.md` 零 diff，工作树干净。同一命令在 `origin/main` 上也返回 0（`BASE_GATE_EXIT=0`），说明 base 侧本来就是绿的，本 PR 没有破坏可复现性。

## 死链扫描结果

对 `docs/plans/delivery-issues.json`（head 版）的**全部** `url` 字段做机器扫描，blob 链接按 `ref:path` 用 `git cat-file -e` 解析（`#L<n>` 与中文锚点按 `#` 切分，只看路径），issues/pull 引用按 GitHub 解析：

```text
blob urls: 189  ok=189  dead-path=0  unknown-ref=0
relative doc refs: 0   dead relative refs: 0
issue numbers not found on GitHub: []
PR numbers not resolvable: []
```

- **189/189 条 blob 证据 URL 解析成功，dead = 0**（与作者「修后 dead targets = 0」一致）。
- 正文里 15 处内联文件名提及中，2 处是裸文件名（`minio-test-service.sh`、`startup_privileges.py`），仓库内均存在（`scripts/dev-env/minio-test-service.sh`、`backend/src/speechnexus_backend/startup_privileges.py`），不算死链。
- 5 个 issue 引用 + 16 个 PR 引用全部可解析。
- 额外验证：`delivery_tracker.validate_documents()`（渲染时强制校验 `prd`/`designs` 的本地路径与锚点）在渲染中通过，等于顺带证明了矩阵顶部的本地文档链接与 `docs/prd.md#<anchor>` 锚点全部有效。

## `check-doc-refs` 基线归因（main vs head 对照）

```text
$ cd /private/tmp/sn727  # origin/main
$ python3 scripts/dev-env/check-doc-refs.py --problems-only; echo $?
ORIGIN_MAIN_EXIT=1
SUMMARY refs=644 AMBIGUOUS=28 EXTERNAL=1 MISSING=2 OK=613
RESULT FAIL

$ cd /private/tmp/sn727-pr  # PR head aa04d5a1
$ python3 scripts/dev-env/check-doc-refs.py --problems-only; echo $?
PR_HEAD_EXIT=1
SUMMARY refs=644 AMBIGUOUS=28 EXTERNAL=1 MISSING=2 OK=613
RESULT FAIL

$ diff <(main 问题集) <(head 问题集)
IDENTICAL: no new MISSING/AMBIGUOUS introduced by PR
```

- **基线即红成立**：`origin/main` 上 exit=1、`RESULT FAIL`。
- **本 PR 未新增**：两边的 `SUMMARY` 逐字段相同（`refs=644`、`AMBIGUOUS=28`、`MISSING=2`），问题集逐行相同。
- **2 条 MISSING 的位置与作者自述一致**：`docs/reviews/2026-11-13-workbench-tts-probes.md` 第 48 行与第 110 行，两条都指向 `orphan_probes.py` 的第 415 行与第 419 行（该文件仓库内不存在）。
- 28 条 AMBIGUOUS 全部落在 `docs/reviews/2026-11-13-*.md` 7 个历史审查文档内；本 PR 的 diff 既不碰 `docs/reviews/` 也不碰 `orphan_probes`（`git diff --name-only` 已验证），归因清晰，「越界未修」的判断正确。
- 该脚本**不在 CI**（`docs/validation/ci-validation.md` 明示），所以此红不阻断合并。

## 静态检查

```text
$ uvx --from "ruff==0.17.2" ruff --version
ruff 0.17.2

$ uvx --from "ruff==0.17.2" ruff format --check .        # origin/main
1830 files already formatted
FORMAT_EXIT=0

$ uvx --from "ruff==0.17.2" ruff check .                 # origin/main
All checks passed!
CHECK_EXIT=0

$ cd /private/tmp/sn727-pr && ruff format --check .       # PR head
PR_FORMAT_EXIT=0   1830 files already formatted
$ cd /private/tmp/sn727-pr && ruff check .                # PR head
PR_CHECK_EXIT=0    All checks passed!
```

（ruff 用 CI 钉住的 0.17.2，经 `uvx` 运行，不触碰任何 `uv.lock`。）

本报告自身也过了同一个工具，避免审查文档复刻它审计的缺陷（初稿引用了 `orphan_probes.py` 的行号，被自身判为 2 条新 MISSING，已改写）：

```text
$ python3 scripts/dev-env/check-doc-refs.py --problems-only     # 本审查分支
SUMMARY refs=664 AMBIGUOUS=28 EXTERNAL=1 MISSING=2 OK=613→633
# 问题集与 origin/main 逐行相同（AMBIGUOUS=28 / MISSING=2 未增），只多了 20 条有效引用
```


## 新绿（本 PR 相对 main 新增的通过项）

无。本 PR 是纯文档刷新，不新增测试或门禁；`delivery_tracking` gate 在 main 与 head 上本来就都是绿的。

## 只能依赖作者陈述的点

1. **真实部署侧的负面声明**——O-MEDIA「真实部署的跨平台解码与端到端联调未实测」「镜像构建与接线测试不证明真实容器运行资格」、F-ASSET/V-REFERENCE「真实容器联调仍未执行」。我可以证明仓库内**没有**这类证据、Compose 与契约**存在**，但无法证明「从未实测」。作者给出的是否定式声明，方向上是保守的（不夸大），采信风险低。
2. **历史变异计数**——F-ASSET `verification` 里的「恢复 `available=False` 12 failed、删参考上限 5 failed、删 purpose 诊断 1 failed、传输失败改回 busy 9 failed」，以及 V-REFERENCE 的「12 条用例失败」。这些是先前切片执行过程的记录，我没有重跑变异（本 PR 未改代码，重跑对合并决定没有增量信息）；但相关测试文件与断言行号我核到了（如 `test_audio_inspection_http.py:400` 的 `asset_inspection_purpose_unsupported` 断言）。
3. **更早时点的核验结论**——如 F-TTS 的「2026-11-04 增量核验…主干与受检候选整树一致」、O-BUILD 的「#596 在 macOS bash 3.2.57 下复现」等。这些是保留原时点的记录，按要求不回滚重验。

## 时间线（本地时间 Asia/Shanghai）

| 时刻 | 动作 |
| --- | --- |
| 09:56 | `git fetch`，确认 `origin/main`=`cef39433`，PR head=`aa04d5a1` 直接坐在其上（领先 1 个提交） |
| 09:57 | 建 worktree `/private/tmp/sn727`（`-b review/727-matrix origin/main`），校验 `HEAD` 是 `origin/main` 的祖先；`git diff --stat` 确认只有 3 个 `docs/plans/` 文件 |
| 09:58 | 读 head 版 `delivery-issues.json`；核实 `audio_inspection_process.py` 在 main/head 均不存在，`8c023545` 为删除提交（含 5 个单测） |
| 09:59 | 核实替代实现：`asset-inspector/`（12 文件）、内部契约、Compose profile/`internal: true`/`read_only`/`cap_drop`/无卷、退役键 guard 与接线测试 |
| 10:00 | 核实 `#406`：GitHub `CLOSED/COMPLETED`（`2026-11-13T14:30:27Z`）、关闭评论为依据代码交付、三处代码依据在 main 存在 |
| 10:01 | 快照 delta 计算（+6 / `#406`）与 78 条 vs 实时 GitHub 全字段对照（0 不一致）；标签与 `parent_issue_url` 亦 0 不一致 |
| 10:02 | 死链扫描：189 条 blob URL + 5 issue + 16 PR 全部解析通过；5 个被弃用目标确认在 main 不存在，9 条改写逐一核对 |
| 10:03 | 复现 CI gate（head 快照 worktree）：离线重渲染 + `git diff --exit-code` → 0；main 侧同样 0 |
| 10:04 | 反例 A（谎言 `implementation` 原样照抄，exit 0）与反例 B（改坏 `state` 被刷新改回） |
| 10:05 | `check-doc-refs` main vs head 对照：两边 exit=1、`SUMMARY` 与问题集完全相同 |
| 10:06 | `ruff format --check .` / `ruff check .`（0.17.2）：main 与 head 均绿 |
| 10:07 | 复核 F-ASSET 行号漂移（194→240、105→118；`#L115` 永久链接无漂移）与 F-ASSET `gap`/`#599` 矛盾 |
| 10:08 | 写本报告并提交 |
