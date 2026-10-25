# 编排清单与本地快照

仅当相关 issue 属于当前实施编排，或其变化确实影响跟踪快照时读取本页。远端状态须重新核实；带日期清单与矩阵均不能替代验收证据。

- [编排清单](../../../../docs/plans/2026-10-23-implementation-issues.json)：工作包、Epic、Gate、总控及具名制品依赖；与当前原生关系不一致时报告，不自动改关系。
- [三团队方案 §12](../../../../docs/plans/2026-10-23-three-team-delivery-plan.md)：用于分段交付与整包关闭的核对。
- [刷新工具](../../../../scripts/dev-env/refresh-implementation-tracker.py)：只读 GitHub 并生成本地矩阵，不能证明 AC 通过。

远端写后回读通过，再从仓库根刷新：

```bash
python3 scripts/dev-env/refresh-implementation-tracker.py
```

先检查输出目标已有 diff；存在他人或来源不明改动时，用 `--output <临时文件路径>` 生成并核对，不覆盖现有文件。不把 `--root-body` 输出直接覆盖总控的四部分正文。

失败时报告快照未更新，保留已核实的远端结果；不因刷新自动 commit、push 或创建 PR。清单不再代表当前编排时，先依据[文档索引](../../../../docs/README.md)定位有效入口，不扩展到全库 issue 改写。
