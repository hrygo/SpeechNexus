# Base 克隆节点模板与运行边界

本目录保存 Base（声音克隆）变体的节点模板样例、warmup 请求体与一份合成参考音频；
不含模型权重、端点地址、凭据、binding revision 或容量 manifest。真实部署由节点平台方按
获批镜像与制品固定后填入。

## Base 与其它 TTS 变体必须不同进程

实测（vLLM-Omni `0.32.0rc1`，`vllm/vllm-omni:latest`，arm64）：引擎按 checkpoint 与
`task_type` 校验加载，Base、VoiceDesign、CustomVoice 是三次不同的任务加载；同一进程
不能切换，且同时常驻会在后续实例 stage1 直接 `CUDA error: out of memory`。因此：

- 模板声明 `variant_slot`，与 CustomVoice、VoiceDesign 取同一槽位（`tts-variant`），
  同一槽位只有在旧实例取得已核验 stop 后才释放；控制器按冲突拒绝并存 prepare。
- 网关侧同样只配置一条常驻 TTS 变体泳道：同时配置 CustomVoice、VoiceDesign 或 Base
  会在启动时失败。
- 切换变体是「停止旧实例 → 确认释放 → 以新模板 prepare」，不是并行扩容。

## 启动参数

`template.example.json` 的 `command` 与 `environment` 对应实测启动形态：

- `--task-type Base`：checkpoint 必须以 Base 任务加载。
- `--gpu-memory-utilization 0.08 --max-model-len 1536 --max-num-seqs 1 --enforce-eager`：
  Base 常驻实测占用 7.2–7.4 GiB；模板按 10 GiB 峰值/容器上限预留，真实节点必须按实测
  峰值重新核定后替换，而不是照抄示例。
- `VLLM_USE_V2_MODEL_RUNNER=0`、`HUMMING_DISABLE_PARALLEL_BUILD=1`：避免 V2 runner
  与并行构建在本镜像下引入额外初始化路径。
- `VLLM_API_KEY` 由控制器按实例 generation 注入，模板环境变量不能声明或覆盖它。

冷加载实测约 101 s；热态单请求在实测环境为 3.49 s 首包 / 6.88 s 生成。warmup 预算
必须覆盖冷加载与首个真实请求，本模板取 240 s（控制器上限 300 s）。

## 就绪判定

`/health` 返回 200 只证明进程在监听，不能作为就绪证据。模板使用 `http_warmup_ok`，由
控制器向 `/v1/audio/speech` 发送 [warmup-request.json](warmup-request.json)（按
`body_sha256` 校验请求体）并只接受 2xx。

该请求体包含一份 2.84 s、16 kHz、单声道 16-bit 的**合成**参考音频
[warmup-reference.wav](warmup-reference.wav)（macOS `say` 合成，内容为
"This is a quiet reference sample for voice cloning."），`ref_text` 与该音频逐字对应。
它不含任何真实用户声音，可入库；部署若替换参考音频，必须同时重建请求体、更新
`body_sha256`，并保持 `ref_text` 与新音频逐字对应。

网关侧另有独立就绪探针 `voice_clone_warmup`：它在自己的预算内发起一次真实克隆合成并
要求得到合法 WAV；`/health` 200 不会让网关报告 ready。该合成在后台执行，`/readyz` 只
返回已结算结论；并发检查共享同一次尝试（single-flight），成功结果进程内永久缓存，失败
后按 `readiness.retry_interval_seconds` 重试。部署时把该服务的 `start_period` 调到覆盖
冷启动（建议 ≥ 240 s）。

网关的 warmup 参考音频由私有配置 `warmup.reference_audio_file` 指向，部署时复制本目录
的合成音频即可；`warmup.reference_text` 必须与其逐字对应，否则网关启动即失败。

## 参考音频的接受面

网关在发出引擎请求前完成全部结构校验，不把原始内容写日志：

- 只接受纯 RIFF/WAVE，`fmt ` 为 PCM（format 1）、单声道、16-bit；
- 采样率只接受实测可用的 16 kHz 与 24 kHz；其它采样率（含 44.1 kHz/48 kHz）拒绝，不做
  重采样；
- 时长默认 1–60 s，字节上限 8 MiB，且必须与命令声明的 `sample_rate_hz`/`channels` 一致；
- 通过校验的 PCM 会重新封装为规范 44 字节 WAV 头再转 base64 data URL，避免其它 chunk
  或尾部字节进入引擎；`file://` 参考音频不受支持；
- 请求体、data URL、参考文本与音频字节不进入日志、错误信息或公开回执。

`preview_text`：合同允许它为 null；本泳道每次执行都是一次预览合成，因此 null 在命令进入
引擎前以 `invalid_execution_request` 拒绝，而不是让引擎决定。

`ref_text` 与音频的配对：网关只能校验语音时长下的字符密度（默认 0.5–20 字符/秒）与非空，
**不能证明**文本就是音频内容。实测错配时输出会复述参考文本片段，因此配对正确性是调用方
的责任，也是本模板未验证项之一。

`x_vector_only_mode` 的语义由合同字段推导：`reference_text` 非空即 ICL 模式并发送
`ref_text`；为空即 x-vector-only 模式并显式发送 `x_vector_only_mode: true`。网关默认
要求 ICL，只有 binding 显式 `allow_x_vector_only: true` 时才允许无文本克隆，因为该模式
相似度可能下降。

## 释放证据

`POST /v1/omni/sleep` **不是**释放证据：实测返回 `SUCCESS` 且 `freed_bytes=0`，随后推理
超时。释放只认进程退出或容器停止，并需容器已退出与资源释放确认；网关的
`VoiceCloneRejectedError`/`VoiceCloneCallError` 都不构造释放证据，控制器仍按既有 stop
证据规则核验后归还预算。408/429/5xx 与超时一律按 `TRANSPORT_UNKNOWN` 且
`request_may_have_run=true` 保守处理；只有明确的 4xx 才是不重试的 `ENGINE_REJECTED`。

## 语言

网关只发送实测有对应厂商值的语言（zh/en/fr/de/it/ja/ko/pt/ru/es；区域子标签按主语言
归并，如 `zh-Hans` → `Chinese`）。其他取值（包括契约允许的 `auto`）在命令进入引擎前以
`invalid_execution_request` 拒绝。

## 部署前必须补齐

- 镜像 digest：沿用已验证的 vLLM-Omni 镜像记录；模型 checkpoint/revision 由制品所有者
  固定后写入 `artifact-pin.json`，本目录不伪造模型摘要。
- `<VOICE_CLONE_MODEL_ROOT>`、只读挂载源、容器内存上限与 `max_peak_memory_bytes` 按节点
  实际路径、实测 7.2–7.4 GiB 常驻与峰值核定。
- warmup 请求体中的 `model` 必须与实例实际 `served_model` 一致；替换模型名时同时更新
  `warmup-request.json`、其摘要与网关私有端点描述。
- 参考音频与 `ref_text` 换成部署方合法的合成或授权语料，并同时更新模板 body 摘要与网关
  私有配置。
- `readiness.warmup.timeout_seconds` 覆盖冷加载与首个真实请求，并按节点实测调整。

## 未验证项

- 本模板未在真实 GPU 上运行；冷加载 101 s、常驻 7.2–7.4 GiB、热态 3.49 s/6.88 s 来自
  MegaCube DEL-521 的实测，仅作为预算与上限依据。
- ICL 模式下 `ref_text` 与音频语义一致的自动校验不存在，只有长度/密度边界。
- x-vector-only 模式的相似度损失未量化。
- Base 与实时 ASR/TTS 共存的显存余量未在本模板上复测。