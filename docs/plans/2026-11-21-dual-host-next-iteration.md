# 双机开发环境：下一迭代（同构收口与取证）

上一迭代把应用面补齐到 10 个 Running 单元，并把显存额度改成部署配置。本迭代的目标是把
[ADR 0014](../decisions/0014-development-environment-isomorphism.md) 里的同构缺口真正关掉，
并让已经写好的额度机制拿到端到端证据。

## 1. 现状与目标

同构度当前 7/12（把 ◐ 计为同构）。缺口与待取证项：

| # | 现状 | 目标 |
| --- | --- | --- |
| 1 | 执行网关与节点控制器只在 MegaCube | Mac 侧各一份，本机 daemon 受管 |
| 2 | 隧道承载 5 条通路（含引擎与网关） | 只保留数据面回连 |
| 3 | 显存额度只在单元测试与手工探针里验证过 | 容器内 `total_memory` == 配置值 |
| 4 | `binding-disable-worker`、`engine-readiness` 已定义未启用 | 按需启用并验证一次真实运行 |

目标同构度 9/12。剩下三项（实时链路、中心级切换、平台侧 GPU 行为）不是部署缺口，
已在[部署单元与平台规划](../architecture/deployment-units.md)第 8 节声明为开发环境无法验证。

## 2. 切片

四个切片按依赖顺序排列，每个独立可验收；任一失败不阻塞其余三个。

### 切片 A：Mac 侧部署网关与节点控制器

- 应用面清单新增 `gateway` 与 `node-controller` 两个 Deployment，配置键与 MegaCube 侧一致
- 节点控制器绑定 Mac 的 Docker Desktop socket，只管理获准模板
- 验收：`kubectl -n speechnexus get pods` 出现两者且 Ready；提交一次执行走本机网关，
  证据里 `gateway_id` 指向 Mac 的实例
- 反例：把网关指向 MegaCube 的端点时验收必须失败，否则测的不是本机网关

### 切片 B：隧道收窄

- 删除 `engine`（18190）与 `gateway`（18010）两条转发，保留数据面回连（15440 / 19100）
- 验收：删除后切片 A 的路径仍能跑通一次完整执行；`bridge` 日志里不再出现那两条通路
- 顺序要求：必须在切片 A 之后，否则业务会断

### 切片 C：显存额度端到端取证

- 用配置 `ENGINE_GPU_MEMORY_MIB=12316` 启动引擎，容器内读
  `torch.cuda.get_device_properties(0).total_memory`，与配置值比对
- 验收：读数与配置一致（±1 GiB 内的对齐误差需在证据里说明）；把配置改成更小的值后，
  同一断言必须变红
- 判据限制：`nvidia-smi` 在统一内存设备上显示 `[N/A]`，不能作为判据

### 切片 D：启用两个休眠单元

- `binding-disable-worker` 与 `engine-readiness` / `engine-readiness-renew` 按 profile 启用
- 验收：各产生一次真实运行记录（Job 完成或 worker 处理一条意图），日志证明它连到了
  正确的库与桶
- 不要求：把它们设成常驻；参考 Compose 也是按需启用

## 3. 出范围

| 事项 | 原因 |
| --- | --- |
| 引擎改为 Pod（`KubernetesRuntime`） | 需要新的 runtime 实现与调度路径，单独迭代 |
| 双中心切换演练 | 依赖供应方端点，属于生产方案而非开发环境 |
| 平台侧 GPU 切分验证 | 开发机是裸卡，只能在平台联调环境做 |

## 4. 风险

| 风险 | 处理 |
| --- | --- |
| Mac 侧控制器连错 daemon，误管用户容器 | 控制器只接受获准模板；先用只读模板跑通再放开 |
| 删隧道导致业务中断 | 切片 B 排在 A 之后，且逐条删除、每条删完各跑一次执行 |
| 显存读数与配置不符 | 先查 `LD_PRELOAD` 是否注入成功，再查额度单位（MiB vs GiB） |

## 5. 证据要求

- 每个切片的验收都要有可复现命令与实际输出，不接受"应该可以"
- 切片 C 必须给出"改小配置后断言变红"的对照，否则不算验证
- 证据落在 `docs/archive/audits/evidence/` 对应目录，与既有格式一致

## 6. 与既有跟踪的关系

实施与验收由 [#1092](https://github.com/hrygo/SpeechNexus/issues/1092) 跟踪；
实施顺序与依赖以本文件为准。生产侧的双中心改造
见[双中心生产架构方案](2026-11-21-architecture-optimization.md)，两者不共享验收。