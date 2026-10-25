# 团队 C：音色设计与克隆创建任务交接

2026-10-24，base_sha=head_sha `eb299f7a6a14868be77adb09c70c0ad19a6a9987`，分支 `codex/team-c-c00-versions`。按[实施计划](2026-10-24-team-c-voice-create-plan.md)新增正式「音色创作」入口，完成C03的设计/克隆分开输入、材料确认和持久任务消费者。C03已进入in-progress；全部C00–C08保持open，完整Goal active，未commit/push或发布。

## 已实现范围

- 模型和工作空间明确填写，无虚构初值。设计保留描述/试听原文；克隆使用已有公开Asset编号，首发前GET核对同workspace、ready、voice_reference及audio类型。材料或输入修改清除原确认；不读取私人音频，不新增上传协议。
- 请求使用已有生成 `JobCreateRequest` 的 `voice_design/voice_clone`，通过原 `POST /v1/nexus/jobs`；响应和素材消费现行 `Job/Asset/JobCancellation` parser。未增加或修改公共协议、生成器、生成物、锁文件或生产路由。
- sessionStorage只在确认后保存固定原命令文本/编号和独立创建/取消键，闭合version2记录最多128KiB（兼容保守读取version1）；无音频或缓存Job。重载不自动发请求，原创建/取消结果未知时只显式重放原键及正文。每次POST先完成同一原意图CAS，损坏、外部改写或quota失败阻止写入。已发出的原创建重放不因素材后来过期而换意图。
- 仅当前调用的首次422 unsupported_parameter、invalid_request_error、retryable=false且无nexus允许纠正；历史未知/重载/回执落盘失败后的422继续固定原键。拒绝记录移除失败先阻止写入，显式重试保存后才开放表单。两种409恢复code保留原operation_id，重载不自动POST，显式GET原执行再核对关联Job；恢复身份保存失败保留内存更新，先重试存储。取消pending不因GET任务终态清除。
- 创建/取消回执与GET当前状态分层；Job的kind/workspace/id交叉核对，失败只展示稳定code。终态与清理分别显示，仅当前GET确认终态且cleanup为complete/not_required、无未知意图时允许下一项。导航/隐藏终止本地请求并忽略迟到结果，已提交后台任务继续；StrictMode不销毁仍在使用的控制器。
- 同源、redirect:error、15秒调用期限及1MiB JSON响应上界，错误响应流显式取消。材料确认和取消确认独立；正式页面不注入fake服务。切换保留未提交输入，已确认原文本可从固定原意图恢复。稳定label/id及360px布局消费现有设计Token。

## 验证与证据

- TDD覆盖14项命令/素材、29项恢复会话，以及页面/transport/App；相关62项通过。Web五门禁通过，1525＝contracts921＋Admin79＋Workbench525。初期材料、缺模块/入口、重载模式、错误body取消、label与失败码均有RED→GREEN日志。
- 生产bundle Chromium156.0.8078.4合成公开HTTP：设计/克隆及错误恢复共4任务、8写请求、1个context/4页面、1次原operation GET；创建/取消接受后截断响应、重载不自动POST、原键精确重放、错误purpose零创建、修改材料清确认、克隆原文/模式重载、首次unsupported_parameter明确拒绝后原文保留/新键纠正、response_not_replayable原执行编号落盘/重载不自动POST且显式GET定位原任务、外部改写阻止取消、quota阻止创建、取消焦点/基本Tab和360px无横向溢出。合成producer仅存在于独立探针脚本，出站/意外异常/serverErrors为0。
- Web五门禁及28源契约/生成漂移按最终源码验证；Python/PG/生成源码未修改，上一[依赖集成交接](2026-10-24-team-c-eb299-integration-handoff.md)Python3314/隔离PG296仅复用其原范围，未在本单元重跑。
- 同版既有ASR/TTS生产bundle合成回归也通过：ASR 3 sessions/10 writes；TTS 1 session/5 writes、3 utterances/96000样本Worklet渲染。不是模型或物理设备资格。
- 唯一fresh终审0 Critical/2 Important/0新增Minor；两项Important（首次明确拒绝锁死、丢失operation恢复身份）均已一轮TDD修复，4项会话及2项页面RED、拒绝移除quota路径额外1 RED后全部GREEN；未派复审，原报告不冒称修复后批准。
- 终审、最终门禁、浏览器截图/JSON和累计补丁精确hash记录在 `.runtime/team-c/voice-create-eb299/`。累计包为 `.runtime/team-c/team-c-current.patch` 和相邻manifest；旧310文件/567bc81b包保留 `.runtime/team-c/team-c-before-voice-create.*`。最终累计323文件：旧310中304字节不变，6处具名原文件变化/13新增；干净base apply/所有字节/链接/index保护通过，无提交或推送。

复现浏览器流程：

```bash
pnpm build
node scripts/dev-env/workbench-voice-create-probe.mjs --output .runtime/team-c/voice-create-browser
```

## 未实现与生产依赖

候选发现/再生成/试听、复用校验、preview_only发布屏障、不可变声音显式发布及后续TTS复用、退役/禁用/删除继续。当前Job返回draft引用不构成published声音；页面明确说明这些功能未接入，不给虚假发布按钮或可复用资格。

A06/B05生产创建/资格仍待联合验收。当前生成Operation.kind没有voice_design/voice_clone，C不发明类型映射；Operation仅提供原执行编号和job_id定位提示，仍以公开Job及原命令kind/workspace/id验证，合成恢复不证明生产voice-operation语义；该缺口由A同版契约补齐。完整VoiceCreationAccepted、VoiceDraftView、generation/preview/validation/publication与VoiceRevision等需A同一生成流水线提供runtime及HTTP正负样例，再由C消费；不复制另一套wire默认值或手写parser。新增素材上传、真实声音质量/材料重建、实际停止/资源事实、设备/网络/Linux/GPU和完整跨浏览器/a11y另验，C不代验A/B或放行G2。

八项既有Minor继续保留；本单元新增审查发现、逐项裁决和一轮修复结果以本机ledger及最后交接为准，不以合成浏览器或源码页面代替完整C03/C00–C08退出条件。
