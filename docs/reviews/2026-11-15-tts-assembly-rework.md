# PR #843 返修第二轮非作者审查：同步 TTS 参考部署装配（#821）

- 审查对象：PR #843 `fix/821-tts-assembly`，head `4256b4f8cec04da248fbd674db8aa1e6e596780e`，两个提交 `708d1730`（转写样例修复）+ `4256b4f8`（装配主体）。被审对象是第一轮报告 [`2026-11-15-sync-tts-assembly.md`](2026-11-15-sync-tts-assembly.md) 的返修。
- 基线：本卡要求基于 `origin/main`，审查分支基于审查结束时的 `origin/main = c9a48963`（审查期间 main 从 `f7ee17ae` 经 `53469b24` 前进到 `c9a48963`，都是 review/CI 文档提交，不影响本判定）。被审 head 与 main 的分叉点 `git merge-base 4256b4f8 origin/main = f7ee17ae168f5521ea15c23761e4cd3de19c3138`，即返修确在第一轮报告合并提交之后重放（MF-1 复核见 §3）。
- 审查分支/检出：`/private/tmp/sn848`，`review/848-tts-rework`（基于 `origin/main`，本目录只放本报告）；运行检出 `/private/tmp/sn848-head`（detached `4256b4f8`，跑 loader、checker、单测、例数统计）。被审内容一律 `git show 4256b4f8:<path>` 读取。
- 变异只写 `/tmp/sn848-mut/*`；未写产品代码/测试/脚本/部署文件，未 push 被审分支，未起 PostgreSQL/MinIO/引擎。
- 环境：macOS arm64、Python 3.14.9、uv 0.13.5、gh 2.103.2。
- 审查时间：2026-11-15 18:09–19:0x（Asia/Shanghai）。
- 报告文件名预检：`origin/main` 的 `docs/reviews/` 共 63 个文件，无 `2026-11-15-tts-assembly-rework.md`，无同名/近名报告。
- 范围：`git diff f7ee17ae 4256b4f8 --stat` = 14 文件 / +1110 / −47，全部在 `deploy/`、`docs/`、`scripts/dev-env/`、`tests/` 与一个 backend loader 的错误文案，无业务语义变更。

## 结论

**不通过（必须修 6 项；其中 1 项是本卡的核心——MF-4 的 new design 仍可被绕过）。**

返修把第一轮 6 项里的 5 项真正收口了：rebase + CI 绿（MF-1）、`deploy/README.md` 旧结论删除（MF-2）、两处路径/叙述/行数更正（MF-3）、五条受理前置带真实源码依据（MF-5）、转写样例独立 commit 且叙述属实（MF-6）。`replace-ready` 判词删除、逐字段四态、6 类占位各自归类也确实落地，作者自报的反例（只留 `policy_revision_id` → exit 1、`28 通过 / 1 仍是样例值 / 0 不一致 / 18 无法判定`）**实测逐字复现**（§4）。

但 MF-4 想消灭的那一类 false-green 只是换了入口：**三个私有文件里，`gateway-file-tts.json` 从来没有过它自己的生产 loader。** checker 对 recipe 与 endpoint 都调了 `load_tts_recipe` / `load_controlled_tts_endpoint`，唯独对 gateway 的 `file_tts_v2` 配置只做 `json.loads`。后果有二：

1. 18 项「无法从文件判定」里，`tts_policy.task` 与 `tts_policy.sample_rate_hz` **本来完全可判定**（`TtsTask` 是单成员枚举；gateway loader 明确写死 `sample_rate_hz != 24000 → ValueError`，`file_tts_runtime.py:88-89`）。把它们改成生产 loader 必拒的值（`task="Base"`、`sample_rate_hz=8000`），checker 仍 **exit 0**。
2. 三个文档共 82 个叶子字段，四态只覆盖 47 个；**另外 35 个字段既不在「仍是样例值」，也不在「不一致」，也不在「无法从文件判定」**。其中 `tts_policy.speed=99` 同样被生产 loader 拒绝，而 checker **连一行都不打印**就 exit 0。

也就是说：第一轮的结论「校验器会给一个照自己夹具替换完的装配盖章」没有被消灭，只是盖的章从 `replace-ready` 换成了 `exit 0` + `summary: … 0 仍是样例值`。**更严重的是这句 `0 仍是样例值` 本身是假的**：实测一次「完全替换」的装配仍有 18 个字段逐字等于样例值，工具把它们归为「无法从文件判定」而非「仍是样例值」，但 `deploy/README.md` 把 exit 0 定义为「no field still carries the example's value」。这是本轮必须修的口径失实（§2）。

四个核心问句的短答：

| 问句 | 短答 |
| --- | --- |
| 18 项里有没有本可判定却没判的 | **有。3 项可完全判定**（`quota_dimensions`、`tts_policy.task`、`tts_policy.sample_rate_hz`），**2 项可判合法集/一致性**（`service_class`、`runtime_plan.memory_architecture`），另 4 项可判一致性（`native_speaker_id`/`voice` 互等）。其余预算类确实无本地 oracle。且「18 项」本身不完整：同为预算的 `steady.*` 4 项与 `per_slot.*` 5 项根本没进四态。 |
| exit 0 的语义是否被诚实表达 | **部分。** 有明确的缩小句（stdout 的 `scope:` 行、runbook「它不判断能否受理」、README「There is no "ready" verdict」），**但 stdout 里没有一句「exit 0 不代表可受理」**；而 README 与 docstring 对 exit 0 的定义（「no field still carries the example's value」）与实测矛盾，`summary` 的 `0 仍是样例值` 也是同一误导。 |
| 半替换能否绕过 | **能。** 替换全部 47 个被检查字段，只把 `tts_policy.task` 改成 `Base`、`sample_rate_hz` 改成 `8000`（或把未被枚举的 `tts_policy.speed` 改成 99）→ checker **exit 0**，而 gateway 的生产 loader 三个都拒绝。§4 有完整红/绿输出。 |
| ASR-only 的取舍对不对 | **不对（结论：显式 fail-closed 开关优于让人 fork 仓库文件）。** 论证的前提有两处与事实不符：仓库**已经**有「空值即关闭」的环境开关（结算 DSN 空 → `build_speech` 告警并返回 None），而 fail-closed 开关完全可以做到「未设置=开、拼错=启动失败」；反而让部署方维护 compose 副本会引入更隐蔽的漂移。§5.2 展开。 |

## §1 18 项逐项判断：哪些本可判定

checker 的 18 项「无法从文件判定」按 `scripts/dev-env/check-tts-assembly.py:198-214`（recipe 14 项）+ `:259-265`（gateway 4 项）列出。逐项核本地 oracle（全部在 head 检出上实测/读源码）：

| # | 字段 | checker 判定 | 本地 oracle（源码） | 本可判定？ |
| --- | --- | --- | --- | --- |
| 1 | `service_class` | 无法判定 | `tts_recipe.py:33` `Literal["realtime","interactive","batch"]`；`transcription_recipe.py:75-91` 与 domain 只校验枚举；与 `pool.service_class` 相等需 DB | **部分**：合法集可判；取值需 `resource_pools` 行 |
| 2 | `quota_dimensions` | 无法判定 | **`tts_recipe.py:57` 直接要求 `== ["concurrent_attempts"]`**（同义检查另见 `domain/admission.py:358`） | **完全可以判定**（域常量；且该 loader 已被 checker 调用） |
| 3 | `memory_architecture` | 无法判定 | `transcription_recipe.py:119` 两值枚举 + `capacity.py:68-78` 要求与向量形状一致（unified 时 device/host 必须为 0） | **部分**：形状一致性可判且已由 loader 强制（实测 §4 `mem_arch_bogus` 变红）；选哪一个需 `host_nodes` 行 |
| 4 | `max_slots` | 无法判定 | `transcription_recipe.py:122` `ge=1`；`runtime_intents.py:180-183` 仅约束 `realtime_reserved_slots <= max_slots` | 值确实无 oracle（需注册实例 plan） |
| 5 | `realtime_reserved_slots` | 无法判定 | 同上 | 值无 oracle；上界可判 |
| 6-11 | `peak.*`（6 项） | 无法判定 | `_Physical` 只 `ge=0`；`steady.fits(peak)`、architecture 一致由 `runtime_intents.py:190-193` 强制 | 值无 oracle（实例 plan 是权威） |
| 12-13 | `steady.unified/device` | 无法判定 | 同上 | 值无 oracle |
| 14 | `per_slot.device` | 无法判定 | 同上 | 值无 oracle |
| 15 | `native_speaker_id` | 无法判定 | `domain/tts.py:145-156` 只校验 `_IDENTIFIER`；`file_tts_runtime.py:96-97` 要求 `== tts_policy.voice` | **部分**：互等可判（checker 的 agreement 已做）；具体 speaker 名需模型 |
| 16 | `tts_policy.voice` | 无法判定 | 同上 | **部分**：格式 + 与 15 互等可判；合法声音列表在引擎 |
| 17 | `tts_policy.task` | 无法判定 | **`domain/tts.py:85-92` `TtsTask` 只有 `CustomVoice` 一个成员**；`file_tts_runtime.py:84` `TtsTask(values["task"])` 对其它值抛错 | **完全可以判定** |
| 18 | `tts_policy.sample_rate_hz` | 无法判定 | `domain/tts.py:29` `ENGINE_SAMPLE_RATES=(8000,24000)` + **`file_tts_runtime.py:88-89` 明确 `!= 24000` 即拒绝** | **完全可以判定**（只能 24000） |

**结论：3 项本可完全判定（#2、#17、#18），另 5 项可判「合法集/一致性」的一半（#1、#3、#15、#16）。** `_semantic` 的 docstring 写「Equality proves nothing … only the operator can confirm the value against the registered plan」——对 #17、#18 是错的：注册 plan 根本不携带这两个值，判它的是 gateway 自己的 loader。

两项不对称也要指出：

- 18 项会漏掉 9 个同类预算字段。`peak` 收了 6 个子字段，`steady` 只收 `unified/device` 两项，`per_slot` 只收 `device` 一项；`steady.host_memory_bytes`、`steady.cpu_decode_millicores`、`steady.host_pcm_buffer_bytes`、`steady.scratch_disk_bytes`、`per_slot.unified_physical_memory_bytes`、`per_slot.host_memory_bytes`、`per_slot.cpu_decode_millicores`、`per_slot.host_pcm_buffer_bytes`、`per_slot.scratch_disk_bytes` 既不在四态，也没在任何地方说明被跳过。
- 「每字段四态」不成立。三个文档共 **82 个叶子字段**，四态覆盖 **47 个**（29 checked + 18 undecidable），**35 个未进入任何一态**（完整清单见 §4.6；唯一兜底是 `_marker`：这些字段里任何字符串若仍含 `-compose-example` 会被抓成「仍是样例值」，但取值错误不会被点名）。其中 gateway 文档 26 个叶子里只有 7 个进入四态（`controlled_endpoint_file`、`objects.*` 4 项、`worker_limits.*` 4 项、`tts_policy` 的 `instructions/speed/engine_format/language/seed/max_pcm_bytes/max_audio_ms/timeout_seconds`、`builtin_voice_binding.voice_revision`、`target.instance_generation` 等全部不在列）。

## §2 exit 0 的语义是否被诚实表达

**有明确说清的部分**（引原文）：

- stdout 每次都打印（`check-tts-assembly.py:369-372`）：
  `scope: 只覆盖「文件级替换与文件内一致性」。受理另需 TTS engine_instances 注册、plan 摘要与预算与注册值一致、policy_revision_id 等于声音资格的 gateway_policy_revision_id、该 binding 下已发布且未退役的内建声音，见 deploy/README.md「Synchronous TTS assembly」。`
- `deploy/README.md:147-148`：「There is no "ready" verdict: exit 0 means "no field still carries the example's value and the files agree", nothing more.」
- `docs/operations/compose-minimal.md:1880`：「只有前两类为空才 exit 0；它不判断能否受理。」

**不诚实的部分（阻塞）**：

1. **定义句与实测矛盾。** 「exit 0 means no field still carries the example's value」为假：§4.1 的「完全替换」装配里，18 个字段逐字等于样例值，`admission_expires_at_us` 也仍是样例的 `4000000000000000`，工具照样 exit 0。docstring `:17-18` 重复了同一句。正确的说法应是「exit 0 表示：它检查过的字段里没有仍是样例值的身份字段、文件之间不矛盾；18 项等同样例值但无法判定的字段不阻止 exit 0」。
2. **`summary` 行强化了错误印象。** 输出把 18 项标为「无法从文件判定」，但 summary 写 `0 仍是样例值`，而 `test_a_fully_replaced_assembly_reports_no_placeholder` 把 `assertIn("0 仍是样例值")` 固化成断言（`tests/unit/backend/test_private_recipe_examples.py:251`）——第一轮担心的「测试反向保护 false-green」在新夹具上以另一种形式重现：这次保护的是误导性措辞。
3. **stdout 没有一句直接说 exit 0 不代表可受理。** 只有 `scope:` 那一行在罗列「还差什么」，`summary` 之前没有任何免责句；只读 stdout 的人看到的最后两行是「28 本地检查通过 / 1 仍是样例值」与一句范围说明。README 与 runbook 的句子是对的，但工具本身的输出没有把「exit 0 ≠ 可受理」写成一句话。
4. **结算 DSN 从两份「还差什么」清单里消失了。** 第一轮 MF-4 的子项是「settlement DSN 缺失仍 replace-ready」；返修的处理是**从 checker 里删掉该项检查**（脚本不再读 env，可接受），但 stdout 的 `scope:` 行与 README「What acceptance still needs」五条**都没有把结算 DSN 列进去**，而它确实是装配的必要输入（`speech_runtime.py:49-66`：空值 → 告警点名缺失键并返回 None，TTS 保持关闭）。于是「exit 0 + 五条都满足」仍可能落在 TTS 未装配的状态。

## §3 MF-1 ~ MF-6 逐项核对

```text
$ git log origin/main --oneline -3
c9a48963 fix(web): 生产构建不再依赖 unsafe-eval 并使部署镜像的浏览器门禁可见 (#840) (#842)
53469b24 docs: 记录 #840 CSP/standalone 校验器修复的非作者审查 (#847)
f7ee17ae docs: 记录 #843 同步 TTS 装配的非作者审查结论 (#846)
$ git merge-base 4256b4f8 origin/main
f7ee17ae168f5521ea15c23761e4cd3de19c3138
$ git rev-list --count f7ee17ae..4256b4f8
2
```

| 项 | 自报 | 实测 | 判定 |
| --- | --- | --- | --- |
| MF-1 交付状态 | rebase 到 `f7ee17ae`；CI run 43701721092 success，21 SUCCESS / 2 SKIPPED | `git merge-base 4256b4f8 origin/main = f7ee17ae`（确认在其之后）；`gh api repos/hrygo/SpeechNexus/actions/runs/43701721092` → `head_sha=4256b4f8…`, `status=completed`, `conclusion=success`；该 commit 的 check-runs 汇总 `{"skipped":2,"success":21}`，0 failure；`gh pr view 843` → `mergeable=MERGEABLE`；冲突行已取 main 侧作业名（`docs/plans/mvp-release-readiness.md:44` 现为「ci.yml 的 `compose` 作业只做 config 校验」，main 的 `32498ccc` 正是把 `deployment`→`compose`） | **成立** |
| MF-2 自相矛盾 | 删除 「public TTS remains blocked by…」，改「已装配但未验证」 | head 的 `deploy/README.md` 全文 `grep -c -i blocked` = 3，均非 TTS 结论（`:88` ASR、`:479` 保留期 JSON、`:843` disable worker）；原句只存在于 `origin/main`（未合并前本应如此）。新结论 `:91-95`「public synchronous TTS is assembled here … but it is not exercised: … no real engine, no published and qualified builtin voice, and no public 200」；`deploy/compose.yaml:151-153` 三条 `SPEECHNEXUS_SPEECH_*` 与 `sn_speech_settlement` 授权确实在位（`compose-minimal.md:547-556` 安装路径、`:1481-1485` 升级路径） | **成立** |
| MF-3 失实引用 | 路径改 `tests/unit/backend/`；「摘要过期」全删；1730 → 1979（实测） | 行 12（表行 12，`:44`）现写 `tests/unit/backend/test_private_recipe_examples.py`；`git show 4256b4f8:docs/operations/compose-minimal.md \| wc -l` = **1979**（`async-job-runbook.md` = 965，与文中一致）；`rg 摘要过期 docs deploy` 只命中第一轮报告本身（须保留）；§4 B4 已改成 `binding_publication_id != binding_revision_id` 的正确原因 | **成立**（但 `docs/components/synchronous-speech.md` 新增段落有一处引用错误，见建议修 1） |
| MF-4 假绿收口 | `replace-ready` 删除、逐字段四态、6 类占位归类 | `replace-ready` 全文只剩测试里的**否定断言**（`test_private_recipe_examples.py:255`）；6 类占位确实各自归类：identity（`policy_revision_id`、`stage.instance_id`、3 个 plan 摘要、endpoint `address/port`）、clock（过期）、undecidable（18 项）；逐类复现见 §4。**但**第三个生产 loader 缺失 → 新的 false-green 仍在（§4.2/§4.3），exit 0 定义句失实（§2） | **部分成立（本卡核心未过）** |
| MF-5 受理前置 | 新增五条各带源码依据 | 五条全部核实：① `runtime_instance_heads.current_generation`（`qualified_speech_facts_postgres.py:78-85`，`SELECT current_generation FROM runtime_instance_heads`，行数≠1 抛 `missing instance head`）；② 手动注册 `_spec_from_manual`（`engine_registration.py:1072`）确实存在，README 列出的 19 个 flag（16 个必需标量 + `--peak/--steady/--per-slot` 三个向量）与 `:1073-1090` 的 `required` 元组及 `:1260-1281` 的 parser 一一对应；`--realtime-reserved-slots` 有默认值故未列入，正确；③ plan 逐项相等（`transcription_admission.py:88-123`：`current_generation == instance_generation`、`(host_node_id,pool_id,gateway_id)`、`binding_revision_id`/`manifest_id`/`approved_template_id`/`config_digest` 相等）；④ `policy_revision` 两处事实（`transcription_admission.py:116` 的 `quota["policy_revision"]`、`builtin_voice_qualification.py:54` 的 `gateway_policy_revision_id`）**行号逐字命中**；⑤ `has_qualified_voice`（`qualified_speech_facts_postgres.py:56-62`）。**特别核**「`engine-register` 只吃转写 recipe」：`deploy/compose.yaml:568` 只设 `SPEECHNEXUS_TRANSCRIPTION_RECIPE_FILE`（无 speech recipe），`engine_registration.py:1135-1139` 只 `load_transcription_recipe` | **成立** |
| MF-6 转写样例叙述 | 独立 commit `708d1730`，真实原因 `binding_publication_id == binding_revision_id`，并明确「原摘要本身是对的」 | `git show 708d1730 --stat` = 单文件 `deploy/private/transcription-recipe.example.json`（+2/−2）；提交说明逐字写「This was not a stale digest. Recomputing the digest from the committed bytes reproduces the committed `sha256:8d520c7d…` exactly」。**用提交字节独立重算**：main 侧 `committed = computed = sha256:8d520c7d…`（match True），head 侧 `committed = computed = sha256:de85ebca…`（match True） | **成立** |

## §4 变异实测（全部在 `/tmp/sn848-mut`，checker 从 head 检出运行）

### 4.1 完全替换：exit 0，但 18 项逐字等于样例

```text
$ uv run --all-packages python scripts/dev-env/check-tts-assembly.py --private-dir /tmp/sn848-mut/replaced
无法从文件判定 (18)
  service_class: 'interactive' equals the example but may be correct
  quota_dimensions: ['concurrent_attempts'] equals the example but may be correct
  runtime_plan.memory_architecture: 'unified_memory' equals the example but may be correct
  runtime_plan.max_slots: 1 equals the example but may be correct
  runtime_plan.realtime_reserved_slots: 0 equals the example but may be correct
  runtime_plan.peak.unified_physical_memory_bytes: 120000000000 equals the example but may be correct
  runtime_plan.peak.device_memory_bytes: 0 equals the example but may be correct
  runtime_plan.peak.host_memory_bytes: 0 equals the example but may be correct
  runtime_plan.peak.cpu_decode_millicores: 8000 equals the example but may be correct
  runtime_plan.peak.host_pcm_buffer_bytes: 0 equals the example but may be correct
  runtime_plan.peak.scratch_disk_bytes: 20000000000 equals the example but may be correct
  runtime_plan.steady.unified_physical_memory_bytes: 40000000000 equals the example but may be correct
  runtime_plan.steady.device_memory_bytes: 0 equals the example but may be correct
  runtime_plan.per_slot.device_memory_bytes: 0 equals the example but may be correct
  builtin_voice_binding.native_speaker_id: 'Vivian' equals the example but may be correct
  tts_policy.voice: 'Vivian' equals the example but may be correct
  tts_policy.task: 'CustomVoice' equals the example but may be correct
  tts_policy.sample_rate_hz: 24000 equals the example but may be correct
本地检查通过 (29)   # …含 admission_expires_at_us: 4000000000000000 is still ahead
summary: 29 本地检查通过 / 0 仍是样例值 / 0 不一致 / 18 无法从文件判定
exit=0
```

「0 仍是样例值」与 18 个 `equals the example` 同时出现——这就是 §2 里被 README/docstring 写成「no field still carries the example's value」的状态。

### 4.2 半替换（本卡第 3 问）：只破坏它标为「无法判定」的两个可判字段 → 仍然绿

`tts_policy.task = "Base"`（`TtsTask` 只有 `CustomVoice`）、`tts_policy.sample_rate_hz = 8000`（gateway loader 只允许 24000）：

```text
# checker
summary: 29 本地检查通过 / 0 仍是样例值 / 0 不一致 / 18 无法从文件判定
exit=0
# 同一份 gateway 配置走生产 loader（load_file_tts_configuration + 真实 stub settings）
task=Base    -> REJECTED ValueError: invalid gateway file TTS configuration
rate=8000    -> REJECTED ValueError: invalid gateway file TTS configuration
# 未改动的合法基线
baseline(valid) -> LOADED task=CustomVoice sample_rate_hz=24000
```

分步证明失败点就是这两个字段本身（不是别的字段）：`TtsTask('Base')` → `ValueError: 'Base' is not a valid TtsTask`；`sample_rate_hz=8000` 通过 `TtsPolicy` 后被 `file_tts_runtime.py:88-89` 的 `!= 24000` 拒绝。也就是说：**替换了它检查的全部 47 项、只把两个「本可判定」的字段改成生产 loader 必拒的值，checker 仍 exit 0。**

同一机制对**完全未进入四态**的字段更彻底（`tts_policy.speed = 99`）：

```text
# checker
summary: 29 本地检查通过 / 0 仍是样例值 / 0 不一致 / 18 无法从文件判定
exit=0
# 生产 loader
speed=99 -> REJECTED ValueError: invalid gateway file TTS configuration
```

`tts_policy.speed` 在输出里**一行都没有**，既不是「仍是样例值」也不是「无法判定」——读者无法知道它没被看过。

### 4.3 对照：loader 已覆盖的项确实会红（说明缺陷只落在那一个缺位 loader）

```text
# quota_dimensions = ["requests"]（tts_recipe.py:57 拒绝）
不一致 (1)
  loader: rejected by the production loader: Invalid private speech admission recipe … (ValueError)
summary: 0 本地检查通过 / 0 仍是样例值 / 1 不一致 / 0 无法从文件判定
exit=1

# memory_architecture=unified 但把 device 内存挪到非 0（capacity.py:68-78 拒绝）
不一致 (1)
  loader: rejected by the production loader: Invalid private speech admission recipe … (ValueError)
exit=1

# 保留 stage.instance_id + 两个 plan 摘要（identity 类）
仍是样例值 (3)
  stage.instance_id / runtime_plan.topology_digest / runtime_plan.qualification_digest
不一致 (1)  agreement: recipe.stage.instance_id == endpoint.target.instance_id: …
summary: 26 本地检查通过 / 3 仍是样例值 / 1 不一致 / 18 无法从文件判定
exit=1
```

### 4.4 作者自报反例的逐字复现（`policy_revision_id` 只留一项）

```text
仍是样例值 (1)
  policy_revision_id: 'policy-revision-tts-compose-example' is the committed example's value
summary: 28 本地检查通过 / 1 仍是样例值 / 0 不一致 / 18 无法从文件判定
checker exit=1
```

与作者自报的「28 通过 / 1 仍是样例值 / 0 不一致 / 18 无法判定、exit 1」完全一致。

### 4.5 `admission_expires_at_us` 的「等于样例不算占位」

- 推理本身成立：`4000000000000000` µs = 2096-10-02 前后，远超任何部署寿命；`domain/admission.py:313` 只在 `now_us >= expires` 时拒绝，`speech_runtime.py:72-74` 也只在过期时告警关停。所以复制样例的远期值**可用**，把它判红确实是错的判法。
- 但它让「别的都换了、只留这个远期值」静默通过（§4.1 即此形态），而**输出没有说清这一点**：`admission_expires_at_us: 4000000000000000 is still ahead` 既不说这是样例值，也不说「刻意接受」。只有源码注释（`check-tts-assembly.py:220-221`）写了「the example's far-future value is usable as-is」；README 与 stdout 都没有。卡里要求「若这是刻意允许的，输出里必须说清」——**未满足**。

### 4.6 未进入四态的 35 个叶子字段（`82 − 47`）

```text
speech-recipe: protocol, stage.minimum_slots, stage.reserved_slots, runtime_plan.service_class,
               runtime_plan.loading_policy,
               runtime_plan.steady.{host_memory_bytes,cpu_decode_millicores,host_pcm_buffer_bytes,scratch_disk_bytes},
               runtime_plan.per_slot.{unified_physical_memory_bytes,host_memory_bytes,cpu_decode_millicores,host_pcm_buffer_bytes,scratch_disk_bytes}
gateway-file-tts: protocol, controlled_endpoint_file, builtin_voice_binding.voice_revision,
                  objects.{endpoint,result_bucket,tls_ca_file,result_timeout_seconds},
                  tts_policy.{instructions,speed,engine_format,language,seed,max_pcm_bytes,max_audio_ms,timeout_seconds},
                  worker_limits.{max_result_bytes,max_command_bytes,max_pending,shutdown_seconds}
gateway-file-tts-endpoint: protocol, target.instance_generation
```

这些字段不在四态的任一态，也不会因取值错误被点名（只有字符串里出现 `-compose-example` 时才会被 `_marker` 抓到）。其中 `target.instance_generation` 尤其要紧：README 的受理第 1 条要求「registered at the generation the endpoint descriptor pins」，但工具从不对它做任何判断。

### 4.7 文档里唯一的检查命令在相对路径下对合法装配报红

`deploy/README.md:140` 与 runbook §3 都用相对目录：

```text
# 同一份完全合法、且绝对路径下 exit 0 的装配，改成相对路径调用：
$ uv run --all-packages python scripts/dev-env/check-tts-assembly.py --private-dir ../sn848-mut/deploy/.runtime/private
不一致 (1)
  loader: rejected by the production loader: Invalid private speech admission recipe ../sn848-mut/deploy/.runtime/private/speech-recipe.json (ValueError)
summary: 0 本地检查通过 / 0 仍是样例值 / 1 不一致 / 0 无法从文件判定
exit=1

# 同一份装配，绝对路径：
$ uv run --all-packages python scripts/dev-env/check-tts-assembly.py --private-dir /tmp/sn848-mut/replaced
summary: 29 本地检查通过 / 0 仍是样例值 / 0 不一致 / 18 无法从文件判定
exit=0
```

`tts_recipe.py:43-44` 要求绝对路径，checker 没有 `.resolve()`。第一轮 MF-4 第三子项（「相对 `--private-dir` 被误报成 production loader 拒绝，应 `.resolve()`」）**未被修**：新的失败文案至少带上了 `(ValueError)`，但文档里的唯一入口在 happy path 上直接红，会把替换正确的部署方劝退。

## §5 另外必须核的两处新改动

### 5.1 `tts_recipe.py` 的 except 分支（第 10 条）

```diff
-    except ValueError, TypeError, KeyError, UnicodeError, RecursionError, OSError:
-        raise ValueError("Invalid private speech admission recipe") from None
+    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError, OSError) as error:
+        # Name the file and the failure class: …
+        raise ValueError(
+            f"Invalid private speech admission recipe {path} ({type(error).__name__})"
+        ) from None
```

- **只加了诊断信息**：捕获的异常元组、包装类型（仍是 `ValueError`）、`from None` 与返回路径全未变，接受/拒绝边界不变。
- 文案确实点名文件与类别，实测：缺失文件 → `… /run/speechnexus-private/speech-recipe.json (FileNotFoundError)`；相对路径 → `… deploy/.runtime/private/speech-recipe.json (ValueError)`。`deploy/README.md:121-124` 引用的正是前者，与实测一致。
- **有没有测试断言旧文案**：全仓只有 `test_private_recipe_examples.py:185` 断言 `startswith("Invalid private speech admission recipe ")` 且 `assertIn(str(target))`、`assertIn("ValidationError")` —— 新文案兼容；**没有任何测试断言过不带路径的旧整句**，所以不存在未同步的断言。`load_transcription_recipe` 一侧仍是旧的裸文案（未改，属范围外）。

### 5.2 「为什么不做环境开关」（第 11 条）

论证原文（`deploy/README.md:192-201`，runbook §3 以同样措辞复述）：

> The recipe path is set unconditionally so that "TTS is off" cannot happen by accident. A deployment that only wants synchronous ASR can still opt out: delete the three `SPEECHNEXUS_SPEECH_*` lines from its own copy of `deploy/compose.yaml`. … There is deliberately no environment switch for this: a switch would reintroduce exactly the silent-disable path that made this issue necessary, because an unset or misspelled switch is indistinguishable from "off on purpose".

（runbook 版：`docs/operations/compose-minimal.md:302-306`，末句为「本仓库不提供环境开关：未设置/拼错的开关与『故意关闭』在启动期无法区分」。）

判断：**取舍不成立**，两条依据都可实测：

1. **前提不成立（fail-closed 开关完全可行）。** 把开关定义成 `Literal["true","false"]`、默认 `true`、只有字面 `false` 才关闭：未设置 → 开（于是缺 recipe 仍然启动失败，fail-closed）；拼错 → 配置校验失败（仍 fail-closed）。「未设置/拼错与故意关闭不可区分」只是把开关写成 truthiness/默认关闭时的性质，不是开关的固有属性。
2. **「本仓库不提供环境开关」与事实不符。** 这一套装配里已经有两个「空值即关闭」的软开关：`SPEECHNEXUS_SPEECH_SETTLEMENT_DATABASE_URL` 空 → `speech_runtime.py:61-66` 告警点名缺失键并返回 `None`（`compose.env.example` 与 runbook `:1688`、`:1709-1713` 都这么写）；`SPEECHNEXUS_SPEECH_DEFAULT_LANGUAGE` 缺失同理。也就是说「未设置 = 关闭」的形态在同一个能力里已经存在，只是没覆盖 recipe 路径——论证在逻辑上自相矛盾。
3. **代价比较。** 让人「删掉自己那份 compose 的三行」意味着部署方必须长期维护一个仓库文件的副本：`deploy/compose.yaml` 在 2026-11-13～15 三天里被 5 个 PR 改动（#642、#643、#647、#825、#833），副本的每次升级都要人工 re-merge，且漂移**不可校验、不进 CI**——这本身就是比环境开关更隐蔽的静默漂移路径。成本对比：一个显式开关 = 1 个 env 键 + 启动日志一行 + 一条单测；fork 副本 = 无界的升级负债。

**结论**：装配方想要的最小改动是 `SPEECHNEXUS_SPEECH_ENABLED: Literal["true","false"] = "true"`（只有字面 `false` 关闭，关闭时 INFO 日志点名），或退一步提供一个受支持的 `deploy/compose.asr-only.yaml` overlay（compose 多 `-f` 叠加以 `!reset` 去掉三键）并在 CI 里 `docker compose config` 校验；两者都比「改自己那份副本」好。当前「删三行」的写法只应作为过渡，且必须同时说明这是降级手段、升级时要重新处理。

## §6 例数与 CI

```text
$ gh pr checks 843 --repo hrygo/SpeechNexus     # 23 行，21 pass / 2 skipping，0 fail
$ gh api repos/hrygo/SpeechNexus/actions/runs/43701721092 --jq '.head_sha,.conclusion'
4256b4f8cec04da248fbd674db8aa1e6e596780e
success
$ # 该 commit 的 check-runs 汇总
{"skipped":2,"success":21}
```

- `tests/ci`：pytest `404 tests collected`；`python3 -m unittest discover -s tests/ci` → `Ran 399 tests … OK`。**差 5 的解释成立**：`tests/ci/test_dependency_source.py` 里有 5 个 pytest 风格的模块级 `test_*` 函数（`test_lock_contains_only_official_pypi_sources` 等，由 #831 引入），`unittest discover` 不收集模块级函数；pytest 的 404 里恰好 399 个是 `Class::method`、5 个是 `module::function`，`404 − 5 = 399`。
- `tests/unit/backend`：head `3287 tests collected`（`origin/main` 3273 + 新文件 14；新文件实测 `14 passed`）。
- `tests/unit`：head **`7160 tests collected`**，与自报的 **7157** 不符（差 3）。`origin/main` 为 7146，差额正好是新文件的 14 条；7157 = 7146 + 11，是上一版用例数（11 条）的旧口径，返修把新用例加到 14 条后总数未同步（自报例数需改成 7160，或改用 `tests/unit/backend` 单一口径）。
- 质量门禁：`uv run --no-project ruff format --check` 对 5 个改动 Python 文件 `5 files already formatted`；`ruff check` `All checks passed!`；`tests/ci/test_reference_deploy_dsn_coverage.py` 23 passed。

## §7 必须修清单

1. **M1（本卡核心）`check-tts-assembly.py` 必须让 gateway 配置也过它自己的生产 loader。** 现状只对 recipe 与 endpoint 调 loader，`gateway-file-tts.json` 仅 `json.loads`，于是 `tts_policy.task`（单成员枚举）与 `tts_policy.sample_rate_hz`（loader 写死 24000）这两个**本可完全判定**的字段被标成「无法从文件判定」，改成生产 loader 必拒的值仍然 exit 0；`tts_policy.speed=99` 这类未被枚举的字段同样 exit 0。修法：调用 `load_file_tts_configuration`（可用 stub `Settings`/`SimpleNamespace` + 解析出的 endpoint 绝对路径，离线可跑，实测基线 `LOADED`），失败按 `inconsistent` 报并点名字段；同时把 18 项里 `quota_dimensions`（`tts_recipe.py:57` 域常量）、`task`、`sample_rate_hz` 三项从「无法判定」改正。
2. **M2 exit 0 的口径必须改到与实测一致，并同步改测试断言。** `deploy/README.md:147-148` 与脚本 docstring 的「exit 0 means no field still carries the example's value」为假（§4.1：18 项逐字等于样例值仍 exit 0）。应写成「exit 0 = 它检查的字段里没有仍是样例值的身份字段、文件之间不矛盾；标为『无法从文件判定』的字段不阻止 exit 0」。`summary` 行要明确 `N 项等于样例值但无法判定`（现在的 `0 仍是样例值` 会被读成全部替换完），`test_a_fully_replaced_assembly_reports_no_placeholder` 里 `assertIn("0 仍是样例值")` 同步修改，并在 stdout 加一句「exit 0 不代表可受理」。
3. **M3 「逐字段」必须名副其实。** 82 个叶子字段只有 47 个进入四态，35 个既不检查也不声明（§4.6），且 `peak` 6 / `steady` 2 / `per_slot` 1 的不对称极易被读成「其余已检查」。要么补全（gateway 一侧接 loader 后大部分自动覆盖），要么在输出里打印一行「未覆盖字段清单」，不要把 `steady/per_slot` 的兄弟字段静默跳过。
4. **M4 文档里的唯一检查命令必须对合法装配 exit 0。** `--private-dir deploy/.runtime/private`（`deploy/README.md:140`、runbook §3 的 `D=deploy/.runtime/private`）是相对路径，`load_tts_recipe` 要求绝对路径，实测合法装配 exit 1 并报成「production loader 拒绝」。应 `Path(directory).resolve()`（第一轮 MF-4 第三子项，未收口）。
5. **M5 runbook 把 `tts-batch` 说成占位是错的、会指导部署方改坏常量。** `docs/operations/compose-minimal.md:221-223` 新写「`model_id`/`pool_id`/`approved_template_id`/`instance_id` 必须与部署的 TTS 模板、资源池与实例一致，`tts-batch` 等取值只是占位」。但 `pool_id` 对 `file_tts_v2` 是域常量：`domain/tts.py:129` `self.pool_id != "tts-batch"` 即拒绝（实测 `TtsExecutionTarget(... 'tts-prod' ...)` → `invalid configured file TTS execution target`），checker 自己也打印「'tts-batch' is the only legal TTS pool」。应改成「`pool_id` 必须保持 `tts-batch`；需要替换的是 `model_id`/`approved_template_id`/`instance_id`」。
6. **M6 `admission_expires_at_us` 的刻意允许必须在输出里说清，并顺手消掉 MF-2 的同型残留。**
   - 前者：`checked: 4000000000000000 is still ahead` 既不说明这是样例值、也不说明「远期样例值可用、不算占位」；卡里要求「若刻意允许，输出必须说清」。加一句 detail 即可（例如 `equals the example's far-future value; usable, but no rotation is forced`）。
   - 后者：`docs/plans/mvp-release-readiness.md` §4 B4 的标题仍是「参考部署装配不出公开同步 TTS（装配已补齐，真实 200 未复跑）」——标题与括号内正文互相否定，正是 MF-2 在同一 PR 里被要求消灭的那类残留。标题应改为「参考部署已装配同步 TTS，真实 200 未复跑」。

## §8 建议修（不阻塞）

1. `docs/components/synchronous-speech.md` 新增段落：「由运行手册 §3 与 §6 的安装/升级两条路径重放」——两条预置路径都在 **§6**（§6 正文 `:547-556`，§6「已有部署的授权升级」`:1481-1485`）；§3 是私有文件准备。应改为「§6 的两条路径」。
2. 结算 DSN 从两份「受理还差什么」清单里缺席（stdout `scope:` 行、README「What acceptance still needs」五条）。它决定 `build_speech` 是否装配（`speech_runtime.py:49-66`），建议补成第 0 条或明确写「已在本节上一段说明」。
3. runbook `:1379-1380`「共十四个角色」与该段 `new_roles` 的 **15 项**仍不一致（漏 `sn_artifact_retention`；第一轮建议修 4，未改）。
4. `tests/unit` 自报例数 7157 → 实测 7160（见 §6）。
5. 第一轮对 `objects.endpoint: http://172.28.0.10:9000` 的「占位」担忧可以撤销：`deploy/compose.yaml:74` 把 MinIO 固定为 `ipv4_address: 172.28.0.10`，该地址是参考部署的正确值。返修把它从检查面移除是对的，但复核记录里应说明这一点，避免下一轮重复提出。
6. `docs/operations/compose-minimal.md` 的「仍不可用的能力」标题与 TTS 行「已装配」并存（第一轮建议修 5，未改）。

## §9 只能依赖作者陈述 / 未验证项

- 「真实引擎上公开 `POST /v1/audio/speech` 的 `200`」：本轮同样未在真实 GPU/引擎上复跑（本机 macOS 无 GPU），只有 2026-11-03 窗口的历史记录。装配与受理前置是否正确仍以源码 + 文档为准，**不代表可用**。
- `runtime_instance_heads`/`engine_instances`/`speech_builtin_voice_qualifications` 的真实数据未验证（未起 PG）；相关判断全部来自 SELECT 与比较条件的源码阅读。
- `load_file_tts_configuration` 的离线调用用了 stub（`SimpleNamespace` + `SecretStr` 合成凭据），未连 MinIO；它证明的是「接受/拒绝边界」而非真实对象存储连通性。
- CI run 43701721092 只核了 head sha、conclusion 与 23 个 check-run 的结论分布，未逐 job 读日志。

## §10 时间线

| 时刻 | 动作 | 结果 |
| --- | --- | --- |
| 18:09 | `git fetch`；建 `/private/tmp/sn848`（review 分支，基于 `origin/main`=f7ee17ae）与 `/private/tmp/sn848-head`（detached 4256b4f8） | 两检出就绪 |
| 18:11 | 读第一轮报告；核报告文件名；核 `merge-base`/ancestry | 分叉点 = f7ee17ae，MF-1 前半成立 |
| 18:12 | `gh pr checks 843` / `gh api runs/43701721092` / check-runs 汇总 | 21 SUCCESS / 2 SKIPPED / 0 fail，head sha 正确 |
| 18:13 | 读 head 的 checker、三个样例、`tts_recipe.py`、`transcription_recipe.py`、`capacity.py`、`runtime_intents.py` | 定位 18 项的 oracle |
| 18:14 | 读 `file_tts_runtime.py`、`domain/tts.py`、`execution_target` | 发现 task/sample_rate 可判定 + 第三 loader 未接 |
| 18:15 | `pytest tests/ci --collect-only`=404；unittest discover=399；定位 5 条模块级函数 | 差值解释成立 |
| 18:16 | head 上收集 `tests/unit`=7160、`tests/unit/backend`=3287 | 自报 7157 有 3 条偏差 |
| 18:17 | 造 6 个变异装配（replaced / task / rate / quota / slots / mem_arch）跑 checker | 见 §4 |
| 18:18 | 用 stub settings 跑 `load_file_tts_configuration` 对三个变体 | 基线 LOADED、两个变体 REJECTED |
| 18:19 | 核 MF-5 五条源码依据 + `engine-register` 只吃转写 recipe + `_require_capacity` | 全部成立（行号漂移 ≤4 行） |
| 18:20 | 核 MF-3 行数（1979/965）、四处路径与叙述、`摘要过期` 全文 grep | 成立；新增段落 §3/§6 引用错 |
| 18:21 | 独立重算 `708d1730` 前后摘要（rfc8785） | 两侧 committed==computed |
| 18:22 | 读 `tts_recipe.py` diff、全仓 grep 旧错误文案 | 契约不变，无过期断言 |
| 18:23 | 相对路径复现（文档唯一命令） | 合法装配 exit 1 |
| 18:24 | 数叶子字段（82）与四态覆盖（47），列未覆盖 35 项 | 「逐字段」不成立 |
| 18:25 | 核 ASR-only 论证：`build_speech` 软关闭路径、settlement DSN 空值语义 | 论证前提与设计自相矛盾 |
| 18:3x | 写报告、提交、push、开 PR | 见文末 |

## 硬要求自检

本分支提交后：

```text
$ git diff origin/main...HEAD --stat
 docs/reviews/2026-11-15-tts-assembly-rework.md | 344 +++++++++++++++++++++++++
 1 file changed, 344 insertions(+)
$ git diff origin/main...HEAD --name-only
docs/reviews/2026-11-15-tts-assembly-rework.md
```

除本报告外，本审查分支没有产品代码、测试、脚本或部署文件改动；所有变异都在 `/tmp/sn848-mut/*` 与 detached 检出 `/private/tmp/sn848-head` 的 `deploy/.runtime/` 之外，未写入被审分支，未 push 被审分支，未合并、未 push main。
