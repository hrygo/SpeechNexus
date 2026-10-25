---
title: "团队 C 交接：五个孤儿探针接入门禁（存在却无人运行）"
status: active
version: "0.1.0"
date: 2026-10-25
---

# 团队 C 交接：五个孤儿探针接入门禁（存在却无人运行）

上一轮修掉"两份合成实时探针存在却无人运行"后，本轮把同样的检查做成一次扫描：

> 对 `scripts/dev-env/` 里的每个探针，统计**除自身之外**有多少文件引用它。

C 侧结果为 **0 引用**（即没有任何测试或门禁运行）的探针有六个：

| 探针 | 域 | 本轮处理 |
| --- | --- | --- |
| `workbench-capture-probe.mjs` | C04 采集图（合成设备） | **接入门禁** |
| `workbench-media-probe.mjs` | C04 AudioWorklet（离线合成） | **接入门禁** |
| `workbench-voice-create-probe.mjs` | C03 音色创作 | **接入门禁** |
| `admin-task-center-probe.mjs` | C01 Admin 任务中心 | **接入门禁** |
| `workbench-file-demo-probe.mjs` | 文件工作流开发场景 | **接入门禁** |
| `admin-workspaces-probe.mjs` | C01 Admin 工作区 | 暂不接入：已有的门禁用 `admin-workspaces-live-probe.mjs`，两者关系需先判定，避免重复覆盖 |

（其余 0 引用探针属于 A/B 的引擎与控制器域，不在 C 范围内。）

## 接入方式与实测结果

新增 `tests/integration/test_workbench_orphan_probes.py`（5 个用例）并列入 `--browser` 门禁。每个探针的接口不同（有的 `--output` 是目录、有的是 JSON 文件；只有部分接受 `--screenshot`），已按各自真实接口调用，并一律读**它写出的结果文件**（stdout 只当日志留档）。关键实测性质：

| 探针 | 被钉住的诚实性质 |
| --- | --- |
| capture | `kind=synthetic_device_audio_worklet_capture`、`bundle=production`、**`physicalMicrophoneAccessed=false`**、**`autoplayPolicyBypassedForProbe=true`（探针自曝绕过了真实用户无法绕过的 autoplay 策略）**、`unexpectedNetworkRequests=0`、`pageErrors=0`、7 项 checks 全真 |
| media | `kind=synthetic_offline_audio_worklet`、`devicePermissionsRequested=false`、`renderedSamples=256`、**`firstRenderedOffsetSamples=0`**、`pcmSampleRate=24000`、布尔 checks 全真 |
| voice-create | 生产包 + 合成公开 HTTP、`physical_device=false`、`model_execution=false`、4 个 Job/8 次写入、`original_create_replayed`、`original_cancel_replayed`、`cas_blocks_cancel`、`quota_blocks_post`、`first_rejection_correctable`、`original_operation_restored_by_explicit_get`、`viewport360`、`basic_focus_tab`，且 `outbound`/`errors`/`serverErrors` 全空 |
| admin-task-center | `kind=development_admin_task_center`、5 个场景、`keyboardConfirmation`、`originalCancellationIdentity`、**`acceptanceNotRelease=true`（探针自述"开发场景通过≠发布"）**、`productionScenarioExcluded=true`、`mobileOverflow=false`、`browserErrors=0`、`unexpectedRequests=0` |
| file-demo | `kind=development_file_workflow`、9 个场景、`originalKeyReplay`、**`cleanupNotAssumed=true`**、**`exactDecimalSseCursor=true`**、`syntheticDownload=true`、`productionScenarioExcluded=true`、`mobileOverflow=false`、`devicePermissionsRequested=false` |

## 证据范围与边界

- 全部为**合成/开发场景**证据：无模型执行、无物理设备、无实时底座、**不含**语音或音频质量结论。`liveServiceVerified`/`liveSpeechVerified`/`publicRealtimeOrGpuVerified` 一律断言为 `false`——探针不得声称它没有的资格。
- capture 探针自曝绕过 autoplay 策略，这一点被写进断言而不是被忽略：**用户在真实浏览器里无法这样绕过**，因此该用例不能当作真实播放资格。
- media 的"渲染 256 样本、首个渲染偏移为 0"只证明本地渲染水位与对齐，不证明物理输出。
- voice-create 覆盖的是 C03 的**状态机与恢复语义**（原键重放、CAS 阻止取消、配额阻止提交），不是真实音色质量或复用资格。

## 门禁与结果

| 证据 | 结果 |
| --- | --- |
| 全量隔离 PG 探针（含 `--browser`） | 第 76 轮 **406**；第 77 轮 **410**；第 78 轮（+6 个欠账用例）见本轮报告 |
| `ruff format --check .` / `ruff check .` | 通过 |
| `mypy`（531 文件） | 通过 |

实测：5 个用例单独运行 18.5 秒全部通过。`mypy` 先拦下 `create_subprocess_exec(*command)` 的元素类型不明确——已用 `TypedDict` 描述每个探针的接口，而不是加 `# type: ignore`。

## 第 77 轮：判定、接入第六个，并立一道永久守卫

### 先更正上一轮的两个错误

1. **计数错了。** 上一轮说"C 侧共 6 个孤儿探针"。真实情况是 **15 个**——上一轮的扫描把 `.runtime/` 里的引用也算成"有人运行"（例如 `.runtime/team-c/c02-tts-package.py` 提到某探针就被计入），因此漏掉了 9 个。
2. **因此上一轮"孤儿已清理"的印象不成立。** 正确的说法是：上一轮接入 5 个，另有 9 个仍是孤儿。

### `admin-workspaces-probe.mjs` 的判定（已接入）

读了它与 `admin-workspaces-live-probe.mjs` 的实际接口：后者头部写明"Chromium-only probe of an explicitly supplied disposable A HTTP/PG fixture"，需要 A 的临时夹具；前者是**生产 Admin 包 + 合成生成的 HTTP**，因此是**不依赖 A 就能跑**的那条路径。两者不是重复，故**接入门禁**（第 6 个用例），钉住：`production_bundle`、`synthetic_generated_http`、8 次请求、`writes=0`、`explicit_reads`、`pagination_retry_original`、`terminal_cursor_ignored`、**`late_selection_ignored`**、**`wrong_identity_rejected`**、**`private_errors_hidden`**、`hidden_visible_dom_fault_injection`、`viewport360`、`basic_focus_tab`，且 `errors`/`outbound` 为空。

### 永久守卫：`tests/integration/test_probe_inventory.py`

靠"记得去扫"不可靠——两次扫描我自己都扫错了。现改为测试断言：

- C 侧每个探针必须**被某个测试、PG 探针驱动或 CI 配置提到**，否则必须出现在测试里显式的 `NOT_GATED` 清单中，并写明**域与未接入原因**；
- 清单里过期条目（探针已不存在或已被引用）会让测试失败；
- 门禁驱动必须仍然列着两份包装测试。

目前 `NOT_GATED` 记录 **9 个已知欠账**（domains + 原因）：`admin-task-events`、`admin-task-recovery`、`workbench-asset-reconcile`、`workbench-file-host`、`workbench-recovery`（判定为**不同路径**，各自需要包装）；`workbench-manuscripts`、`workbench-prompter`、`workbench-tts`、`workbench-tts-host`（**疑似已被已在门禁的新探针取代**，须先核实分支覆盖再决定接入或标注）。

### 计划项的诚实结论（不重复劳动）

- "扩展 credit 耗尽/迟到音频"：**已覆盖**——`workbench-realtime-asr-probe.mjs` 已断言 `credit_exhaustion_stopped`（第 2 个会话刻意用 2400 样本额度触发），迟到/旧 epoch、dispose 后迟到采集、迟到音频图分别由 `asr-stream.test.ts`、`realtime-session.test.ts`、`tts-playback.test.ts` 覆盖。故本轮不做重复探针。

## 第 78 轮：9 个欠账逐个见真章——接入 6 个，3 个证实已陈旧

### 方法：先实跑分类，再决定

对 9 个欠账逐个手工运行（每个先试目录再试文件路径），得到三类结论——**不是**凭印象判"疑似被取代"：

| 结果 | 探针 | 处理 |
| --- | --- | --- |
| **通过** | `workbench-tts-probe`、`workbench-asset-reconcile`、`workbench-file-host`、`workbench-recovery`、`admin-task-events`、`admin-task-recovery` | **接入门禁**（6 个新用例） |
| **失败（陈旧）** | `workbench-tts-host-probe`、`workbench-manuscripts-probe`、`workbench-prompter-probe` | 记入守卫的陈旧清单，附实测失败信息 |
| — | （前一轮已接入的 `admin-workspaces`） | — |

### 机械对比推翻了我的"疑似被取代"

对 4 个疑似被取代者与门禁内对应探针做结果字段差集，发现 `workbench-tts-probe` 独有 **`nativeWavDecoded`**（浏览器原生解码合成 WAV，不是计数器）与 **`blobUrlsRevokedOnUnmount`**（卸载时真的撤销 object URL）——门禁内的实时配音探针没有这些。所以它**不是**被取代，而是**漏接入**。实跑确认通过，已接入。

### 三个陈旧探针的实测失败（不得当作证据）

| 探针 | 实测失败 |
| --- | --- |
| `workbench-tts-host-probe.mjs` | `AssertionError: Unexpected synthetic route` ×2——生产包现在会请求这个旧合成服务未建模的路由 |
| `workbench-manuscripts-probe.mjs` | `expect(locator).toHaveValue` 失败：`element(s) not found`——页面已经变化 |
| `workbench-prompter-probe.mjs` | 严格模式冲突：`提词阅读` 区域内现在有**两个** `status` 元素——需人工判断是漂移还是缺陷，**先不当作缺陷** |

### 本轮新接入的 6 个用例钉住什么

- `tts`：`nativeWavDecoded`、`blobUrlsRevokedOnUnmount`、`cleanupNotAssumed`、`originalKeyReplay`、4 个场景、`devicePermissionsRequested=false`、零出站/零页面错误。
- `asset-reconcile`：`kind=synthetic_loopback_asset_recovery`、**`productionUI=false`（自曝驱动的是构建出的消费者类而非页面）**、10 个场景、不自动恢复、不发操作请求、只处理元数据。
- `file-host`：`productionUI=true`、合成 HTTP、**`downloadBytesVerified=46`（真实字节数）**、**`blobUrlsRevoked`**、`ownerChangeBlocked`、`firstRejectedUploadCorrected`、`keyboardFocus`、`width360NoOverflow`、`fakeExcluded`。
- `recovery`：3 次重载、**`unknownUploadRecovered=false`（诚实负例）**、**`qualifiesLiveSpeechOrGpu=false`（自曝不具备实时/GPU 资格）**、原键与原请求体保持、`uniqueUploadIntents=1`/`uniqueCreateIntents=1`（重放不放大意图）、不含音频内容、服务已停。
- `admin-task-events`：**`historyExpiredReread`（历史过期后重读权威状态）**、`largeCursorResume`、`duplicateReadSuppression`、`noAutomaticReconnect`、`wrongIdentityRejected`、`failedSnapshotDidNotAdvance`。
- `admin-task-recovery`：**`acknowledgementSaveFailureUnknown`（结果未知不得变成成功）**、`expiredCursorClearedAcrossReload`、`deniedWritePreventedPost`、`changedOwnerPreserved`、`corruptStoragePreserved`、`deniedReadNoRenderCrash`、`keyboardConfirmation`。

## 第 79 轮：把"待人工判断"变成判定——提词页确有一个真实的可访问名称缺失

上一轮我把 `workbench-prompter-probe` 的失败记为"两可：正常或缺陷，需人工判断"。本轮**读代码把它判定清楚**，不留给猜测：

- `提词阅读` 区域里确有**两个** `role="status"`，但含义不同：`本地提词状态`（本地操作消息）与 `部署提词状态`（操作结果消息）；另有第三个在 `serverState.state !== null` 时出现，内容是"部署内已保存：脚本修订…位置…手动接管…"。
- 判定：**不是重复播报缺陷**（两者内容不同、各自有用），但**第三个活区域没有可访问名称**——这是真实的可访问性缺失：读屏会念出内容却不说明是什么变了。
- 修复：给它加上 `aria-label="部署提词快照"`（与两个兄弟活区域一致）。这是最小、行为不变的修复。
- 单测：新增 `TeleprompterPanel.test.tsx` 用例（注入假客户端，读取部署项目与部署状态），断言**两个活区域各自可按名称寻址且互不相同**——把"两个活区域"的意图固化，避免以后有人把它们合并或再添一个匿名的。web workbench **597 通过**（596+1）。
- 探针修复：把它的断言从不精确的 `getByRole("status")` 改为 `getByRole("status", { name: "本地提词状态" })`。**修复后实跑通过**（`kind=production-manual-prompter-synthetic`、`exact_text`、`explicit_restore_manual`、`keyboard_and_focus`、360px 无横向溢出、**`microphone_calls=0`**、**`api_requests=0`**、零出站、零页面错误、自述资格 `manual-browser-reading-only`），已**接入门禁**并从陈旧清单移除。守卫因此只剩 2 个陈旧项。
- lint：新单测最初触发 3 条 `@typescript-eslint` 错误（`import type` 与无 `await` 的 `async`）——已按规则改写而不是禁用规则。

## 第 81 轮：两个陈旧探针各自定位到根因——一个修好接入，一个只剩 1 项

### `workbench-tts-host-probe`：**已修好并接入门禁**

- **先改诊断再猜**：该探针原本只抛 `Unexpected synthetic route`，不说是哪条。先把消息改成带方法与路径（`` `Unexpected synthetic route: ${req.method} ${path}` ``），一次实跑就定位到 **`GET /v1/workbench/projects` ×2**——页面后来开始读取项目目录，而旧合成服务只建模了 artifact 路由。
- **按契约补路由**：用 workbench 契约 fixture 里已有的 `listProjects-response-declared-200` 响应样例作答，而不是手写 DTO（避免自造协议副本）。
- **修复后实跑通过**（`production_ui=true`、`synthetic_loopback=true`、3 个受监控上下文/3 次上传/3 次创建、`input_utf8_bytes=42`、`audio_bytes=48044`、`audio_duration_seconds=1`、原键与原版本、刷新只读、**`native_wav_preview_and_download`**、**`late_content_discarded`（迟到音频被丢弃）**、**`switch_revokes_url`**、键盘与 360px、`fake_excluded`、零 handler 错误/零出站/零页面错误、自述 **`real_backend_or_model_qualification=false`**）。
- 已接入门禁（第 14 个用例），陈旧清单 **2 → 1**。

### `workbench-manuscripts-probe`：**部分修复，仍陈旧**（不谎报为通过）

- 第一次失败点：`getByLabel('校对草稿')` 找不到——**不是改名**（该标签在当前生产组件与 web 单测里都存在），而是流程过时：现在必须先"核对当前版本"读目录、再从稿件列表选中稿件，编辑器才出现。
- 按当前流程改了探针（核对 → 选稿件 → 断言编辑器），失败点因此**前移**到下一步：`核对当前版本` 处于**禁用**状态并超时。
- 读到判定条件：`locked = state.busy || record.pending !== null`（`ManuscriptHost.tsx:33`），该按钮 `disabled={locked || state.blocked}`——即为新项目保存原稿后，本探针的场景里该控件保持禁用。**我没有把它记为产品缺陷**：已在门禁的真实运行时探针在**同一按钮**上成功执行（`workbench-manuscripts-real-probe.mjs:109`，注释"Verifying the head is a read"），说明差异在本探针的场景状态，而不在产品行为。
- 因此保守处理：探针的流程修复保留（它反映当前 UI），陈旧登记更新为**精确诊断**（含 `locked` 的定义与上面这条对照证据），待进一步确认该场景里究竟是 `busy` 未清零还是留有 pending 草稿。
- 本题的有效证据仍是已在门禁的 `test_workbench_real_runtime_manuscripts_browser.py`。

### 可复用的教训

探针的诊断信息应当**自带定位**（方法 + 路径），否则"陈旧"会退化成反复猜测；本轮正是因为先改消息，一次运行就锁定了根因。

## 第 82 轮：最后一个陈旧探针——把"排除了什么"钉住，并停止无界深挖

对 `workbench-manuscripts-probe` 做了第三轮定位，本轮不再推理，而是**让探针自己产出证据**：

1. **在失败点 dump 页面状态**：页面显示"正在读取或提交，请稍候…"与"原请求已保存在当前标签页。重放沿用相同内容和原键，不会自动发送。"，`按原键重放` 处于禁用——即应用停在它自己文档化的**结果未定态**。**这是正确且保守的行为**（不把未知当成功），探针却直接要继续读目录。
2. **按应用意图补上恢复动作**：探针改为「保存原稿 → 等 `按原键重放` 可用 → 点击 → 等 `核对当前版本` 可用」。重放确实发生了（诊断显示 **两次 `createTranscript`、同一个请求键**），但 `核对当前版本` 仍然禁用。
3. **修掉探针"失败即失声"的毛病**：原 `catch {}` 吞掉了真实原因，只在结果 JSON（成功路径）里输出收集到的错误；失败时什么都看不到。现已：①`catch (cause)` 保留真实消息；②进程非零退出时打印**已尝试的变更列表**与**服务端处理器错误**。这是本轮最有复用价值的改动——下一轮不必再猜。
4. **证据排除了什么**：`DIAG handler errors: []` → **不是服务端拒绝**（合成服务对所有请求都给了应答）；同键两次 → **不是没有重放**。因此原因仍**未解释**，已如实登记，不写成"产品缺陷"，也不写成"通过"。

**对照证据**（为何不判产品缺陷）：已在门禁的真实运行时探针在**同一个按钮**上成功执行（`workbench-manuscripts-real-probe.mjs:109`）。

**停止条件**：本轮到此为止——继续深挖的边际收益已低，按 ROI 优先把结论、诊断能力与守卫固化；下一轮如果再动它，从上面第 3 条的诊断输出出发。

## 下一接入动作

1. `workbench-manuscripts-probe`：用本轮新增的诊断输出确认重放后的应答去向（合成服务确有应答、应用仍 busy），据此修完或正式退役（退役需明确授权，未获授权前保持登记）。
2. 真实设备/远端实时/真实音色就绪后，接上对应的**真实**证据；本文件中的所有用例都不得被当作那些资格的替代。
3. 新增探针时先跑守卫测试：它会在探针成为孤儿的那一刻失败。
