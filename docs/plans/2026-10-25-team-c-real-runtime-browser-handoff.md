---
title: "团队 C 交接：真实运行时 × 真实 Chromium 交付资格"
status: active
version: "0.1.0"
date: 2026-10-25
---

# 团队 C 交接：真实运行时 × 真实 Chromium 交付资格

本轮把浏览器证据的等级从"合成服务集 + 手工装配 ASGI"提升到**部署自己的组合根**：真实生产包、真实 Chromium、真实启动检查、真实删除账本重放、真实隔离 PostgreSQL，且**没有任何一处手工驱动 worker**。

## 基线

- `base_sha` = `head_sha` = `4b08c396c0fe8fc43c7e85683e534befab6bb22b`（未提交、未推送）。
- 上一冻结单元 `C06-minutes-worker-and-intranet-llm`（441 路径，补丁 `8843f4d8…`）。
- 本单元 `C08-real-runtime-browser-qualification`：新增 `tests/integration/test_workbench_real_runtime_browser.py` 与本文件，修改 `scripts/dev-env/workbench-pg-probe.py`（浏览器选择集 + 描述）与计划文档。最终哈希见 `.runtime/team-c/team-c-current.manifest.json`。

## 本轮改动

- 新增 `test_workbench_real_runtime_browser.py`：用 `create_runtime_app(settings)` 起真实运行时（`lifespan="on"` → 真实启动检查、真实删除账本重放、真实稳态 sweeper、真实清理身份检查），把生产包交给**独立前端层**，前端层同源代理 `/v1` 到运行时；真实 Chromium（`scripts/dev-env/workbench-deletions-probe.mjs`）走完整删除与恢复流程。
- `scripts/dev-env/workbench-pg-probe.py`：`--browser` 追加本用例，并把描述改为 `production-bundle-real-c-http-private-pg-fake-llm-and-real-runtime`。
- 运行期**不做**手工清理：`cleanup_interval_seconds=60` 保证页面能看到"已排队 · 等待清理"，随后测试**重启 API**（前端层与浏览器会话不动），由真实启动期账本重放完成欠账清除，再断言修订行真的没了、独立项目里的同名稿件仍在。

## 两个真实发现（有复用价值）

1. **API 应用不是静态站点**：把生产包 `StaticFiles` 挂进 `create_runtime_app` 后，契约入口把 `/assets/*.js` 判为 `400 Request does not match the contract.`，Chromium 里是三个资源 400 与空白页。部署必须由前端层提供资源并代理 `/v1`（本项目 `deploy/` 的职责），不能把静态资源塞进 API。本用例因此显式搭了"前端层 + 运行时"两段拓扑，并把 `allowed_origins` 设为前端层来源，保持同源语义。
2. **真实 sweeper 太快，会掩盖页面的中间态**：`cleanup_interval_seconds=0.2` 时删除在页面断言"已排队 · 等待清理"之前就被清理掉了。这本身是正确行为，说明旧用例"手工调用 worker 再断言"的做法并非部署行为；本用例改为重启重放，顺带把 ADR 0005 的"先重放再开放内容"变成了可执行证据。

## 证据

| 证据 | 环境 | 结果 |
| --- | --- | --- |
| 新用例 | 真实 Chromium + 真实生产包 + `create_runtime_app` + 隔离 PG | **通过**（6.26s；重跑 7.46s 亦通过） |
| 旧删除浏览器用例（对照） | 手工装配 ASGI + 真实 PG | **通过**（5.37s，未被本轮的探针选择扩展影响） |
| `--browser` 全量探针 | 隔离 PG + MinIO + 三份浏览器用例 | 见本轮 manifest 记录 |
| 静态/类型 | `ruff check .`、`mypy`（518 文件） | 全绿 |

用例内部断言（均为真实交互）：页面在真实运行时下渲染并完成删除预检；两次"提交回执丢失"由前端层注入 503 触发，浏览器按原键重放（`writeRequests=5`，`writes[0]==writes[1]`、`writes[3]==writes[4]`）；`physicalCleanupCurrent=true`；独立项目稿件在级联删除后仍为 200；探针自检 `unexpectedErrors=0`、`outbound=0`、360px 窄视口无横向滚动、焦点可达。

## 未验证与边界

- 语音底座仍缺席：本轮不证明 ASR/TTS、真实设备、音色或真实 A/B 闭环；也不证明 Linux 后端与 GPU。
- 前端层注入的 503 是**测试装置**（模拟提交回执丢失），运行时的删除语义未被替换。
- 对象字节清理在本流程中未覆盖（无对象区配置）；对象区删除已有独立真实 MinIO 证据（隔离 PG 探针中的 saved-assets 清理用例）。
- 生产迁移、deploy 与清理 DSN 注入仍属 A07。

## 下一接入动作

1. 等 A 绑定三条下载/事件流操作与非 JSON 响应体 → 导出下载、已保存副本下载、事件流 UI。
2. 等 A Job API `generateRenderUnit` → 生成派发与代次写入。
3. 等 A06/B05 → 音色候选/试听/校验/发布（含助手所需已发布音色）。
4. 生产迁移/部署（A07）与 Linux/GPU（B07）就绪后，在同一套真实拓扑上取得跨内容网络与目标环境资格。
