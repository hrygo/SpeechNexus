---
title: "SpeechNexus 设计审查"
status: reviewed
version: "0.3.0"
date: 2026-10-22
---

# SpeechNexus 设计审查

本记录审查 SpeechNexus 0.3.0 文档基线，包括[项目主设计](../speechnexus-product-architecture.md)、[语音接口](../architecture/speech-api.md)、[资源治理](../architecture/runtime-governance.md)、[复用边界](../architecture/speechrail-reuse.md)、[实施计划](../plans/2026-10-22-speechnexus-implementation-plan.md)、ADR 和双语 README。0.3.0 是文档版本，项目尚无产品实现。

## 1. 结论与证据范围

已完成主审、独立只读复核、修订及针对修订结果的复核。在本次审查范围内，未发现尚待修改的实质设计冲突。审查结论覆盖产品 / 模块边界、关键命令路径和状态 / 资源一致性；不构成未来实现无缺陷或真实模型能力达标的保证。

用户确定的范围保持一致：独立企业语音底座、Linux / vLLM 路线、实时与非实时 ASR/TTS、OpenAI 兼容子集与音色增强协议、AdminUI / 个人语音工作台。首期无用户身份体系，工作空间、两个界面入口与执行 owner 均不冒充个人权限边界。

本次只修改设计文档与导航，没有实现代码、安装依赖、操作模型、启动服务、执行 UI 自动化或改变 SpeechRail 运行态。

## 2. 已修复的实质问题

| 编号 | 可复现的原设计问题 | 修正裁决 | 实施时必须验证 |
| --- | --- | --- | --- |
| F-01 | PATCH 允许修改声音输入并使旧候选失效，却没有消费修改后 revision 的生成命令；修改描述后无法沿协议继续发布 | PATCH 仅元数据；generations 接受完整声音配方，原子建立下一 revision / Job / outbox；与 Job.draft_target 等价。旧候选 / 校验不覆盖新 head；显式失败 retry 建立新 Job / revision | 两个生成请求并发、同 key 重放、生成失败、迟到 validation、旧校验发布新 head 的拒绝 |
| F-02 | 一处规定 accepted 输入即结算，另一处规定 TTS frozen 才结算；append 获 ack 后不 commit 就取消，可能得到两种用量 | ASR accepted 结算；TTS accepted 维持预留，首次冻结才结算，未冻结取消释放。输入以稳定区间跨 attempt 去重，输出以各 attempt 实际生成样本去重 | 重复 append / commit、部分冻结取消、作业内重试、显式新 Job 重试、跨日预留与待协调用量 |
| F-03 | Operation 取消超期可 failed + quarantine，但父 Job 的 cancelling 分支只明确到 cancelled，缺少异常映射 | Job 取消确认超期为 failed / cancellation_unconfirmed；cleanup_state=quarantined，附未清理 Operation，旧执行清理前拒绝 retry；后续只更新清理事实 | 取消 / 完成竞争、abort 未确认、资源未提前释放、恢复后终态不反转 |

F-01 / F-02 由独立初审确认，F-03 在修订后的独立复核中确认；三项均已修订并复核闭环。

## 3. 协议与规范补强

| 项目 | 采用的明确规则 |
| --- | --- |
| REST TTS 格式 | 默认 MP3，基线 MP3 / WAV / PCM；PCM 为无头 24 kHz、单声道 signed 16-bit LE；编码 / 重采样计入资源预算 |
| 文件 ASR | 默认 json；timestamp_granularities 仅与 verbose_json 同用，辅助格式依真实时间证据开放；标准 language 省略映射内部 auto |
| 实时配置 | 使用当前 session.audio.input 字段层级；active 只允许 ASR 策略白名单在下一段生效，TTS 声音与配方固定；换声新建会话 |
| 引擎适配 | 公开协议与 vLLM / vLLM-Omni 原生协议分开，明确采样率、commit 顺序、文本冻结和音频交付的转换责任 |
| 产物读取 | Asset 输入与 Artifact 输出各自具备元数据 / content 入口；已提交产物支持 Range，过期 / 删除不能伪造成功内容 |
| 幂等与默认版本 | 先查找 / claim 规范意图，再为新意图解析默认值；重放复用原固定版本，幂等表不保存未明确提交的输入全文 |
| 取消与清理 | 精确 Operation 可取消，Job 内操作映射为所属 Job 取消；结果终态与 cleanup_state 分开，终态不等于算力已释放 |
| 模块与保留 | 工作台独立保存项目副本、导出任务与成品；底座源删除不自动追删已提交的工作台副本，跨模块级联需明确范围 |

音色设计不保证天然产生跨模型可用的声音。只有候选到后续 TTS 的复用路径验证通过，才允许发布；否则为 preview_only。真实效果和许可仍需逐模型 / 制品核对。

## 4. 上游官方依据

核验日期：2026-10-22。下列资料说明上游协议，不证明 SpeechNexus 已实现。stable 页面会变化，P0 / P1 需要记录采用的实际版本与 commit。

- [OpenAI TTS 指南](https://developers.openai.com/api/docs/guides/text-to-speech)：默认 MP3 与 PCM 格式；[Speech API](https://developers.openai.com/api/reference/resources/audio/subresources/speech/methods/create)用于核对字段和可选格式。
- [OpenAI Transcriptions API](https://developers.openai.com/api/reference/resources/audio/subresources/transcriptions/methods/create)：timestamp_granularities 与 verbose_json 的组合条件；不能由文本均分伪造时间戳。
- [OpenAI Realtime 客户端事件](https://developers.openai.com/api/reference/resources/realtime/client-events)、[实时转写指南](https://developers.openai.com/api/docs/guides/realtime-transcription)：transcription 会话字段层级、正文事件及关联身份。
- [vLLM Speech to Text](https://docs.vllm.ai/en/stable/serving/online_serving/speech_to_text/)：原生实时输入为 PCM16 / 16 kHz，事件与 commit 顺序不同于公开 OpenAI 子集，需 adapter 转换。
- [vLLM-Omni Speech API](https://docs.vllm.ai/projects/vllm-omni/en/stable/serving/speech_api/)：文本流入与音频流出分别控制；Base / CustomVoice / VoiceDesign 是不同模型角色，原生音色注册接口不能替代平台的候选 / 发布流程。
- [vLLM 支持模型](https://docs.vllm.ai/en/stable/models/supported_models/)、[vLLM-Omni 安装说明](https://docs.vllm.ai/projects/vllm-omni/en/stable/getting_started/installation/)：适配能力与版本组合需资格验证，不能以 latest 标签替代锁定发行版。
- SpeechRail 复用事实见[固定 commit 来源与证据范围](../architecture/speechrail-reuse.md#2-固定来源与证据范围)；历史记忆只作为经验来源，不能替代当前代码或 Linux 实测。

## 5. 文档正规化与验证

产品与架构主设计使用稳定文件名，协议、资源治理与复用各自集中维护；带日期草稿中的计划和审查内容拆为独立过程记录。主设计及配套规范使用 active，计划使用 planned，ADR 0001 的 accepted 只覆盖已确认的产品范围。文档版本不充当软件版本或实施进度。

双语 README、目录索引与 ADR 链接指向同一主设计，原 dated specs 文件不再作为第二份活动方案。复用来源固定到原 commit，保留历史与当前设计的区别。

核验时间：2026-10-22，Asia/Shanghai。

| 检查 | 实际结果 |
| --- | --- |
| Markdown 结构 | 14 份文档代码围栏闭合，标题 / 目录引用可解析，无尾随空白 |
| JSON 示例 | 16 段均通过 JSON 语法解析；未声称已通过尚未建立的 schema |
| 本地导航与锚点 | 87 处本地链接的目标与锚点均存在 |
| 场景与范围 | 8 类场景预设保留；首期无身份体系、独立底座与工作台边界一致 |
| 单一活动设计 | 旧 dated specs 入口已迁出，README 双语、索引与 ADR 引用正式主文档 |
| Git 差异检查 | git diff --check 通过；文档修改未提交、未推送 |
| 独立复核 | 原两项与新增取消异常分支均确认闭环，未发现仍需修改的实质冲突 |

这些检查验证文档结构与样例语法；尚未创建机器契约，不能声称已经通过 OpenAPI / JSON Schema 或真实 API 测试。

## 6. 后续验证与投入边界

- P0 交付机器契约、负例、字段 / 状态表与 fake，验证并发 claim、发布 / 取消 / 禁用的事务边界。
- P1 验证真实引擎 status / abort、请求关联、网关接管屏障与停止证据，以及音色发布后的后续 TTS 可复用路径。
- 在实际部署验证采样率转换、Artifact Range / 过期 / 复制中断、跨 attempt 计量、混合负载、长时资源曲线、恢复与删除。
- 浏览器采集、播放、网络中断、键盘 / 无障碍和完整应用工作流分别验收；文档检查不能替代 UI 流程验证。
- 质量、P95 延迟、并发容量和 RPO / RTO 均为候选目标；36–53 人周 / 4–7 个月是预算假设，须在 P0 和首条真实路径后重估。

实施计划明确上述退出条件；未验证能力保持禁用或 experimental，不能通过界面存在、单次试听、ready 状态或一项测试推断生产资格。
