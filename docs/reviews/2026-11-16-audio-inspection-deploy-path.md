---
title: "参考部署的音频输入路径：装配事实、部署级证据与缺口"
status: active
version: "1.0.0"
date: 2026-11-16
---

# 参考部署音频输入路径的复核（#589 闭环之后、#572 之前）

本页回答一个问题：**「照仓库文档起一套参考部署，上传一个真实 WAV 得到 `ready`」这条 MVP 主链的第一步，在当前的 `origin/main` 上是缺实现，还是缺验收？** 结论和路径都在本页，实测与未实测严格分开。

核验基线：`origin/main` @ `d6a50691`（2026-11-16，取证时的 head）；文档定稿 rebase 到 `f0038183`（其间两个提交只改 CI 与文档，未触及本页涉及的代码与部署文件）。独立 worktree `/private/tmp/sn869`，本机 Docker Desktop 4.99.0（linux/arm64 容器，Docker 29.9.3，内核 `7.0.14-linuxkit`）。

## 结论

1. **能力已经在仓库里，不是待建项。** 音频检查的部署形态是**独立检查容器 + 私有 HTTP**（`deploy/compose.yaml:293` 的 `asset-inspector`，`profiles: ["asset-inspection"]`），不是 api 容器内的 `bwrap` 子进程。后者已随旧进程检查器一并退役：五个沙箱键在 `deploy/compose.env.example:192-200` 被明确标为 "Unconsumed legacy keys"，配置了会让 API 启动失败并点名替代项（`backend/src/speechnexus_backend/asset_runtime.py:207` 的 `_select_asset_inspector`）。
2. **默认 `text` 是刻意的 fail-closed，不是缺陷。** 选择器默认 `text`（`deploy/compose.yaml:196`、`deploy/compose.env.example:176`），`TextAssetInspector` 对非 `text_input` 一律 `Inspection(available=False)`（`backend/src/speechnexus_backend/speech/application/assets.py:87,92`），`reserve_asset()` 据此在写入任何记录前抛 `asset_inspection_unavailable`（`backend/src/speechnexus_backend/speech/domain/assets.py:277`）。要接收音频必须**同时**满足四件事：`SPEECHNEXUS_ASSET_INSPECTOR=audio`、`SPEECHNEXUS_ASSET_INSPECTION_URL`、非空 `SPEECHNEXUS_ASSET_INSPECTION_TOKEN`、启用 `asset-inspection` profile。缺任一都在启动期点名失败，不存在静默降级（同一函数 `asset_runtime.py:222` 的拒绝分支）。
3. **本轮取得 F-ASSET 音频用途的部署级证据。** 独立 Compose 项目 `sn869`，真实 `postgres:16.15-alpine` + 真实 MinIO + `migrate` 到 head `0054_artifact_deleted_ledger` + 文档 §6 的 18 个非 owner 角色与授权 + 从仓库源码构建的 `api` 与 `asset-inspector`。`POST /v1/nexus/assets`（`purpose=asr_input`，真实合成 WAV 32044 B）→ **`201 Created` / `status=ready` / `duration_ms=1000` / `sample_rate_hz=16000` / `channels=1`**，对象 `storage_version_id` 固定，下载内容 SHA-256 与源文件一致；同一栈把选择器改回 `text` 后同一上传 → **`503 asset_inspection_unavailable`**，行数不变。逐条输出见 §2。
4. **与 runbook §14 的 #680 那轮（判定表记作 B 档）相比，本轮不需要那两处非生产改动。** `--no-deps` 只是命令行范围控制（gateway 未启动是因为本机没有真实引擎，它不在上传路径上）；两份私有配方样例现在能通过生产 loader（`#821` 修好了样例一致性，本页 §2.3 有实测），`sn_api` 也不需要超范围授权。所以判定表里「F-ASSET 音频用途无 A 档」这一格可以按本页证据更新。
5. **真正还没做的只剩验收的剩余项**：§8 的 A4（超时）、A5（取消）、A7（令牌不一致）、A3b 的「超时长参考音频」一半、带 `Origin` 的音频上传，以及 gateway/引擎参与的完整 `up`（那是 `#572` 的范围）。清单见 §3。

## 1. 五个问题

### 1.1 检查器入口是什么，需要哪些依赖，能否在 api 镜像里直接跑

| 项 | 事实 | 出处 |
| --- | --- | --- |
| 工作区成员 | `asset-inspector/`，独立可执行入口 `speechnexus-asset-inspector` | `asset-inspector/pyproject.toml`（`[project.scripts]`）、`asset-inspector/Dockerfile:92` |
| 服务协议 | `GET /healthz`、`GET /readyz`（未冻结的自检端点）、`POST /internal/v1/inspection/audio` | `contracts/internal/asset-inspection.openapi.yaml`、`asset-inspector/README.md` |
| Python 依赖 | `speechnexus-media[audio]`（解码）、`starlette`、`uvicorn`；实际解码依赖是 `av` + `numpy` | `asset-inspector/pyproject.toml`；本轮实测镜像内 `av 19.0.2`、`numpy 2.5.4` |
| 解码实现 | `packages/media` 的 `inspect_audio()` 与独立 CLI `main()`；只接受 `audio/wav` 与显式 PCM | `packages/media/src/speechnexus_media/inspection.py:82,164` |
| 每请求隔离 | 父进程用 `prlimit` 包一个子进程（`--as/--cpu/--fsize/--nofile/--core`），独立期限，stdout 有上限，stderr 丢弃 | `asset-inspector/src/speechnexus_asset_inspector/runner.py:82,111,135` |

**能否在 api 镜像里直接运行？** 依赖层面**能**，架构层面**不采用**，两点都要说清：

- 能：`deploy/images/python.Dockerfile:28` 用 `uv sync --frozen --all-packages --all-extras`，所以 api 镜像里 `av`、`numpy`、`speechnexus_media`、`speechnexus_asset_inspector` 全部存在；本轮实测 `sn869-api:latest` 内 `av 19.0.2` 可导入，`/usr/bin/prlimit` 存在，`bwrap` 不存在、`/run/inspection` 不存在。此前「api 镜像里没有 `av`/`numpy`」的说法是错的（`#589` 2026-11-11 的更正评论已纠正），本页再次证伪。
- 不采用：隔离能力不是来自「镜像里没有解码器」，而是来自检查器容器的运行时边界（见 1.2）。在 api 容器里直接跑解码，等于把不可信字节放进持对象存储凭据、持 PostgreSQL 连接、且必须对宿主暴露端口的进程；这正是旧形态要解决的问题。

### 1.2 bwrap 在容器里能不能用；仓库已批准的沙箱模式是什么

**不能，而且不需要。** 实测证据来自 `#589` 的 `DEL-524` 取证（2026-11-11，MegaCube）：

| 探测 | 结果 |
| --- | --- |
| api 容器 `CapEff` / `NoNewPrivs` / `Seccomp` | `0` / `1` / `2` |
| 容器内 `bwrap --unshare-user …` | exit 1 |
| 加 `apparmor=unconfined` 后重试 | 仍 exit 1 |
| 一次性容器 `apparmor=unconfined` + `unshare --user` | `Operation not permitted` |

根因写在设计页：moby 默认 seccomp profile 只在具备 `CAP_SYS_ADMIN` 时放行 `unshare`/`mount`/`umount2`/`setns`/`sethostname`，`pivot_root` 直接 `ERRNO`（`docs/components/asset-inspection-container.md:222` §4.2）。装 `bwrap`、放行 AppArmor、只读挂载三项加起来仍跑不通——所以放开 `SEccomp`/`privileged` 不是「更省事的等价方案」，而是**换一条更差的安全边界**。

**仓库已批准的沙箱模式**（同文件 §4.1 `:199`）：把命名空间创建从「进程内」移到「容器创建时」，由容器运行时完成；容器内进程一次都不调用 `unshare`/`mount`/`pivot_root`/`setns`，于是 `CAP_SYS_ADMIN` 不再被需要。

本轮在真实部署上核对了这套边界（`docker inspect` + `docker network inspect`）：

| 边界 | 实测值 |
| --- | --- |
| `Privileged` | `false` |
| `CapDrop` | `[ALL]`，无 `cap_add` |
| `ReadonlyRootfs` | `true` |
| `SecurityOpt` | `no-new-privileges:true`（seccomp/AppArmor 保持默认 profile） |
| `PidsLimit` / `Memory` / `NanoCpus` / `Init` | `64` / `805306368`（768 MiB）/ `2000000000`（2.0）/ `true` |
| `PortBindings` / `Mounts` | `{}` / `[]`（不发布宿主端口，无 volume、无 bind mount、无 `docker.sock`） |
| 网络 | 只接 `sn869_asset-inspection`，`internal=true`，成员恰好是该容器与 `api` |

结论：**不需要任何提权**；`privileged: true`、`cap_add`、`seccomp=unconfined`、`apparmor=unconfined`、宿主 userns 调整都不在方案内，本页也不建议任何一项。

### 1.3 `prlimit` 是否随镜像已有

是。检查器镜像显式安装 `util-linux`，并**故意删除** `unshare`/`mount`/`umount`/`nsenter`/`setpriv`（`asset-inspector/Dockerfile:73` 起），运行期要求 `/usr/bin/prlimit` 是普通文件，否则进程退出并点名（`asset-inspector/src/speechnexus_asset_inspector/main.py:15,16,31`）。本轮实测镜像内 `prlimit from util-linux 2.38.1`。

补充事实：api 镜像里也有 `/usr/bin/prlimit`（Debian slim 自带 util-linux），但**没有** `bwrap`——所以即便走旧路径，缺的也不是 prlimit，而是 1.2 里的 seccomp/capabilities 边界。

### 1.4 「approved deployment image」缺的是什么

都不缺实现了，缺的是**验收记录**，而且是特定几项：

| 维度 | 现状 |
| --- | --- |
| 镜像是否存在、是否可复现构建 | 有 `asset-inspector/Dockerfile`（`uv export --frozen` + `--require-hashes` + 非 root、只读根、`HEALTHCHECK NONE`），本轮从仓库源码构建成功（`sn869-asset-inspector:latest` `23a8b257790e`，419 MB），启动自检 `asset inspector self-check passed` |
| 入口是否接上 | 已接：Compose profile（`deploy/compose.yaml:293-294`）、专用 `internal` 网络（`:856-857`）、API 侧三键（`:196-198`）、不建 `depends_on`（`:248` 的注释说明意图） |
| 是否进 CI 构建 | **有独立 workflow**：`.github/workflows/asset-inspector-image.yml`（`#831` 引入）按构建输入路径触发，构建 `asset-inspector/Dockerfile`；它刻意不被 `ci.yml` 调用、也不注册 required check，避免只影响检查器的失败阻塞无关 PR（`#589` 2026-11-12 记录的那条未验证项已经关闭）。本 PR 只改文档，不在该 workflow 的触发路径内 |
| 是否符合「approved」的验收面 | **部分**：§8 的 A0/A1/A2/A3/A3b（前半）本轮在部署上通过；A4/A5/A7、A3b 后半、带 `Origin` 的音频上传未做；`#572` 需要的完整 `up`（gateway + 真实引擎）不在本页范围 |

一句话：**镜像是有的，入口是接的，缺的是 §8 剩下几项和一次进 CI 的构建验证。**

### 1.5 最小可行的部署形态：api 内 bwrap 子进程，还是独立检查器容器

仓库已经选了 **(b) 独立检查器容器 + 私有 HTTP**。本页复核后仍然推荐 (b)。

| 维度 | (a) api 容器内起 bwrap 子进程 | (b) 独立检查器容器 + 私有 HTTP（已采用） |
| --- | --- | --- |
| 能否在本机/参考部署跑通 | **不能**：容器 `CapEff=0` + moby 默认 seccomp，`unshare`/`pivot_root` 必然失败（§1.2）；要跑通必须放宽 api 容器的 seccomp 或加 `CAP_SYS_ADMIN` | 能：命名空间由容器运行时创建，容器内不需要任何特权系统调用 |
| 安全边界变更 | **需要**：放宽 api 容器（持对象存储凭据、PG 连接、对宿主发布端口）的系统调用面 | **不需要**：检查器容器 `cap_drop: [ALL]`、只读根、`internal` 网络、无端口、无挂载 |
| 解码进程的邻居 | api 进程自身的内存与 fd 空间；解码器内存破坏直接落在持凭据的进程旁 | 独立容器 + 独立 cgroup（768 MiB / 2.0 CPU / 64 pid）+ 每请求 `prlimit` 子进程 |
| 故障隔离 | 解码崩溃可能带走 api；资源上限只能靠进程内 rlimit | 停掉检查器后只有检查请求 `503`，`api` 容器保持 `Up (healthy)`（本轮实测） |
| 网络暴露 | 无新增 | 无出站、无宿主端口；只与 api 同处一个 `internal` 网络 |
| 运维复杂度 | 镜像少一个，但需要 bwrap/AppArmor 规则、宿主 userns 策略、启动期检查 | 多一个 profile + 一个镜像 + 3 个配置键；启动顺序无关（无 `depends_on`，停检查器不影响 API 可用性） |
| 升级/回退 | 随 api 镜像一起变 | 可单独重建检查器；回退只需把 selector 改回 `text` 并清空两键（`docs/components/asset-inspection-container.md` §9.2） |

代价对比是量级不同的：**(a) 的代价是一次不可逆的权限面放宽，(b) 的代价是一个 profile 和三行配置。** 所以除非出现「必须与 api 同进程才能满足的低延迟」这类硬需求（当前没有：一次上传一次检查，同步调用），没有理由回到 (a)。

## 2. 部署级证据

### 2.1 栈与本地适配

```bash
# 项目名、环境文件、只改网络与端口的本地 override
docker compose -p sn869 --env-file deploy/compose.env -f deploy/compose.yaml \
  -f /tmp/sn869-deploy/override.yaml …
```

本地适配只有两处，均与音频检查语义无关，且不修改任何仓库文件：

1. `172.28.0.0/24` 与宿主端口 8000 已被同机另一套栈占用（`sn832_default`、`sn832-workbench-api-1`），因此 override 把默认子网挪到 `172.30.0.0/24`、MinIO 静态地址随之变成 `172.30.0.10`（两份 gateway 私有 JSON 的 `objects.endpoint` 同步），api 宿主端口用 `8099`。这与文档 §1 的要求一致：「若需使用其他网段，Compose 子网、MinIO 静态地址和两份 gateway 私有配置的对象存储 endpoint 必须一起调整」。
2. `api` 用 `up -d --no-deps api` 启动：本机没有真实引擎，`gateway` 起不来，而 Compose 里 `api` 依赖 `gateway: service_healthy`。这是命令行范围控制，**没有**改动 `depends_on`。gateway 不在上传路径上（上传只触达 PostgreSQL、MinIO 与检查器）。

其余全部按文档执行：§2 环境文件与随机口令、§3 私有文件（六个样例文件原样复制，仅改对象存储 endpoint 的 IP）、§4 起 PG/MinIO 与 `minio-init`（`bucket ready: speechnexus-inputs (versioning enabled)` / `speechnexus-results (versioning enabled)`）、§5 `--profile migration run --rm migrate`（`speech.alembic_version = 0054_artifact_deleted_ledger`）、§6 的 18 个非 owner 角色与授权（`roles ready: …`）+ `provision-job-roles.py`（`job roles ready: sn_job_writer sn_job_admission sn_job_dispatch; grants: 221 statements`）、runbook §10.2 的三步音频装配。测后 `down -v`，容器、卷、网络全部删除。

### 2.2 逐项实测

**A0 默认部署（`text`）下音频上传必失败** —— `POST /v1/nexus/assets`，`purpose=asr_input`，同一份 32044 B 合成 WAV：

```text
http=503
{"error":{"code":"asset_inspection_unavailable","type":"service_unavailable_error",
          "message":"Asset inspection is unavailable.","param":null,
          "request_id":"1956e752a4fb452ab9828023649f8372","retryable":false}}
speech_assets rows before=0 after=0
```

**检查器装配生效** —— 从 api 容器内读检查器的 `/readyz`（同时证明 API 能按服务名到达检查器，且自检真的解码过一次）：

```text
{"contract_version":1,"recipe_version":"pcm-decode-1","max_encoded_bytes":4194304,
 "max_wall_seconds":20.0,"in_flight":0}
```

**A1 正向闭环** —— `purpose=asr_input`，真实合成 WAV（1 s / 16 kHz / 单声道 / 440 Hz）：

```text
HTTP/1.1 201 Created
location: /v1/nexus/assets/asset-9b8eebd8f5e243f08a7b8d92815c5b0d
{"id":"asset-9b8eebd8f5e243f08a7b8d92815c5b0d","object":"nexus.asset",
 "workspace_id":"workspace-default","purpose":"asr_input","status":"ready",
 "media_type":"audio/wav","bytes":32044,
 "sha256":"sha256:4b6dd1fcf21ad11df003cb0b93e24e7a21beb57553bad384cf83f50e00d2c08a",
 "created_at":"2026-11-15T19:35:19.954680Z","expires_at":"2026-11-22T19:35:19.954680Z",
 "duration_ms":1000,"sample_rate_hz":16000,"channels":1}
```

- `duration_ms`/`sample_rate_hz`/`channels` 来自检查器回执（真实解码），不是客户端声明：源文件本身就是 1000 ms / 16000 Hz / 1 声道，三者一致。
- 回读数据库：`asr_input | ready | audio/wav | 1000 | 16000 | 1 | 32044`；`speech.speech_asset_objects` 一行，`storage_version_id = b86b64e7-1b8a-4342-826a-77219f7e0c36`（对象版本固定）。
- 下载 `GET /v1/nexus/assets/asset-9b8eebd8…/content`：`200`、`32044` 字节，SHA-256 与源文件逐字节一致。

**A3 畸形音频** —— 8192 随机字节：`422`，`{"code":"invalid_asset_audio","type":"invalid_request_error","retryable":false}`，无新行。

**A3b 参考音频（前半）** —— 同一份 WAV 改 `purpose=voice_reference`：`201`、`status=ready`、`purpose=voice_reference`、`duration_ms=1000`、`sample_rate_hz=16000`、`channels=1`。

**A2 检查器不可用** —— 保持 selector=`audio`，停止检查器容器后重复 A1：`503`，响应体与 `x-should-retry: false` 与 A0 完全一致；`speech_assets` 行数 `2 → 2`；同时 `api` 容器保持 `Up (healthy)`——它的健康检查打的正是 `/readyz`，所以检查器故障没有升级成 API 故障。

**反例：同一栈改回 `text`** —— selector 改回 `text` 并清空 URL/令牌后重建 api，同一份 WAV、同一 `purpose`：

```text
http=503
HTTP/1.1 503 Service Unavailable
x-should-retry: false
{"error":{"code":"asset_inspection_unavailable","type":"service_unavailable_error",
          "message":"Asset inspection is unavailable.","param":null,
          "request_id":"37083876f1c547e29f4530677ebbb912","retryable":false}}
speech_assets rows before=2 after=2
```

**防降级：`text` + 已配置 URL/令牌 → API 启动失败**（不是「忽略多余配置」）：

```text
ValueError: SPEECHNEXUS_ASSET_INSPECTION_URL, SPEECHNEXUS_ASSET_INSPECTION_TOKEN configured
but SPEECHNEXUS_ASSET_INSPECTOR=text; select audio or unset every asset-inspection setting
```

### 2.3 与 runbook §14（`#680`，2026-11-13）的差异

那轮为了让 API 起来用了两处**非生产**改动，判定表据此把 F-ASSET 记作 B 档（`docs/plans/mvp-release-readiness.md` 第 1 行）。本轮两种情况都不再需要：

| #680 的改动 | 本轮 | 依据 |
| --- | --- | --- |
| override 清空 `api` 的 `depends_on` | 不改文件，只用 `--no-deps` | 同样不启动 gateway，但 compose 文件保持原样 |
| 合成配方（对齐 `binding_publication_id` 并重算 RFC 8785 摘要） | 六个样例文件原样使用 | `#821` 已修正样例一致性；本轮用生产 loader 直接验证：`load_transcription_recipe` → `TranscriptionRecipe`（`binding-revision-compose-example`）、`load_tts_recipe` → `TtsRecipe`（`binding-revision-tts-compose-example`），均无异常 |
| 给 `sn_api` 授予 `ALL TABLES` 的读写删 | 只用 §6 的 `asset_runtime_grants(schema, "sn_api")` | 上传成功即证明最小授权足够 |

### 2.4 顺带发现：§6 的口令块顺序有一处真实缺陷

`docs/operations/compose-minimal.md:392-408` 先导出 14 个新生成的角色口令，`:410-414` 才 `set -a; . deploy/compose.env`。第二步会用 `deploy/compose.env.example` 里的 `change-me-*` 占位符覆盖刚导出的值，于是：

- 角色**按占位符口令创建**，写回脚本（`:690-788`）也把占位符写进 DSN——功能上自洽，但「口令本轮重新生成、不打印、不落占位值」的意图落空；
- 照文档执行的人不会看到任何报错，也不会看到口令被丢弃。

修法很小：把 `:410-414` 的载入环境文件挪到 `:392` 之前（或让写回脚本自己生成口令）。这与本页音频主题无关，但属于「他人照着起一套」的静默失败点，建议单独修。本轮执行时先载入环境、再生成口令，因此产物用的是随机口令。

## 3. 未实测清单

| 项 | 为什么没做 | 怎么补 |
| --- | --- | --- |
| §8 A4 超时（`SPEECHNEXUS_AUDIO_INSPECTION_WALL_SECONDS=0.05` + 2 分钟 WAV） | 需要重建 api 并构造长音频；本轮优先取正向与反例 | 单列一次部署窗口，按 §8 A4 原样执行 |
| §8 A5 取消（`timeout 0.15 curl` 后 `in_flight` 回 0、无残留子进程） | 同上 | 同上；检查器镜像不含 `procps`，按 `#592` 的做法读 `/proc` |
| §8 A7 令牌不一致（中性 `unauthorized`，无 token 明文） | 与 A2 共用同一条失败路径，本轮只做了 selector 与可达性两种 | 重建 api 时故意写错令牌 |
| §8 A3b 后半（超过 `MAX_REFERENCE_DURATION_MS` 的 `422 asset_audio_too_long`） | 需要合成 >60 s 的参考音频 | 直接补一条 |
| 带 `Origin` 的音频上传 | 本轮用无 `Origin` 的 curl。Origin/Host 中间件在 purpose 分支之前执行，`#851` 已在同一部署上证明带 `Origin` 的 `text_input` 上传返回 `201`；两者不是同一条实测 | 用 `-H "Origin: http://127.0.0.1:8081"` 重跑 A1 |
| `gateway` 参与的完整 `up -d --build`、`engine-register`、Job 端到端 | 本机没有真实引擎；按项目约定不能用 fake 引擎替代 | 属 `#572` 的 GPU 窗口 |
| CI 上检查器镜像 workflow 的实际构建结果 | 该 workflow 只在 `asset-inspector/**`、`packages/media/**`、装修正文件等构建输入变化时触发，本 PR 不触发它 | 打开一次改到 `asset-inspector/**` 的 PR，或 `workflow_dispatch` 手跑，确认 `Image: …` 作业绿 |
| 设计页 §11 剩余项：`io_uring_setup` 是否被 seccomp 拒绝并优雅回退、真实解码墙钟耗时分布、`asset_max_bytes` 与检查器上限的端到端关系、公开错误元数据改可重试对 SDK 的影响 | 需要专门探测或压测；本轮只覆盖了「自检能解码成功」与「一次 1 s WAV 端到端」 | 逐项按 §11 的验证方式补 |

已经可以划掉的设计页 §11 两行：检查器镜像**确实**在 linux/arm64 的 Docker 默认 seccomp 下完成真实解码（`self-check passed`，且一次 1 s WAV 的端到端上传成功）；`internal: true` 网络、`pids_limit`、`cpus`、`read_only`、`tmpfs` 在目标运行时上都被接受（容器 healthy）。

## 4. 建议的动作

| 动作 | 落在哪 |
| --- | --- |
| 把本页 §2.2 的证据写进 F-ASSET 行（音频用途），A 档从「无」改为「有（`sn869`，2026-11-16，无生产配置改动）」 | `docs/plans/mvp-release-readiness.md` 第 1 行、§3 第 1 条 |
| 补 §8 的 A4/A5/A7 与 A3b 后半 | 一次部署窗口，不改代码 |
| 修 §6 口令块顺序 | `docs/operations/compose-minimal.md:392-414` |
| 让 CI 构建检查器镜像 | `.github/workflows/ci.yml` 的 compose 作业 |
| 定「原创音频上传」这一格的验收口径（本页只覆盖 `asr_input`/`voice_reference` 的 Asset 面） | `#572` 的执行前置 |
