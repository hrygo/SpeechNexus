---
title: "A02 共享媒体制品与私有音频检查交接"
status: review
version: "0.2.0"
date: 2026-10-26
---

# A02 共享媒体制品与私有音频检查交接

## 目标

解除 A02 真实音频检查的共享媒体前置：从既有 B 实现提取独立制品，
保留网关原消费者和异常身份，在 A 已有受限进程内运行实际 decoder，输出有界媒体事实。
关联 [A02 #21](https://github.com/hrygo/SpeechNexus/issues/21) 与[团队 A Goal](2026-10-23-team-a-goal.md)。
本单元不启用公开 ready 或完成 A02 AC。

## 进展总结

提取源固定为 `230031043a76ac6c2d04ee6bb2fe93ca38f991c5`；收尾 PR 已安全吸收
main `54daef6ae4792b69a0e5e8645b09240ff715082e`，此来源版本及原 blob 不变。
本文件与实现一并提交评审，尚未合入或由 B 接收；后续安排交新负责人决定，见[最终交接](2026-10-26-team-a-final-handoff.md)。
独立包 `speechnexus-media 0.1.0.dev0` 保持 recipe `pcm-decode-1`。
原生类型文件逐字节不变，decoder 只改原生类型 import；
源 blob、原件和制品文件 SHA256 见[源码映射](../../packages/media/source-inventory.json)。
B 原路径兼容导出同一对象，没有第二套 MediaError，也未改 B 的 codec/停止/回执/预算语义。

私有 `speechnexus-inspect-audio` 命令只读固定 `/input/media`，
服务端显式给格式、大小/时长/期限及 raw PCM 元数据。
哈希和全量解码后只输出摘要、长度、采样率、声道、样本数与时长，不输出 PCM、路径或 vendor 错误。
初版检查限 PCM WAV/显式 PCM；FLAC/MP3/Ogg 继续由原 decoder 支持，尚未纳入此检查 profile。

### 完整性缺口与保护

原 decoder 对合成 4800 帧 WAV 截去尾部 20 字节后仍正常结束，仅解出 4795 帧。
新私有检查同时核对 RIFF 长度及 WAV 头采样率/声道/样本数与真实解码事实；
原长度不变和仅修正外层长度两种截断均拒绝，没有改变 B 原 decoder。
其他容器完整性、公共错误映射及生产资格继续待验，不能据 codec EOF 宣布 ready。

### 已执行验证

| 范围 | 结果和边界 |
| --- | --- |
| 原网关单元集 | `uv run pytest -q tests/unit/gateway --no-cov`：1225 通过，含旧媒体/执行消费者；不是模型或数据库资格 |
| 新媒体检查 | `uv run pytest -q tests/unit/media --no-cov`：16 通过，含真实合成 WAV、摘要/样本、两种截断、限额、取消、期限与脱敏 |
| 实际受限进程 | 新媒体集含 2 项实际 decoder：最小只读镜像复制解释器/锁定依赖/共享包，经原 supervisor 禁网络/派生；合法 WAV 成功，损坏输入失败且无部分输出，runner 均确认 close |
| 原边界预检 | 原 `test_real_worker_sees_only_readonly_input_and_no_host_environment`：1 通过；无宿主策略加载/修改 |
| 制品构建 | `uv build --all-packages`：五包 wheel/sdist 成功，媒体 wheel 内含源码映射 |
| 树外 wheel | 临时 cwd、Python `-I`，从 wheel 加载并禁止 backend/gateway/controller/contracts 导入，实际解码、摘要和样本核对通过 |
| 源映射 | 固定 main 原件与两文件哈希/字节核对成立，仅允许 decoder import 路径变化 |
| 静态/格式 | Ruff 全库格式/检查通过；mypy 848 文件通过；8 条依赖边界通过；文档链接与 Prettier 通过 |
| 部署依赖 | Python Dockerfile 补媒体包 COPY；仅按 COPY 清单重建临时上下文，CPython 3.14.9 执行 frozen/no-dev/no-editable 安装及原入口导入通过；Compose config 通过，未实际构建 Docker 镜像 |

本地 wheel：`.runtime/media-package-build/speechnexus_media-0.1.0.dev0-py3-none-any.whl`，
SHA256 `076a0236835c5f48b406db03e74b1578abab4aa20b279847d940e18a3bad8489`。
独立消费/源映射回执在同目录 `verification.json`；这是私有制品，不是批准的运行镜像。
未运行全库数据库、模型、公开链或物理拓扑验收。

## 任务清单

- [x] 独立包、可选 audio 依赖、B 兼容入口、workspace/锁及禁止反向服务依赖门禁。
- [x] 私有命令、WAV 长度/样本保护及有界无正文回执。
- [x] 原消费者、合成媒体、实际进程、wheel 和源映射验证。
- [ ] B 复核源映射、兼容入口和容器完整性要求，形成共同可接收版本。
- [ ] 收尾 PR 完成评审与集成；PR 候选不算共同 main，也不代替 B 接收。
- [ ] 冻结/构建部署镜像、解释器/原生依赖；验容量、旧进程恢复与格式完整性。
- [ ] 先冻结公共错误/投影，再接 AssetInspector 和真实 HTTP/PG/MinIO，验 ready/原键/重启/丢确认/TTL。

## 验收与后续依赖

本单元只验证共享制品及私有检查入口。真实公开链仍 0/2，F01–F12 完整公开矩阵未验，
关键违规基线未知；组件测试不填充完整 KR。

| 依赖 | owner | 制品和解除条件 |
| --- | --- | --- |
| 媒体共同版本 | A 打包、B codec | 复核候选/旧入口兼容，提供可获取的集成制品，保留语义与完整性边界 |
| 实际检查部署 | A/环境 owner | 固定镜像/摘要、容器格式/限额及进程恢复方案；测试镜像不是正式装配 |
| 权威执行输入 | B #142、A 接纳 | 已批准 binding/model/image/参数/上下界、可信 readiness/资格，不从草稿或 fixture 补默认 |
| 真实执行与结果 | B #142 | 原身份回执、停止+释放双事实及 TTS 上界修复，由 A 原事务消费 |
| 联合消费 | A/C、隔离环境 owner | 公开端点和隔离 PG/MinIO 凭据引用，同一 SHA 的 SDK/页面/模型证据 |

当前云环境未配置真实模型端点、权威 binding/readiness 与隔离服务凭据。
不操作 B 独占硬件，不启用假 ready/发布，不因本单元释放执行预算。
后续实施、优先级与窗口由新负责人按这些具名输入重新安排，原工作包范围保留。
