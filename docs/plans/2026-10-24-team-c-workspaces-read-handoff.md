# 团队 C：工作空间读取与2a46依赖集成交接

2026-10-24，base_sha=head_sha `2a46c6e2c7e5ea2f9d29b475bb0524c0b344ef21`，分支 `codex/team-c-c00-versions`。按[实施计划](2026-10-24-team-c-workspaces-read-plan.md)将Admin工作空间列表/详情接入正式入口。全部C00–C08保持open/status:in-progress，完整Goal active；没有commit/push或新跨chat消息。

## 依赖集成与已实现范围

- GitHub核对#90/#91/#92/#93 merged及精确merge SHA，eb299f7→2a46c6e固定目标。323文件在干净目标树完整预演，唯一交集source-inventory保留A admin摘要与C workbench摘要；安全FF后322字节不变、全部实际字节匹配。未修改上游CI或生成语义。
- A #93提供同版admin/admin-http的Workspace/WorkspaceList/ErrorResponse和仅listWorkspaces/getWorkspace的校验客户端；source0.1.0.dev0、bindings/package0.1.0；生成器SHA-256 `fa5cdbec660faa8ee1563d67e6b9fa62f2674e9c2e8cbc5bae8df9f480963055`。浏览器只消费两个GET，不使用计划中的POST/PATCH，不手写DTO、分页默认值或假配额。
- 正式Admin工作空间区显式读取列表/下一页/详情；省略默认query，同源、redirect:error、cache:no-store、15s期限、1MiB生成JSON预算。列表重复id、非终页游标不前进或周期拒绝；终页忽略next_cursor并禁用下一页；分页失败保留历史列表并仅显式重试原cursor。
- 详情交叉校验原id，切换选择或重读列表清除旧详情；新读取取消旧请求/代次隔离，隐藏/pagehide/卸载撤销当前资格，返回不自动读取。StrictMode保留有效会话。页面显示共享分组、active/disabled、元信息与策略版本，不将Workspace当用户/安全隔离。
- 失败仅稳定code，无服务端message、内部端点、身份或新持久化；React纯文本呈现名称。详情读取后焦点转到详情标题，样式复用现有Token/任务中心控件。

## 验证与复现

- 客户端12、会话13、页面6项TDD由缺模块RED到GREEN；正式App标题断言1 RED后通过。相关34项（含App既有3）；最终Web1604=contracts969+Admin110+Workbench525，五门禁及28契约/漂移通过。
- 生产bundle Chromium156.0.8078.4→合成生成HTTP：1context/1page、8 GET/0写请求，显式读取、分页503历史与原cursor重试、合法终页非空/重复cursor可见且不继续请求、迟到旧详情、404与错误id/无正文泄漏、隐藏/恢复无自动请求、360px/基本focus+Tab；出站/意外异常/serverErrors为0。隐藏为显式DOM故障注入，不代表后台节流资格。
- 集成后Python unit/contract 3404 passed/39.43s，无skip；format579、ruff、mypy428、7依赖边界，28契约/漂移通过。新增Web消费者未改Python/SQL。
- 唯一fresh终审0 Critical/1 Important/0新Minor：本地错误拒绝合法终页。7项RED后同时修复client/session，34 focused及全Web1604 GREEN；非终页周期防护保留，未派复审，不将原报告当修复后批准。审查者另以真实导航确认待决连接关闭/返回无自动读，未证明BFCache命中。
- 私有socket-only PG296/21.29s及生产bundle→真实C HTTP/PG/fake LLM会议/删除浏览器通过，独立pg_ctl3/无pid；未开启coverage。C03合成4jobs/8writes、ASR3sessions/10writes、TTS1session/5writes/3utterances/96000样本同版回归通过，各出站/异常/serverErrors为0。
- 全部最终门禁、隔离PG与既有C03/ASR/TTS及真实C HTTP浏览器回归、一次终审、增量和累计补丁精确hash写入 `.runtime/team-c/integration-2a46c6e/`。累计334路径（旧323中317不变/6变化，新增到包11路径=10新文件+原tracked App.test新改动）；累计包 `.runtime/team-c/team-c-current.*`；旧323/2830d6d6保留before-2a46-integration，未发布到main。

```bash
pnpm build
node scripts/dev-env/admin-workspaces-probe.mjs --output .runtime/team-c/workspaces-browser
```

## 未验证及交接

A #93仅生成读取前置；A真实PG/GET/组合根在后续分支，尚未计入本轮main。新页面在未部署服务时准确显示读取失败，合成GET不授真实Admin服务资格。后续消费A同版真实GET，再逐项接模型绑定/预算/策略/诊断；工作空间创建/修改和配额页面未实现。

#91的B实测修正为CustomVoice固定内网服务可合成且ASR回环一致，但Base/VoiceDesign、流式/延迟/并发/发布仍未验；C只吸收报告，不运行模型、不代授C03或G2资格。#90/#92四分片完整性和双库来源属于上游CI，不冒称C实测InnoDB或远端加速。

八项既有Minor继续保留，新增终审发现以本轮ledger及最终验证为准。真实部署/设备/网络/跨浏览器完整a11y/Linux/GPU、独立副本和恢复及G0–G5继续；完整Goal不因本单元完成而关闭。
