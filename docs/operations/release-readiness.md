# 上线就绪状态（快照）

> **核对时点**：2026-10-28 13:45 CST（UTC+8）
>
> **候选**：`origin/main` @ `57a7e4145edd7daaa2a4812f2286f4e79258d6de`
>
> **口径**：这一页是快照，不是自动更新的看板；状态、证据和负责人均以核对时点的 `origin/main` 为准。

## 1. 一屏结论

**结论：No-Go。**

一句话：同步 ASR 已有成功终态代码，但公开 200 还没有证据；同步 TTS 在候选主干仍固定 503。G4、G5、G6、G7、G9、G10 仍是未实现。

必须清掉（按对首发用户路径与放行的影响排序，不按编号）：

1. **G1 成功终态**：用户上传音频后可能一直等，最后超时，拿不到转写。
2. **G2 TTS 503**：用户点朗读或配音直接失败，没有音频。
3. **G4 生产资格**：不能把实验数据当生产放行依据，对外承诺没有证据。
4. **G5 Job planner**：异步任务提交后不执行、不重试、不出结果。
5. **G9 最终验收**：没有一条命令能对候选给出可复核的 Go/No-Go，放行靠口头。
6. **G6 生命周期**：发错版本无法撤回或回滚，客户端缓存不刷新。
7. **G7 对账入口**：卡住的执行占着容量，用户持续遇到 503，运维只能手工查库。
8. **G10 质量门禁**：噪声、重叠、高并发和长稳下的质量与容量未知，对外承诺会翻车。

G8 已闭合，不在必须清掉的清单里。

## 2. 缺口矩阵

> `现状` 只允许三档：**已闭合** / **有代码未验证** / **未实现**。`预计` 列取 DEL-120 派工表的数值（已闭合项写 `—`），本页不重估、不排期。

| 缺口 | 影响（用户能感觉到什么） | 现状 | 证据（可复核命令/文件:行/PR） | 负责人 | 解除条件 | 预计 |
| --- | --- | --- | --- | --- | --- | --- |
| **G1 成功终态** | 上传音频后可能一直等，最后超时，拿不到转写。 | **有代码未验证** | `#284`（`053c36e`）已合入；`execution-gateway/src/speechnexus_gateway/application/execution_completion.py:55` 写 `record_stop(..., SUCCEEDED)`；`execution-gateway/src/speechnexus_gateway/infrastructure/file_execution_worker.py:285` 已接线。公开 `POST /v1/audio/transcriptions` 200 无证据（未验证：未重跑真实网关）。 | GPU 执行（#284 已交；公开 200 复验未建卡） | 公开 ASR 返回 200 和非回显正文，并记录 submit_intent_id、网关请求 ID、对象 SHA；非作者复核。 | 0.5–1.5 d |
| **G2 TTS 503** | 点朗读或配音直接失败，没有音频。 | **未实现** | `backend/src/speechnexus_backend/transcription_runtime.py:171` 只传 `transcriptions`；`backend/src/speechnexus_backend/app.py:151` 的 `speech` 默认 `None`；`backend/src/speechnexus_backend/speech/interfaces/http/speech.py:360` 返回 `backend_unavailable`。DEL-122 分支 `c59911e` 有生产装配，但未合入 `main`。 | 契约集成（DEL-122 分支）；GPU 执行（DEL-130 部署，in_review） | 合入 DEL-122 的生产装配并在真实部署验证：公开 `POST /v1/audio/speech` 200、WAV 大于 44 字节且 voice/format 正确。 | 1.5–2.5 d |
| **G4 生产资格** | 对外不能承诺生产可用；按实验数据放行会把风险留给用户。 | **未实现** | `jq '{release_tier,overall_status,required_scenarios_passed}' deploy/engines/file-asr-audio/evidence/gb10-20261027/qualification-report.json` 输出 `experimental / incomplete / false`；`deploy/engines/file-asr-audio/evidence/gb10-20261027/qualification-report.json:10`；`docs/operations/qualification-report.md:284` 列未验证项。 | 证据文档（DEL-114 已交格式/样例）；GPU 执行（原始测量） | 报告为 `production` / `pass` / `required_scenarios_passed=true`，并用 live DB 证据校验。 | 1–2 d 汇总；测量另计 |
| **G5 Job planner** | 异步任务提交后不执行、不重试、不出结果。 | **未实现** | `backend/src/speechnexus_backend/job_dispatch_runtime.py:255` 抛 `DispatchDependenciesUnavailableError("Job dispatch planner is not assembled")`；`rg -n "planner is not assembled" backend/src/speechnexus_backend/job_dispatch_runtime.py`。 | 契约集成（未建卡） | 生产 `DispatchPlanner` 和 result publisher 装配，`/v1/nexus/jobs` 有成功、重试、重启恢复证据。 | 2–3 d |
| **G6 生命周期** | 发错版本无法撤回或回滚，客户端缓存不刷新。 | **未实现** | `docs/operations/publish-binding-revision.md:77` 写明没有受支持的 CLI/HTTP retire，head/activate/rollback 未实现；`backend/src/speechnexus_backend/publish_binding.py:5` 写明不创建、不修改、不激活；#285（`3cd9b26`）只推进 catalog revision/ETag。 | 契约集成（DEL-138 进行中） | 受支持的 retire/activate/rollback（或等价 head CAS），ETag 变化，在途执行不受影响，审计可追。 | 2–3 d（DEL-119 已完成） |
| **G7 对账入口** | 卡住的执行占着容量，用户持续遇到 503，运维只能手工查库。 | **未实现** | `docs/operations/reconciliation-recovery.md:15` 写明当前没有面向运维的产品命令；`docs/operations/reconciliation-recovery.md:222` 写明 quarantine-releases 契约有声明、生产无 handler。 | 证据文档（DEL-124 runbook，blocked）；产品入口归契约集成 | 受支持的 CLI/HTTP 入口按原身份查证并收口 unknown，不重放；覆盖 live unknown 行。 | 0.5–1 d |
| **G8 证据校验** | 发布证据可能“假绿”，把不合格版本当合格。 | **已闭合** | `scripts/dev-env/verify-release-evidence.py:1040` 对 `--binding-definition` 调用 `check_definition`；`tests/unit/test_release_evidence_verifier.py:97` 缺 `capacity_manifest_id` 必须 FAIL；`tests/unit/test_release_evidence_verifier.py:110` id 不匹配必须 FAIL；#280（`0deeb73`）；实测见 `docs/operations/release-evidence-verification-output.txt` 的 G8 semantic-check rerun。 | 证据文档（DEL-121 done） | 已闭合：语义检查和三类反例已固化；后续改动需保持这些反例为红。 | — |
| **G9 最终验收** | 没有一条命令能对候选给出可复核的 Go/No-Go，放行靠口头。 | **未实现** | `test -e scripts/dev-env/final-acceptance.sh` 不存在；`test -e docs/operations/final-acceptance.md` 不存在；现有 `scripts/dev-env/release-preflight.sh:3` 只覆盖 C0–C12；DEL-139 进行中。 | 交付主管（DEL-139 进行中） | `final-acceptance.sh` 和判定表合入，对最终候选跑出 SUMMARY；由非作者执行。 | 0.5–1 d |
| **G10 质量门禁** | 噪声、重叠、高并发和长稳下的质量与容量未知，对外承诺会翻车。 | **未实现** | `deploy/engines/file-asr-audio/evidence/gb10-20261027/qualification-report.json:10` 为 `experimental`；`docs/operations/qualification-report.md:187` 写明 gateway/cancel/concurrency 未执行；`docs/operations/qualification-report.md:284` 列未验证项；`deploy/engines/file-asr-audio/evidence/gb10-20261027/README.md:74` 列边界。 | GPU 执行（未建卡） | 噪声、重叠、并发、P95、长稳和目标拓扑有测量与边界声明；不拿实验样例替代。 | 2–3 d |

G3（部署装配）不在本页 9 项内：DEL-123 复核后降级，#287（`3dc9ee0`）已补最小部署面；完整装配仍见 #259。

## 3. 已闭合的（#284–#289、#291、#292）

- `#284` `053c36e`：文件 lane 成功终态驱动者。
- `#285` `3cd9b26`：catalog revision 随可见集合推进，ETag 正确变化。
- `#286` `4fa15f6`：门禁 C4 要求 `record_stop` 带 `SUCCEEDED` 实参。
- `#287` `3dc9ee0`：最小可复现同步 ASR/TTS 部署面，C9 同步为 5 服务集合。
- `#288` `817370e`：5 处裸文件名引用补成可定位路径，校正漂移行号。
- `#289` `896d7fc`：军团状态面板 `fleet-status.sh`。
- `#291` `57a7e41`：PO/SM 运行手册（合并提交）。
- `#292` `7f6965e`：军团面板 P0×3 修复（合并提交）。

`#290` 核对时点仍为 **OPEN**（head `d8613e2`），未合入 `main`，因此不计入已闭合。

另：G8 的校验器修复来自更早的 `#280`（`0deeb73`），见矩阵。

## 4. 这一页的口径

- **状态三档**：只允许 **已闭合** / **有代码未验证** / **未实现**；不写任何模糊的完成度描述。
- **状态基准**：按核对时点的 `origin/main` 判定；未合入的分支不计入候选已有，已合入才算。
- **证据**：每行给出可复核命令、文件:行或 PR；没有来源的写“未验证（原因）”。链接可打开不等于取得原件。
- **快照**：顶部时点与 commit 是本页的唯一边界；这一页不承诺持续自动更新，合入新 PR 后需要新快照。
- **不排期**：`预计` 列取 DEL-120 派工表的数值（已闭合项写 `—`），不是本页的新估算；优先级和排期由唯一 SM+PO 决定。
- **范围**：只覆盖任务指定的 9 项（G1/G2/G4/G5/G6/G7/G8/G9/G10）；G3 不在其中。
