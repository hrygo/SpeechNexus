---
title: CI 验证范围与维护
status: active
---

# CI 验证范围与维护

CI 使用[工作流](../../.github/workflows/ci.yml)和[标准库路由](../../scripts/dev-env/ci-routing.py)选择验证范围。未知路径保守运行全量，不维护易遗漏的业务测试白名单。

| 触发或变更 | 范围 |
| --- | --- |
| PR 仅修改根 README / AGENTS、`docs/` 内 Markdown、`.agents/skills/<skill>/` 内 Markdown / JSON / YAML | 路由回归、Design Token 静态门禁、差异检查与最终门禁；不安装项目依赖、不启动业务测试服务 |
| PR 仅修改已知 Web 源码/静态资产，可混合上述文档 | Python 静态/单元及完整 Web 检查；双数据库与部署检查不适用 |
| PR 仅修改已知独立引擎目录或明确列出的引擎单元测试，可混合上述 Web/文档 | `no-db`：保留 Python 静态/契约/覆盖率、完整 Web、构建和部署检查；六个数据库分片及其汇总不适用 |
| CI、脚本、测试、依赖、配置、契约、迁移、部署、其他路径或空/不确定差异 | 完整 Python/Web/部署检查，六个 PG/InnoDB/MinIO 分片及证据汇总 |
| `main` push、手动运行及未知事件 | 始终全量 |

精确白名单以路由脚本为准。新增应用、资源类型或目录默认全量；缩窄范围前先证明其不依赖被省略的验证，并添加回归。

Skill 的说明、参考、合成评测数据及展示元数据不参与产品构建，复用文档轻量路径。技能内的 Python、Shell、JavaScript 等脚本和未知文件仍走全量；混合业务代码时按业务范围选择，其他 `.agents/` 配置不在白名单内。轻量路径不执行 skill 行为评测，也不证明 JSON/YAML 内容或引用有效，维护者须按变更核对。`main` push 仍按既有约定全量验证。

独立引擎目录白名单为 `deploy/engines/` 下的 `cpu-vad`、`file-asr-audio`、`vllm-asr-lifecycle`；测试白名单为 `test_native_asr_scope.py`、`test_worker_fence.py`（均在 `tests/unit/gateway/`）。集成测试会导入其他部分单元测试工具，因此不按整个 `tests/unit/` 跳过数据库。后端/网关业务实现、共享测试工具、契约、迁移、共享依赖和 CI 配置仍触发全量；任意混合变更取更严格的范围。主干 push 与手动运行继续全量。引擎若接入数据库集成测试，须同步收紧路由并补回归。

## 必需检查与证据

保留 `Python quality and tests`、`Web quality, tests, and build`、`Deployment configuration` 三个受保护检查名。作业级条件让不适用项明确显示 skipped；最终 Python 门禁用 `always()` 执行，核对路由成功和各项实际结果。非法/缺失路由、失败、取消或本应执行却跳过均失败。不适用不表示业务验证已通过。

完整范围仍要求原有覆盖率、契约漂移/兼容检查及六个真实双数据库分片。集成汇总继续核对同一提交、完整收集、互斥并集和实际执行结果，不以数量或仅收集代替证据。

Python 测试作业通过 `workbench-pg-probe.py --full-coverage` 在同一次 pytest 中运行完整非集成测试和 Workbench PG／Runtime 用例，使用仓库默认全项目覆盖率与 80% 门槛；不通过收窄统计范围或降低门槛放行。作业检查 PostgreSQL 16 工具并启动已有隔离 MinIO，驱动器创建私有 Unix socket 数据库，不连接 runner 的系统数据库。原 `--coverage` 仍是独立的 C 层 90% 检查，两个模式互斥。完整模式的执行报告以 `workbench-pg-full-coverage` 上传，覆盖率不足仍使作业失败。

集成分片在安装 Python 依赖后，使用 `uv run python integration-shards.py requirements` 实际收集完整 integration nodeid（包括参数化身份），以执行时相同的 SHA-256 分片算法输出 `node_contracts` 与 `browser` 需求。`*_browser.py` 与两个合成浏览器探针模块声明浏览器依赖；回归核对当前所有 Node 浏览器探针消费者均被分类。收集错误或外部 `PYTEST_ADDOPTS` 缩窄选择会失败，不能输出“无需工具”放行。该步骤只收集，实际执行和六份报告的互斥并集核验仍独立必需。

有 `browser` 需求的分片安装完整锁定 Web 依赖、构建两个实际入口，并安装相同版本 Chromium headless shell 及系统依赖后运行测试；只有 Node 契约消费者的分片保留锁定生产依赖安装；数据库分片不安装浏览器。需求随真实 nodeid 分区计算，不在 workflow 中固定某几个分片号。汇总 `check` 子命令与 `tests/ci` 轻量路由回归继续仅依赖 Python 标准库。

浏览器准备复用[独立脚本](../../scripts/dev-env/prepare-ci-browser.py)：先从锁定 Playwright CLI 的依赖模拟结果取得缺失/升级包，再向 APT 请求 URI/大小/SHA256 清单。仅在这条清单命令中选择 SHA256 输出，避免 `--print-uris` 的历史 MD5 默认值；不改系统或实际安装配置。归档通过 APT 自带的 `apt-helper download-file` 最多四个并发下载，同时传入强摘要和 `Checksum-FileSize`，让原生获取阶段约束已认证大小；不新增下载器依赖或覆盖传输政策。写入完整 APT 缓存前仍独立核对 SHA256/SHA512 与大小，MD5-only、错误大小、错误强摘要及不匹配的已有缓存均拒绝。不覆盖未知缓存内容。

随后仍执行原 `playwright install --with-deps --only-shell chromium`。信任链依赖 APT 认证的索引与上述写前强校验，不能假定安装器会重新校验完整缓存：APT 2.8 的[现有归档路径](https://github.com/Debian/apt/blob/2.8.1/apt-pkg/acquire-item.cc#L3494-L3507)可能只按大小复用。未知或无法得到强摘要的计划直接失败，不降级到弱摘要或强制信任。

不改软件源、APT/网络安全配置、超时预算或六分片/受保护汇总，不引入跨运行缓存或外部服务。真实 runner 确认 HTTP/HTTPS 配置了 `Timeout`、`Pipeline-Depth`、`No-Cache`，并通过限定字段名诊断定位到直接 HTTP `User-Agent-Non-Interactive`。APT 2.8.1 的[原生实现](https://github.com/Debian/apt/blob/2.8.1/methods/http.cc)按既有配置与 systemd unit 语境选择非交互标记；apt-helper 使用同一 acquire HTTP method，不改写配置值、header 或上下文。仅对上述已核实控制及无控制情形开放预取；HTTPS 同名项、代理、自定义 User-Agent、地址族、速率/队列/hash、特定 APT binary、替代配置、未知/嵌套键或不可读取配置仍沿用原安装器。

诊断输出固定分类、退出码与错误类型；对直接字段仅允许 1–64 字符、字母开头且只含 ASCII 字母/数字/下划线/连字符的字段名。空字段另分类，其他文本只给分类/数量；不输出配置值、代理凭据、嵌套主机名、URL 或原始 stderr。识别出字段名不自动授予预取资格，必须先核对原生语义。未知计划、下载或校验失败会阻止安装，临时预取目录自动清理。首版真实 CI 全部观察到 fallback，不能证明预取或提速；精确支持后仍须取得新的真实 runner 执行证据。

此改进针对已记录的官方字体/库归档串行下载耗尽分片预算，需以新组合实际执行测试验收；单次提速不承诺长期耗时，取消、未开始测试与断言失败分别报告。回退该脚本接线即可恢复原官方安装路径，原失败证据保留。

每个集成分片还检查 PostgreSQL 16 工具，通过 `workbench-pg-probe.py --integration-shard` 启动独立的私有 Unix socket PG，再把 Workbench DSN 传给原 `integration-shards.py run`。双数据库与 MinIO 环境仍指向该作业既有隔离测试服务，不用私有 Workbench DSN 替换协调数据库地址。原分片报告路径、完整 collection、实际执行与意外跳过拒绝规则保持不变；驱动器的生命周期报告与分片报告一并上传，分别保存，不替代分片执行报告。仅有单个分片作业成功不能替代汇总验收。

真实内网模型资格与可复现集成分别验收。`qualification` 标记的三条纪要真实模型用例保留原部署调用与结果断言，不进入普通 `integration` 分片，也不计作 CI 通过；它们验证实际部署资格，不能替代协调事务的确定性门禁。`make workbench-llm-qualification` 要求显式的私有 Workbench PG DSN、获准内网 LLM endpoint/model，并构建真实 Web 入口后执行全部三项。缺少配置时入口直接失败，不以跳过取得资格。普通 CI 的分片完整性仍拒绝任何必需用例跳过，阶段 AC 仍须另附这三项真实模型与版本证据。

不在整个 workflow 的 `on` 中设置 `paths` / `paths-ignore`：被整体过滤的必需检查可能长期 Pending。[GitHub 路径过滤说明](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#onpushpull_requestpull_request_targetpathspaths-ignore)、[作业条件与 skipped 语义](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-jobs-with-conditions)是维护入口；核验日期为 2026-10-25。

## 路由维护

- 对比事件的准确 base SHA 与实际 checkout 的合并结果；路径用 NUL 分隔，不按换行拆分，也不使用 API 文件列表截断后的子集。
- 重命名同时考虑旧/新路径。将 Python 文件移入文档目录或删除源码仍须全量。
- 基线不可用时回退全量；路由或轻量回归失败时阻止合并。不要用 `continue-on-error` 或缺失输出兜底放行。
- 轻量入口使用 runner 的系统 Python 3.12+，只依赖标准库；Ruff 对该脚本及 `tests/ci/` 固定 `py312`，产品代码继续遵循项目 Python 版本。

从仓库根目录验证变更：

```sh
python3 -m unittest discover -s tests/ci -v
uv run pytest tests/ci tests/contract/test_ci_workflow.py tests/unit/test_integration_shards.py -q --no-cov
```

需要完整复核时可在 Actions 手动运行 CI；本机完整检查仍按 [AGENTS.md](../../AGENTS.md) 的宿主和隔离服务前置条件执行。

## Design Token 门禁

`changes` 作业用 `.nvmrc` 固定的 Node 执行独立回归与全 Web 静态扫描，不安装 pnpm 依赖；适用于所有路由，失败会阻断最终必需检查。Web 作业的 `pnpm lint` 继续执行相同校验器。

| 范围 | 当前检查 |
| --- | --- |
| `tokens.json` | 必须可解析且包含有效 token；支持 `color` / `dimension` / `fontFamily` / `number` / `shadow`。primitive 颜色与 CSS 同名声明值一致（规范化空白与小数）；其余 token 使用单个 `var(--name)`。未知格式、缺失值及 DTCG 别名明确失败 |
| TypeScript 导出 | 所有字面量 CSS 引用必须已注册，包括 primitive；元数据文字不能充当注册记录 |
| Web 消费者 | 扫描 TS/TSX/JS/JSX/MTS/CTS/MJS/CJS、CSS、HTML、SVG；检查带空白、换行、嵌套 fallback 的 `var()` 引用。项目策略要求 fallback 引用也已声明；同一 CSS 规则块内拒绝循环别名 |
| 颜色 | 拦截 hex、RGB/HSL/HWB/Lab/LCH/OKLab/OKLCH/`color()` 字面量及颜色属性中的命名色。允许 `transparent` / `currentColor`；fragment href 只豁免其自身，不能放过同一行的颜色 |
| 例外 | 共享样式仅在 custom property 值中允许原始颜色；[静态资源例外](../../web/shared/design-token-exceptions.json) 按精确路径、SHA-256 和理由固定。当前为两个启动 HTML 元数据与四个独立品牌 SVG；内容改变须审查，不能批量刷新哈希掩盖新增违规。例外仍检查引用与外链 |

独立验证（均无需安装依赖）：

```sh
node --test tests/ci/design-tokens.test.mjs
node scripts/dev-env/validate-design-tokens.mjs
```

生产扫描排除 `.test.*` / `.spec.*` / `__tests__` 和构建/依赖目录。规则是静态字面量门禁，不能证明动态拼接、CSS 转义、浏览器级联/跨规则循环、视觉效果、对比度或所有出站请求正确。当前注册表是 CSS 字符串项目格式，**不是完整 DTCG 2025.10 交换格式**；不使用标准合规表述替代实际验证。

依据（2026-10-25）：[CSS Variables 的引用与 fallback 语义](https://www.w3.org/TR/css-variables-1/)、[DTCG 2025.10 格式与别名要求](https://www.designtokens.org/tr/2025.10/format/)。本门禁对未声明 fallback 的限制是项目策略，比浏览器 fallback 语义更严格。

## Web 无障碍与颜色对比度

Web／全量路由在现有受保护的 Web 作业中，构建后运行 `pnpm test:a11y`。使用锁定的 `@axe-core/playwright` 与现有 Playwright，只安装 Chromium headless shell；两个预览服务绑定 `127.0.0.1`，严格检查端口，不复用其他服务，不启动后端或模型。浏览器请求仅允许这两个回环端口。文档路由不安装浏览器。

| 覆盖 | 门禁 |
| --- | --- |
| 两个实际构建入口 | 系统浅/深色、显式浅色覆盖深色系统、显式深色覆盖浅色系统，各测 320px／1280px；页面运行 WCAG 2.0/2.1/2.2 A/AA 适用规则与 axe best-practice，所有 violation 失败 |
| 主题语义文字 | 每个页面情景检查 21 对前景／背景：主、次、弱化与强调文字 × 四种表面，三个能力状态，以及强调／警示色对。透明值在明确的 page 底色上合成，普通文本以 4.5:1 检查；每个探针必须产生明确通过证据，不允许 incomplete 或缺失 Token |
| 键盘与焦点 | 首次 Tab 到可见跳转链接，Enter 后主内容真正获得焦点；焦点环可见、至少 2px，对三个基础表面达到 3:1。该项目策略不等于完整证明 WCAG 焦点区域／遮挡成功条件 |
| 用户偏好 | 两入口分别在浅/深色下验证增强对比度、强制颜色和减少动效；状态动效最长 5 秒，减少动效时无无限循环；强制颜色测 UA 实际调整后的颜色 |
| 门禁自证 | 故意注入无名称按钮、低对比度文字、失效跳转目标、隐藏焦点环、局部坏色带及玻璃合成回归，证明对应保护实际失败；亮度公式以已知色对和 4.5 临界值回归 |

axe 的背景图像／渐变 `color-contrast` incomplete 转入[栅格门禁](../../tests/web/raster-contrast.mjs)，不再使用文字位置或前景 Token 白名单。Chromium 在当前 viewport 按真实滚动位置截图，覆盖实际渐变、半透明背景及单层 `backdrop-filter`；DOM Range 定位每条文字行框，检查其中**每个 CSS 像素**的最差对比度，不只取中心点或渐变端点。小文本要求 4.5:1；24px 以上，或至少 18⅔px 且字重 ≥700 的大文本要求 3:1；判断使用未舍入比值。

测量时只临时隐藏选中文字节点的墨迹，保留 `currentColor`、背景、边框与玻璃合成；校验文字几何和原有绘制属性没有变化，异常时也恢复 DOM 和滚动。可能影响同一玻璃背景的文字分开截图，避免隐藏后方文字改变被测底色。每情景最多 512 个文字行框、12 张 viewport 截图，超限失败而非截断。PNG 用现有浏览器解码，不新增图像库、依赖或 CI 作业。报告增加逐行最差背景、比值、阈值、采样数及截图批次，失败额外附背景截图。

该方法限定于固定 Chromium、DPR=1、sRGB 8-bit 的现有静态情景。行框包含字间空白，比逐字形检查保守；通过只覆盖这些实际渲染像素，不能外推到所有尺寸、DPR、浏览器或动态帧。组透明度、可测的半透明前景、前景 filter/mix-blend/mask/clip、伪元素改变文字或背景、生成文字、动态绘制、嵌套玻璃及与文字交叠的非祖先玻璃层等未建模情况，明确失败并要求补充实现或针对性复核，**不保留自动放行例外**。其他种类的 axe incomplete 仍失败。依据：[W3C F83 最不利背景检查](https://www.w3.org/WAI/WCAG22/Techniques/failures/F83)，核验日期 2026-10-25。

成功和失败均上传 `web-accessibility-evidence`，保留 7 天；包含 HTML/JSON、逐情景 axe 与语义色对证据，桌面浅/深色截图，失败时额外保留截图和 trace。报告仅含合成页面。维护测试时同时维护其覆盖声明，不能使用全局关闭规则、自动重试或既有 violation 基线掩盖回归。

本机复现：

```sh
pnpm install --frozen-lockfile
pnpm build
pnpm exec playwright install --only-shell chromium
pnpm test:a11y
```

Linux CI 使用 `--with-deps` 安装浏览器系统依赖。本机须有对应浏览器前置条件；失败不改用 jsdom 伪装浏览器通过。自动门禁覆盖当前工程外壳，不能代替屏幕阅读器、人工键盘走查、缩放/复杂遮挡、所有非文本图形或后续业务流程验收。规范核验日期为 2026-10-25：[WCAG 2.2](https://www.w3.org/TR/WCAG22/)、[文本对比度](https://www.w3.org/WAI/WCAG22/Understanding/contrast-minimum.html)、[Playwright 自动与人工测试边界](https://playwright.dev/docs/accessibility-testing)。

## 轻量路径实测（2026-10-25）

| 指标 | 原文档 PR | 新文档 PR |
| --- | ---: | ---: |
| 实际执行作业 | 13 | 2 |
| 整轮耗时（含调度/排队） | 821 秒 | 16 秒 |
| 累计作业执行时间 | 1584 秒 | 11 秒 |

来源：[原 PR #138](https://github.com/hrygo/SpeechNexus/actions/runs/40155498616)、[新 PR #144 首次运行](https://github.com/hrygo/SpeechNexus/actions/runs/40158818797)。新运行的七个业务作业按范围不适用，最终必需门禁通过。完整路径另由 [PR #143](https://github.com/hrygo/SpeechNexus/pull/143) 的 14 项检查通过验证。

以上是增加 Design Token 前置步骤之前的两次文档改动托管观测；runner 与排队情况不同，不代表当前耗时、长期分位数或业务测试自身提速。`main` 与手动运行继续执行完整验证。
