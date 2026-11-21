# 最小同构集群 S7：五类故障演练

- 采集时间：2026-11-21 01:35 起（Asia/Shanghai）
- 判据：每类只演一次；不出现重复发布（同一 Job 不产生第二个制品）；不提前释放（未取得停止证据前
  `held_slots`/`held_attempts` 不归零，租约不转 `released`）。

## 演练 1：API 重启（执行中）

| 步骤 | 实测 |
| --- | --- |
| 建 Job | `202`，进入 `running` |
| 注入 | `kubectl -n speechnexus rollout restart deploy/api`，随后 rollout 成功 |
| 结果 | Job 仍到达 `succeeded`，制品**恰好 1 个** |
| 容量 | 释放完成后 `held_slots=0`、`held_attempts=0`、租约 `released/complete` |

结论：API 重启不打断已受理的执行；没有重复发布，也没有提前释放（释放发生在成功与停止证据之后）。

## 演练 2：网关重启（执行中）

| 步骤 | 实测 |
| --- | --- |
| 建 Job | `202`，进入 `running`，网关已 `200` 受理提交 |
| 注入 | MegaCube 上 `docker compose restart gateway` |
| 网关侧 | 执行被记为 `unknown_quarantined`、`cleanup_state=quarantined`、`error_code=engine_transport_unknown` |
| 制品 | 无（`artifacts` 为空） |
| 容量 | `held_slots=1`、`held_attempts=1`、租约 `reserved/pending` —— **未提前释放** |

结论：网关重启把在途执行判为「传输结果未知」并隔离，不冒充成功、不发布制品，容量继续被占用，
符合「取消或任务结束不等于算力停止」。该隔离后的容量只能经隔离释放流程归还，不能自动回收。

## 演练 3：执行中杀掉引擎

| 步骤 | 实测 |
| --- | --- |
| 建 Job | `202`，进入 `running` |
| 注入 | MegaCube 上 `docker kill speechnexus-asr-1`（容器以 137 退出） |
| 网关侧 | 执行被记为 `unknown_quarantined/quarantined`、`error_code=engine_transport_unknown` |
| 制品 | 无 |
| 容量 | `held_slots=1`、`held_attempts=1`、租约 `reserved/pending` —— **未提前释放** |
| 恢复 | `docker start speechnexus-asr-1` 后 `/v1/models` 重新返回 `qwen3-asr-1.7b` |

## 演练 4：PostgreSQL 短暂不可用

| 步骤 | 实测 |
| --- | --- |
| 建 Job | `202`，进入 `running` |
| 注入 | `kubectl -n speechnexus scale statefulset postgres --replicas=0`，约 15 s 后回到 1，Pod 就绪 |
| 结果 | Job 仍到达 `succeeded`，制品**恰好 1 个** |
| 容量 | `held_slots=0`、`held_attempts=0`、租约 `released/complete` |

结论：已在执行中的 Job 不因应用面数据库短暂离线而丢结果；写路径重试后释放仍成立。

## 演练 5：桥接中断

| 步骤 | 实测 |
| --- | --- |
| 建 Job | `202`，进入 `running` |
| 注入 | `kubectl -n speechnexus delete pod -l app=bridge`，桥接 Pod 重建为 `Running` |
| 结果 | 在途执行被切断，Job 停在 `running`，无制品 |
| 容量 | `held_slots=1`、`held_attempts=1`、租约 `reserved/pending` —— **未提前释放** |

## 灾后恢复

清掉隔离状态后重建一次正常 Job：`succeeded`，制品 1 个，随后 `held_slots=0`、
`held_attempts=0`、租约 `released/complete`。集群在五次演练后仍可正常服务。

## 五类演练的共性结论

- **不重复发布**：五次演练中没有任何一次产生第二个制品；失败的两次（网关重启、引擎被杀）
  与中断的一次（桥接）都没有发布任何制品。
- **不提前释放**：执行结果未知时容量一律继续被占用（`reserved/`或隔离），没有一次在缺少
  停止证据的情况下归还。
- **产品缺口**：`unknown_quarantined` 之后没有自动归还路径；`held_slots` 会一直占着，直到
  运行手册 §13.1 的操作员出口（隔离释放证词）。最小集群里这就是「一次故障换一个永久占用槽」，
  与运行手册描述的默认部署行为一致。
