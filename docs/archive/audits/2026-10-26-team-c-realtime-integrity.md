# C04：实时发送水位与本地播放停止确认

2026-10-26，基于 main `5164b6a89e2d15486afbe4e8d1e5cb6db5f3de91`。对应 [C04 #39](https://github.com/hrygo/SpeechNexus/issues/39) AC1/AC2/AC5 的局部缺陷修复；完整 AC 保持未验收。

## 行为与事实边界

- ASR 原先在检查 socket 容量及 `send` 前就提交本地水位。现在先校验命令，再执行有界、匹配当前 socket/stream/generation 的同步发送，成功后才提交样本、credit、未提交缓冲、阶段与命令身份。拒绝或发送抛错保留原快照，显式重试仍使用原命令身份。`send` 返回只证明浏览器本地入队，服务端 ACK 水位仍单独记录。
- TTS 原先发起 `AudioContext.close()` 就报告 stopped，并吞掉关闭失败。现在立即停止旧 Worklet 通道，保留唯一关闭 Promise；关闭期间为 closing，关闭确认后才为 stopped，失败为 failed。重复停止与 abort 加入同一结果；初始化中止也等待其拥有的 context 清理，迟到初始化不会发送 start。
- 控制器保留未确认清理的播放实例，禁止创建下一播放/替换会话；服务端 done、REST 成功或刷新不能清除本地失败事实。清理失败公开显示停止待确认并阻断新播放；关闭页面后的最终物理设备释放不由本次测试证明。
- 服务端生成终态先到、PCM 尚未播放完时，页面继续提供“停止本轮播放”。它只停止本地播放，不对已终结操作重复发出取消。新一轮须同时满足服务端原操作清理完成及本地播放关闭确认。

## 验证

本地 Node 26 / Python 3.14.9，生产构建配 Chromium 156.0.8078.4。详细日志及截图留在本工作区 `.runtime/realtime-integrity/`，不提交业务内容或运行产物。

| 检查 | 实际结果及覆盖 |
| --- | --- |
| 修复前复现 | ASR 定向 3 项失败；TTS 播放与控制器 6 项失败，确认提前 stopped、忽略失败及远端 done 后允许新 context |
| 反例/变异 | 将 ASR 发送移回水位提交之后，6 项失败；允许 closing/failed 开始新播放，2 项页面测试失败；均恢复后验证 |
| `pnpm test` | Workbench 691、Admin 151、契约包 1870 项通过，无新增跳过 |
| `pnpm format:check` / `pnpm lint` / `pnpm typecheck` / `pnpm build` | 通过；原有设计 Token、能力状态及边界检查保留 |
| `pnpm test:a11y` | 52 项通过；仅覆盖实际自动化范围，不代替真人读屏 |
| `uv run pytest -m integration tests/integration/test_workbench_realtime_synthetic_probes.py -q` | 4 项通过：原 ASR 三会话、原 TTS 两轮完整 48000 样本渲染及取消回归保留；新增延迟/拒绝 native close 两个模式 |
| `uv run ruff check tests/integration/test_workbench_realtime_synthetic_probes.py` | 通过 |

新模式运行正式页面、真实 AudioContext/AudioWorklet 和合成 REST/WS，仅在 native close 边界注入延迟/拒绝。断言生成 succeeded 时可停止尚未播完的音频、关闭仅一次、等待中阻断新播放、确认后可复用会话、失败后刷新仍保留失败、渲染水位停止推进、终态不重复远端取消及零页面错误/出站。两个模式通过 pytest 包装进入现有集成门禁；未移除原探针断言。

## 剩余验收

本次没有证明真实语音生产、双工服务、服务端执行停止/预算释放、物理设备、权限/设备切换、系统音频、HTTPS/WSS 目标部署、长时或完整辅助技术矩阵。真实链路继续依赖 A05/B04/B06 的具名制品，C 自有其余 Minor 和缺口继续推进。PR 合并与以上合成回归不替代 M1/M2/M3、原 41 条 AC 或其 278 个子条件。
