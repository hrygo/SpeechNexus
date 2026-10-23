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
须一起升级后启用；公共 OpenAI 字段与公开内容边界不变。业务服务仍未实现。

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

网关先按 schema 验证，再按 RFC 8785 canonical JSON 的 UTF-8 字节计算 SHA-256，
比较 command_digest。禁止 Unicode normalization、隐式填默认值、NaN、Infinity
和不成对代理字符；数值必须可由该规范无损表达。传输键顺序不影响摘要，文本或
参数任何变化都改变意图。同一 submit identity 的完整规范请求（含期限、预算、
fencing 和命令）变化返回 submit_intent_conflict；摘要不匹配返回
invalid_execution_request。业务输入内容只经私有命令传输，不写日志或公开 receipt。

公开资源版本与 command.resources 一致；外层 voice_revision_id 若非空必须与
固定音色版本一致。非 built-in 音色须带已发布材料；built-in 由 binding 固定实现。
词表/发音资源内容由指定 immutable revision 解析，不能把有内容却未固定版本的
参数作为可重放意图。Diarization 非空 min_speakers 不得超过 max_speakers。
TTS/codec 等会输出 PCM 的 stage 与 manifest.emits_audio_pcm、sample_rate_hz 和
max_output_audio_ms 必须一致。契约级 fake 传递测试只证明协议，不证明真实引擎支持。

Codec 对象格式 `nexus.codec_tokens.v1` 是 UTF-8 JSON：schema_version 为 1.0.0，
codebook_count 为正整数，frames 为非空二维非负整数数组，每帧长度等于
codebook_count；codebook_count 与命令一致，词表上界和帧时钟由固定 manifest
声明。对象必须按同一严格格式校验后送 adapter，不能用于承载未定义命令。

## 内部文本流

[execution-stream.schema.json](../../contracts/internal/execution-stream.schema.json)
1.1.0 显式区分 pcm/text 输入。两个方向分别递增 envelope.seq；epoch 不复用。
text 模式 input=null、input_credit_samples=0；pcm 模式字符额度为零。
opened 必须回显相同模式、执行身份与 fencing。TTS command.text 和 text_stream
恰好选一个；文本流身份必须匹配 open，命令中不能再带未冻结初始文本。

append/commit/finish 使用独立 text_seq 和 Unicode scalar offset。一次只发送一个
未确认事件。按相同 text_seq 重发完全相同内容返回原 ack；不同内容冲突，跳号拒绝。
字符额度是可接受的绝对末端位置，不是叠加赠送；UTF-8 字节与 UTF-16 code unit
都不能作为字符数。只向 adapter 提交不可变 commit 前缀。finish 精确冻结全部
已接受文本并提交最后后缀；完成 ack 之后的新文本被拒绝，原事件精确重放仍成立。

断线不能续拼未知前缀或重新生成已确认内容。先查询原 submit，按原执行恢复清理，
停止证据不明时 quarantine；新 epoch 不继承未确认文本。关闭连接不释放预算。
