# 最小同构集群 S5：首个跨主机文件 ASR Job 成功

- 采集时间：2026-11-21 01:25–01:28（Asia/Shanghai）
- 结论：**一次合成音频的公开文件 ASR Job 跨两台物理主机到达 `succeeded`，制品 sha256 与记录一致。**
  租约与预算**未释放**，因此 S6 仍未通过。

## 链路

| 段 | 实测 |
| --- | --- |
| 公开创建 | `POST /v1/nexus/jobs` → `202`，`job-a45968b4cbbd43d99f04016b516d12d4` |
| 本机 k8s | API 准入 → `speech_jobs`/`admission_leases`/`submit_intents` 同一事务写入 |
| 桥接 | k8s `bridge` Pod 的 `-R 15440`/`-L 18010` 承载 PG 与网关流量 |
| 网关（MegaCube） | `POST /internal/v1/executions/submit` → `200`，轮询 `/staging-result` → `200` |
| 引擎（MegaCube） | `127.0.0.1:18190` 上的 `qwen3-asr-1.7b` 完成识别 |
| 回读 | `GET /v1/nexus/jobs/{id}` → `succeeded`，制品 `artifact-e32bf929…`，74 B |

## 制品校验

```
GET /v1/nexus/artifacts/artifact-e32bf929…/content  → 200, 74 B
计算 sha256   da4d6eb2acf468fc84a98323f15007317f9cc1ba76193dc84be4948e404e0c53
记录 sha256   da4d6eb2acf468fc84a98323f15007317f9cc1ba76193dc84be4948e404e0c53
内容          {"text":"The quick brown fox jumps over the lazy dog.","duration_ms":3447}
```

音频 `asr-input.wav` 为 3447 ms 合成语音，转写时长与之一致。

## 卡住这次的四个配置事实（都已修）

1. **控制事务上限**：网关默认 `CONTROL_TIMEOUT_SECONDS=3`，隧道化 PG 上必超时；提到 20 s 后提交才 `200`。
2. **`rollback_tokens` 必须为 0**：binding 的执行默认值取自示例（5），ASR lane 的结构校验直接判命令无效。
3. **policy revision 必须等于网关 VAD 的 `config_revision`**：两边写成不同字符串即被判「命令与绑定不匹配」。
4. **recipe 的 binding revision 有两处**：顶层 `binding_revision_id` 与 `runtime_plan.binding.binding_revision_id` 必须一致，只改一处会让注册报 `invalid_recipe`。
5. **制品读取需要独立 DSN**：缺 `SPEECHNEXUS_ARTIFACT_READER_DATABASE_URL` 时内容接口 `503 artifact_service_unavailable`。

## 未通过：租约与预算未释放

Job 成功、`execution_stop_evidence` 有 1 行、意图为 `succeeded/complete`，但：

```
engine_instances.held_slots = 1
workspace_quotas.held_attempts = 1
admission_leases: lease_state=reserved, cleanup_state=pending, budget_released_at_us=NULL
```

派发器打完 `lease release deferred to reconciliation` 就停在终态，没有归还。S6 的接收条件因此不成立，
这是下一轮的唯一目标；五类故障演练（S7）与 teardown（S8）在其后。
