---
title: "注册并激活一个引擎实例"
status: active
version: "0.1.0"
date: 2026-10-28
---

# 注册并激活一个引擎实例

`speechnexus-register-engine` 是当前受支持的运维入口：它把一个**已经运行**的引擎
实例写入准入读取的关系事实，并在同一事务里把它置为 `ready`。它不启动容器、不连接
GPU 主机、不发布 binding，也不代表模型/硬件资格。

## 为什么不用 runtime delivery worker

`speechnexus-runtime-delivery-worker` 只把 `runtime_instance_intents` 投递给
**节点控制器**（`controller_address` / `controller_port` / `controller_token`），
再将控制器返回的原响应记为投递 ACK；它不调用 `observe_runtime` 推进实例状态。
没有节点控制器时它也无法自己把实例置为 `ready`：
`create_runtime_intent` 只创建 `preparing` 实例和 outbox，准入还要求
`intent_state=succeeded`、`observed_instance_state=ready` 以及 readiness 收据。
该运维命令适用于已运行实例的人工登记，不替代控制器观察和完整调度循环。
节点控制器的观察与消费路径另见[控制器客户端](../components/runtime-controller-client.md)。

## 前置条件

1. 目标 schema 已迁移到当前 head：
   `speechnexus-job-migrate --schema <schema>`（DSN 用
   `SPEECHNEXUS_MIGRATION_DATABASE_URL`）。
2. 已有 owner-only 私有同步转写配方（`SPEECHNEXUS_TRANSCRIPTION_RECIPE_FILE`），
   且其 `runtime_plan` / `binding` / `stage.instance_id` 是本次要注册的实例事实。
3. 引擎健康端点可从执行命令的主机访问；命令会把 origin 补成 `/health`，也接受
   显式 `/health` URL。执行者须先独立核对该端点确实属于目标实例及 generation；
   健康检查只要求 HTTP 200 和有界响应，不解析响应以验证模型或实例身份。
4. 同一实例同一 generation 只能有一个 ready 行；同一池不能有另一个存活 owner。
5. `speechnexus-register-engine` 控制台脚本需要 backend workspace 包已安装。仓库根
   `pyproject.toml` 是 `package=false` 的 uv workspace，某些 `uv sync` 结果不会在
   `.venv/bin` 生成 `speechnexus-*`；先用 `uv run speechnexus-register-engine --help`
   确认，若命令不存在，改用等价的模块入口（本任务实测路径）：

   ```bash
   PYTHONPATH=backend/src:execution-gateway/src:packages/contracts/src \
     uv run python -m speechnexus_backend.engine_registration --help
   ```

   两种入口走同一个 `main()`；在仓库根目录执行时，可将下面命令里的
   `speechnexus-register-engine` 替换为
   `PYTHONPATH=backend/src:execution-gateway/src:packages/contracts/src uv run python -m speechnexus_backend.engine_registration`，
   并保留后续参数。

## 推荐命令（配方模式）

通过部署批准的秘密配置向进程提供 `SPEECHNEXUS_DATABASE_URL`；不要把含凭据的 DSN 写入命令参数或终端历史。以下路径和实例参数仅为示例，执行前须按目标部署确认：

```bash
export SPEECHNEXUS_TRANSCRIPTION_RECIPE_FILE=/absolute/private/asr-recipe.json

speechnexus-register-engine \
  --schema speech \
  --recipe-file "$SPEECHNEXUS_TRANSCRIPTION_RECIPE_FILE" \
  --engine-url http://127.0.0.1:18100 \
  --generation 1 \
  --workspace workspace-default \
  --max-attempts 8 \
  --owner-id speechnexus-operator \
  --transaction-timeout-seconds 10
```

参数含义：

| 参数 | 含义 |
| --- | --- |
| `--database-url` | 准入库 DSN；缺省读 `SPEECHNEXUS_DATABASE_URL`。含凭据时使用批准的秘密配置，不放入命令参数 |
| `--schema` | 协调表 schema；缺省读 `SPEECHNEXUS_DATABASE_SCHEMA` 或 `speech` |
| `--recipe-file` | owner-only 私有配方；缺省读 `SPEECHNEXUS_TRANSCRIPTION_RECIPE_FILE` |
| `--engine-url` | 引擎 origin 或 `/health` URL；缺省读 `SPEECHNEXUS_ENGINE_HEALTH_URL` |
| `--generation` | 实例 generation，必须为正整数且不得回退到 high water 以下 |
| `--workspace` | 准入 workspace（默认 `workspace-default`） |
| `--max-attempts` | 该 workspace 首次创建时的并发 attempt 上限；已有上限必须大于或等于请求值，不会被该命令调大 |
| `--owner-id` | 池 owner 身份；默认 `speechnexus-operator` |
| `--fencing-token` | 可选；同一 owner 必须与当前 token 一致；接管另一已过期 owner 时必须更大，不传则自动递增 |
| `--owner-ttl-seconds` | 池 owner 租约，默认 86400 秒，范围 1–31536000 秒 |
| `--readiness-ttl-seconds` | readiness 收据有效期，默认 86400 秒，范围 1–31536000 秒 |
| `--intent-deadline-seconds` | 注册 prepare intent 的期限，默认 300 秒，范围 1–3600 秒 |
| `--intent-id` | 可选；缺省为 `register-<instance>-g<generation>` |
| `--health-timeout-seconds` | 健康探测超时，默认 5 秒，大于 0 且不超过 30 秒 |
| `--transaction-timeout-seconds` | 数据库事务超时，默认 10 秒 |

配方模式以配方中的 plan 和 policy revision 为准：`--node`、`--pool`、`--gateway`、
`--instance`、`--model` 若显式提供，必须与配方一致；其他手动 plan 字段不会覆盖配方，
不要依靠这些参数调整已加载的 plan。`--host-capacity` 是可单独指定的容量输入。

命令成功时输出 JSON，包含 `outcome`、实例身份、fencing token、readiness 摘要，以及
`engine_instances` / `runtime_instance_heads` / `runtime_instance_intents` /
`resource_pools` / `workspace_quotas` 的实际行。输出还包含健康检查 URL；保存或分享前须按部署的敏感信息规则处理。

## 手动模式

没有配方文件时，必须显式给出冻结的 binding 与 plan 字段。`--model` 仅用于操作记录；
真正进入数据库的是 `--template-id`、`--binding-revision-id`、`--manifest-id`、
`--config-digest`、`--topology-digest`、`--qualification-digest` 和容量向量。
如果当前进程已设置 `SPEECHNEXUS_TRANSCRIPTION_RECIPE_FILE`，命令默认仍会选择配方模式；
使用手动模式前须在该命令的环境中取消该变量，并确认未传入 `--recipe-file`。

```bash
speechnexus-register-engine \
  --schema speech \
  --engine-url http://127.0.0.1:18100 --generation 1 \
  --model asr-quality --node node-1 --pool asr-batch --gateway gateway-1 \
  --instance instance-1 --template-id template-1 \
  --binding-revision-id binding-rev-1 --manifest-id manifest-1 \
  --config-digest sha256:... --topology-digest sha256:... \
  --qualification-digest sha256:... \
  --architecture discrete_device_memory --service-class batch \
  --loading-policy resident --max-slots 2 --realtime-reserved-slots 0 \
  --peak 0,4000000000,1000000000,1000,0,0 \
  --steady 0,4000000000,1000000000,1000,0,0 \
  --per-slot 0,500000000,250000000,100,0,0 \
  --workspace workspace-default --policy-revision policy-revision-1 --max-attempts 8
```

容量向量固定六项：
`unified,device,host,cpu_millicores,pcm_buffer,scratch`。
`--host-capacity` 缺省为 `steady + per_slot * max_slots`（并至少覆盖 `peak`）；已有
host 的 allocatable 向量不会被静默放大。

## 写入与校验

一个事务内：

- `host_nodes`：按 node 幂等插入；校验 architecture / topology，拒绝 quarantined，
  必要时把非 active 节点置为 active。
- `resource_pools`：按 pool 幂等插入；校验 service class；同 owner 保留 token，租约剩余时间足够时不更新，否则续租；
  另一个存活 owner 直接 `pool_owner_conflict`；过期 owner 接管时 token 必须更大。
- `workspace_quotas`：按 workspace 幂等插入；已有 policy revision 必须一致，
  已有上限不得更小。
- `engine_instances` / `runtime_instance_heads` / `runtime_instance_intents` /
  `coordination_outbox` / `runtime_intent_deliveries`：走既有
  `create_in_transaction` + `observe_in_transaction`，所以 generation 单调、
  实例不得重复 ready、fencing、容量和 readiness 来源仍由领域事务校验。
- readiness 的 `evidence_digest` 来自本次真实 `/health` 探测（URL、状态码、
  body SHA-256、观察时间）和操作者提供的实例身份；这是运维确认，不是节点控制器
  的观测或模型资格证明。写入观察中的 `observed_peak_memory` 来自批准计划的 peak，
  不是该健康检查实测的内存峰值。

重复执行完全相同的注册返回 `already_ready`，不新增实例、head 或 intent；若存储的
readiness 已过期，则用本次 `/health` 重签 readiness 并更新 `ready_at_us`，不会新建
实例。同一 generation 但配置不同返回 `instance_config_conflict`。低于 high water 的
generation 返回 `stale_instance_generation`。当前 generation 仍 ready 时注册更高
generation 返回 `previous_generation_active`。

命令未明确成功时，应查询原实例、generation、intent 和池 owner，再决定是否按原参数重试。
数据库提交回执丢失时结果可能未知；即使命令输出“no registration was committed”，也不能仅凭
该错误文案断定事务未提交，更不能换 generation 或身份盲目重试。

## 验证 SQL

以下示例使用 `speech` schema；若注册时使用其他 schema，应替换为同一个经批准的 schema。

```sql
SELECT instance_id, instance_generation, instance_state, base_budget_kind
FROM speech.engine_instances WHERE instance_id = '<instance>';
SELECT instance_id, current_generation, generation_high_water
FROM speech.runtime_instance_heads WHERE instance_id = '<instance>';
SELECT target_node_id, intent_id, intent_state,
       observation_document IS NOT NULL AS has_observation,
       readiness_document IS NOT NULL AS has_readiness
FROM speech.runtime_instance_intents
WHERE instance_id = '<instance>';
```

`engine_instances.instance_state` 应为 `ready`、`base_budget_kind` 为 `steady`，
`runtime_instance_heads.current_generation` 等于注册的 generation，intent 为
`succeeded` 且 observation/readiness 均非空。

## 边界

- 不修改 `contracts/`、`packages/`，不需要新迁移。
- 不创建或修改 binding 发布；配方中的 binding/revision 必须与后端实际使用的私有
  配方一致，否则准入会以 `recipe_mismatch` 拒绝。
- 该命令不执行真实转写、不启动节点控制器、不验证 GPU 模型；这些仍由对应执行与
  资格任务分别取证。
- 单元回归（不连数据库）：`uv run pytest tests/unit/backend/test_engine_registration.py
  tests/unit/backend/test_engine_registration_transaction.py -q`。真实 PostgreSQL 链路
  见 `tests/integration/test_engine_registration_pg.py`。
