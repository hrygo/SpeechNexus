---
title: "音频资产检查的独立检查容器"
status: active
version: "1.0.0"
date: 2026-11-12
---

# 音频资产检查的独立检查容器

本文规定公开 `POST /v1/nexus/assets` 的音频检查（`asr_input` 与 `voice_reference` 两种用途）如何由一个**独立、无出站网络、非特权**的检查容器完成，以及 Speech API 与它之间的调用协议、输入传递、安全边界、容量语义、生命周期、配置、验收与迁移。API 侧已按本页装配 HTTP 适配器并完成失败投影；检查容器镜像（C2）、[Compose 装配](#44-compose-装配)（C4）与旧进程路径退役（C6）已交付。真实部署验收（C5，§8 A1–A7）尚未完成，因此本页不声明公开音频检查已通过部署验收。旧进程监督器（`audio_asset_inspector.py`、`audio_inspection_process.py` 与固定 launcher `_audio_inspection_launcher.py`）已删除，`audio` 路径只有 HTTP 适配器一种实现。交付状态以[跟踪矩阵](../plans/delivery-tracking.md)为准。

四条不变量，任一不成立都不得声明可用：

1. 音频字节只出现在两条链路上：客户端 → API，API → 检查容器。检查容器没有任何出站网络，不参与内容持久化。
2. 检查容器不写 PostgreSQL、不写对象存储、不发布 Asset、不判定 `ready`。它只返回可被 API 复核的解码事实。
3. 公开 `ready` 只能由 API 在完成回执校验、PG 事务和对象校验后产生。
4. `SPEECHNEXUS_ASSET_INSPECTOR=audio` 时不存在文本降级路径；检查器缺失、超时、超限或崩溃都是显式不可用（`503`）或显式拒绝（`422`）。

## 1. 边界与职责

### 1.1 检查容器

部署单元名 `asset-inspector`，是独立的工作区成员与独立镜像。单一职责：把一段不可信的编码音频变成可复核的媒体事实，或明确说它不可接受。

| 职责 | 说明 |
| --- | --- |
| 解码与媒体事实 | 按固定配方 `pcm-decode-1` 完整解码，产出采样率、声道、源样本数、时长、编码字节数与摘要 |
| 格式准入 | 只接受 `audio/wav`。raw PCM 需要调用方声明的采样率/声道，公开上传契约不携带该声明，因此不在本形态范围内 |
| 输入边界 | 单请求编码字节上限、解码时长上限、墙钟期限、输出字节上限，全部由容器自身配置固定，不接受客户端指定端点、命令、路径或环境 |
| 并发与拒绝 | 有界并发槽；无排队；超限立即拒绝并让调用方重试 |
| 单请求隔离 | 每个请求一个新子进程、一份 tmpfs 私有输入、独立期限；子进程退出或被杀死后才释放槽位 |

检查容器**不**做的事：不读对象存储，不连 PostgreSQL，不持有 Asset/Job/租约/预算身份，不写任何持久状态，不产生 `ready`、`rejected`、事件或审计事实，不向部署网络之外发起任何连接。

### 1.2 Speech API

| 职责 | 说明 |
| --- | --- |
| 入口边界 | 接收 multipart、限制并发与字节数、按 `asset_max_bytes` 私有缓冲并计算 SHA-256（`backend/src/speechnexus_backend/speech/interfaces/http/asset_ingress.py:28`） |
| 调用 | 用同一份字节向检查容器发一次请求，携带期限与时长上限；只尝试一次，不自动重试 |
| 事实判定 | 不信任回执自述：重新核对摘要、字节数、媒体类型、采样时钟与时长上限（复用 `backend/src/speechnexus_backend/speech/infrastructure/audio_inspection_http.py:281` 的判定） |
| 持久化 | 校验通过后才进入既有 `reserve → begin_write → PUT → publish` 路径（`backend/src/speechnexus_backend/speech/application/assets.py:179`） |
| 错误投影 | 把检查器的私有状态映射为公开错误；不可用与永久拒绝严格分开 |

API 不解码、不解析 WAV 头、不做格式嗅探，也不把客户端声明的文件名或 MIME 当作依据；它只搬运字节并复核回执。

`AssetInspector` 端口的签名不变（`backend/src/speechnexus_backend/speech/application/assets.py:83`），替换的是它的实现：从「本机子进程监督」换成「私有 HTTP 客户端」。端口仍只接收 `BinaryIO`、`AssetPurpose` 与 `AssetPolicy`，返回 `Inspection`。

### 1.3 结果如何回到 API

```
client ──multipart──▶ API: 私有缓冲(≤4 MiB) + sha256
                        │
                        ├──POST /internal/v1/inspection/audio (bytes, deadline)──▶ inspector
                        │                                                          │ prlimit → decoder(新子进程)
                        │◀──────────── 200 + AudioInspectionSuccess ───────────────┘
                        │
                        ▼ 摘要/字节/时钟/时长复核
                    Inspection(facts=MediaFacts(...))  ── 或 ──  AssetError / Inspection(available=False)
                        │
                        ▼
                 PG reserve(reserved) → S3 PUT(版本固定) → publish(ready)
```

回传的只有**计数与摘要**，没有音频、转写或任何可还原内容：

| 字段 | 用途 | 产生方 |
| --- | --- | --- |
| `sha256` | 绑定本次请求的原始字节，防止串请求或替换内容 | 解码器对实际输入重新计算，API 用自己算的摘要核对 |
| `encoded_bytes` | 绑定字节数 | 同上 |
| `media_type` | 只允许 `audio/wav` | 固定配方 |
| `sample_rate_hz` / `channels` / `source_samples` / `duration_ms` | 进入 `MediaFacts`，并满足 `speech_assets` 对 `ready` 音频的列约束（`backend/src/speechnexus_backend/speech/infrastructure/asset_schema.py:47`） | 完整解码 |
| `schema_version` / `recipe_version` | 契约与配方版本 | 固定常量 |

`AudioInspectionSuccess` 已经是冻结的内部契约（`contracts/internal/audio-inspection.schema.json`，入口选择见 `contracts/bindings.json:252`，生成消费者见 `packages/contracts/src/speechnexus_contracts/audio_inspection.py:10`）。本设计**不新增回执字段**：检查容器返回的就是该契约的 JSON，API 用现有 `parse("AudioInspectionSuccess", ...)` 校验。

**明确禁止的内容通道**（任何一条都会破坏集群同构与数据权威）：

- 跨容器的 Unix socket、共享内存、命名管道或本地回环连接；
- 跨容器共享卷、宿主目录、`docker.sock` 或任何容器管理权限；
- 通过 PostgreSQL 大字段、outbox 或事件表搬运音频；
- 任何离开部署网络的第三方端点、遥测、错误上报或代理。

## 2. 调用协议

### 2.1 形态

| 项 | 规定 |
| --- | --- |
| 传输 | HTTP/1.1 over TCP，仅部署内网（见 §4.1 的网络项）；不引入消息队列、gRPC 或长连接会话 |
| 端点 | `POST /internal/v1/inspection/audio` |
| 粒度 | 一次请求 = 一次检查。无跨请求状态，无会话，无 job 身份 |
| 认证 | `Authorization: Bearer <token>`，token 由部署注入；常量时间比较；缺失或错误返回 `401` |
| 幂等 | 纯函数式：同样字节 + 同样期限产生同一事实。协议层不提供幂等键；API 侧只尝试一次 |
| 连接 | 允许 keep-alive；连接池上限等于 API 单进程上传并发上限（4），不做 HTTP/2 |
| 调用线程 | 同步调用，运行在 Starlette 线程池中（`backend/src/speechnexus_backend/speech/interfaces/http/assets.py:107`），永不进入事件循环 |

### 2.2 请求

```
POST /internal/v1/inspection/audio HTTP/1.1
Authorization: Bearer <secret>
Content-Type: application/octet-stream
Content-Length: <n>                 # 必填，且必须与实际字节数一致
X-Nexus-Deadline-Ms: <n>            # 解码墙钟预算，检查器按自身上限夹取
X-Nexus-Max-Duration-Ms: <n>        # 媒体时长上限，检查器按自身上限夹取
X-Request-Id: <opaque>              # 仅用于日志关联，不含内容
<n bytes>
```

请求体就是原始编码音频，没有 JSON 包装：多一层 base64 会放大约 33% 且无收益。请求不携带文件名、客户端 MIME、workspace、purpose、幂等键或资产身份：检查器不需要这些信息，少给一项就少一处可被记录或误用的内容。两种音频用途共用同一配方与容器，`purpose` 只体现为 API 下发的媒体时长上限：`asr_input` 用 ASR 解码预算，`voice_reference` 用独立的参考音频上限。所有策略值都是**预算上界**，不是权限：检查器把自己配置的上限作为硬上限，客户端只能收紧。

### 2.3 成功响应

`200 OK`，`Content-Type: application/json`，body 为 `AudioInspectionSuccess`，长度上限取检查器 `max_receipt_bytes` 与 API `SPEECHNEXUS_ASSET_INSPECTION_MAX_RESPONSE_BYTES` 的较小者（默认均为 4096）。API 在读取时按该上限截断，超限即视为不可用（不解析部分内容）。

### 2.4 失败分类

私有响应统一为 `{"schema_version":1,"status":"<status>"}`，不含 vendor 消息、路径、命令或输入片段。

| HTTP | status | 含义 | API 侧映射 |
| --- | --- | --- | --- |
| 200 | — | 回执 | 复核通过 → `Inspection(facts=…)`；复核不通过 → `503 asset_inspection_unavailable` |
| 400 | `bad_request` | `Content-Length` 缺失/不符、头非法 | `503`（内部契约违例，不暴露） |
| 401 | `unauthorized` | token 缺失或错误 | `503`（配置错误） |
| 413 | `encoded_too_large` | 超过检查器字节上限 | `503`（两侧上限不一致，是配置问题，不是内容拒绝） |
| 415 | `unsupported_media` | 配方不接受该媒体类型 | `AssetError("invalid_asset_audio")` → 公开 `422`，**不落库** |
| 422 | `invalid_media` | 完整读取后判定不是合格 WAV（截断、RIFF 不一致、块结构异常、非 PCM） | `AssetError("invalid_asset_audio")` → 公开 `422`，**不落库** |
| 422 | `media_limit_exceeded` | 时长或解码预算被媒体本身超出（期限未到期） | `AssetError("asset_audio_too_long")` → 公开 `422`，**不落库** |
| 429 | `busy` | 并发槽已满（无排队） | `503 asset_inspection_busy`（可重试） |
| 503 | `unavailable` | 服务未就绪、自检失败、子进程异常 | `503 asset_inspection_busy`（可重试） |
| 504 | `deadline_exceeded` | 墙钟期限到，子进程已被杀死 | `503 asset_inspection_busy`（可重试） |

同一个 `media_limit_exceeded` 可能来自字节/时长上限，也可能来自解码器自身的协作式超时。检查器按自己的墙钟是否到期区分：已到期记 `504`，未到期记 `422`。

`413` 与 `415` 的内容归属不同。公开入口已按 `asset_max_bytes + 16 KiB` 先拒绝超长 body，字节数在到达检查器之前就由本进程约束，因此检查器返回 `413` 只可能表示两侧字节上限不一致——那是部署配置问题。媒体类型不经公开入口过滤：调用方的 MP3 或文本会原样送到检查器，`415` 描述的是这份文件本身，重试同一份字节不会成功，因此它与 `invalid_media` 同属内容判决，回公开 `422 invalid_asset_audio`。

无法连接、连接复位、连接/读超时、响应超限、JSON 非法、回执字段不合法，一律 `503 asset_inspection_unavailable`（不可重试）。这些失败**不**产生 `rejected` 行，也**不**被解释成内容非法。传输层失败与「检查器明确答复」严格分开：只有检查器自己回的 `busy`/`unavailable`/`deadline_exceeded` 才是可重试的 `asset_inspection_busy`，因为那证明服务活着；连不上或响应中途断开的端点与「根本没配置」不可区分，其最常见原因是配置错误或进程不在，报可重试会把死配置伪装成容量并导致永久重试。

`503` 与 `422` 的区分是设计约束，不是文案偏好：把基础设施故障说成内容非法，会让调用方删除合法素材；把内容非法说成暂时不可用，会让调用方无限重试同一份坏数据。

### 2.5 超时与取消

| 时钟 | 归属 | 值 | 行为 |
| --- | --- | --- | --- |
| 客户端预算 `SPEECHNEXUS_ASSET_INSPECTION_TIMEOUT_SECONDS` | API | 默认 15 s | 覆盖连接、写入、等待与读取；到期即放弃并返回 `503` |
| 解码预算 `SPEECHNEXUS_AUDIO_INSPECTION_WALL_SECONDS` | API 下发给检查器 | 默认 10 s | 检查器在该时刻 SIGKILL 子进程 |
| 检查器硬上限 `SPEECHNEXUS_INSPECTION_MAX_WALL_SECONDS` | 检查器 | 默认 20 s | 夹取客户端预算，防止被调用方放宽 |
| 单请求解码时限 `--timeout-seconds` | 解码器自身 | = 夹取后的预算 | 协作式检查，超时抛 `media_limit_exceeded` |

装配期强制 `WALL_SECONDS + 2 ≤ TIMEOUT_SECONDS`：客户端预算必须晚于解码预算到期，否则 API 会在检查器仍可能返回成功时放弃；两者相等又会让「谁先到期」不可判定。

- **API 超时**：立即返回 `503`；检查器仍可能在自身期限内继续解码，最坏多做 `WALL_SECONDS` 的无效工作，不写任何状态，槽位在子进程死亡后释放。协议不做「取消消息」，因为检查没有副作用，取消只影响资源占用。
- **客户端断开**：请求体未读完时，检查器读到短读，**不启动解码**，不产生回执；请求体已读完后，ASGI 层不保证可观测断开，因此取消的确定性边界是「检查器期限」而不是「断开时刻」。该语义写入验收（§8 A5），不宣称立即取消。
- **API 侧协作式取消**：`AssetInspector` 实现保留 `cancelled` 回调并在发送前、收到回执后检查；公开上传路径当前没有可用的取消信号源（`backend/src/speechnexus_backend/speech/infrastructure/audio_inspection_http.py:123` 的默认值即 `lambda: False`），本设计不改变这一点，只保留接口。

## 3. 输入传递

### 3.1 采用：API 单次转发

API 已经把上传内容完整收进私有、非链接的临时文件（`backend/src/speechnexus_backend/speech/application/assets.py:117`），并算好摘要；直接把同一份字节写入检查容器请求体即可。

成立条件：

- 内容只经部署内网；检查容器无出站网络，因此不存在把内容带出部署的路径；
- 检查器在读取时按 `Content-Length` 与实际字节数双重核对，短读立即失败，不进入解码；
- 回执的 `sha256`/`encoded_bytes` 由解码器对**实际收到的字节**重新计算，API 再与自己那份比对，因此传输层无法替换内容而不被发现；
- 上限是 `asset_max_bytes`（默认 4 MiB，`backend/src/speechnexus_backend/settings.py:52`），一次请求的内存与带宽开销有界。

失败模式：

| 情形 | 结果 |
| --- | --- |
| 检查容器未启动 | 连接拒绝，`503`，无行 |
| 容器中途崩溃 | 连接复位，`503`，无行；容器重启后无残留状态 |
| API 超时 | `503`；检查器按自身期限收尾 |
| 字节数与声明不符 | `400 bad_request` → `503`；不解码 |
| 网络抖动 | 无自动重试；调用方以新请求重试（无副作用，安全） |

### 3.2 备选：对象存储预签名读取（不采用）

做法：API 先把原始字节写入对象存储，再给检查器一个短期预签名 GET。

不采用的理由是它与现有发布不变量冲突，而不是实现成本：

1. 当前顺序是「先检查、后预留、后 PUT」：`inspect()` 在 `reserve()` 之前调用（`backend/src/speechnexus_backend/speech/application/assets.py:177`），检查失败时**一行都不写**。预签名读取要求对象先存在，等于把对象写入提前到 Asset 预留之前，破坏「`reserved` 行存在才证明没有授权过外部写入」这一不变量（见[生产资产发布单元](production-assets.md)）。
2. 需要新的检查前暂存前缀、独立账本与清理路径；否则崩溃会留下无人认领的业务内容副本，与内容删除账本（`docs/architecture/data-deletion.md`）的「预检—账本—固定版本」模型不一致。
3. 检查容器要么持有对象存储凭据（扩大最小权限面），要么接收预签名 URL（把 bearer 凭据放进请求与日志）。
4. 检查容器必须能访问对象存储，于是无法放在完全无出站的网络上，§4.1 的无出站网络形态不再成立。

成立条件（供未来评估，不属于本形态）：只用于单个请求超过约 8 MiB 的规模，并同时具备检查前暂存前缀、写入账本、失败清理与「检查容器只读单个固定版本」的权限模型。当前 4 MiB 上限使该优化没有收益。

### 3.3 禁止的通道

跨容器共享卷、宿主目录、Unix socket、共享内存、`docker.sock`、PG 大字段、outbox、以及任何部署网络之外的端点。它们要么把业务内容落到第二个持久位置，要么把本地 IPC 当成跨进程协调，两者都被[仓库工作约定](../../AGENTS.md)的「集群同构」与「数据权威」排除。

## 4. 安全边界

### 4.1 容器加固

| 边界 | 配置 | 说明 |
| --- | --- | --- |
| 用户 | 固定非 root UID/GID，镜像内文件只读 | 不需要任何宿主 UID 映射 |
| 根文件系统 | `read_only: true` | 镜像之外无写入点 |
| 可写区 | `tmpfs /run/inspection`（默认 64 MiB）、`tmpfs /tmp`（16 MiB） | 每请求私有目录，退出即回收；tmpfs 页计入容器内存 |
| 能力 | `cap_drop: [ALL]`，不 `cap_add` | 有效能力为 0 |
| 提权 | `security_opt: [no-new-privileges:true]` | 禁止 setuid/文件能力提权 |
| seccomp | 保持运行时默认 profile | 不设 `seccomp=unconfined` |
| AppArmor | 保持默认 profile | 不设 `apparmor=unconfined` |
| 进程数 | `pids_limit`（默认 64） | 替代旧方案的「禁止 fork/thread」seccomp 过滤器 |
| 内存 / CPU | `mem_limit`（默认 768 MiB）、`cpus`（默认 2.0） | cgroup 级聚合上限；另加每子进程 `prlimit` |
| 网络 | 仅接入专用 `internal: true` 网络，不发布宿主端口 | 无默认网关，无出站 |
| 挂载 | 无 volume、无 bind mount、不挂 `docker.sock` | 无共享目录 |
| 进程 1 | `init: true` | 回收被 `prlimit`/解码器遗留的僵尸 |
| 重启 | `restart: unless-stopped` | 崩溃即重建，状态全在 tmpfs，无需恢复 |
| 期限 | `stop_grace_period` ≥ 最大墙钟 + 2 s | 优雅退出等待在途请求 |

单请求内部还有一层：新子进程、`prlimit --as/--cpu/--fsize/--nofile/--core`、独立期限、stdout 上限 `output_bytes`、stderr 丢弃。cgroup 限制是容器级总量，`prlimit` 是子进程级上限，两者不互相替代。

子进程环境把数值库线程数固定为 1（`OPENBLAS_NUM_THREADS`、`OMP_NUM_THREADS`、`MKL_NUM_THREADS`）。`--as` 是按子进程固定的地址空间，而 BLAS 默认按核数开线程池；不锁线程时，同一份 256 MiB 在大核数机器上会让 `import numpy` 直接分配失败（`Memory allocation still failed after 10 retries`），小核数机器看不出差异。单元测试在真实 `prlimit` 下导入 `av`、`numpy` 与 `speechnexus_media.inspection` 并解码内置 5 ms WAV，作为该包络的回归证据。

### 4.2 为什么不需要 `CAP_SYS_ADMIN`，也不再需要 bwrap

旧形态里，bwrap 必须在**容器内部**再创建一次 user/mount/PID 命名空间。`DEL-524` 的实测已经定位失败根因：api 容器 `CapEff=0`、`NoNewPrivs=1`、`Seccomp=2`、`CapDrop=[ALL]`，而 moby 默认 seccomp profile 只在具备 `CAP_SYS_ADMIN` 时放行 `unshare`/`mount`/`umount2`/`setns`/`sethostname`，`pivot_root` 直接返回 `ERRNO`；因此 `bwrap --unshare-user` 必然失败，`apparmor=unconfined` 与一次性容器都不能改变这一点。

新形态把命名空间创建从「进程内」移到「容器创建时」：

| 旧路径要进程自己做的事 | 新形态由谁完成 |
| --- | --- |
| `unshare(CLONE_NEWUSER/NEWNS/NEWPID/NEWNET)` | 容器运行时在 `clone` 时创建，进程不调用 |
| `mount`/`pivot_root` 组装只读根 | 运行时准备只读 rootfs 与 tmpfs 挂载 |
| 丢能力、禁 setuid、禁新 userns | `cap_drop: ALL`、`no-new-privileges`、默认 seccomp |
| 网络隔离 | 专用 `internal` 网络 |
| 资源上限 | cgroup + `prlimit` |

于是检查容器内的进程一次都不调用 `unshare`/`mount`/`pivot_root`/`setns`，`CAP_SYS_ADMIN` 不再是被需要的权限；`--privileged`、`cap_add`、`seccomp=unconfined`、`apparmor=unconfined` 以及宿主 `kernel.apparmor_restrict_unprivileged_userns` 的调整都不在方案内。

旧监督器里为 bwrap 服务的机制（pidfd 身份传递、固定 launcher、guardian、进程内 seccomp 过滤器、`_audio_inspection_launcher.py`）已随旧路径一并删除。它们存在的目的曾是「在 wrapper 可能撒谎时证明真实 worker 已退出」；现在子进程就是解码器本身，`Popen` 的 PID 即真实身份，`SIGKILL` + `wait` 是可验证的退出证据。容器级兜底更强：子进程在 1 s 内无法回收时，服务进程主动 `exit(非 0)`，容器重启后整个 PID 命名空间被销毁，不存在存活进程。

### 4.3 残余风险

- 解码器（FFmpeg/PyAV）仍处理不可信字节，存在内存破坏面。利用成功后攻击者得到一个无能力、无出站网络、只读根、非 root、有资源上限的进程；这是本方案明确接受的边界，且比「与 API 同容器」的旧形态更小。
- 共享 bearer token 只授权「提交一次检查」，不授予任何其他能力；网络内只有 API 与检查器两个成员，token 用于阻断误连与他人加入网络后的横向调用。token 通过环境注入，不写日志、不进错误消息。
- 检查器不做内容分级、不做病毒扫描、不做转写；它不解码就不产生任何业务结论。

### 4.4 Compose 装配

`deploy/compose.yaml` 以 opt-in profile 装配检查容器：默认 `up` 不启动它，基础服务列表、宿主端口映射与镜像构建入口都不变。

| 项 | 装配事实 |
| --- | --- |
| 服务 / profile | 服务名 `asset-inspector`，`profiles: ["asset-inspection"]`；启动命令为 `docker compose --env-file deploy/compose.env -f deploy/compose.yaml --profile asset-inspection up -d asset-inspector` |
| 构建 | `build.context: ..`（仓库根，与其它服务一致）、`dockerfile: asset-inspector/Dockerfile`；不挂载私有配置目录 |
| 网络 | 只接入专用网络 `asset-inspection`（`internal: true`，无默认网关）；该网络成员仅有 `api` 与 `asset-inspector`，需要出网的 postgres、minio、gateway 都不在其中 |
| 端口暴露 | 不写 `ports`，不发布任何宿主端口；API 侧唯一入口是 `http://asset-inspector:8080`，8080 由镜像 `EXPOSE` 声明 |
| 挂载 | 无 volume、无 bind mount、不挂 `docker.sock`；可写区仅 tmpfs `/run/inspection`（64 MiB）与 `/tmp`（16 MiB），属主 `10001:10001`、`mode=0700` |
| 加固 | `read_only`、`cap_drop: [ALL]`、`no-new-privileges`、`pids_limit: 64`、`mem_limit: 768m`、`cpus: 2.0`、`init`、`stop_grace_period: 30s`；不加 `cap_add`，不设 `seccomp=unconfined`/`apparmor=unconfined`，`user` 沿用镜像的 `10001:10001` |
| 健康检查 | 镜像内 `GET /healthz`（未鉴权、不写盘），`interval 5s`、`timeout 3s`、`retries 20`、`start_period 5s`；`/readyz` 仍由启动自检控制，不纳入 Compose 探针 |
| 鉴权 token | 唯一来源是部署文件中的 `SPEECHNEXUS_ASSET_INSPECTION_TOKEN`，同一值分别注入 API 的 `SPEECHNEXUS_ASSET_INSPECTION_TOKEN` 与本容器的 `SPEECHNEXUS_INSPECTION_TOKEN`；仓库内无字面默认值，示例文件留空。selector、URL、token 三键必须同时设置 |
| API 装配 | `SPEECHNEXUS_ASSET_INSPECTOR` 默认 `text`；切到 `audio` 需同时设置 `SPEECHNEXUS_ASSET_INSPECTION_URL=http://asset-inspector:8080` 与该 token |
| 上线前检查 | `make stack-config` 校验基础配置、`job-dispatch` 与 `asset-inspection` 三个 profile 均可解析；默认 `config --services` 仍为 `api gateway minio minio-init postgres` |

`api` 不建立 `depends_on: asset-inspector`。启动期校验的对象是配置（URL 形态、token 是否存在、预算关系），不是可达性：检查器停机时 API 照常启动，`asr_input`/`voice_reference` 在请求期返回 `503 asset_inspection_unavailable`，文本资产、健康检查与保留期维护路径不受影响（§8 A2/A6）。反向的硬 `service_healthy` 门控会把检查器故障升级为 API 全量不可用，让不需要解码能力的路径一起不可用，因此不采用。

token 的强制点同理不在 Compose 层：Compose 对所有服务（含未启用 profile 的服务）做插值，`${VAR:?}` 会让从不启用该 profile 的部署在 `config`/`up` 阶段失败；给示例文件填占位符又会让 `text` 模式启动失败（「配置了 token 但选择器是 `text`」是防降级校验，见 §7.3）。因此 Compose 层保留空白默认值，强制点在启动期，且**空值与全空白值一律按未配置处理**：检查器进程以自己的变量名退出，`audio` 选择下 API 在装配检查器时以 `SPEECHNEXUS_ASSET_INSPECTION_TOKEN` 拒绝启动（`backend/src/speechnexus_backend/asset_runtime.py` 的 `_build_http_audio_inspector`）。该检查刻意不依赖 `Settings` 中「空串归一为 `None`」的共享校验器——那个校验器同时服务二十多个无关字段，一旦收窄，全空白 token 会让进程启动成功、每请求发出非法的 `Authorization` 头并被报成运行期 503；装配点的本地判空保证错误始终停在启动期。与 `SPEECHNEXUS_JOB_DISPATCH_DATABASE_URL` 留空即失败的形态一致。

## 5. 容量与并发

| 参数 | 默认 | 依据 |
| --- | --- | --- |
| 并发槽 `SPEECHNEXUS_INSPECTION_CONCURRENCY` | 2 | API 单进程上传并发上限为 4（`backend/src/speechnexus_backend/speech/interfaces/http/asset_ingress.py:28`）；2 槽覆盖常见单请求节奏；4 个上传同时到达时其中 2 个立即 429，而不是挂起等 10 s |
| 单子进程地址空间 | 256 MiB | 与旧默认一致（`backend/src/speechnexus_backend/settings.py:69`） |
| 输入上限 | `min(SPEECHNEXUS_ASSET_MAX_BYTES, SPEECHNEXUS_AUDIO_INSPECTION_MAX_INPUT_BYTES)` = 4 MiB | API 先按自己的上限拒绝 |
| 容器内存 | 768 MiB | 2 × 256 MiB 地址空间 + 2 × 4 MiB 输入 + 运行时开销，留余量 |
| 容器 CPU | 2.0 | 2 槽、单线程解码 |
| `pids_limit` | 64 | 每请求 1 个子进程 + 服务进程线程 |
| 单请求 CPU 秒 | 5 s | 现有默认 |
| 单请求输出 | 4096 B | 回执上限 |

排队策略：**无队列**。槽位用非阻塞方式获取；拿不到立即 `429 busy`，检查器不维护等待队列，也不把请求挂在连接上等槽位。理由是有界拒绝比无界等待更可预测，且检查没有副作用，调用方用新请求重试即可。

拒绝语义：

| 场景 | 私有响应 | 公开响应 | 是否可重试 |
| --- | --- | --- | --- |
| 并发槽满 | `429 busy` | `503 asset_inspection_unavailable` | 是：新请求，稍后重试 |
| 容器不可用/未就绪 | `503 unavailable` | 同上 | 是 |
| 期限到 | `504 deadline_exceeded` | 同上 | 是 |
| 内容非法 | `422 invalid_media` | `422 invalid_asset_audio` | 否 |
| 时长超限 | `422 media_limit_exceeded` | `422 asset_audio_too_long` | 否 |

公开信封目前对所有 Asset 错误固定 `retryable:false` 与 `X-Should-Retry:false`（`backend/src/speechnexus_backend/speech/interfaces/http/asset_errors.py:83`、`asset_errors.py:68`）。这与本设计的「不可用可重试」冲突，必须在该类错误上改为可重试；由于改动触及公开错误语义，单独列为契约卡（§10 C1），不随实现偷偷改变。

**不可用即不可用**：检查器不可用时，`asr_input` 上传返回 `503`，不产生 `staging`/`reserved`/`rejected` 行，不创建 Job，也绝不交给文本检查器；`text_input` 的上传不受影响。

## 6. 生命周期

| 阶段 | 行为 |
| --- | --- |
| 启动 | 服务进程不依赖 PG/对象存储；先绑定内部端口，再执行一次自检（解码镜像内自带的 5 ms 合成 WAV 并核对回执），通过后 `/readyz` 变 200，失败则进程退出并由 `restart` 重建 |
| 就绪 | `GET /healthz`：进程存活；`GET /readyz`：自检通过且返回 `contract_version`、`recipe_version`、`max_encoded_bytes`、`max_wall_seconds`、`in_flight` |
| 处理中 | 每请求一个子进程；服务进程不导入 PyAV，解码器崩溃不会带走服务进程 |
| 停止 | SIGTERM → 停止接受新请求 → 等待在途请求至多 `stop_grace_period` → SIGKILL 剩余子进程 → 退出；tmpfs 随容器销毁 |
| 崩溃 | 容器重建；无持久状态需要对账。若子进程在 1 s 内无法回收，服务进程主动退出以触发重建，用命名空间销毁换取确定停止 |
| 版本 | 回执携带 `schema_version`/`recipe_version`；不匹配即 `503`，不做兼容降级 |

API 侧衔接：

- `AssetRuntime.close()` 现有顺序是「先关检查器监督器，再关对象存储与连接池」（`backend/src/speechnexus_backend/asset_runtime.py:269`），目的有二：让关闭标志在传输仍在时生效，使新检查无法进入正在拆除的运行时；并让唯一可能「未确认停止」的步骤先报告。新形态保持该顺序与返回值语义：
  - `close()` 置关闭标志、拒绝新的检查、关闭 HTTP 客户端连接池；
  - 返回值 `True`，因为不存在需要确认停止的本地进程；检查容器由部署单元自己管理，API 不声称停止过它；
  - 在途检查运行在线程池中，关闭后其调用失败即返回 `503`；由于检查发生在任何 PG 事务与对象写入之前，关闭不会留下半完成状态。
- `open()` 失败路径已经调用 `close()`（`backend/src/speechnexus_backend/asset_runtime.py:266`），新实现必须保证未使用过的客户端也可以安全关闭。
- API 不 `depends_on` 检查器：一个能力的可用性不应该阻塞整个 API 启动；启动时做一次有界（2 s）的能力探测，连不上只记警告，**连上但契约/上限不兼容则启动失败并点名两侧变量**。

## 7. 配置

### 7.1 新增

API 侧：

| 键 | 默认 | 约束 |
| --- | --- | --- |
| `SPEECHNEXUS_ASSET_INSPECTION_URL` | 无 | `audio` 时必填；必须是 `http(s)` 绝对 URL；不提供 `localhost` 或服务发现的隐式回退 |
| `SPEECHNEXUS_ASSET_INSPECTION_TOKEN` | 无 | `audio` 时必填；空值与全空白值都按缺失处理并在启动期报错；`SecretStr`，不落日志 |
| `SPEECHNEXUS_ASSET_INSPECTION_TIMEOUT_SECONDS` | 15 | `1..60`，且 ≥ `AUDIO_INSPECTION_WALL_SECONDS + 2` |
| `SPEECHNEXUS_ASSET_INSPECTION_MAX_RESPONSE_BYTES` | 4096 | 回执读取上限 |

检查器侧（独立前缀 `SPEECHNEXUS_INSPECTION_`）：`HOST`、`PORT`、`TOKEN`、`CONCURRENCY`（1–4）、`MAX_ENCODED_BYTES`、`MAX_DURATION_MS`、`MAX_WALL_SECONDS`、`MEMORY_BYTES`、`CPU_SECONDS`、`OUTPUT_BYTES`、`MAX_RECEIPT_BYTES`。这些键只出现在检查器容器，API 不需要也不读取它们。

### 7.2 保留

| 键 | 保留原因 |
| --- | --- |
| `SPEECHNEXUS_ASSET_INSPECTOR` | 选择器语义不变：`text`（默认）| `audio` |
| `SPEECHNEXUS_AUDIO_INSPECTION_MAX_DURATION_MS` | `asr_input` 的判定时长上限，同时作为请求预算下发 |
| `SPEECHNEXUS_AUDIO_INSPECTION_MAX_REFERENCE_DURATION_MS` | `voice_reference` 的判定时长上限，同时作为该用途的请求预算下发；默认 60000，取值 1000..60000，即克隆策略接受的参考音频范围 |
| `SPEECHNEXUS_AUDIO_INSPECTION_MAX_INPUT_BYTES` | API 侧字节上限，与 `asset_max_bytes` 取小 |
| `SPEECHNEXUS_AUDIO_INSPECTION_WALL_SECONDS` | 变为下发给检查器的解码预算 |
| `SPEECHNEXUS_AUDIO_INSPECTION_MAX_RECEIPT_BYTES` | 与 `ASSET_INSPECTION_MAX_RESPONSE_BYTES` 取小 |

### 7.3 无消费者的旧键（只用于拒绝）

13 个 `SPEECHNEXUS_AUDIO_INSPECTION_*` 键的去向合计为：8 个无消费者、5 个保留。选择器仍只有 `SPEECHNEXUS_ASSET_INSPECTOR`，不新增取值。

`SPEECHNEXUS_AUDIO_INSPECTION_RUNTIME_ROOT`、`_COMMAND`、`_LAUNCHER_PYTHON`、`_BUBBLEWRAP`、`_PRLIMIT`：镜像挂载点、命令行、解释器与两个宿主工具的路径全部由检查容器镜像与部署固定，API 不再需要知道；旧进程监督器删除后这 5 个字段没有任何消费者。

`_MEMORY_BYTES`、`_CPU_SECONDS`、`_OUTPUT_BYTES` 同样没有消费者：它们迁到检查器的 `SPEECHNEXUS_INSPECTION_*`，语义不变。

这 8 个字段仍声明在 `Settings`，且只用于**拒绝**：前 5 个任一被显式设为非空值、后 3 个任一偏离默认值时，`audio` 与 `text` 都启动失败，错误消息点名旧键并指向 `SPEECHNEXUS_ASSET_INSPECTION_URL`/`_TOKEN` 或检查器侧新键（`backend/src/speechnexus_backend/asset_runtime.py:101` 的 `_refuse_retired_inspector_settings`）。空值（Compose 转发空串）按未配置处理（`backend/src/speechnexus_backend/asset_runtime.py:80` 的既有判定）。删除这些字段会让残留配置被静默忽略，因此它们不随旧路径代码一起删除。

「配了音频前置但选择器是 `text`」的反向校验（`backend/src/speechnexus_backend/asset_runtime.py:200`）覆盖 §7.1 的新增键，防止静默降级。

## 8. 验收条件

全部在真实部署上执行，不是单元测试。前置：目标主机已部署 PG、MinIO、API、Gateway；`deploy/compose.env` 已设 `SPEECHNEXUS_ASSET_INSPECTOR=audio`、§7.1 的新增 API 键，并已按 §9 启动检查器。

```bash
export COMPOSE="docker compose --env-file deploy/compose.env -f deploy/compose.yaml"
export PSQL='psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc'
rows() { $COMPOSE exec -T postgres sh -c "$PSQL \"SELECT count(*) FROM speech.speech_assets\""; }
```

未使用自定义 schema 时上面的 `speech.` 前缀可省略；变更了 `SPEECHNEXUS_DATABASE_SCHEMA` 时按实际值替换。

**A0 默认部署不变。** `SPEECHNEXUS_ASSET_INSPECTOR=text`（默认）下：`$COMPOSE config --services` 仍恰好是 `api gateway minio minio-init postgres`（上线前检查 C9，`scripts/dev-env/release-preflight.sh:782`）；`asr_input` 上传返回 `503 asset_inspection_unavailable`，`rows` 前后相同。

**A1 正向闭环。** 检查器 healthy 后：

```bash
$COMPOSE exec -T postgres sh -c "$PSQL \"SELECT count(*) FROM speech.speech_assets\""
curl -sS -o /tmp/a1.json -w '%{http_code}\n' -X POST http://127.0.0.1:8000/v1/nexus/assets \
  -H "Idempotency-Key: $(python3 -c 'import uuid;print(uuid.uuid4())')" \
  -F purpose=asr_input -F file=@./sample.wav
$COMPOSE exec -T postgres sh -c "$PSQL \"SELECT asset_state, media_type, duration_ms, sample_rate_hz, channels FROM speech.speech_assets ORDER BY created_at_us DESC LIMIT 1\""
```

预期：状态码 `201`，`/tmp/a1.json` 的 `status=ready`，数据库最后一行为 `ready|audio/wav|…` 且三列非空。

**A2 检查器不可用。** `$COMPOSE --profile asset-inspection stop asset-inspector` 后重复 A1：状态码 `503`、`error.code=asset_inspection_unavailable`、`error.retryable=false`、`X-Should-Retry: false`、`rows` 与 `SELECT count(*) FROM speech.speech_asset_commands` 均不变、`SELECT count(*) FROM speech.speech_asset_objects` 不变。不可达是部署故障而不是容量：把它报成可重试会让调用方对死配置永久重试。同一判定适用于 `SPEECHNEXUS_ASSET_INSPECTION_URL` 指向无监听端口的情形。

**A3 畸形音频。** `head -c 8192 /dev/urandom > /tmp/bad.bin`，用它重复 A1：状态码 `422`、`error.code=invalid_asset_audio`，`rows` 不变，无对象、无 `speech_asset_events` 行。

**A3b 参考音频。** 用同一份合格 WAV 以 `-F purpose=voice_reference` 重复 A1：状态码 `201`、`status=ready`、`purpose=voice_reference` 且 `duration_ms`/`sample_rate_hz`/`channels` 非空，声音克隆据此取得参考材料。再用一段超过 `SPEECHNEXUS_AUDIO_INSPECTION_MAX_REFERENCE_DURATION_MS`（默认 60 s）的合成音频重复：状态码 `422`、`error.code=asset_audio_too_long`、`error.retryable=false`、`X-Should-Retry: false`，`rows` 不变。参考音频与 ASR 输入共用检查器，但时长上限是两个独立键。

**A4 超时。** 临时把 API 的 `SPEECHNEXUS_AUDIO_INSPECTION_WALL_SECONDS` 设为 `0.05` 并重建 api，上传约 2 分钟的 WAV：预期 1 s 内返回 `503`，`rows` 不变；`$COMPOSE exec -T asset-inspector ps -eo pid,stat,comm` 只剩服务进程本身（无解码子进程）。若该机器仍能在 50 ms 内解完（极快），改用更小的 `WALL_SECONDS` 或更长的输入。恢复配置并重建。

**A5 取消。** 用同一份文件执行 `timeout 0.15 curl -sS -o /dev/null -X POST …`：请求被中止，`rows` 不变；在 `WALL_SECONDS + 2 s` 内由同网络容器读取 `readyz`，`in_flight` 回到 0：`$COMPOSE exec -T asset-inspector python -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8080/readyz').read().decode())"`，且无残留子进程。该验收同时证明「取消以期限为确定性边界」（§2.5）。

**A6 无降级。** 检查器停止且选择器为 `audio` 时连续上传 3 次：全部 `503`，`SELECT count(*) FROM speech.speech_assets WHERE purpose='asr_input' AND asset_state='ready'` 为 0；日志中不出现 `text` 检查器处理音频的痕迹。

**A7 配置错误显式化。** 故意把 `SPEECHNEXUS_ASSET_INSPECTION_TOKEN` 与检查器不一致后重建 api：每次上传 `503`，检查器日志为中性的 `unauthorized`，无内容、无 token 明文。

## 9. 迁移与回退

### 9.1 迁移步骤

1. 构建检查器镜像并以 `asset-inspection` profile 启动；确认 `/readyz` 200 且返回的契约/配方版本为 `1`/`pcm-decode-1`。（检查器优先启动，API 不做 `depends_on`。）
2. 在 `deploy/compose.env` 加入 §7.1 的 API 键，保留 `SPEECHNEXUS_ASSET_INSPECTOR=audio`，删除 §7.3 的 8 个旧键（或留空）。
3. 重建 api；启动日志应显示能力探测通过。若探测成功但版本或上限不兼容，API 拒绝启动并点名变量。
4. 执行 §8 的 A1–A7，把结果写入[跟踪矩阵](../plans/delivery-tracking.md)对应需求的验证列。
5. 旧进程监督器（`audio_inspection_process.py`、`audio_asset_inspector.py`、`_audio_inspection_launcher.py`）与它们的单测已删除；[旧检查边界](../archive/components/audio-asset-inspection.md) 作为历史记录保留并指向本页。§7.3 的 8 个旧键仍保留在 `Settings` 中，配置了就让启动失败，因此部署侧必须删掉这些变量，不能指望它们被忽略。

数据库无需变更：`speech_assets` 的状态机（`backend/src/speechnexus_backend/speech/infrastructure/asset_schema.py:24`）与音频列约束（`backend/src/speechnexus_backend/speech/infrastructure/asset_schema.py:47`）已经足够；不新增表、不新增列、不新增授权，也不改迁移。

### 9.2 回退到 `text`

1. `$COMPOSE --profile asset-inspection stop asset-inspector`（或整体移除该 profile）。
2. 把 `SPEECHNEXUS_ASSET_INSPECTOR` 改为 `text`，并清空 §7.1 的新增键（任一被显式设置且选择器为 `text` 会导致启动失败，这是有意的防降级校验）。
3. 重建 api。行为回到默认：`asr_input` 返回 `503 asset_inspection_unavailable`，不产生行。
4. 不需要数据修复：回退不删除已 `ready` 的资产，对象与元数据照常可读；也不需要迁移回滚。

回退是能力级回退，不是部署级回退：检查器容器可留在部署里不影响 `text` 路径，但会持续占用少量资源，建议同时停止。

## 10. 工作量与拆卡建议

估算按“一位实现者 + 现有 CI”计。

| 卡 | 内容 | 依赖 | 估算 | 验收点 |
| --- | --- | --- | --- | --- |
| C1 契约冻结 | 在 `contracts/internal/` 冻结检查 HTTP 协议（请求头语义、`200` 复用 `AudioInspectionSuccess`、失败信封与状态词表）；公开错误新增 `invalid_asset_audio`、`asset_audio_too_long`；`asset_inspection_unavailable` 的 `retryable`/`X-Should-Retry` 改为可重试 | 无 | 0.5 d | `make contracts-check`、`uv run pytest tests/contract`、`make contracts-drift` 全绿；正例/负例覆盖 `422`/`503`；路由一致性检查不改动既有错误码语义 |
| C2 检查器服务与镜像 | 新工作区成员 `asset-inspector/`（入口 `speechnexus-asset-inspector`），异步服务 + 每请求子进程 + `prlimit` + 期限 + 并发槽 + `/healthz`/`/readyz` + 自检；独立 Dockerfile，非 root、只读根、最小依赖；新增工作区成员会改动 `uv.lock`，须单独审阅 | C1 | 1.5 d | 合成 WAV 正例、截断/随机字节负例、期限强杀、槽位拒绝、子进程不可回收时进程退出；镜像在 aarch64 构建并通过 `/readyz`；单测只用合成字节 |
| C3 API 适配器 | `AssetInspector` 的 HTTP 实现替换进程实现：一次请求、回执复核、失败分类、`AssetError` 永久拒绝、`close()` 语义、装配期校验与能力探测 | C1（可与 C2 并行） | 1 d | 单测覆盖回执篡改（摘要/字节/时钟/时长/版本）、不可用不落库、永久拒绝不落库、关闭后拒绝新检查；`open()` 失败路径可安全关闭 |
| C4 部署装配 | `deploy/compose.yaml` 的 `asset-inspector` 服务（profile `asset-inspection`）、专用 `internal` 网络、加固与资源上限、`/healthz` 探针、`compose.env.example` 与 `deploy/README.md` 的 profile 说明；事实见 §4.4 | C2 | 0.5 d | `make stack-config`（含 `--profile asset-inspection`）通过；默认 `config --services` 仍为 5 个且不含检查器（C9）；检查器 `Ports` 为空、无 volume、无默认网关（§4.1/§4.4） |
| C5 真实部署验收 | 在 aarch64 参考部署执行 A1–A7，记录命令、输出、镜像摘要与环境 | C2 C3 C4 | 0.5 d | §8 全部通过；证据写入跟踪矩阵 |
| C6 退役旧路径 | 删除 `audio_asset_inspector.py`、`audio_inspection_process.py`、`_audio_inspection_launcher.py` 与只测它们的 5 个单测；内容判决（合格/畸形/超时长）改由 HTTP 适配器接真实解码子进程与 `asset-inspector` 服务单测承担；8 个无消费者的旧键保留为启动拒绝锚点；归档页指向本页 | C5 | 0.5 d | `git grep audio_inspection_process`/`audio_asset_inspector` 在 `backend/src`、`packages`、`tests` 无产品引用；`uv run pytest tests/unit` 无新增失败；旧键仍会让启动失败 |

依赖顺序：`C1 → {C2, C3} → C4 → C5 → C6`。C1 是唯一对外可见的语义变更，必须先行冻结；C2/C3 可并行；C4 依赖 C2 的镜像与服务名；C6 必须等 C5 通过，避免在新路径未验收时失去唯一实现。

## 11. 未验证项

以下事实没有在目标环境实测，实施前必须验证；不要用本页的推断替代实测。

| 项 | 需要什么才能验证 |
| --- | --- |
| Docker 默认 seccomp profile 是否放行 PyAV/FFmpeg 在 aarch64 上的全部系统调用（如 `io_uring_setup` 是否被拒并优雅回退） | 在目标主机以 §4.1 的限制启动镜像，跑 `/readyz` 自检与一次真实检查，观察是否有 `EPERM`/`ENOSYS` |
| 当前锁定的 `av` 版本是否为 linux/aarch64 提供自带 FFmpeg 的 wheel | `docker run --rm <image> python -c "import av; print(av.__version__)"`，并确认无需额外系统库 |
| 目标主机 Docker/内核是否支持 `internal: true` 网络、`pids_limit` 与 `cpus` | `docker version`、`docker compose config`，以及一次实际启动 |
| 所选 ASGI 服务器在请求体读完后是否可观测客户端断开 | 用实际版本做一次断连实验；本设计的取消边界不依赖它，但需要确认不会误判 |
| 单次完整解码（≤4 MiB、约 131 s WAV）在目标主机上的实际墙钟耗时 | 目标主机上重复 10 次计时，用于定档并发与默认期限 |
| `asset_max_bytes`（4 MiB）与检查器 `max_encoded_bytes` 的组合在真实部署上的端到端行为 | §8 A1 与 A4 的实际结果 |
| 公开错误元数据改为可重试对现有 SDK/客户端的影响 | 用 `make contracts-sdk-test` 与一次真实 SDK 调用核对 |
| 检查器镜像的运行依赖闭包（是否需要额外系统库、镜像大小） | 构建后 `docker history`/`ldd` 核对，并记录镜像摘要 |
