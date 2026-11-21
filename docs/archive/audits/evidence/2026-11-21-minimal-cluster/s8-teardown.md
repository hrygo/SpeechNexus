# 最小同构集群 S8：拆除与基线恢复

- 采集时间：2026-11-21 01:55–02:00（Asia/Shanghai）
- 结论：**teardown 后应用面与算力面回到基线，MegaCube 生产服务零改动。**

## 拆除对象

`deploy/cluster/teardown.sh --compute-host megacube`：

1. 删除 `speechnexus` 命名空间（全部对象与 PVC）；
2. 在 MegaCube 上 `docker compose down --volumes --remove-orphans`；
3. 删除手工启动的 `speechnexus-asr-1` 引擎容器；
4. 清理项目目录（见下），保留 `triton-cache` 残留并报告。

## 实测结果

| 检查 | 结果 |
| --- | --- |
| `kubectl get ns \| grep speechnexus` | 无输出（命名空间已不存在） |
| 全集群 `kubectl get all -A` 中的 `speechnexus` 资源 | 0 |
| MegaCube 上的 `speechnexus-cluster-compute-*` / `speechnexus-asr-1` 容器 | 0 |
| Compose 卷 | 0 |
| 项目目录 | 仅剩 `triton-cache/`（4.3 MB，root 所有，Triton JIT 缓存，无凭据） |

生产容器对照（拆除前后按 `名称|镜像` 逐条比较）：

```
diff 前后名单（排除 speechnexus-* 自身容器）→ 无差异
enterprise-agent-eval-poc-*   ×6
qwenpaw-embedding / qwenpaw-reranker
speechnexus-coordination-mysql / speechnexus-paperclip-minio
zymail-registry-verdaccio-1
```

**MegaCube 生产服务零改动**：上述容器在拆除前后名称、镜像完全一致，全程未被停止或重建。

## 残留与说明

- `~/speechnexus-cluster-compute/triton-cache/` 由引擎容器以 root 写入，本账号无权删除；
  `sudo` 需要密码，因此保留。其中没有凭据，只有编译缓存。清理命令：`sudo rm -rf ~/speechnexus-cluster-compute`。
- `teardown.sh` 把这条残留显式打印出来，而不是假装主机已干净；脚本以 `TEARDOWN OK` 结束的条件是
  命名空间与集群容器都不存在。
