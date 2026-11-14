/**
 * The published dispatch-refusal vocabulary and the operator step each code demands (#750).
 *
 * `generateRenderUnitV2` answers a refused dispatch with one stable `code` per
 * operator step instead of a single 422: the upstream Job contract shares one
 * status class between a text over its bound, an unpublished voice, an
 * unprovisioned workspace, an unready asset, an unpublished model binding and an
 * oversized body, and only the code says what the operator has to do next.
 *
 * This list is the consumer half of that promise, and it is the only place that
 * knows the vocabulary: `render-refusals.test.ts` pins it against the contract
 * prose and the backend table, and `tests/contract/test_render_refusal_vocabulary.py`
 * fails when either side gains a code the other does not have. Without that pair a
 * new backend code would silently fall through to the generic "结果尚未确认"
 * alert, which is a worse answer than the refusal it replaced.
 *
 * `render_submission_rejected` stays in the list on purpose: it is the backend's
 * "no cause this workbench can act on" fallback, and the panel answers it with the
 * generic refusal plus — only when it measured the text itself — the split step.
 *
 * `param` is deliberately absent: it is a field pointer for diagnostics, not
 * operator copy, and `WorkbenchResponseError` does not carry it.
 */
export const RENDER_REFUSAL_CODES = [
  "render_submission_text_too_long",
  "render_submission_field_unsupported",
  "render_submission_workspace_not_configured",
  "render_submission_workspace_unavailable",
  "render_submission_voice_unavailable",
  "render_submission_asset_unavailable",
  "render_submission_binding_unavailable",
  "render_submission_body_too_large",
  "render_submission_rejected",
] as const;

export type RenderRefusalCode = (typeof RENDER_REFUSAL_CODES)[number];

/**
 * One operator step per code, stated as the next action rather than the cause.
 *
 * `Record<RenderRefusalCode, string>` is the compile-time half of the guard: a code
 * added to the list above without copy fails `pnpm typecheck`.
 */
export const RENDER_REFUSAL_STEPS: Record<RenderRefusalCode, string> = {
  render_submission_text_too_long:
    "服务端报告该单元正文超过一次派发的上限（4096 字符），因此没有提交作业。下一步：把文本切分为每段不超过 4096 字符的多个单元后分别派发；200000 字符的源容量不必缩小。",
  render_submission_field_unsupported:
    "派发请求里有字段取值不被接受：钉住的字段与契约不符，或指向的资源已不存在（例如声音、模型、绑定修订或发音集）。下一步：核对这些固定字段后重新派发。",
  render_submission_workspace_not_configured:
    "该工作区还没有作业配额，派发被拒绝。下一步：让运维为该工作区配置配额后再派发；重复点击不会成功。",
  render_submission_workspace_unavailable:
    "目标工作区不存在或已停用，派发被拒绝。本页不提供工作区选择（服务端固定使用部署的工作区）。下一步：联系运维恢复或重建该工作区后再派发。",
  render_submission_voice_unavailable:
    "该单元钉住的声音修订尚未发布，派发被拒绝。下一步：发布这个修订，或把单元改钉到已发布的修订后重新派发。",
  render_submission_asset_unavailable:
    "该单元钉住的资产尚未就绪，派发被拒绝。下一步：等资产就绪，或重新上传后再派发。",
  render_submission_binding_unavailable:
    "该单元钉住的模型、模型绑定或预设修订不可用（未发布、已退役，或与该请求不匹配），派发被拒绝。下一步：改钉到该部署上已发布且匹配的修订（必要时先发布），再重新派发。",
  render_submission_body_too_large:
    "派发请求体超过部署的接收上限，因此没有提交作业。下一步：缩短该单元正文后再派发，或由运维调大接收上限。",
  render_submission_rejected:
    "请求被拒绝，且服务端没有给出本页能据以指示下一步的原因。下一步：核对单元文本与固定字段后重做；重复提交不会改变结果。",
};

/** The code as a refusal disposition, or `null` when this workbench does not know it. */
export function refusedCode(code: string): RenderRefusalCode | null {
  return (RENDER_REFUSAL_CODES as readonly string[]).includes(code)
    ? (code as RenderRefusalCode)
    : null;
}
