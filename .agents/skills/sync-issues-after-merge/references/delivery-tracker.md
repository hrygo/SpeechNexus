# 当前交付清单与本地视图

仅当相关 issue 属于当前交付清单，或其变化影响跟踪视图时读取本页。

- [总控 #336](https://github.com/hrygo/SpeechNexus/issues/336)与当前 issue：范围、事实、具体任务、AC 和必要证据的权威入口，正文须自足。
- [交付清单](../../../../docs/plans/delivery-issues.json)：只登记当前层级和制品依赖；新增交付或依赖变化同步，不复制当前进展。
- [本地矩阵](../../../../docs/plans/delivery-tracking.md)：从 GitHub 读取当前排期、负责人、实现/集成/验证、阻塞、证据和生命周期；未登记的 open issue 单列为覆盖缺口。
- [刷新工具](../../../../scripts/dev-env/refresh-implementation-tracker.py)：只读 open issue，并按需补读清单中的 closed issue；不读取旧卡、历史原文或迁移散列，不证明业务 AC 通过。

正式交付正文保留与清单 ID 一致的隐藏标记 `<!-- speechnexus-delivery:ID:v3 -->`。优先级、排期、交付负责人、实现、集成、验证、阻塞、证据各用唯一的 `字段：值` 行；总控另有“下一队列”。缺失或重复字段显示未知，不用旧值补齐。普通反馈不强制采用这些机器字段。

远端写后回读通过，再从仓库根刷新：

```bash
python3 scripts/dev-env/refresh-implementation-tracker.py
```

先检查输出目标已有 diff；有他人或来源不明改动时，用 `--output <临时文件路径>` 生成并核对。`--snapshot` 只供离线核对，时间未知不视作实时；旧格式不再参与日常刷新。历史决定按需查 Git/GitHub，无需为刷新补齐旧卡。

读取或校验失败保留旧输出并报告，未知字段不补猜测；不因刷新自动提交或改远端。当前清单与原生关系冲突时报告具体差异，不自动改关系或扩展全库维护。
