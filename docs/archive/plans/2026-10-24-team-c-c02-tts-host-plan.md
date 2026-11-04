# C02 正式文本配音宿主实施计划

Spec：[三队 C02/C08](2026-10-23-three-team-delivery-plan.md#43-c-团队应用与体验)、现行 speech-openapi、[配音消费交接](2026-10-24-team-c-c02-tts-handoff.md)。完整 C00–C08 goal 保持 active。

在既有正式文件任务宿主中接入文本配音，复用任务恢复、原键重放、取消确认、事件与安全新建。ASR/TTS 使用独立 sessionStorage key；用户输入真实部署模型、声音和参数版本，无默认虚构选项。发现接口的运行时 validator 未发布，手填不代表发现或资格完成。沿用现有 Token 与双栏布局，不新增依赖、服务或机器契约。

## Task 1：输入与音频结果

Interfaces：FileTaskSession 增加私有视图 inputFixed；校验失败不锁定，恢复记录与有效意图锁定。宿主按 kind 选择独立表单与结果，共用生命周期/恢复政策；正式 App 切换时卸载旧宿主，返回显式恢复。WAV 下载仅当前任务的已提交制品，上限沿用 16 MiB。

- [x] RED→GREEN：固定版本/纯文本输入；无效输入仍可修改；独立存储严格拒绝异类/字符串版本；恢复不自动写请求、未知创建原键重放、hint 只读后显式继续、原文本指纹恢复。ASR 既有测试保持通过。
- [x] RED→GREEN：WAV 元数据不符拒绝；试听/下载 URL 释放；隐藏/pagehide/停止/卸载撤销迟到结果；原生播放器与显式下载。
- [x] Run Workbench tests/typecheck；Expected 全通过。

## Task 2：正式浏览器与交付

- [x] 正式生产 bundle + loopback 合成 HTTP：实际 text/plain multipart、版本固定、刷新无请求、原键/原素材恢复、SSE/音频解码与下载、切换释放、360px 无横溢、所有 context 出站/页面错误监测及截图；不授予真实引擎或网络隔离资格。
- [x] 全 Web format/lint/typecheck/test/build 与原有生产 ASR/开发文件和配音/Asset 恢复探针；Expected 无回归。
- [x] 一次独立只读终审；Important/Critical 一次 TDD 修复，Minor 记录暂缓。累计包逐文件归属、hash、干净基线 apply、链接和真实 index 验证；不 commit/push。

## Review Focus

1. 无效参数不会锁死表单；接受有效意图后不改变固定文本、模型、声音与参数版本。
2. 两个槽独立且严格校验；恢复不自动 POST；未知结果不换键或删除。
3. 切换、隐藏、pagehide、停止与卸载不能留下播放、Blob URL 或迟到 DOM 下载。
4. 下载只接受本次 WAV 制品；只读事实与合成浏览器证据不代表后端/模型资格。
