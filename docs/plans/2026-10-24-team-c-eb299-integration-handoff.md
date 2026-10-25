# 团队 C：eb299f7 依赖集成交接

2026-10-24，本机分支 `codex/team-c-c00-versions` 从 `9eea85987232eaebbcbd6768c46fe0a084a4d177` 安全前进到 `eb299f7a6a14868be77adb09c70c0ad19a6a9987`。本轮 base_sha=head_sha 为后者，未新建提交或推送。C00–C08 全部 open，C03 planned，其余 in-progress，完整 Goal active。开工前和交付后均更新 issue 状态及任务并读回核对。

## 依赖和字节保留

- GitHub 核验 [#85](https://github.com/hrygo/SpeechNexus/pull/85)、[#86](https://github.com/hrygo/SpeechNexus/pull/86)、[#87](https://github.com/hrygo/SpeechNexus/pull/87)、[#88](https://github.com/hrygo/SpeechNexus/pull/88) 已合并，merge SHA 与本地祖先一致；分别吸收声明式预热门、上游真实预热记录、独立完整配方准入和固定节点 TLS 原意图客户端。
- 上一[实时 TTS 交接](2026-10-24-team-c-realtime-tts-handoff.md)累计309文件，补丁 SHA-256 `bde08884d0040eb572efe8656352e6794974aea5c70fb22089176ee38f7cd52b`。干净目标完整预演保留307文件字节，仅双语 README 三方合并。临时反向应用已备份的自有 README hunks，再 FF 和应用预演结果；实际所有字节匹配，未用 reset、checkout 或 stash。
- 本轮 C 新增此交接，更新 README 双语、计划索引和 Goal 当前基线。最终旧309中305文件字节不变、4个具名文档变化、新1文件，累计310文件。未改 A/B owner 代码或手改生成物。
- 上游增加 RFC8785 运行依赖及锁文件；本机执行 `uv sync --locked --all-packages --all-groups`。workbench 包/协议仍 `0.1.0.dev0`，bindings `0.1.0`；协调源 `1.0.0`、runtime-control `0.1.0.dev0`。C 的 workbench HTTP 样例421条保持，runtime HTTP 新增39条由同版生成流水线消费。
- 新补丁及逐文件 manifest 位于 `.runtime/team-c/team-c-current.*`，旧309包保留 `.runtime/team-c/team-c-before-eb299-integration.*`；精确 SHA、干净 apply、全部字节及链接检查以 manifest 为准。制品仅在本机，未发布到 main。

## 同版验证

- Python `uv run pytest tests/unit tests/contract -q`：**3314 passed / 55.00s，无测试 skip**；format571、ruff、mypy424源文件、7项依赖边界通过。
- `make contracts-check` 和 `make contracts-drift`：28份源契约及生成漂移检查通过。
- Web format/lint/typecheck/test/build 五门禁通过，**1471 = contracts921 + Admin79 + Workbench471**；contracts零失败/跳过。Node26.11.1/pnpm12.14.0 与既有环境一致，Node声明26.12.1差异仍保留。
- 私有 PostgreSQL18.6 socket-only 探针 `--browser`：**296 passed / 30.18s，无测试 skip**。独立检查 pg_ctl 返回3且无 postmaster.pid；未访问共享数据库。本轮未启用 coverage，不复用旧覆盖率冒称新测。
- Chromium156.0.8078.4生产 bundle →真实 C HTTP→隔离PG/fake LLM：会议和删除各一个上下文/5写请求；原预检/封存/确认/纪要提交后丢回执重放、LLM关闭、quota存储屏障、纯文本/过期隐藏/物理清理及草稿保留、360px和基础焦点验证通过。各自出站与意外异常为0。
- 同版实时 ASR 合成 REST/WS/麦克风回归：3 sessions/10 REST writes；实时 TTS 合成 REST/WS/PCM与实际AudioWorklet：1 session/5 REST writes、3 utterances、累计96000样本渲染，started前取消、drain后finish与可见恢复故障注入通过。两探针出站、意外异常和 serverErrors 为0；不是物理设备或真实后台节流资格。

精确日志、上游元数据、预演、PG停止、浏览器结果/截图在 `.runtime/team-c/integration-eb299f7/`。从仓库根目录复现：

```bash
uv sync --locked --all-packages --all-groups
uv run pytest tests/unit tests/contract -q
make contracts-check
make contracts-drift
pnpm build
uv run python scripts/dev-env/workbench-pg-probe.py --pg-bin /path/to/postgresql/bin --browser --output .runtime/team-c/integration-probe.json
node --experimental-strip-types scripts/dev-env/workbench-realtime-asr-probe.mjs --output .runtime/team-c/integration-asr
node --experimental-strip-types scripts/dev-env/workbench-realtime-tts-probe.mjs --output .runtime/team-c/integration-tts
```

## 依赖边界和余项

本轮是依赖集成。上游[配方准入](../architecture/production-admission.md)仍需原命令解析、批准/资格 provider、实际调度、完整业务配额及公开 Operation/receipt；[TLS 客户端](../architecture/runtime-controller-client.md)仍需部署 TLS、持久派发和节点内观察生产者。GET/精确重放只读快照，不推进预热，HTTP ready 不产生资格。#86 的真实预热为 B 的来源证据，本轮没有代验模型、GPU、A双库或实际 TLS 控制循环。

生产组合根、owner迁移/grants、Meeting删除图及cleanup仍由A串行同版接入。八项既有Minor继续保留：cursor错误文案、foreign-owner确认文案、双Meeting共享Transcript并发回归、成功HTTP坏输出注入、Meeting冲突后的useLatest重核、Meeting真实dispose后的GET、ASR发送失败sent计数、TTS生成done后缓冲未完的局部停止按钮。详见各原交接，本轮不追加功能修复或重复终审。

后续继续 C03 音色工作室、C04 双工、C05 实时会议/要点/固定版本导出、助手和有声书/独立副本及恢复。真实模型/设备/网络、Linux/GPU、完整跨浏览器/a11y和G0–G5另验，不将待实施项勾选完成。
