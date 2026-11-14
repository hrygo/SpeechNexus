/**
 * The published dispatch-refusal vocabulary and the operator step each code demands (#750).
 *
 * `generateRenderUnitV2` answers a refused dispatch with one stable `code` per
 * operator step instead of a single 422: the upstream Job contract shares one
 * status class between a text over its bound, an unpublished voice, an
 * unprovisioned workspace, an unready asset, an unpublished model binding and an
 * oversized body, and only the code says what the operator has to do next. The
 * same operation also splits the status families a 422 says nothing about: a 401
 * credential refusal, a 403 permission refusal and a 415 media-type refusal are
 * deployment facts, a 409 is either a spent key, a cleanup wait or an unnamed
 * conflict, and a 429 is a retryable refusal -- not an unknown outcome (#754,
 * #755). The 429 code names neither rate limit nor capacity: the status proves
 * only that the request was declined now, and the two share one step.
 *
 * This list is the consumer half of that promise, and it is the only place that
 * knows the vocabulary: `tests/contract/test_render_refusal_vocabulary.py` fails
 * when either side gains a code the other does not have. Without that pair a new
 * backend code would silently fall through to the generic "结果尚未确认" alert,
 * which is a worse answer than the refusal it replaced.
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
  "render_submission_idempotency_conflict",
  "render_submission_cleanup_pending",
  "render_submission_conflict",
  "render_submission_credentials_rejected",
  "render_submission_permission_denied",
  "render_submission_media_unsupported",
  "render_submission_rate_limited",
  "render_submission_rejected",
] as const;

export type RenderRefusalCode = (typeof RENDER_REFUSAL_CODES)[number];

/**
 * The refusals whose next step is "wait, then dispatch again" (#754, #755).
 *
 * This is the consumer half of the backend's retryability table and it is stated
 * once, here, so the copy below and the journal's "was this attempt settled?"
 * decision cannot disagree with the server's `error.retryable`. Everything else in
 * the vocabulary is terminal: the same request cannot start a Job, whatever the
 * operator does next. The 429 refusal and the 409 cleanup wait are the only two
 * exceptions. The cleanup wait is listed although no dispatch returns it today
 * (reserved, see `_CONFLICT_CODES_RESERVED`): the mirror must stay exact, and a
 * consumer reaching the upstream cancel/retry path later needs no copy change.
 */
export const RENDER_REFUSAL_RETRYABLE = [
  "render_submission_rate_limited",
  "render_submission_cleanup_pending",
] as const satisfies readonly RenderRefusalCode[];

/** Whether this refusal's next step is to wait and dispatch again. */
export function isRetryableRefusal(code: RenderRefusalCode): boolean {
  return (RENDER_REFUSAL_RETRYABLE as readonly RenderRefusalCode[]).includes(code);
}

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
  render_submission_idempotency_conflict:
    "本次派发没有创建作业。这个请求键已经被另一次意图不同的派发占用，服务端拒绝把它当作同一次派发。下一步：先刷新单元状态，确认那次派发是否已经生成了作业；确认后用一个新的请求键重新派发，不要复用旧键。",
  // Reserved: the upstream raises this from its cancel and retry handlers only, and
  // this operation reaches neither, so no dispatch answers with this code today
  // (see `_CONFLICT_CODES_RESERVED`). The copy stays so a caller that later sees it
  // has a step without a vocabulary change.
  render_submission_cleanup_pending:
    "本次派发没有创建新作业。该单元的上一次执行还在清理，服务端尚未确认它已经停止，因此拒绝再提交。下一步：等这一次清理完成后再派发；这里可重试，不需要换键，也不要把它当成结果未知。",
  render_submission_conflict:
    "本次派发没有创建作业，但服务端以冲突拒绝了它，且没有给出本页能据以区分具体原因的机器码。下一步：刷新单元状态确认是否已有进行中的派发，再用新的请求键重新派发；不要按旧键原样重放。",
  render_submission_credentials_rejected:
    "语音底座拒绝了这个部署的共享凭据，派发请求没有到达作业准入。下一步：由运维修正工作台的语音底座凭据（或凭据轮换后的配置）后再派发；改单元内容或重复提交都不会成功。",
  render_submission_permission_denied:
    "语音底座认可凭据，但这个部署没有权限提交作业（策略或 Host/Origin 限制），派发被拒绝。下一步：由运维放开该部署的提交权限后再派发；改单元内容或重复提交都不会成功。",
  render_submission_media_unsupported:
    "拒绝这次派发的一层不接受本页发送的 JSON 媒体类型，请求没有到达作业准入。本页固定发送 UTF-8 application/json，因此这不取决于单元内容。下一步：由运维修正前置代理或部署的媒体类型配置后再派发；重复提交不会改变结果。",
  render_submission_rate_limited:
    "服务端以 429 拒绝了这次派发（限流或容量不足），没有创建作业。429 只能说明「现在不行」、无法区分限流与容量，因此本页不再替它断言具体原因。下一步：等服务端要求的时间后重新派发这次生成；这是可重试的拒绝，不是结果未知，不要按旧结果推断是否已生成。",
  render_submission_rejected:
    "请求被拒绝，且服务端没有给出本页能据以指示下一步的原因。下一步：核对单元文本与固定字段后重做；重复提交不会改变结果。",
};

/**
 * The operator step for a refusal, with the server's own retry delay when it
 * stated one. Only a retryable refusal can name a delay, so a stray value on a
 * terminal disposition is ignored instead of promising a retry that cannot work.
 */
export function refusalStep(code: RenderRefusalCode, retryAfterSeconds?: number): string {
  const step = RENDER_REFUSAL_STEPS[code];
  if (!isRetryableRefusal(code) || retryAfterSeconds === undefined) return step;
  return `${step} 服务端要求至少等待 ${retryAfterSeconds} 秒（Retry-After）。`;
}

/** The code as a refusal disposition, or `null` when this workbench does not know it. */
export function refusedCode(code: string): RenderRefusalCode | null {
  return (RENDER_REFUSAL_CODES as readonly string[]).includes(code)
    ? (code as RenderRefusalCode)
    : null;
}
