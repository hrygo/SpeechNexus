# 独立 Node Controller 镜像源码候选

此候选仅覆盖 [#225](https://github.com/hrygo/SpeechNexus/issues/225) 的 Controller 制品切片，
固定源码基线为 `7753c559c1b68c5a9805c27bc39b5a089171af03`。
原候选已完成离线元数据导出和安装 dry-run。本次返修增加生产身份校验、定向测试及构建 CA 接口。
当前 checkout 无独立依赖环境，固定 Python 缺 pytest/Pydantic；定向测试尚未执行到收集阶段，
行为验证为 **not_run**。干净安装、实际安装闭包、镜像构建及所有运行验收也均为 **not_run**。

## 构建输入与安装边界

从仓库根目录向 [node-controller.Dockerfile](node-controller.Dockerfile) 提供构建上下文。
Builder 读取原样的根 `pyproject.toml`、`uv.lock`、所有 workspace 成员的 `pyproject.toml`
及其声明的 README；仅复制 `node-controller/src` 产品源码。
其他成员的元数据用于 uv workspace 发现，不安装这些成员，也不复制它们的源码。

安装使用 `uv sync --frozen --package speechnexus-node-controller --no-dev --no-default-groups
--no-editable`，保留包自身声明的 `uvicorn[standard]`。
不选择 `--all-packages`、`--all-extras` 或媒体 extra，不追加 CPU VAD requirements。
最终阶段仅从 builder 复制非 editable 的 `/app/.venv`；不带 uv、构建缓存、workspace 源码、
backend/gateway/contracts/media 包或 CUDA/GPU runtime。
Python 基础镜像自身的系统工具不属于应用 venv 的依赖清单。

Builder 中的 `uv pip check`、已安装 console script 的加载和 ASGI 模块导入是后续构建门禁，
本轮没有执行。导入不会调用 `main()` 或 `create_app()`，不能证明服务已经装配或就绪。
实际入口来自 [包元数据](../../node-controller/pyproject.toml)：
`speechnexus-node-controller` → `speechnexus_node_controller.main:main`，
固定使用 `app:create_app`、单 Uvicorn worker；监听参数来自环境变量，不是 CLI 参数。
该进程没有 `process_role` 服务选择器。

### 不可变镜像引用：not_resolved

frontend `docker/dockerfile:1.18`、uv `ghcr.io/astral-sh/uv:0.13.5` 和
Python `python:3.14.9-slim-bookworm` 仍是原候选的 tag 引用。
正式输入没有这三项的获批 digest，本次不访问 registry、不拉取镜像，也没有编造 digest。
因此候选尚不能声称基础层可复现；这些 tag 不能用于结算制品资格。
具名镜像/构建平台提供方须交付：frontend 引用及 digest，uv 和 Python 的
Linux/arm64（CPython 3.14.9）manifest digest、对应 index/manifest 关系、来源及批准记录。
取得并核验这些材料后，才可将引用改为 `name:tag@sha256:…`，
并让 builder/runtime 复用同一个 Python digest。该动作仍需非作者复审和构建许可。

### PEP 517 构建后端：not_resolved

`--frozen` 固定运行依赖，不验证锁的新鲜度，也不锁定 PEP 517 的隔离构建依赖。
现有 `node-controller/pyproject.toml` 声明 `hatchling>=1.28,<2`，当前 `uv.lock` 未锁该后端；
本卡没有获批的精确版本、发行物摘要或完整构建依赖闭包。仅记录构建时选中的版本，
或仅约束 Hatchling 顶层版本，都不能解决完整构建闭包复现问题。
本卡不改共享锁、根配置或包元数据，也不关闭隔离构建/哈希/TLS 校验来绕过缺口。

Controller 包依赖所有者与获批包源提供方须提供一份独立构建依赖制品：
满足上述范围的 Hatchling 精确版本、由该版本 METADATA 确认的全部传递/条件构建依赖、
每个制品的版本/文件名/来源/SHA-256、适用 Python/平台，以及批准回执。
可实现路径是任务专属、完整固定且带摘要的 constraints/wheelhouse，在实际 uv 支持的入口
消费并验证完整闭包；取得材料后再接线，不在本轮放入未经验证的空约束或任意版本。
本机 uv 0.13.5 的 `uv build --help` 与 `uv pip install --help` 声明
`--build-constraints`，后者还声明 `--require-build-hashes`；`uv sync --help`
未声明这些 CLI 选项，不能直接把它们追加到当前 sync 命令就宣称已解决。
实际闭包安装及后端构建均 **not_run**。

### 构建 CA 接口与环境信任

安装 RUN 接受可选 BuildKit secret `proxy_ca`。存在该 secret 时，仅在该 RUN 设置
`SSL_CERT_FILE=/run/secrets/proxy_ca`，供 uv 读取批准的完整 PEM 信任 bundle；
不复制到镜像层、最终 venv、运行环境或日志，不关闭 TLS 验证。
未提供 secret 时使用 uv 默认信任根；需要组织 CA 的包源必须显式提供此 secret。
CA 提供方是构建网络/PKI 所有者，须确认 bundle 的有效期、链和对应包源/代理用途。
本轮只接入接口，没有取得或验证真实 CA，也没有执行 TLS 下载；实际环境信任为
**not_resolved**。CA 信任不锁定构建后端；后端版本固定也不证明 CA 有效。

## 部署配置与权限输入

运行用户为 `10001:10001`，应用 venv 为 root 所有。镜像不声明自动 volume 或 socket mount。
部署方必须显式提供节点身份、组件凭据和批准模板，并为节点证据提供持久存储：

| 配置 | 镜像值 / 部署方必须提供的内容 |
| --- | --- |
| `SPEECHNEXUS_CONTROLLER_ENVIRONMENT` | `production` |
| `SPEECHNEXUS_CONTROLLER_HOST` / `PORT` | `0.0.0.0` / `8020`；在批准的节点网络上显式映射监听；`EXPOSE` 不发布端口 |
| `SPEECHNEXUS_CONTROLLER_NODE_ID` | 必须由部署方显式分配唯一节点 ID；production 拒绝遗漏、`node-local-1` 和不符合现有 runtime Identifier 语法的值 |
| `SPEECHNEXUS_CONTROLLER_COMPONENT_TOKEN` | 必须由批准的秘密引用注入，至少 16 字符；镜像不提供值 |
| `SPEECHNEXUS_CONTROLLER_APPROVED_TEMPLATES_FILE` | `/etc/speechnexus/controller/approved-templates.json`；部署方只读挂载，UID 10001 可读 |
| `SPEECHNEXUS_CONTROLLER_INTENT_STORE_PATH` | `/var/lib/speechnexus/controller/intents.sqlite3`；整个父目录持久化，含 SQLite WAL/SHM；挂载后须仍允许 UID/GID 10001 写入 |
| `SPEECHNEXUS_CONTROLLER_DOCKER_SOCKET_PATH` | `/run/speechnexus-controller/docker.sock`；默认无文件或 mount，部署方仅在获准节点显式提供 socket 与访问策略 |
| `SPEECHNEXUS_CONTROLLER_ENGINE_AUTH_ROOT_KEY` | 仅批准模板声明 native engine authentication 时提供；不随镜像分发 |

模板文件必须符合 [现有加载器](../../node-controller/src/speechnexus_node_controller/infrastructure/template_config.py)，
至少包含一个模板；不能用空列表替代有效配置。实际镜像/digest、argv、内存上限、池 ID、
只读挂载与网络均由节点模板所有者提供。此镜像本身无需 GPU device/runtime；受控引擎另行部署。
Controller 不需要 PostgreSQL、业务对象存储或 Gateway 凭据。

绑定宿主目录会覆盖镜像中的目录权限，镜像内 `chown` 不能证明挂载后的可写性。
真实 daemon socket 的 GID/ACL、user namespace 映射及允许操作必须由节点平台方核定；
非 root 身份不自动赋予 socket 权限，挂载为只读也不等于 daemon API 只读。
不使用 privileged、Docker-in-Docker、宽松 chmod 或默认 docker 组解决权限问题。
节点证据目录不能与其他 Controller 并发共写，也不能随容器替换丢弃；更换镜像遵循
[现有停止恢复规则](../../docs/architecture/node-stop-recovery.md)。
网络准入/TLS 与出站限制由部署配置提供，监听所有接口不代表允许公开该内部控制 API。

身份校验位于 [Controller 设置入口](../../node-controller/src/speechnexus_node_controller/settings.py)，
console entrypoint 和直接 ASGI factory 都先构造同一个 Settings，再访问节点资源。
`development`、`test`、`staging` 及省略 environment 时保留既有默认行为；
不自动生成、修剪或替换生产 ID，不改变 HTTP 协议及真实部署配置。
本地校验只能拒绝缺失/默认/无效值；跨节点唯一分配仍由部署提供方负责。
初始化错误的文本隐藏输入值，避免新校验打印组件凭据。
专属定向测试覆盖构造参数、环境变量、拼错 NODE_ID、合成 `.env`、合法边界、
缺失/默认/无效身份、非法 environment、开发测试兼容和错误文本凭据隐藏；
本轮没有启动服务来验证入口，测试执行状态见首段。

## 后续独立验收输入与命令

以下操作全部 **not_run**，需要另行授予干净安装/下载、构建及隔离节点运行许可。
验收者须先取得候选 patch/manifest、核验基线与文件摘要，并使用自己的独立 checkout。
不得复用其他角色的 `.venv`、缓存私有路径、共享 daemon 或真实业务数据。

生产身份单元测试只需独立 checkout 的锁定开发依赖，不需要模板文件、SQLite、daemon 或服务。
提供方准备好依赖后，从仓库根目录执行以下命令；缺少依赖时应报告前置失败，不算测试通过：

```bash
uv run --offline --no-sync pytest -q tests/unit/controller/test_settings.py
uv run --offline --no-sync ruff format --check \
  node-controller/src/speechnexus_node_controller/settings.py tests/unit/controller/test_settings.py
uv run --offline --no-sync ruff check \
  node-controller/src/speechnexus_node_controller/settings.py tests/unit/controller/test_settings.py
```

1. **干净安装**：提供固定工具链、Python 3.14.9、获批包源或完整离线 wheelhouse
   （含 Linux aarch64 / CPython 3.14 的原生轮子及 Hatchling 构建闭包）和独立空环境。
   不复用本轮 dry-run 的结果作为安装证据。

   ```bash
   controller_env="$(mktemp -d "${TMPDIR:-/tmp}/del454-install.XXXXXX")/venv"
   UV_PROJECT_ENVIRONMENT="$controller_env" uv sync --frozen \
     --package speechnexus-node-controller --no-dev --no-default-groups --no-editable
   uv pip check --python "$controller_env/bin/python"
   uv pip list --python "$controller_env/bin/python" --format json
   "$controller_env/bin/python" - <<'PY'
   from importlib.metadata import distribution
   import speechnexus_node_controller.app
   entry = next(e for e in distribution("speechnexus-node-controller").entry_points
                if e.group == "console_scripts" and e.name == "speechnexus-node-controller")
   assert callable(entry.load())
   PY
   ```

   对比锁导出的运行闭包和实际安装清单；本基线 Linux CPython 的 dry-run 选择 25 个包。
   不得含其他 SpeechNexus 部署包、媒体/VAD/GPU 包或 dev group；保存实际版本与原始结果。

2. **镜像构建**：先解除上述 digest 和完整构建后端闭包阻塞，取得获批 BuildKit 窗口、
   包源及必要的可信 CA；完成专属依赖测试与非作者复审。下列命令仅展示 CA 接口，
   不使原 tag 或未固定的后端变成可复现输入；上述阻塞解除前不得据此结算构建。

   ```bash
   # 由构建网络/PKI 所有者在获批环境提供 bundle 路径；不要将证书字节写进仓库。
   : "${controller_ca_bundle:?provide the approved PEM bundle path}"
   docker buildx build --platform linux/arm64 --load \
     -f deploy/images/node-controller.Dockerfile --target runtime \
     --secret "id=proxy_ca,src=$controller_ca_bundle" \
     -t speechnexus-node-controller:del454 .
   ```

   获批包源仅依赖默认信任根时可省略 `--secret`；不得用不安全主机选项代替 CA。
   记录基础镜像及最终 digest、安装检查输出；检查最终文件系统、用户、entrypoint、
   mount/device 清单和 Python 模块。构建成功仍不授予容器或 GPU 引擎资格。

3. **无 socket / 缺配置负例**：提供有效合成模板、测试用秘密引用、节点 ID 和可写独立证据目录，
   不提供 daemon socket，禁止发 prepare/stop 请求。实际启动正式 entrypoint 后，
   `/healthz` 可为 200，`/readyz` 应为 503，`container_daemon` 失败，
   `runtime_intent_store` 成功；记录真实 HTTP/日志及持久目录权限。
   另分别移除 node ID、设置 `node-local-1`、设置无效 node ID、移除 token、
   移除/损坏模板、使证据目录不可写，应在监听前启动失败。
   不把源码推导或 Python 导入算作这些负例已通过。

4. **非 root socket / 节点运行**：节点平台方提供获批隔离 daemon、确切 UID/GID/ACL、
   模板及 state 挂载、网络/TLS、资源窗口和最小合成测试。
   分别验证拒绝与获准访问、重启后的同一节点证据保留、真实受控实例的观察及停止/释放证据。
   GPU 引擎、调度权威预算核验和物理拓扑资格按各自授权另验。

镜像禁用内置 HEALTHCHECK；部署方按职责消费现有 `/healthz` 和 `/readyz`。
二者分别只说明进程存活、daemon 与本地 SQLite 的可用性；均不证明引擎资格、可信停止、
资源释放、预算归还或完整 #225 / G5 验收。本轮候选仍需非作者审查。
