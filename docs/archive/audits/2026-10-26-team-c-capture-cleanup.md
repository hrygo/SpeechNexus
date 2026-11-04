# C04：ASR 采集停止与资源释放确认

2026-10-26，基于 main `e959c43650ce74433d70a079e1c2c9608035d2c4`。对应 [C04 #39](https://github.com/hrygo/SpeechNexus/issues/39) AC2/AC5 的局部实施，延续 [#182 的发送/播放修复](2026-10-26-team-c-realtime-integrity.md)。完整 AC 仍未验收。

- 底层采集先停止 track、断开本地节点并通知 stopping；只有已持有 AudioContext 关闭完成后，closed 才确认资源释放。失败保留 `capture_cleanup_failed`，重复停止共享同一 Promise。
- 初始化中止也等待已持有资源清理，失败优先保留清理错误；无法取消的权限提示不阻塞中止等待，迟到返回的 track 仍立即停止。未取得设备与已持有设备分别处理。
- 控制器保留 pending/已持有采集，stopping 或 failed 时阻断新会话/采集。迟到初始化仍由原 owner 清理，旧 generation 不更新新上下文；刷新、REST 终态和接受回执不能覆盖本地失败。
- 原生采集中断立即停止本地链路并请求服务端中止，独立等待本地资源确认；已读到服务端释放事实时，不因随后本地关闭失败重复发送取消。页面分别显示“正在停止”和“释放未确认”，失败建议关闭浏览器标签页，不宣称远端资源已释放。

## 实际验证

本地 Python 3.14.9 / Node 26 / Chromium 156.0.8078.4；图谱未索引，直接核对所改源码，未改变索引。日志与合成截图保存在本工作区 `.runtime/capture-cleanup/`。

- RED：底层5项、控制器2项、页面2项分别实际失败；另外复现原生中断没有及时通知/中止、初始化关闭错误及重复取消事实。将底层改回提前终态的变异使4项失败，恢复后通过。
- `pnpm test`：Workbench704、Admin151、契约包1870项通过；`format:check`、`lint`、`typecheck`、`build`通过；自动可访问性52项通过，覆盖范围不扩为真人读屏。
- `uv run pytest -m integration tests/integration/test_workbench_realtime_synthetic_probes.py -q`：8项通过。原ASR/TTS与#182关闭回归保留；新增主动中止/原生context中断 × native close延迟/拒绝共4项正式页面、实际Worklet及fake麦克风验证。断言track及时结束、context关闭仅一次、等待/失败不允许替换会话、原键/原体重放、远端已释放后不重复取消、停止后不再发送音频及零出站/页面错误。
- 一次构建因TypeScript收尾分支缩窄错误失败，旧dist上的2项新探针失败保留，不计通过；另一次新探针未等重放回执就比较写入记录，4项失败保留。修正为等待真实原回执后核对原键/原体，未放宽断言或期限；最终8项在当前构建实际通过。

这些是本地生命周期与合成生产者消费者证据，不证明物理设备/系统权限、真实ASR/TTS/双工、GPU停止与预算、目标HTTPS/WSS、跨浏览器、长时、读屏或物理拓扑。其余C自有缺口继续实施，A05/B04真实流制品及目标设备资格继续按具名依赖联验；全部原41条AC及278子条件不据此升级。
