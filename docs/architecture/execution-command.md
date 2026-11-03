---
title: 规范执行命令与内部文本流
status: active
version: "1.0.0"
date: 2026-10-23
---

# 规范执行命令

[execution-command.schema.json](../../contracts/internal/execution-command.schema.json)
是执行网关与生产者之间唯一的私有命令定义。Submit 必填 command 与
command_digest；旧 input_asset_ref 被完整命令替代。此变更不兼容旧内部生产者，
须一起升级后启用；公共 OpenAI 字段与公开内容边界不变。文件 ASR 和 TTS 已有原命令消费者，
仅接收各自支持的子集，不支持的能力会被拒绝；装配分别见[文件 ASR](../operations/file-asr-runtime.md)
和[文件 TTS](../operations/file-tts-runtime.md)。同步合成业务入口见[同步语音合成](../components/synchronous-speech.md)；
这些接线不代表所有命令种类均已接通。

当前文件 ASR 消费者要求 `asr_file` / `asr_batch`、server VAD 与完整段定稿，只接受
`json` / `text`，拒绝时间戳粒度、alignment、diarization 和词表资源。文件 TTS 消费者要求
`tts_file` / `tts_connector`、已配置的 builtin voice、完整文本和 `audio_delivery=complete`，
不接受音色材料、词表或发音资源。两者还分别核对固定 binding、策略和执行目标；具体限制以
[ASR 消费说明](../components/file-asr-recognition.md)及[TTS 消费说明](../components/file-tts.md#原命令消费)为准。

各个 command.kind 冻结 ASR、TTS、声音设计/克隆/验证、对齐、说话人分离、VAD 和
codec 阶段的输入及有效参数。生产者先解析模型/预设/音色/词典的固定版本，把
实际生效参数全部填入，不依赖网关重新读取后来变化的默认值。ASR turn_detection、
TTS planner 的子字段也完整填写。标准 TTS instructions 的 4096 上限在内部命令
中完整保留；Nexus override 的 2000 上限仍在其公开入口校验。

资源词表和发音规则携带明确的 typed entries；音色材料和输入音频引用不可变对象，
包含所属 workspace、精确对象、非空长度与内容摘要，网关仅使用服务端配置的对象
存储读取并验证。不能使用任意 URL、临时 vendor 字典或共享业务表补全命令。
语音材料格式必须由固定 binding/manifest 约定的 adapter 解码并验证；不把私有
特征暴露为公开 receipt。跨 workspace 引用、摘要或长度不匹配均拒绝执行。

命令中的对象引用固定 bucket、key、长度和摘要，但不携带 S3 version ID。当前文件 ASR
输入适配器在每次读取时先 HEAD 获取非空版本，再对该版本执行 GET 并核验长度和摘要；
这固定的是本次读取，不能将原命令解释为直接指定了对象版本。许可判断须在命令生产和准入
边界完成，具体读取与失败规则见[冻结执行输入](../components/frozen-execution-inputs.md)。

网关先按 schema 验证，再按 RFC 8785 canonical JSON 的 UTF-8 字节计算 SHA-256，
比较 command_digest。禁止 Unicode normalization、隐式填默认值、NaN、Infinity
和不成对代理字符；数值必须可由该规范无损表达。传输键顺序不影响摘要；规范化后的
命令内容变化会改变摘要，而仅数值表示不同（如 `1` 与 `1.0`）可产生相同规范字节。
同一 submit identity 的完整规范请求（含期限、预算、
fencing 和命令）变化返回 submit_intent_conflict；摘要不匹配返回
invalid_execution_request。业务输入内容只经私有命令传输，不写日志或公开 receipt。

公开资源版本与 command.resources 一致；外层 voice_revision_id 若非空必须与
固定音色版本一致。非 built-in 音色须带已发布材料；built-in 由 binding 固定实现。
`SubmitExecutionRequest` 的 schema 允许省略 `voice_revision_id`，但当前网关转换代码直接
索引该字段，省略会触发 `KeyError`；调用当前实现时无音色也需显式传 `null`。这是契约与
实现的差异，不能把 schema 接受省略视为网关已支持。
词表/发音资源内容由指定 immutable revision 解析，不能把有内容却未固定版本的
参数作为可重放意图。当前 schema 分别声明条目和来源字段，但不强制非空条目携带来源；
生产者 / 消费者须另行校验来源和内容的一致性。当前文件 ASR/TTS 消费者均拒绝这些资源。
Diarization 的 `min_speakers` 和 `max_speakers` 均非空时，前者不得大于后者；
当前命令 schema 仅分别约束其范围，不校验两者的大小关系，生产者 / 消费者须另做语义校验。
TTS/codec 等会输出 PCM 的 stage 与 manifest.emits_audio_pcm、sample_rate_hz 和
max_output_audio_ms 必须一致。提交 schema 只约束时长和采样率同时为空或同时非空，
不能单凭它确认 stage 与 manifest 一致；后者须在资源资格和准入边界另行核验。契约级 fake
传递测试只证明协议，不证明真实引擎支持。

Codec 对象格式 `nexus.codec_tokens.v1` 是 UTF-8 JSON：schema_version 为 1.0.0，
codebook_count 为正整数，frames 为非空二维非负整数数组，每帧长度等于
codebook_count；codebook_count 与命令一致，词表上界和帧时钟由固定 manifest
声明。对象必须按同一严格格式校验后送 adapter，不能用于承载未定义命令。
当前命令 schema 只固定格式标识、对象引用和 `codebook_count`，不校验对象正文；
上述正文格式是设计要求，真实 codec 消费者和正文校验仍需交付与验收。

## 内部文本流

本节描述内部流契约与目标语义，不代表流式执行链已经装配。当前文件 TTS 的
`CommandFileTts` 只接受完整 `text` 且要求 `text_stream=null`；增量文本消费与断线恢复
须单独实现和验收，见[文件 TTS 原命令消费](../components/file-tts.md#原命令消费)。

[execution-stream.schema.json](../../contracts/internal/execution-stream.schema.json)
1.1.0 显式区分 pcm/text 输入。两个方向分别递增 envelope.seq；epoch 不复用。
text 模式 input=null、input_credit_samples=0；pcm 模式字符额度为零。
opened 必须回显相同模式、执行身份与 fencing。TTS command.text 和 text_stream
恰好选一个；文本流身份必须匹配 open，命令中不能再带未冻结初始文本。

收到 opened 后才能发送 append/commit/finish；三类事件在每个 epoch 共用独立于
envelope.seq 的 text_seq，从 0 开始连续递增，并使用 Unicode scalar offset。一次只发送一个
未确认事件。按相同 text_seq 重发完全相同内容返回原 ack；不同内容冲突，跳号拒绝。
字符额度是可接受的绝对末端位置，不是叠加赠送；UTF-8 字节与 UTF-16 code unit
都不能作为字符数，字符额度也不豁免单条消息的字节数上限。旧事件重放返回的原 ack
不能覆盖较新的已接受位置或额度。只向 adapter 提交不可变 commit 前缀。finish 精确冻结全部
已接受文本并提交最后后缀；完成 ack 之后的新文本被拒绝，原事件精确重放仍成立。

断线不能续拼未知前缀或重新生成已确认内容。先查询原 submit，按原执行恢复清理，
停止证据不明时 quarantine；新 epoch 不继承未确认文本。关闭连接不释放预算。
