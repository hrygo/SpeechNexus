# 团队 C：9eea859 依赖集成交接

2026-10-24，本机分支 `codex/team-c-c00-versions` 已从 `7cd1887905dd57e8c647238416b6030049833e38` 安全FF至 `9eea85987232eaebbcbd6768c46fe0a084a4d177`。本轮 base_sha=head_sha 为后者，没有创建提交或推送；完整C00–C08 Goal保持active。

## 范围与保留

- GitHub实测[#82](https://github.com/hrygo/SpeechNexus/pull/82)、[#83](https://github.com/hrygo/SpeechNexus/pull/83)、[#84](https://github.com/hrygo/SpeechNexus/pull/84)均已合并，包含租约释放、私有文件ASR适配器和admission原始命令/请求存储。未修改A/B owner实现或运行其服务、模型。
- 上一[会议页面交付](2026-10-24-team-c-meeting-ui-handoff.md)266文件包SHA-256为 `d4c209719ba8666984620a18c5c86aeb10dc12b38e7d5c39493cde56975edbfd`。干净目标完整预演：264个C文件字节不变，中英文README三方合并保留双方内容。
- 临时仅反向应用已备份、已校验的自有README hunks，再FF及应用预演结果；未使用reset/checkout/stash。实际266文件hash与预演一致，无暂存。
- 依赖、生成器未改，未手改生成产物。workbench `0.1.0.dev0`、bindings `0.1.0`、421条HTTP样例保持；上游release_lease入口随main验证，不意味着C消费内部执行协议。
- 新累计包及完整校验位于 `.runtime/team-c/team-c-current.patch` / manifest；旧266包保留为 `.runtime/team-c/team-c-before-9ee-integration.*`。实际文件数、SHA、干净apply、逐字节和链接检查以manifest为准。

## 本轮验证

- Python unit/contract **3006 passed /38.42s/无测试skip**；format535、ruff、mypy398源文件、7依赖边界、28源契约及生成漂移通过。
- Web五门禁通过，**1268=contracts805+Admin79+Workbench384**，无测试skip。沿用Node26.11.1/pnpm12.14.0；Node与仓库26.12.1声明的差异未变。
- PostgreSQL18.6 socket-only独立探针 `--browser --coverage`：**295 passed /19.64s/94.42%**，独立pg_ctl退出3且无pid；未访问共享库或运行A双库迁移/协调联验。
- Chromium156.0.8078.4生产bundle→真实C HTTP→私有PG/fake LLM：一个上下文/五写请求，两次实际提交后丢回执原键恢复、LLM关闭恢复、quota跨功能保留精确Unicode、纯文本/过期隐藏/删除及物理清理后草稿、360px/基础focus+Tab，出站与意外异常均0。
- 本轮证据在 `.runtime/team-c/integration-9eea859/`：preflight/result、PR元数据、门禁日志、PG停止和独立保存的browser结果/截图。复现先 `pnpm build`，再 `uv run python scripts/dev-env/workbench-pg-probe.py --pg-bin /path/to/postgresql/bin --browser --coverage --output .runtime/team-c/integration-probe.json`。

## 边界与下一动作

#83不是公开ASR/持久dispatch/结果发布/完整status或abort；#84不是完整admit_recipe或公开Operation/receipt消费者；#82不代表C已验实际调度/预算/双库/GPU。A/B专业证据仅保留来源，不代验。

生产组合根、owner迁移/grants和Meeting删除图/cleanup仍由A串行同版接入。此前六项Minor原样延期；本轮纯集成不追加A/B资格审查。完整Goal/G0–G5保持未完成。

下一C业务单元接工作台删除页面：消费既有冻结预检、完整确认和当前状态协议，保留未知原键、完整范围与独立副本语义；先更新issue再实施，不把下一步记成已实现。
