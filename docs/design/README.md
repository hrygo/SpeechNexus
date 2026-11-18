# SpeechNexus 设计文档

本目录保存跨组件、以判据为中心的设计：它们定义目标行为、拒绝的候选与验收断言；实施排期在
[计划目录](../plans/README.md)，既有架构与组件说明分别在[架构文档](../architecture/README.md)与
[组件说明](../components/README.md)。

| 文档 | 职责 |
| --- | --- |
| [同步转写的租约结算](sync-transcription-settlement.md) | 同步 ASR 成功后如何由独立结算运行时归还租约与预算；持久 root、释放前置证明、未知形状归属、幂等与权限面 |
| [外部托管引擎的运维自证声音资格](operator-attested-voice-qualification.md) | 以运维自证替代 node-controller 观测的第二条内建声音资格来源；信任模型、时效与写入边界 |
| [UI/UX 设计框架与 Token](ui-ux-design-framework-and-tokens.md) | 共享样式、设计变量、诊断状态映射与静态校验器 |

设计文档采用终态语态；已落地与未落地的边界在各自文首说明，并以当前代码、契约与实测为准。