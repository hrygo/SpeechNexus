# SpeechNexus 审查记录

审查结论以记录中的时间、范围和证据为限；文档审查不等于实际系统通过验收。

- [2026-10-22 设计审查](2026-10-22-speechnexus-design-review.md)：方案缺陷、修正、上游核验和未验证边界。
- [2026-10-22 开发环境就绪审查](2026-10-22-development-environment-readiness.md)：本机工具链、独立服务、验证证据、推理缺口与后续入口。
- [2026-10-23 架构与契约合规审查](2026-10-23-architecture-contract-compliance.md)：上一轮阻断项复核、OpenAI SDK 兼容、契约严格性、SOLID / DRY、容量账目收口与残余风险。
- [2026-10-23 UI/UX 设计框架与 CI 门禁终审](2026-10-23-ui-ux-design-framework-audit.md)：多层次排版对比度审查、DTCG Token 资产、矢量品牌、响应式与 CI 拦截能力。

[项目主设计](../speechnexus-product-architecture.md) · [文档索引](../README.md)

- [2026-10-23 架构与契约修复闭环](2026-10-23-architecture-contract-repair-closure.md)：F01–F10、R1/R2 的修复、实际验证和运行资格边界。
- [2026-10-23 集群协调重构后的架构与契约审查](2026-10-23-architecture-contract-post-refactor-review.md)：运行角色最小权限、双库门禁、契约结果覆盖与端口原型一致性、SOLID / DRY 与开放项。
- [2026-10-24 B00 MegaCube 环境与能力缺口前检](2026-10-24-b00-megacube-environment-and-capability-gap.md)：主机与 GPU 事实、授权释放与制品获取（含摘要与许可）、首个真实 ASR 生命周期探针与停止证据、逐能力缺口与 B01 首个单元。
- [2026-10-24 B02 受限控制器容器生命周期探针](2026-10-24-b02-controller-container-lifecycle-probe.md)：合成容器上的 prepare / ready / stop 与停止双证据、`Init` 缺口、清理结果与未验证边界。
- [2026-10-24 B02 控制器 HTTP 外壳与持久意图探针](2026-10-24-b02-controller-http-shell-probe.md)：真实控制器进程的组件凭据、prepare → ready → stop、daemon 侧加固事实、停机双证据与重启后持久意图。
- [2026-10-24 B02 有界控制器与真实 ASR 探针](2026-10-24-b02-real-asr-controller-probe.md)：固定内存上限与 HTTP 就绪、0.6B/1.7B 合成语音/静音/长非语音实测、CUDA 失败与缓存诊断、停止清理及历史 Issue 状态更正。
- [2026-10-24 B03 有界媒体与真实录音输入探针](2026-10-24-b03-bounded-audio-probe.md)：五种格式、源时钟/尾部、文件 VAD、合成与两段授权录音实测及未验证边界。
- [2026-10-24 B03 真实文件 ASR 与首次长窗口开销](2026-10-24-b03-file-asr-probe.md)：0.6B/1.7B 文件/VAD/识别、真实 token 截断、首次/立即重复耗时与预热资格要求。
- [2026-10-24 B02 真实 ASR 预热就绪门探针](2026-10-24-b02-warmup-gate-probe.md)：`http_warmup_ok` 在真实 0.6B/1.7B 容器上的 `warming_up → ready` 序列、首轮与立即重复耗时、停止双证据与未验证边界。
- [2026-10-24 B03 Qwen3-TTS 真实引擎可行性复核](2026-10-24-b03-tts-engine-feasibility.md)：更正此前误判；`--omni` 在线服务、旧 runner 兼容、显存预算、两条真实合成请求与 ASR 回环、未验证边界。
- [2026-10-24 B03 TTS 适配器真实探针](2026-10-24-b03-tts-adapter-probe.md)：通过已提交适配器合成 1.7B CustomVoice、WAV/MP3 交付、24 kHz→16 kHz 解码与 0.6B ASR 回环，以及停止/清理证据与未验证边界。
