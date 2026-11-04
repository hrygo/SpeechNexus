# 团队 C 上游 a98b2bb 集成交接

核验日期：2026-10-24（Asia/Shanghai）。上一轮属于有效进展；本轮完成此前 `pull` 授权内的基线更新与组合验证。C00–C08 goal 保持 active，父工作包和 G0–G5 均未关闭。

## 基线与保留证明

本地分支 `codex/team-c-c00-versions` 从 `dc14cf8fb0a7250842a8a7697c6884d6d21b2004` 快进到 `a98b2bbfa2576eeee9f5bdf37300605428499d84`，吸收 #65–#68 四个 A/B 提交（恢复查询、runtime准备、受控执行观察及runtime创建）。未提交或推送本地业务改动。

更新前173文件累计包 SHA-256 `d7b7057c561cab78707d3f05574685f442514fab754eb65c7bbad693a91d9a22` 已逐文件验证并保留。先在隔离目标基线应用原包，再验证 README 三方合并：85个上游变化路径仅与本地两份 README 重叠，无未跟踪文件冲突。README 上游网关说明与 C 的正式页面说明位于不同段落，预演无冲突；合并结果逐字节相同。

实际使用 `git merge --ff-only --autostash <固定目标SHA>`；Git 成功恢复 autostash，173文件全部符合预演。Git index为空，无未解决冲突。后续只新增本交接并更新目标/索引，不改产品源码、生成器、锁或运行配置。旧记录的 dc14cf8 是各历史单元事实，不重写为本轮基线。

## 实际验证

- [GitHub 当前 head 的5项检查](https://github.com/hrygo/SpeechNexus/actions/runs/40039450129) 均 success，核对到的 head 均为 a98b2bb；这是上游 CI 证据，不等于 C 组合后全部资格。
- 本地组合后的 Python：ruff format `385` 文件、ruff check、mypy `280` 源文件、imports `7 kept`，unit/contract `1393 passed`。
- 机器契约 `28` 文件通过，生成漂移 up to date；没有将检查工具临时产物写入源码或锁文件。
- Web format/lint/typecheck/test/build 均通过：contracts `147`、Admin `79`、Workbench `286`，共 `512`。
- 本项目 socket-only PostgreSQL `18.6` 临时探针 `117 passed`，workbench infrastructure coverage `92.04%`，无 skip。集群 `.runtime/team-c/pg-u9fauz9i` 已停止，并再次实测 `pg_ctl status` 返回3 / no server running；没有访问共享实例。本轮不重测 A/B 的完整 PG/InnoDB 或真实模型。
- 重建的 Workbench `index-DZYys-Pd.js` SHA-256 与上一轮完全一致：`11acdd578346b2f4ada9b414d8cbd47a984fadbd9a5bf8b4b7a1b4a2a2d2044f`。因此复用该精确制品已有的真实 Chromium 提词、ASR/TTS 合成协议证据；本轮没有重跑这些浏览器探针，也不扩大为新设备或底座资格。

日志、外部检查元数据、预演、合并和 PG 终止记录位于 `.runtime/team-c/pull-a98-*`。累计包更新为174文件：前轮173中169原字节不变、2份README按预演吸收上游说明、目标/索引2文件更新，新增本交接。产品源码保持原字节。已在干净 a98 基线 apply 并核对全部字节、239个本地链接及真实 Git index 保持不变。

当前累计包为 `.runtime/team-c/team-c-current.patch` 和 `.manifest.json`，SHA-256及全部文件hash以该manifest为准；旧 dc14 上的173文件包保存在 `team-c-c05-prompter-dc14cf8.patch` 和对应manifest。最终复核记录位于 `pull-a98-final-integrity.json`。这只是本地集成与证据核对，不将本轮标为新增独立功能终审或完整联合验收。

## 当前依赖与下一接线

工作台 OpenAPI仍为 `0.1.0.dev0`，契约包/bindings仍为 `0.1.0`；预发布内容须同时固定集成 SHA 和生成清单，不能仅凭版本字符串判断相同。A98 仍只生成工作台 `ProjectCreateRequest` / `ProjectPatch` 两个运行时根；完整静态客户端不等于其余响应已有运行时校验。

| 场景 | 已核对的现有 canonical schema / 所需入口 | 责任与解除条件 |
| --- | --- | --- |
| C00/C02 项目与稿件正式接线 | `Project`、`ProjectList`、`TranscriptCreateRequest`、`TranscriptRevisionCreateRequest`、`TranscriptRevision`、`TranscriptView`、`TranscriptList` | A 将现有schema纳入同源生成及正负例；C 消费后接项目/导入/校对/导出消费者，不自造 wire DTO |
| C05 会议与提词 | `Meeting`、`MeetingCreateRequest`、`MeetingSealRequest`、`TeleprompterState`、`TeleprompterUpdateRequest` | A/C 冻结完整响应/错误及生成入口；C 接已有封存核心、会议业务及服务端提词持久化 |
| C04 实时与语音跟随 | `realtime-events.schema.json` 公开事件根与时序样例 | A 发布生成 validator / 可消费公开流；C 接有界媒体和提词核心，B 提供真实流/停止/模型资格 |

这些是具名制品缺口，不要求 A/B 父包先关闭。本轮只准备本地交接，没有向其他团队发送消息或修改 GitHub。正式 Job/Artifact/Operation、发现/资格和真实语音闭环仍继续等待对应生产制品，同时可推进 C 自有业务实现。

## 裁决与范围

1. 按已有 pull 授权快进到固定且检查通过的 a98，而非移动的远端 HEAD；成本是后续新增提交另行核验。
2. 先用完整累计包和三方预演保护本地173文件，再允许 autostash；实际与预演一致，无需手工改冲突或清除他人内容。
3. 旧浏览器证据仅在最终 JS 原字节相同时复用；成本是新基线不因此取得额外浏览器/设备/部署资格。
4. 不越过 A 维护的共享生成入口，列出准确所需 schema；成本是公开会议/实时及稿件接线仍待制品，不把内部核心当完整交付。
