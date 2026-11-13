# docs 范围 Markdown 代码块门禁的 CI 实跑证据（#673）

本文件只用于验收 [PR #674](https://github.com/hrygo/SpeechNexus/pull/674) 新增的 `Docs: markdown code block formatting` 作业在 `docs` 范围 PR 上真的执行，不参与产品文档。

## 复现方式

第一次提交故意让下面这段 Python 代码块不符合 `ruff format`，第二次提交复原。两次都只改 `docs/`，因此路由为 `docs`，`python_contracts` 与其它依赖项目依赖的作业都是 skipped：

```python
def prove( flag ):
    payload  =  {"flag":flag,"path":  "/tmp/x"}
    return payload
```

判定口径：本作业只跑 `ruff format --check .`，与 `python_contracts` 中的格式步骤逐字相同。
