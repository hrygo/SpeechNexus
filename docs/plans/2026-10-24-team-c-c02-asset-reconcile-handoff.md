---
title: "团队 C C02 素材 HTTP 原身份恢复交接"
status: active
date: 2026-10-24
---

# C02 素材 HTTP 原身份恢复交接

## 范围与基线

落实 [C02/C08](2026-10-23-three-team-delivery-plan.md#43-c-团队应用与体验) 的原上传恢复，实施见[计划](2026-10-24-team-c-c02-asset-reconcile-plan.md)。完整 [C00–C08 goal](2026-10-24-team-c-goal.md)继续 active，Workbench 正式入口/真实后端/模型闭环未完成。

起始 `330d0d70c5d40c03efcb9cb1ed146a97da53d46d`，fetch 后无冲突快进至 `dc14cf8fb0a7250842a8a7697c6884d6d21b2004`（#64 只改契约测试解析与效率资料），既有 135 个 C 文件摘要逐一保持不变。当前 `base_sha` / `head_sha` 均为后者。未 commit/push 或发送外部消息。

消费该 SHA 的 A speech `0.1.0.dev0`、`uploadAsset` 和生成 `Asset` / `Job` / `ErrorResponse` validator；以 #61 的相对 Asset Location 与 `X-Should-Retry: false` 语义为准。不编辑 canonical contracts、生成器/产物、lockfile、后端、schema 或 deploy。

## 已实现

- `SpeechFileClient` 使用 A 的 `uploadAsset` 序列化真实 File，私有单次 transport 注入原 abort signal，并使用 `redirect: error`。省略 TTL/workspace 仍保持原请求语义；不注入部署或产品默认，不在当前请求里改变旧上传 fingerprint。部署须显式设置原契约要求的 retention；未来增加请求选项须另固定意图。
- 仅 409 `asset_upload_in_progress` 与 503 `asset_upload_unknown` / `asset_metadata_unknown` 进入 Asset 恢复；`SpeechUploadRecoveryError` 不虚构 Operation。Location 只接受 `/v1/nexus/assets/{identifier}` 精确相对路径，拒绝绝对/外部/其他资源/编码分隔符/query/hash，不直接跟随任意地址。
- 旧 journal v1/v2 Job 与 v3/v4 upload 保持兼容；新增私有 v5 ASR / v6 TTS，固定 `assetHintId`。原键、输入指纹、固定模型/声音/参数沿用，hint 不可删除、换 ID、降回旧版本或改变任务种类。hint 只表示原素材身份，不是假冒已校验的 Asset 元数据。
- 有 hint 后刷新构造不请求、不需要重选输入；用户显式 `restore` / `refresh` 查询原 Asset。完整生成校验、ID/purpose/TTS MIME 和 journal 保存均成功后才确认素材；ready 也不会在只读恢复中自动建 Job，必须显式继续。确认后仍转原 v1/v2 Job 意图并沿用原 create/cancel 键。
- 原 metadata 404/410/503、ID/purpose 不符、写失败、已知 owner 改写与卸载迟到结果不授权新上传或 Job。缺少/非法 Location 保持 unknown；刷新后仍需原输入并按原键显式核对，不能换键制造替代素材。
- 两个开发视图能显示原素材标识并读取状态；TTS 补齐 ready 后显式继续。正式 Workbench 业务宿主本轮未接入，生产 bundle 仍排除 fake。会话/客户端源码由真实浏览器测试专用 bundle 直接消费。

## 验证（2026-10-24，Asia/Shanghai）

- TDD：最初客户端/记录 16 项全部 RED→GREEN；会话基线 10 失败/8 通过→18 通过，随后补两个卸载迟到用例。浏览器发现默认 native fetch 接收者错误（0 上传请求），注入全局调用 wrapper 定位后修复 default transport；新增 Node receiver 回归对旧实现 RED，修复后通过。新增共 37 项（client/journal 17、session 20）。
- TTS 初次合成 fixture 错带音频 duration/sample_rate/channels，被生成 Asset parser 拒绝；只修 fixture，未放宽校验。泛型 replay wrapper 与可能为空的测试索引修正后类型检查通过。诊断时临时将会话错误向测试抛出，诊断结束恢复原始字节，无诊断输出进入产品。
- 完整 Web `pnpm format:check` / `pnpm lint` / `pnpm typecheck` / `pnpm test` / `pnpm build` 通过：**416 项（Admin 79、Workbench 216、contracts 121），0 skipped**。探针 Prettier/ESLint 单独通过；`make contracts-check` 28 文件、生成 drift up-to-date。未改 Python，没有新增 Python/数据库/模型验证。
- 移除 journal 的固定原 hint 检查，两个 ASR/TTS 行为测试失败；精确恢复源码后新 37 项通过。native receiver 旧实现亦被新增回归杀死。未将 mutation 的预期失败计作最终失败或通过。
- `node scripts/dev-env/workbench-asset-reconcile-probe.mjs --output .runtime/team-c/c02-asset-browser-final.json`：实际 Chromium 156 + 优化构建的真实 consumer 类 + loopback 合成 HTTP，ASR/TTS 各 5 场景：hint 跨刷新/503 后重读/staging→ready→显式原 Job；无 Location 原输入两次 POST/一原键；外部 Location 不跟随；hint 保存失败；owner 改写不覆盖。无自动恢复请求、无 Operation 请求、无正文/文件名存储、0 pageerror/非 loopback/handler error。该探针明确 `productionUI: false`，不是正式工作台页面验收。
- 原上传恢复探针 ASR/TTS 各 4 次刷新，保留上传/创建/取消原键及大水位、无正文存储/出站/错误，服务结束关闭；原文件 UI 9 场景、TTS UI 4 场景通过，含原键、大水位、合成下载/原生 WAV 解码、Blob URL 清理、小窗口和生产排除 fake。

完整日志 `.runtime/team-c/c02-asset-*`；仅合成文本/字节，无真实音频、端点凭据、设备或 GPU。新探针清理自己的临时 bundle/浏览器/HTTP server。

## 证据边界与接力

本单元确认当前消费者正确使用 A 已发布的 HTTP 恢复语义，不证明 A 的真实 PostgreSQL/MinIO 幂等与维护进展、完整语音 worker、ASR inspector、音色固定材料、模型质量/GPU、TLS/代理/出站网络及真实部署。合成服务记录请求/键并返回生成校验的 fixture，不替代服务端并发协调证明。

保留原 v3/v4 的 TTL 省略语义，部署未设置 retention 时仍可能明确拒绝上传；不据此换键或偷偷选默认。已知 hint 只查询原素材，依赖服务端维护/原写入资格最终收敛，不把缺失或超时当成允许第二次写入。sessionStorage 是同标签页元数据和已知 owner 检查，不保证跨进程 CAS、关闭标签页/浏览器崩溃或长期 durability。

图谱尚未索引、analysis 未提供 index_repository，具名路径直接读源码；无完整结构/安全/负载结论。Workbench 正式宿主、校对版本/导出及其生成响应入口、真实服务/浏览器/设备资格、C03/C05/C06/C07 和其余 C08 继续推进。旧交接保留原时点事实，以本单元补充新 HTTP 消费状态。

## 独立审查与累计交付

独立 sol_worker 完成一次只读终审，未发现 P1/P2；独立复跑 Workbench 216、新增 37、typecheck、探针语法及 diff 检查均通过。报告 `.runtime/team-c/c02-asset-review-result.md` 明确区分独立复跑与父代理证据。终审后仅调整交接、计划勾选及文档索引位置，产品源码未变。

暂缓一项 P3：`upload-record.ts` / `task-journal.ts` 对私有 version 作 `Number()` 强转，可能接受字符串 `"4"` 的损坏旧 TTS 记录，随后会话严格数字判断不能识别该恢复记录。正常写入始终为数字，尚无正常流程丢数据或重复提交证据；后续维护须统一严格 discriminant，并补字符串/数组负例。此限制保留，不宣称损坏记录校验完备。

累计交付以 `dc14cf8fb0a7250842a8a7697c6884d6d21b2004` 为基线，共 140 文件；此前 135 文件中 127 个逐字节不变，8 个在声明范围内修改，新增 5 个。最终包及 manifest 位于 `.runtime/team-c/team-c-current.patch` / `team-c-current.manifest.json`；保留旧 135 文件包。使用临时 index 和干净 archive 验证 patch apply、应用后 140 文件字节、本地链接与真实 index 不变，摘要以 manifest 为准，不将审查报告冒充最终包核验。

审查未授予的资格逐项保留，完整 Ruling 与成本见 `.runtime/team-c/c02-asset-progress.md`：

1. 完整产品及关口不关闭；成本是正式宿主和 C00–C08 余项继续实施/验收。
2. 审查不追认其他 127 文件正确性；成本是只复用各单元原证据，由主代理另验最终包完整性。
3. A 的真实数据库/对象与协调不由本轮消费者探针授予；成本是真实服务并发、故障和维护收敛仍需接力。
4. 真实执行、模型与设备不授予；成本是 inspector、worker/GPU、stop/release、质量和负载仍需 A/B 联调。
5. 部署网络及整体安全不授予；成本是 TLS、代理、权限、出站和供应链须独立验证。
6. 正式浏览器体验不授予；成本是正式页面接入及键盘/焦点/辅助技术/尺寸验证另行完成。
7. sessionStorage 仅承诺同标签元数据及已知 owner 检查；成本是不保证多进程 CAS、关页/崩溃或长期保存，无 hint 恢复仍需原输入。
8. TTL 省略与 hint 收敛继续依赖服务端契约；成本是部署显式 retention 默认和真实维护进展尚待证明。
9. 无索引图谱且审查只独立复跑列明子集；成本是源码限定结论，完整 Web/契约/构建/浏览器复用父证据，无 Python/数据库新增资格。

不关闭团队 C goal 或 G0–G5。
