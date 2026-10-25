# 团队 C 证据保留事故与稳定证据根规则

核验日期：2026-10-26，Asia/Shanghai。关联：[阶段 Goal 与验收标准](../plans/2026-10-25-team-c-next-phase-goal-and-acceptance.md)、[T02 映射](2026-10-25-team-c-acceptance-map.json)、[集成验证报告](2026-10-25-team-c-next-phase-integration.md)。

## 结论

2026-10-26 收口 PR #189 时清理团队 C 工作树，删除了位于工作树内、被 git 忽略的 `.runtime/team-c/next-phase/2026-10-25T155546+0800`，其中 82 份证据原件不可恢复。原映射中的证据与源码快照仍按 70ab43d 保留，但**证据原件已不可复核**；本文件记录事实、影响和后续规则，不改写 PR #189 正文或集成报告在当时的结论。

## 事实与时间线

| 时间 | 事件 |
| --- | --- |
| 2026-10-25 15:55 | 证据根建立于团队 C 工作树（`team-c-next-phase`）内的 `.runtime/team-c/next-phase/2026-10-25T155546+0800`，被 `.gitignore` 排除 |
| 2026-10-26 06:38–07:02 | PR #189 rebase 到 `0c45de4`，head `70d11ea`，CI [40242330800](https://github.com/hrygo/SpeechNexus/actions/runs/40242330800) 全14项通过后合入 main `4fcfa61` |
| 2026-10-26 07:02–07:03 | 清理 `team-c-acceptance-map` 与 `team-c-next-phase` 工作树及对应分支；工作树内的 `.runtime` 证据目录随目录删除 |
| 2026-10-26 07:05 | 在 main 复核守卫，出现 86 项 `evidence_invalid`，确认为证据原件缺失 |

## 影响

- 守卫在 main `4fcfa61` 的报告：`evidence_invalid=86`（82 份证据路径 + 3 份 `source_manifest` + 1 份 `metadata`）、`reference_invalid=530`、`mapping_missing=126`，`accepted=false`。
- 82 个 `evidence_registry` 条目全部失效；278 个子条件中有 214 个引用这些证据。
- 全部 278 个条件的验收状态仍为 `not_run`，事故未升级也未降级任何 AC、里程碑或 Gate。
- 集成报告中记录的命令、计数与结论仍在，但对应日志、元数据和源码清单不可再独立核对。

## 原因

1. 证据根放在工作树内部，而工作树是被清理的目标；`.runtime` 被 git 忽略，普通 `git status` 不显示。
2. 清理前只核对了 git 跟踪与未跟踪的源文件是否已被分支或 main 覆盖，未核对被忽略目录中的运行证据。
3. 证据根按仓库相对路径记录，工作树一删即失效，缺少仓库外的稳定位置。

## 恢复尝试

- 工作树删除不经过 Finder 废纸篓，`~/.Trash` 无副本。
- `tmutil listlocalsnapshots /` 无本地快照，无法按时间点恢复。
- `~/.codex/archive/` 仅存后续切片的 `runtime-export-integrity` 证据，不含 T02 文件。
- 结论：不可恢复，后续以新证据重建，不用重跑摘要冒充原件。

## 稳定证据根规则（立即生效）

- 证据根固定在仓库外的稳定目录：`/Users/hrygo/.codex/team-c-evidence/<YYYY-MM-DDTHHMMSS+0800>/`；守卫以 `--evidence-root` 指向该目录，映射内部仍使用相对文件名。
- 采集证据时先建目录并写入 `manifest.json`（时间、基线/head、命令、范围、结果摘要），再写入日志与元数据。
- 清理任何工作树前必须执行 `git -C <worktree> status --ignored --short`，确认无证据或已归档到稳定根；不得只依赖普通 `git status`。
- 证据原件不入库；可共享的结论仍写入 `docs/audits/`，历史报告保持原时点事实。
- 守卫对缺失证据返回非零是本规则期望的行为，不以删除条目或放宽校验消除。

## 复核

```bash
git -C /Users/hrygo/Documents/SpeechNexus rev-parse HEAD
uv run python scripts/dev-env/check-acceptance-map.py \
  --evidence-root /Users/hrygo/.codex/team-c-evidence/<stamp>
```

本文件只记录保留规则与事故，不改变任何 AC 状态、映射条件或 Goal 范围。
