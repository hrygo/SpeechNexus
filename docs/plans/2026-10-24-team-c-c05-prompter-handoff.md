# C05 提词跟随核心与手动阅读交接

核验日期：2026-10-24（Asia/Shanghai）。本单元推进 C05，不代表完整 C05/C04 或实时语音资格；C00–C08 goal 保持 active。

## 基线与范围

base/head：`dc14cf8fb0a7250842a8a7697c6884d6d21b2004`。启动累计165文件补丁 SHA-256 `b86adc6386edc21c1d124d0b945b100cc57d49e1f24b544f55f5afe526041d41`，逐文件核对后保留。公开 speech/workbench OpenAPI `0.1.0.dev0`、bindings `0.1.0` 不变；没有修改公开契约、生成产物、锁、后端、迁移、共享设计或 README。

修改限 Workbench App 导航与测试、独立 teleprompter 目录、浏览器探针及目标/计划/交接索引。生成入口仍只有 Project 请求，未绕过 A 的流水线或手写公开实时 DTO。不 commit、push 或发外部消息，不运行模型、共享数据库或真实麦克风。

## 行为与边界

`FollowSession` 独立固定本地稿件 id/revision/正文、codepoint 阅读位置、接管前恢复点。它是内部应用状态，不冒充服务端 TranscriptDocument revision 或公开事件。`beginFollowing(epoch)` 返回当前会话的不可变 context；消费者必须把这个原对象保存在订阅/异步匹配闭包里，不能把迟到事件重新标为新 context。手动 seek/pause、同 epoch 重启、换 epoch 或新会话都使旧 context 无效；外来 context 即使字段相同也不接受。有效 hypothesis 使用严格递增安全整数 sequence，适配器以后须从合法公开事件生成该内部水位。

匹配只比较当前位置前后各160 codepoints 内的完整词元序列，不匹配英文词内部。最少4个有效字符，hypothesis≤256、稿件≤20000 codepoints。词元经 NFKC/lowercase 比较，原正文与位置映射不改；唯一匹配只前进，重复句歧义、低信息、不匹配和修订回退均停留。这是保守匹配策略，不是声学置信度或质量资格，错识别/分词差异可能要求手动定位。活动 epoch、generation、hypothesis 不进入保存快照；恢复校验有界结构与位置并始终手动。

正式工作台新增“提词阅读”：粘贴本地稿、显式固定版本、编辑框与阅读版本分离；键盘/滑块/按钮定位，Unicode 原文安全展示。阅读块按 grapheme 切分，不将 HTML 当内容执行。位置变化及窗口变窄只滚动阅读视口，不抢键盘焦点；ResizeObserver 在离开时解除。离开/后台使现有 context 失效，不请求设备或网络。

“保存到此浏览器”才写入同源 localStorage，保存固定稿/位置，不保存尚未应用的编辑。加载页面不自动恢复，用户显式“恢复并替换当前稿”才读取和替换。存储不可用/超限/损坏时保留当前稿并报告失败，不假称成功。本地保存无服务端持久化或身份隔离保证，未保存内容切换/刷新会清空。自动语音跟随在正式页面清楚显示尚未接入；页面没有虚构麦克风/跟随成功状态。

## 验证

- 新核心25条测试、Panel6条测试及新增App切换1条；focused共35条（含旧App3条）。原始模块缺失/缺导航 RED 后实现 GREEN。
- 浏览器暴露 textarea 包裹 label 随正文改变的标签缺陷；补精确 label 反例观察 `1 failed / 5 passed`，改成独立 `htmlFor/useId` 后通过。未隐藏失败或放宽标签匹配。
- 窄屏保持当前阅读块的浏览器断言先失败；加入 ResizeObserver 和视口内滚动，处理 border/subpixel 向上取整。验证不把视口外位置当通过。
- 最终 format/lint/typecheck、全量 Web 测试及生产 build 通过：Workbench `286`、Admin `79`、contracts `121`，共 `486`。最初类型检查发现 ES2023 不支持 isWellFormed 声明及严格索引类型问题；保留原工具链，用 Unicode 正则拒绝孤立 surrogate 并补齐索引边界，无依赖/配置升级。
- Chromium `156.0.8078.4` 生产提词探针使用410 codepoints 合成稿：原文保真、显式保存位置80、刷新后显式手动恢复、切换清空未保存稿、键盘 End/Home/PageDown、360px 无横溢及当前块保持可见、真实 Tab 焦点通过；业务 API/外连/麦克风/页面错误均0。探针最终进程退出0，browser 和 loopback preview 关闭。
- 同一最终生产 bundle 重新运行已有 ASR/TTS Chromium 探针，两者均通过（ASR 下载46字节核对、TTS WAV原字节/原身份恢复/切换释放），页面/外连/handler错误均0。它们仍只证明合成 HTTP/SSE，非真实底座或模型资格。
- 浏览器探针另纠正了重复 status 的选择器范围及鼠标后 programmatic focus 不能代表真实键盘焦点的测试假设；保留失败日志，最终使用提词 region 内 status 和真实 Tab 验证。
- 独立终审 Critical/Important/Minor 均0，独立复跑35项 focused tests、12文件冻结 hash、增量双向对齐、其余161文件不变及 diff/index 检查；不把作者全量/浏览器/历史RED证据说成审查者重跑。无需修复或重复审查。报告位于 `.runtime/team-c/c05-prompter-review-result.md`。
- 累计包173文件：旧165中161原字节不变、4修改、新增8；干净基线 apply、全部字节、237本地链接及真实 Git index 保持检查通过。最终包 `.runtime/team-c/team-c-current.patch` 与 `.manifest.json`；前轮165包备份 `team-c-c05-seal-dc14cf8.patch` 和对应 manifest。补丁 SHA-256、逐文件 hash 与最终收尾记录分别存于 manifest 和 `c05-prompter-final-integrity.json`。
- 后端/旧schema字节未变，复用上轮1219 unit/contract及117 PG探针限定证据，本轮未重跑数据库。本单元无新增暂缓 Minor；既有其他单元暂缓项不因此关闭。

可复现命令（仓库根目录）：

```bash
pnpm --filter @speechnexus/workbench exec vitest run src/teleprompter src/App.test.tsx
pnpm format:check
pnpm lint
pnpm typecheck
pnpm test
pnpm build
node scripts/dev-env/workbench-prompter-probe.mjs --output .runtime/team-c/prompter.json --screenshot .runtime/team-c/prompter.png
node scripts/dev-env/workbench-file-host-probe.mjs --output .runtime/team-c/prompter-asr.json
node scripts/dev-env/workbench-tts-host-probe.mjs --output .runtime/team-c/prompter-tts.json
```

原始证据位于忽略目录 `.runtime/team-c/c05-prompter-*`。浏览器只使用合成稿件和 loopback 生产 bundle，不证明真实实时服务、模型/设备、生产本地数据安全或 Linux 部署。服务端提词持久化、公开 Realtime 校验入口与 adapter、真实 ASR 驱动及完整会议/提词仍继续实施。

## 裁决与成本

1. 先交付有限窗口核心与实际手动入口，不伪造实时连接；成本是自动语音接线和真实设备验证继续待办。
2. 本地稿与服务端版本分开，显式保存、手动恢复；成本是未保存内容刷新会丢失，同源存储不按使用者隔离。
3. 使用完整词元唯一精确匹配；成本是错识别和分词差异需要用户手动定位。
4. 沿用现有工作树、dc14cf8与手工补丁；成本是继续人工完整性核验和待上游集成。
5. 聚焦测试命令改用 `pnpm --filter ... exec vitest run`；原 `test -- file` 没有收窄脚本范围，成本是初次 RED 多运行了一次既有254项测试，不影响验收范围。
6. 不授予真实 ASR/公开 Realtime adapter、设备/模型、识别质量或长时负载资格；成本是自动跟随接线及真实验收继续待办。
7. 服务端提词持久化、完整会议、生产存储/内容出站、浏览器矩阵及 Linux 部署继续保留；成本是完整应用与部署资格仍需相应实施和证据。
8. 前轮后端/PG/双库只复用未变字节对应的限定证据；成本是当前整包最终集成仍须验证，不新增数据库正确性结论。
9. 作者历史 RED 和当前独立 GREEN 分别归属，不声称审查者复现 RED 或证明变异敏感性；成本是本单元没有额外 mutation 实验证据。
10. 主 Agent 收尾安装并逐字节核对累计包，不冒称完整累计包独立认证；成本是本次独立审查仍限12文件增量，最终集成另行验收。
