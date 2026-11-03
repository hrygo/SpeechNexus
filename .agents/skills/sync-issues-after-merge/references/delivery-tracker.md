# 编排清单与本地快照

仅当相关 issue 属于当前实施编排，或其变化确实影响跟踪快照时读取本页。远端状态须重新核实；带日期清单与矩阵均不能替代验收证据。

- [当前总控 #336](https://github.com/hrygo/SpeechNexus/issues/336)及[稳定映射](../../../../docs/plans/delivery-baseline.json)：产品目标、独立交付、全部原要求与必要制品依赖；与当前原生关系不一致时报告，不自动改关系。
- [完整本地矩阵](../../../../docs/plans/delivery-tracking.md)：当前排期、负责人、实现/集成/验证和生命周期来自 GitHub，迁移进展与历史勾选单列。旧卡重组关闭不计业务完成，也不替代原 Gate 的用户放行。
- [原文迁移基线](../../../../docs/plans/2026-11-03-issue-migration.json)：历史要求与来源散列；新增交付或范围变化先同步稳定映射，再刷新视图。未登记的 open issue 会单列为覆盖缺口。
- [刷新工具](../../../../scripts/dev-env/refresh-implementation-tracker.py)：只读 GitHub 并生成本地矩阵，不能证明 AC 通过。

远端写后回读通过，再从仓库根刷新：

```bash
python3 scripts/dev-env/refresh-implementation-tracker.py
```

先检查输出目标已有 diff；存在他人或来源不明改动时，用 `--output <临时文件路径>` 生成并核对，不覆盖现有文件。当前格式不提供 `--root-body`；历史[旧清单](../../../../docs/plans/2026-10-23-implementation-issues.json)仅通过显式 `--manifest` 与独立 `--output` 读取，不覆盖当前矩阵或总控正文。

失败时报告快照未更新，保留已核实的远端结果；不因刷新自动 commit、push 或创建 PR。清单不再代表当前编排时，先依据[文档索引](../../../../docs/README.md)定位有效入口，不扩展到全库 issue 改写。
