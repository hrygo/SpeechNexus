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

结构校验只覆盖容器头与声明时钟：头部完全自洽、但样本实际编码或时钟与声明不同的内容，
网关无法判定，只能由调用方与引擎侧保证；这是本泳道的结构校验边界。

`preview_text`：合同允许它为 null；本泳道每次执行都是一次预览合成，因此 null 在命令进入
引擎前以 `invalid_execution_request` 拒绝，而不是让引擎决定。这是本泳道的执行约束，合同
描述与示例未单独标注，列为后续契约修正项。

`ref_text` 与音频的配对：网关只校验非空与上限（≤25 字符/秒），**没有下界**——参考音频
允许包含静音，短句配长静音是合法配对；也**不能证明**文本就是音频内容。实测错配时输出会
复述参考文本片段，因此配对正确性是调用方的责任，也是本模板未验证项之一。

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

参考音频/配对等内容在**发出引擎请求前**被判为结构无效时，网关用一条专用受控 UPDATE
记录确定性 `invalid_execution_request` 的 `failed/not_required`。普通的 observation 重写
禁止清空已分配的请求身份，也不允许 `executing → not_required`，因此该语句单独校验
`executing/pending`、版本未变、无生成样本/暂存键/终止时间/停止证据；输出区间与暂存结果由
`apply()` 在写库前校验，语句本身不查 `output_sample_intervals`。成功后清空未发送的
`engine_request_id` 与 `dispatched_at`。数据库字段无法单独区分“未发送”与“已发出未完成”，
安全边界由唯一调用方（仅处理结构拒绝）和 `apply()` 的输出前置条件共同保证；没有引擎请求
离开发送进程，所以不进入 `unknown_quarantined`、不占用容量；若 STOPPING 或终态先赢，则放弃
该转移、保留既有结果。
一旦 HTTP 请求已发出，4xx 仍按 `ENGINE_REJECTED` 保持容量待执行属主结算，两者不可混同。

## 语言

实测（MegaCube DEL-522）：`language="auto"` 返回 200 并自动判定；10 个厂商语言名
（Chinese/English/French/German/Italian/Japanese/Korean/Portuguese/Russian/Spanish）
均返回 200；直接发送裸 BCP47（`zh`、`zh-Hans`、`en-US`、`ja-JP`）一律 400。

网关因此只发送映射后的引擎取值，从不透传原始标签：

- `auto` 原样发送为 `auto`。它是引擎的自动判定哨兵，不是厂商语言名，也不写入 binding 的
  `supported_languages`；
- `zh`/`en`/`fr`/`de`/`it`/`ja`/`ko`/`pt`/`ru`/`es` 映射为对应厂商名，区域子标签按主
  语言归并，如 `zh-Hans` → `Chinese`；
- 其余标签（如 `ar`、`nl`）在命令进入引擎前以 `invalid_execution_request` 拒绝。

## seed 的语义

实测（MegaCube DEL-522）：同一 `seed` 连续三次合成，输出 WAV 的 SHA256 均不同；文本内容
稳定，波形不确定。`seed` 只稳定文本内容，不保证逐字节可复现，不能作为重放、缓存命中或
幂等重放的依据。

Base 克隆请求体不携带 `seed`，本泳道不发送该字段；该结论适用于携带 `seed` 的
VoiceDesign 与文件 TTS 配方。克隆结果的可比性由参考音频、`ref_text` 与 binding 固定，
不能借用 seed 的稳定性假设。

## 参考文本错配的运维语义

`ref_text` 与参考音频是否语义一致，网关无法证明，只校验非空与长度/密度边界。实测
（MegaCube DEL-522）：

- 轻微错配：引擎仍返回 200，但波形相对正确配对显著改变；
- 完全错配：引擎在 6.7–38.5 s 后返回 5xx（日志以 `192/192 codec tokens` 结束），没有音频。

网关把 5xx 归类为 `TRANSPORT_UNKNOWN` 且 `request_may_have_run=true`：它无法区分确定性
输入错误与瞬态引擎故障，也可能已经占用算力。重试同一输入不会成功；运维侧必须修正
`ref_text` 或参考音频配对，或显式改用 x-vector-only，不能按瞬态故障自动重试。轻微错配
返回的音频虽然结构合法，但波形已改变，不能作为克隆质量的证据。

## 引擎格式与采样率

实测（MegaCube DEL-522）引擎的实际接受面：

| 取值 | 实测结果 |
| --- | --- |
| `response_format=wav` | 24 kHz、单声道、16-bit |
| `response_format=pcm` | 裸 PCM_16，无容器头 |
| `response_format=mp3` / `flac` | 可用 |
| `response_format=ogg` | 400 |
| `sample_rate` | 仅 8000 / 24000 |

网关的 Base 克隆泳道固定 `response_format=wav`、24 kHz，不发送 `sample_rate`：

- 策略与交付层只接受 WAV。预览制品必须带可校验的采样时钟与声道，裸 `pcm` 没有可解析的
  头，`mp3`/`flac` 在本泳道没有交付需求；
- 固定 wav 让网关按响应头与 WAV 头双重校验结果，避免把未知编码当作合法音频写入制品；
- 24 kHz 与参考音频的 16/24 kHz 接受面、策略时钟和 WAV 头一致；8000 会降低克隆预览质量
  且没有需求，因此不开放。

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