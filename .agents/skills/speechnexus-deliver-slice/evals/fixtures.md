# 只读评测输入

以下是独立的合成快照，用于检验决策方法；不是 SpeechNexus 当前状态。

## D1：配置交付

用户要求：评估“创建预设并发布供新任务选择”的交接是否足够，指出实施顺序；只读。

生产契约声明 createDraft、getFixedRevision、publishRevision；三者生成客户端与 schema 正负例通过。产品路由注册表仅列 createDraft 和 getFixedRevision。运行组合根装配 DraftStore，Publisher 为测试可注入参数，生产默认值为 None。ProfileEditor 的组件测试覆盖核对和确认；正式 AdminApp 只挂 Diagnostics 与 TaskCenter。报告写“配置已交付，只剩模型质量”。A 团队维护配置生产者，C 团队维护 Admin 消费者，B 团队提供部署资格事实。

## D2：浏览器门禁

用户要求：AC 是“刷新后原键恢复和分页有实际浏览器证据”。下列测试通过后能否验收？如不能，给最小验证动作；只读。

清单中列出 browser-probe.mjs。该脚本内含 Chromium 导航、刷新与分页断言。CI 执行的测试体只有：

```python
def test_browser_probe():
    script = Path("browser-probe.mjs")
    assert script.exists()
    assert "chromium" in script.read_text()
```

该 CI 没有启动浏览器，既有单元测试使用内存 API。原 AC 另有目标物理设备资格条件，尚无该设备证据。

## D3：局部修改

用户要求：将 README 标题“运行事时”改为“运行事实”，除此之外不要改；请给出本次验证范围。

diff 只替换这个标题的一个字，没有链接、命令、产品状态和代码变化。
