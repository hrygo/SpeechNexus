# C02 素材 HTTP 原身份恢复计划

已批准 C02/C08 范围，沿用 native inline / TDD，一次独立终审。Spec：[三团队 C02](2026-10-23-three-team-delivery-plan.md#43-c-团队应用与体验)、`contracts/speech-openapi.yaml` 和 [production-assets](../architecture/production-assets.md)。C 仅消费 A 生成客户端/上传 helper，不编辑公共契约、生成、依赖或后端。

目标：上传 409/503 原身份未决时保留原键，若 Location 指向原 Asset 则跨刷新只读查询该 Asset；不虚构 Operation、不自动重传或创建替代素材/Job。

- 使用 A `uploadAsset`，在单次调用 transport 注入原 signal，阻止 redirect。TTL/workspace 继续原请求省略语义，依赖部署显式默认；不改变旧记录 wire 意图。
- `SpeechUploadRecoveryError` 承载安全 error code 与可选 assetId。只接受契约的相对 `/v1/nexus/assets/{identifier}` Location；拒绝绝对/跨 origin/编码分隔符/query/hash/其他路径。仅 409 `asset_upload_in_progress`、503 `asset_upload_unknown`/`asset_metadata_unknown` 进入该路径。缺少/非法 Location 仍 unknown，按原输入/原键显式恢复。
- v3/v4 保留；新增私有 v5 ASR / v6 TTS，额外固定 `assetHintId`，可从旧 upload 阶段单向提升。hint 不是已验证素材事实；只有 GET 的生成 Asset/purpose/身份检查通过且保存完整元数据后才进入 validation，用户显式继续才创建 Job。hint 后 404/410/503 不换身份、不重传。恢复不自动网络请求。
- journal 保护原键/输入/固定选择；已保存 hint 不可删除或换 ID，不允许降回旧版；完整素材确认后仍可转原 v1/v2 Job 意图。选择/取消/水位其余行为保留。

## Task 1：客户端与私有记录

Files: `speech-file-client.ts`、`upload-record.ts`、`task-journal.ts`；新 `asset-reconcile.test.ts`。

- [x] RED→GREEN：A multipart/helper 的原 bytes/name/type/key/signal；不注入 TTL；redirect:error。409/503 exact code 与安全 Location；原生断开/无 Location 保留 unknown；严格非法 Location、稳定错误不显示正文；v3/4→v5/6、hint 固定、完整确认→v1/2、非法字段/损坏记录/过期 owner/写失败。
- [x] Run `pnpm --filter @speechnexus/workbench exec vitest run src/file-workflow/asset-reconcile.test.ts`；Expected 全通过。

## Task 2：上传会话与浏览器接入

Files: `file-task-session.ts`，开发 ASR/TTS 界面适配原素材标识；新增 `asset-reconcile-session.test.ts` 和生产消费源码的 loopback 浏览器探针。

- [x] RED→GREEN：两种任务的 hint 持久化/刷新无请求/显式原 ID GET/ready 后显式 Job；未知无 hint 原输入/原键恢复；失败不清 unknown、错误 ID/purpose 拒绝；hint 保存失败/owner 漂移/卸载迟到不创建 Job；旧 v3/v4 与原恢复回归。
- [x] 实际 Chromium + loopback：ASR/TTS 原 Location 刷新、只读查询、无 Operation、ready 显式继续、缺 Location 原键恢复、非法外部 Location 无出站；0 pageerror/非 loopback，metadata 无正文。
- [x] 完整 Web format/lint/typecheck/test/build、契约/生成漂移、新探针与原上传恢复/文件/TTS 浏览器回归；Expected 全通过并准确限定合成证据。
- [x] 一次独立终审；累计补丁含新增文件并校验 SHA/干净基线 apply/逐字节/链接/index；goal 保持 active。

## Review Focus

1. 原始请求 TTL/workspace/键/输入不漂移，helper 未提供 signal 不能丢失 abort 或扩大公共生成边界。
2. Location 不允许任意 URL、跨资源或 Operation；header 仅提示，必须校验完整 Asset。
3. v3/v4 兼容、hint 单向迁移及各阶段存储失败/已知 owner 变化；先保存再跨阶段，未知不能伪成功。
4. 404/410/503、错误 Asset、dispose/迟到 GET 不得提交 Job；ready 也需要显式继续。
5. 合成 HTTP/浏览器不证明真实存储幂等/后台维护/音频 inspector/GPU/生产资格。
