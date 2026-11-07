# VoiceDesign 节点模板与运行边界

本目录保存 VoiceDesign 变体的节点模板样例与 warmup 请求体；不含模型权重、端点地址、
凭据、binding revision 或容量 manifest。真实部署由节点平台方按获批镜像与制品固定后填入。

## 一个节点只常驻一个 TTS 变体

实测（vLLM-Omni `0.32.0rc1`，`vllm/vllm-omni:latest`，arm64）：VoiceDesign 与
CustomVoice 是两个不同 task 的加载，引擎按 checkpoint 与 `task_type` 校验；两者同时常驻
时第二个实例在 stage1 直接 `CUDA error: out of memory`。因此：

- 模板声明 `variant_slot`（两种 TTS 变体模板取同一槽位，如 `tts-variant`）。控制器在
  prepare 时按冲突拒绝同槽位仍有未 drain 实例的请求（`intent_conflict`，HTTP 409，
  消息指明占用的实例）；该槽位只有在旧实例取得已核验 stop 后才释放。
- 切换变体是「停止旧实例 → 确认释放 → 以新模板 prepare」，不是并行扩容。
- 网关侧同样只配置一条 TTS 变体泳道：同时配置 CustomVoice 与 VoiceDesign 会在启动时失败。

## 启动参数

`template.example.json` 的 `command` 与 `environment` 对应实测启动形态：

- `--gpu-memory-utilization 0.06 --max-model-len 1536 --max-num-seqs 1 --enforce-eager`
  （VoiceDesign 阶段显存占用低，默认利用率会与其它常驻进程争抢）。
- `VLLM_USE_V2_MODEL_RUNNER=0`、`HUMMING_DISABLE_PARALLEL_BUILD=1`：避免 V2 runner 与
  并行构建在本镜像下引入额外初始化路径。
- `VLLM_API_KEY` 由控制器按实例 generation 注入，模板环境变量不能声明或覆盖它；
  控制器会把模板环境变量与实例凭据一起写入容器，并把它作为容器身份的一部分校验。

冷启动加载约 110 s，首个真实请求另需约 60 s 完成图捕获；warmup 预算必须覆盖两者之和。

## 就绪判定

`/health` 返回 200 只证明进程在监听，不能作为就绪证据：镜像在完成图捕获前就会应答健康检查。
模板使用 `http_warmup_ok`，由控制器向 `/v1/audio/speech` 发送
[warmup-request.json](warmup-request.json)（按 `body_sha256` 校验请求体）并只接受 2xx。
网关侧另有独立的就绪探针 `voice_design_warmup`：它按自己的预算
（配置项 `readiness.warmup_timeout_seconds`，默认 330 s）发起一次真实合成并要求得到合法
WAV，因此 `/health` 200 不会让网关报告 ready。

该合成在后台执行，`/readyz` 只返回已结算的结论，不做实时合成：

- 通用 `readiness_timeout_seconds` 只约束其它探针，不约束 warmup；
- 并发就绪检查共享同一次尝试（single-flight），成功结果在进程内永久缓存；
- 失败后按 `readiness.retry_interval_seconds`（默认 30 s）才重试。

后果是冷启动期间 `/readyz` 立即返回 503，而不是阻塞到 60 s 超时；容器的健康检查
（`deploy/compose.yaml` 中 `timeout=2`）因此始终能在 2 s 内拿到答案。部署时把该服务的
`start_period` 调到覆盖冷启动（建议 ≥ 240 s），否则容器会在预热完成前被标记 unhealthy
（不会被重启，但依赖 `service_healthy` 的服务会等待）。

## 释放证据

`POST /v1/omni/sleep` **不是**释放证据：实测返回 `SUCCESS` 且 `freed_bytes=0`，随后推理
超时 300 s。释放只认进程退出或容器停止，并需容器已退出与资源释放确认；网关的
`VoiceDesignStopReceipt` 会拒绝 sleep 回执（`engine_sleep_is_not_release`），控制器仍按
既有 stop 证据规则核验后归还预算。

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
幂等重放的依据。请求省略 `seed` 时网关不发送该字段，由引擎自行决定。

## 参考文本错配的运维语义

VoiceDesign 请求体不含 `ref_text`（设计以 `description` + `preview_text` 表达），本泳道
不产生参考文本配对；该确定性输入错误属于 Base 克隆泳道，详见
[Base 克隆模板](../file-voice-clone/README.md#参考文本错配的运维语义)。运维侧需知：

- 参考文本与参考音频完全错配时，引擎可能在 6.7–38.5 s 后以 5xx 返回，日志以
  `192/192 codec tokens` 结束且没有音频；
- 网关把 5xx 归为 `TRANSPORT_UNKNOWN`（请求可能已执行），无法与瞬态故障区分；
- 重试同一输入不会成功，必须修正参考文本或改用 x-vector-only，不能按瞬态故障重试。

## 引擎格式与采样率

实测（MegaCube DEL-522）引擎的实际接受面：

| 取值 | 实测结果 |
| --- | --- |
| `response_format=wav` | 24 kHz、单声道、16-bit |
| `response_format=pcm` | 裸 PCM_16，无容器头 |
| `response_format=mp3` / `flac` | 可用 |
| `response_format=ogg` | 400 |
| `sample_rate` | 仅 8000 / 24000 |

网关的 VoiceDesign 泳道固定 `response_format=wav`、24 kHz，不发送 `sample_rate`：

- 策略只接受 WAV，交付层也只产出 WAV。预览制品必须带可校验的采样时钟与声道，裸 `pcm`
  没有可解析的头，`mp3`/`flac` 在本泳道没有交付需求；
- 固定 wav 让网关按响应头与 WAV 头双重校验结果，避免把未知编码当作合法音频写入制品；
- 24 kHz 与实测可用值一致，且是策略、响应头与 WAV 头必须一致的时钟；8000 会降低预览质量
  且没有需求，因此不开放。

## 部署前必须补齐

- 镜像 digest 与 `runtime-pin.json`：沿用已验证的 `vllm/vllm-omni` 固定记录，模型
  checkpoint/revision 由制品所有者按 VoiceDesign 权重固定后再写入 `artifact-pin.json`。
  本目录不伪造模型摘要。
- `<VOICE_DESIGN_MODEL_ROOT>`、只读挂载源与容器内存上限按节点实际路径与显存核定。
- `readiness.warmup.timeout_seconds` 需 ≥ 冷启动 170 s 与重试余量。