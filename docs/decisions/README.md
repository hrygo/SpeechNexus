# SpeechNexus 架构决策

ADR 记录已确认的范围和理由；[项目主设计](../speechnexus-product-architecture.md)与配套规范负责技术基线，资格报告负责实际验证结果。

- [0001：产品边界与首期身份范围](0001-product-boundaries-and-authentication-scope.md)（accepted，2026-10-22）。
- [0002：语音模型、推理引擎与加载策略](0002-speech-models-engines-and-loading.md)（accepted，2026-10-22；首选验证路径，未取得生产资格）。
- [0003：工作台 LLM 与业务内容的网络边界](0003-workbench-llm-network-boundary.md)（accepted，2026-10-22；仅本机 / 内网 LLM，内容不得出网，待实现与部署验证）。
- [0004：集群就绪架构与单机 MVP 同构部署](0004-cluster-ready-architecture-and-single-node-mvp.md)（accepted，2026-10-22；架构层集群原生、MVP 单机同构部署，多节点运行与高可用待 P5 验证）。

- [0005：完整执行与数据生命周期契约](0005-complete-execution-and-data-lifecycle-contracts.md)（accepted，2026-10-23；契约/草案修复，业务运行时待实现）。
- [0006：集群协调与存储可移植性](0006-cluster-coordination-and-storage-portability.md)（accepted，2026-10-23；集群能力不降级，不采用 etcd；受控应用事务，PG 正式交付与 MySQL 关键协调验证；重构待实施）。
- [0007：遗留协调原型的退出与删除](0007-legacy-coordination-retirement.md)（accepted，2026-10-23；逐项迁移有效业务不变量后，退出旧 PG 必跑门禁并删除废弃原型；条件待验收）。
- [0008：控制器观察前的持久 runtime 意图](0008-runtime-intents-before-controller-observation.md)（accepted，2026-10-24；区分无观察协调快照与实际控制器响应，扩展同库 outbox；生产事务/派发待实现）。

- [0009：工作台显式模型绑定与配置发现的 HTTP 版本迁移](0009-workbench-explicit-binding-http-version.md)（accepted，2026-10-25；保留严格 v1、新增 v2 显式绑定与状态配置发现，消费者按路径迁移）。
- [0010：私有音频回执与旧 JSON 消费者兼容](0010-private-audio-receipt-compatibility.md)（accepted，2026-10-25；独立音频读取视图、复用原回执及样本账本，先升级读取者再启用生产者）。

[文档索引](../README.md)
