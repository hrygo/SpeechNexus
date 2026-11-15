# Web 目录规则

继承[根规则](../AGENTS.md)。设计基线见 [UI/UX 与 Token 框架](../docs/design/ui-ux-design-framework-and-tokens.md)，workspace 与模块说明见[工程目录](../docs/components/repository-layout.md#3-web-工作区)。

- `admin` 与 `workbench` 是独立入口；它们不形成身份或数据隔离。界面不得暗示登录、个人私有空间或未实现的权限边界。
- `shared/` 只放两个入口以相同语义使用的稳定组件与设计 Token；工作台业务编排留在 `workbench`。
- 子应用复用共享 Token，遵守键盘焦点、减少动效与本地资源规则；资源、代理、错误报告和观测同样遵守内容不出部署网络的要求。
- 能力状态使用 `ready` / `contract` / `planned`。未实现能力使用明确状态或空态；不以虚构任务、用户、模型或容量填充产品界面。
- 消费权威机器契约生成或校验的边界类型，不在前端手写独立协议；客户端不能提交任意 LLM 端点。生成契约包在 `web/<app>/package.json` 以 `"@speechnexus/contracts": "workspace:*"` 声明依赖，并只经包内 `exports` 子路径导入，不以相对路径进入 `packages/contracts/src/**`，动态 `import()`/`require()` 必须带字面量说明符；新增子路径必须有经包清单的消费者，或可由生成模块与 `contracts/bindings.json` 的 `client: false` profile 机械判定归属；不存在自由文本豁免。协议变化使用 [speechnexus-contract-change](../.agents/skills/speechnexus-contract-change/SKILL.md)。
- 共享依赖由根 `pnpm-workspace.yaml` catalog 管理；依赖变更同步 lockfile。

验证使用[验证指南](../docs/agent-verification.md)的 Web 行；只有跨到契约或持久化边界时才增加相应检查。

## 断言「禁用控件的动作没有发生」

「控件是禁用的，所以动作没有发生」不能只靠 `toBeDisabled()` 成立。展示层与行为层分别证明，缺任一层都不构成证据。

### 两层

- **展示层**：`toBeDisabled()`。只证明控件渲染为不可用。
- **行为层①**：摘掉 `disabled` 后真实点击，发送数仍不变。证明渲染层还有第二道门。
- **行为层②**：直接调用 React 挂在节点上的 `onClick`，发送数仍不变。这是唯一能证明应用侧动作守卫存在的证据。

行为层①不能替代②：它只走渲染层，摘掉属性后点击仍到不了 handler（实测 0 次调用），删掉应用侧守卫它也不会转红；转红的是直调 `onClick` 的那一层。守卫定义在 `workbench/src/books/RenderUnitPanel.tsx` 的 `execute` 里，用 `!RECOVERED_INTENT[intent].replay` 早退。

### `click({ force: true })` 打 `disabled` 控件不是证据

Chromium 对 `disabled` 表单控件不派发鼠标事件。`force: true` 跳过的是 actionability 等待（含 enabled 检查），不是浏览器的激活规则，所以这种点击既不抛错也不点击：实测 0 次点击、0 次 handler、0 个请求。此时「请求数不变」是 `toBeDisabled()` 的推论，不是独立证据；删掉应用侧守卫后该断言依旧全绿（#794、#798）。

### 摘掉 `disabled` 再 `fireEvent.click` 也不够

把 `disabled` 属性置为 `false` 后 React 仍不执行 `onClick`：监听表为渲染时 `disabled` 的 prop 跳过鼠标事件。jsdom 实测 0 次调用，与真实浏览器一致。这条路径同样只证明「没发送」，不证明「谁拦住的」，所以它只能承担行为层①。

### 骨架

Playwright：

```js
const sends = () => backend.calls.generations; // 该规格已有的请求计数器
const replay = page.getByRole("button", { name: "按原键重放单元请求" });

// 展示层。
await expect(replay).toBeDisabled();
expect(sends()).toHaveLength(1);

// 行为层①：属性被摘掉后真实点击，仍不发送。
await replay.evaluate((button) => {
  button.disabled = false;
});
await expect(replay).toBeEnabled();
await replay.click();
expect(sends()).toHaveLength(1);

// 行为层②：直调 React 的 handler，仍不发送。
const reached = await replay.evaluate((button) => {
  const key = Object.keys(button).find((name) => name.startsWith("__reactProps$"));
  const props = key === undefined ? null : button[key];
  if (props === null || typeof props.onClick !== "function") return "unreachable";
  props.onClick();
  return "invoked";
});
expect(reached, "这一层必须真的调到 handler").toBe("invoked");
expect(sends()).toHaveLength(1);
```

jsdom：

```tsx
const replay = screen.getByRole<HTMLButtonElement>("button", { name: "按原键重放单元请求" });

// 展示层。
expect(replay).toBeDisabled();
expect(view.generate).toHaveBeenCalledTimes(1);

// 行为层①：摘掉属性再 fireEvent.click 到不了 handler，只证明渲染层。
replay.disabled = false;
fireEvent.click(replay);
expect(view.generate).toHaveBeenCalledTimes(1);

// 行为层②：直调 props.onClick()，这是 jsdom 里唯一能到达应用侧守卫的路径。
const key = Object.keys(replay).find((name) => name.startsWith("__reactProps$"));
const props =
  key === undefined
    ? null
    : ((replay as unknown as Record<string, { onClick?: () => unknown } | undefined>)[key] ?? null);
expect(props?.onClick, "控件必须挂着要调的 handler").toBeTypeOf("function");
await act(async () => {
  await props!.onClick!();
});
expect(view.generate).toHaveBeenCalledTimes(1);
```

**反例必须能红**：删掉应用侧守卫后，直调 handler 的用例必须转红。新建该类用例时先在删掉守卫的构建上确认它会红，再提交；只做展示层与行为层①的用例在这个构建上仍全绿。

### 现成示例

- [`render-refusal.behavior.spec.mjs`](../tests/web/render-refusal.behavior.spec.mjs) 的 `415：两层守卫之下，可点的重放控件也发不出已结算的键` 与 `415：刷新之后仍然不可重放，强制点击也不发出已结算的键`：两层齐备，`__reactProps$` 经 `locator.evaluate` 取。
- [`workbench/src/books/RenderUnitPanel.test.tsx`](workbench/src/books/RenderUnitPanel.test.tsx) 的 `invokeReplayHandler`：取 `__reactProps$` 的 `onClick` 并用 `act` 包裹；该文件据此调用被禁用控件的 handler。

同一个浏览器规格里的 `settledReplayIsUnavailable` 只断言展示层（`toBeDisabled()` 加 `click({ force: true })`），不承担行为层证据。
