# 服务前缀环境变量的启动校验

本文说明每个服务进程在启动时如何判定以本服务前缀开头、却匹配不到任何已声明设置项的环境变量。它是 [配置前置与静默无效项](configuration-prerequisites.md)的配套规则：那份文档说明「配了 A 还需要 C」，本文说明「配了拼错的 A，进程根本不会用到它」。

## 规则

四个服务各自声明一个环境变量前缀，扫描范围只限本服务声明的前缀：

| 服务 | 前缀 | 设置来源 | 装配点 |
| --- | --- | --- | --- |
| 后端（API 及后端命令行） | `SPEECHNEXUS_` | `backend/src/speechnexus_backend/settings.py` | `Settings` 构造期 |
| 工作台服务 | `SPEECHNEXUS_WORKBENCH_` | `backend/src/speechnexus_backend/workbench/runtime_settings.py` | `WorkbenchSettings` 构造期 |
| 执行网关 | `SPEECHNEXUS_GATEWAY_` | `execution-gateway/src/speechnexus_gateway/settings.py` | `Settings` 构造期 |
| 节点控制器 | `SPEECHNEXUS_CONTROLLER_` | `node-controller/src/speechnexus_node_controller/settings.py` | `Settings` 构造期 |

启动时逐个判定进程环境中本前缀下的变量，结果分三类：

| 变量形态 | 处置 | 理由 |
| --- | --- | --- |
| 前缀匹配、是已声明的设置项（含空值 `KEY=`） | 正常加载 | 空值是「未配置」，不是「已配置」 |
| 前缀匹配、未声明、也没有登记归属 | **拒绝启动**，消息点名该变量与最接近的已声明键 | 没有任何消费者，静默启动必然是假象 |
| 前缀匹配、未声明，但登记为其他进程或部署面所有 | 启动时 `WARNING` 点名该变量与归属进程 | 共享 env 文件是合法部署形态，拒绝会打断可用部署 |
| 不带本服务前缀（`PATH`、`HOSTNAME`、CI 变量等） | 不参与判定 | 前缀是本规则的唯一边界 |

拒绝消息形态（后端，拼错 `DATABASE_URL`）：

```text
Refusing to start the speech backend process (SPEECHNEXUS_PROCESS_ROLE=api) with
environment variable(s) that match no declared setting: SPEECHNEXUS_DATABASE_URLL
(did you mean SPEECHNEXUS_DATABASE_URL, SPEECHNEXUS_DATABASE_SCHEMA?). Fix the
spelling or unset the variable. Values without a service prefix (PATH, HOSTNAME,
CI variables) are never inspected.
```

消息只含名称，不含取值（设置类同时启用 `hide_input_in_errors`）。候选来自 `difflib`，相似度阈值 0.75，最多 3 个；低于阈值不出候选，改为说明「没有任何已声明设置项与之相似」。

### 为什么不是 `extra="forbid"`

`extra="forbid"` 作用于传入参数与 `env_file` 条目，不作用于进程环境里多余的同前缀变量（本仓库实测：工作台设置早已声明 `extra="forbid"`，`SPEECHNEXUS_WORKBENCH_XXX_DATABASE_URLL` 仍然被静默丢弃）。即使它能生效，进程环境里的 `PATH`、`HOSTNAME`、CI 注入变量与运维自有变量也会一起被拒，任何部署都起不来。因此判定必须按前缀、而不是按进程。

### 为什么「已知但属于别的入口」只告警不拒绝

同一个 `.env` 同时供多个容器使用、或一个 ConfigMap 通过 `envFrom` 注入所有服务，都是合法部署形态。这类环境里出现别的进程的键不会让本进程失效（本进程照常工作），拒绝则会直接打断可用部署——上一轮同类改动（#660 返修）正是这样误伤了两处合法配置。告警点名归属进程，操作者可以判断是否需要搬走：

```text
SPEECHNEXUS_JOB_DISPATCH_DATABASE_URL is set for the speech backend process
(SPEECHNEXUS_PROCESS_ROLE=api) but is only consumed by the job-dispatch worker
entrypoint; it has no effect here. Unset it or move it to that process.
```

## 误伤面

判定只可能误伤「以本服务前缀开头、且未被登记」的变量。已按仓库实际产出的环境变量核对：

- 用 `docker compose -f deploy/compose.yaml --env-file deploy/compose.env.example --profile '*' config --format json` 取解析后的容器 `environment:`，逐容器比对：`api`、`gateway`、`asset-inspector`、`binding-disable-worker`、`job-dispatch-worker` 中带前缀的变量全部是已声明设置项，未匹配键数为 0。唯一例外是 `job-dispatch-worker` 的 `SPEECHNEXUS_JOB_GATE_PRIVATE_DIR`，由同容器的启动闸门 `deploy/job-startup-gate.py` 读取，已登记为该入口所有。工作台 API 不在本 Compose（`workbench` 服务是 Web 前端），其设置项按模块字段判定。
- CI 工作流注入的 `SPEECHNEXUS_TEST_DATABASE_URL`、`SPEECHNEXUS_TEST_MYSQL_URL`、`SPEECHNEXUS_TEST_MINIO_*` 属于测试面，已登记。
- `deploy/compose.env.example` 中的角色口令、端口映射、迁移/注册 DSN、宿主挂载路径等只在宿主机插值，不进入容器 `environment:`；登记为部署面，出现时静默忽略，避免「把整个 env 文件转发进容器」这种合法操作被拒。

机器可读来源与这些判定登记在 `tests/ci/service_environment_keys.py`，由 `tests/ci/test_service_environment_keys.py` 校验：

1. 四个包下每个 `BaseSettings` 子类与手写 `_ENV_PREFIX` 设置类都必须登记在 `SURFACES`，新增设置类未登记即红；
2. 每个 `env_prefix` / `_ENV_PREFIX` 必须登记在 `SERVICE_PREFIXES`，新增前缀未登记即红；
3. 后端扫描 `SPEECHNEXUS_`，因此**其他入口的每个已声明键**都必须被后端的某个登记面覆盖，新增后端入口设置项未登记归属即红；
4. 部署面清单里的每个变量都必须是已声明设置项或已登记归属，新增环境变量未分类即红；反向也校验没有登记了却无人使用的空转条目；
5. 三份扫描引擎（后端、网关、节点控制器）的共享定义按 AST 逐节点比对，任一份漂移即红。

设置项键集合由 `settings.py` 的字段派生（`model_config(env_prefix)` 或 `validation_alias`），不是手抄。该模块在 CI 里由系统解释器（3.12）执行，而产品源码按 Python 3.14 编写（PEP 758 免括号 `except`），所以读不到的源码改走文本级抽取并把命中的模块记入 `DISCOVERY_TEXT_READS` / `TEXT_READS` 供用例断言；抽不出声明或键集合为空时直接红，不静默跳过。

复核误伤面（需要 `docker compose`；`--profile '*'` 才会解析 opt-in 服务的环境）：

```bash
docker compose -f deploy/compose.yaml --env-file deploy/compose.env.example \
  --profile '*' config --format json > /tmp/compose.json
python3 - <<'PY'
import json, sys
sys.path.insert(0, ".")
from tests.ci.service_environment_keys import SURFACES, derived_keys
declared = {key for surface in SURFACES for key in derived_keys(surface).values()}
owned = {
    "api": "backend",
    "gateway": "gateway",
    "asset-inspector": "asset_inspector",
    "binding-disable-worker": "binding_disable",
    "job-dispatch-worker": "job_dispatch",
}
for service, keys in json.load(open("/tmp/compose.json"))["services"].items():
    surface = owned.get(service)
    if surface is None:
        continue
    known = set(derived_keys(next(s for s in SURFACES if s.id == surface)).values())
    unmatched = sorted(
        name
        for name in (keys.get("environment") or {})
        if name.startswith("SPEECHNEXUS_") and name not in known and name not in declared
    )
    print(service, unmatched)
PY
```

## 空白值

`KEY=` 是「未配置」：命名校验只认名称，空值不影响判定，不会被这个前缀规则判成「已配置」，也不会被它拒绝。取值归一由各设置模块的前置校验负责——后端 `empty_profile_writer_is_disabled`、工作台与网关的可选字段归一（#660 返修）都先把空白折成 `None`。

本仓库仍有一处空白值与「未配置」不一致，属 #660 返修范围之外，本文不改变它：`SPEECHNEXUS_OBJECT_STORAGE_ACCESS_KEY=` 或 `SPEECHNEXUS_OBJECT_STORAGE_SECRET_KEY=` 命中 `min_length=1`，启动时报 `Value should have at least 1 item after validation`。把这两个键写成空占位的 env 模板会被拒；要么删掉该行，要么写入真实值。

## 新增设置项或新服务时

- 新增设置字段：无需改登记表，字段派生自动覆盖；若该字段属于**后端**（前缀在 `SPEECHNEXUS_` 之内）请同时确认它归后端 `Settings` 还是后端某个入口设置类——后者需要在 `speechnexus_backend.environment.PLANES` 登记归属，否则 `tests/ci` 第 3 条会红，因为 API 会把该键当成拼错。
- 新增服务或新前缀：在 `SERVICE_PREFIXES` 登记前缀与扫描面，并在该服务的设置模块里调用 `refuse_unrecognised_prefixed_environment`。
- 新增部署面变量（Compose 插值、脚本输入、测试面）：在对应 `PLANES` 登记，`tests/ci` 第 4 条要求它至少匹配一个仓库内真实出现的变量名。

## 覆盖边界

本校验只判定**名称**，且只判定四个服务设置类构造期可见的变量。它抓不住：

- `SPEECHNEXUS_JOB_DISPATCH_*`、`SPEECHNEXUS_RUNTIME_DELIVERY_*`、`SPEECHNEXUS_DISABLE_*`、`SPEECHNEXUS_VOICE_EVIDENCE_*`、`SPEECHNEXUS_TTS_SETTLEMENT_*`、`SPEECHNEXUS_QUALIFICATION_*` 等**后端 worker 入口**：它们各自用独立设置类装配，本卡未在这些入口装配点接入判定；在后端主 `Settings` 里出现时只会得到「属于别的入口」的告警。
- 资产巡检服务（`SPEECHNEXUS_INSPECTION_*`，`InspectionSettings.from_env` 手写读取）：未接入。
- 引擎注册、迁移、对象存储初始化等命令行与 `deploy/*.py`：不构造设置类，未接入。
- 值语义错误（键名正确但内容非法）、键存在于某个 `*.md` 之外的私有 env 文件、以及设置项之间的前置关系（见 [配置前置与静默无效项](configuration-prerequisites.md)）。
- 环境变量名大小写不敏感（判定按大写比较）；同一变量在容器内被 shell 覆盖的情形不在判定范围。
