---
title: CI 验证范围与维护
status: active
---

# CI 验证范围与维护

CI 使用[工作流](../../.github/workflows/ci.yml)和[标准库路由](../../scripts/dev-env/ci-routing.py)选择验证范围。未知路径保守运行全量，不维护易遗漏的业务测试白名单。

| 触发或变更 | 范围 |
| --- | --- |
| PR 仅修改根 README / AGENTS、`docs/` 内 Markdown | 路由回归、Design Token 静态门禁、差异检查与最终门禁；不安装项目依赖、不启动业务测试服务 |
| PR 仅修改已知 Web 源码/静态资产，可混合上述文档 | Python 静态/单元及完整 Web 检查；双数据库与部署检查不适用 |
| CI、脚本、测试、依赖、配置、契约、迁移、部署、其他路径或空/不确定差异 | 完整 Python/Web/部署检查，六个 PG/InnoDB/MinIO 分片及证据汇总 |
| `main` push、手动运行及未知事件 | 始终全量 |

精确白名单以路由脚本为准。新增应用、资源类型或目录默认全量；缩窄范围前先证明其不依赖被省略的验证，并添加回归。

## 必需检查与证据

保留 `Python quality and tests`、`Web quality, tests, and build`、`Deployment configuration` 三个受保护检查名。作业级条件让不适用项明确显示 skipped；最终 Python 门禁用 `always()` 执行，核对路由成功和各项实际结果。非法/缺失路由、失败、取消或本应执行却跳过均失败。不适用不表示业务验证已通过。

完整范围仍要求原有覆盖率、契约漂移/兼容检查及六个真实双数据库分片。集成汇总继续核对同一提交、完整收集、互斥并集和实际执行结果，不以数量或仅收集代替证据。

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
| 门禁自证 | 故意注入无名称按钮、低对比度文字、失效跳转目标、隐藏焦点环和未审查的渐变文字，证明对应保护实际失败；亮度公式以已知色对和 4.5 临界值回归 |

axe 无法自动判定原有渐变／玻璃背景上的部分文字。仅对测试中明确列出的现有位置，且原因限定为背景图像/渐变、实际前景与指定语义 Token 一致时，保留 `incomplete` 供人工复核；新增位置、前景漂移或其他无法判定原因均失败。独立色对探针覆盖固色表面及透明合成，但不替代真实渐变每个像素的对比度证明。不得把这些项目计为通过或扩展为整类忽略。

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
