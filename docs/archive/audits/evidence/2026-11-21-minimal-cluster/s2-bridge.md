# 最小同构集群 S2：跨主机桥接证据

- 采集时间：2026-11-21 00:22–00:40（Asia/Shanghai）
- 范围：S2（k8s 内桥接 Pod 维持双向 SSH 通道），对应实施卡 #1086
- 前置：S1 应用面就绪（PG、MinIO、API）

## 配置

| 项 | 值 |
| --- | --- |
| 方向 | k8s Pod → `47.103.22.160:6000`（MegaCube 公网入口），单条 SSH 连接 |
| 本端转发 | `-L 0.0.0.0:18010:127.0.0.1:8010`、`-L 0.0.0.0:18020:127.0.0.1:8022` |
| 远端转发 | `-R 127.0.0.1:15440:postgres.speechnexus.svc:5432`、`-R 127.0.0.1:19100:minio.speechnexus.svc:9000` |
| 会话选项 | `ExitOnForwardFailure=yes`、`ServerAliveInterval=15`、`StrictHostKeyChecking=yes`、`BatchMode=yes` |
| MegaCube 授权 | 单行 `no-pty,no-agent-forwarding,no-X11-forwarding,no-user-rc ssh-ed25519 …`，追加在既有管理用公钥之后（原文件已备份） |
| 镜像 | `speechnexus-bridge:bookworm-3`（`debian:bookworm-slim` + `openssh-client`，非 root uid 10001，只读根文件系统） |

**与规划文本的偏差**：规划 §8 原写「`restrict,no-pty`」。`restrict` 会同时禁用端口转发，
按原样配置隧道必然建不起来；实际使用 `no-pty,no-agent-forwarding,no-X11-forwarding,no-user-rc`，
保留转发能力、去掉 PTY 与 agent。规划文本已改。

## 结果

- MegaCube 新增监听只有 `127.0.0.1:15440` 与 `127.0.0.1:19100`（`ss -ltn` 前后差集，共 72 → 74），
  没有任何非 loopback 监听；`GatewayPorts` 保持默认。
- 反方向实测（从 MegaCube 经隧道读应用面）：
  - `curl http://127.0.0.1:19100/minio/health/live` → `200`
  - 以 `sn_job_writer` 身份经 `127.0.0.1:15440` 查询 → `('sn_job_writer', 0)`
- 正方向（从应用面 Pod 到 MegaCube）：API Pod 内 `connect bridge.speechnexus.svc:18010/18020` 均成功；
  上游 `8010/8022` 尚无监听，因此连接被 MegaCube 侧拒绝，属预期（网关在 #1087 装配）。
- 就绪探针：`nc -z 127.0.0.1 18010`，本地转发监听存在才 Ready。

## 反例（注入后确实变红）

| 注入 | 观测 |
| --- | --- |
| 删除 MegaCube 上该 `authorized_keys` 行并重建 Pod | `huangzhonghui@47.103.22.160: Permission denied (publickey).`，Pod `0/1 Error`，不进入 Ready |
| 恢复该行并重建 Pod | 10 秒内 Ready，两条反向监听同时恢复 |

不存在「认证失败但显示连接成功」的中间态：`ExitOnForwardFailure=yes` 让 ssh 在转发失败时直接退出。

## 回退

```bash
ssh megacube 'cp ~/.ssh/authorized_keys.two-line ~/.ssh/authorized_keys'   # 或删除该行
kubectl -n speechnexus delete deploy/bridge svc/bridge
```

## 待办

- 正向 200 需网关与控制器就绪（#1087）后补测；本卡保留 `status:integration` 直到联合验收。
