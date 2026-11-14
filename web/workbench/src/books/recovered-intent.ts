import type { UnitCommand } from "./book-journal";

/**
 * The three states a retained render-unit command can be in, and what each one may offer
 * (#794).
 *
 * The panel keeps the operator's original request in a tab-scoped journal, so a submission
 * that may have crossed the network is never silently replaced. The journal records two
 * facts -- `uncertain` (no answer was observed) and `rejected` (a definitive refusal was
 * observed) -- and this table is the one place that turns them into a sentence and two
 * controls, so the paragraph cannot name a handle its state disables (#780/#782's equation,
 * applied to that aside).
 *
 * `replay` re-sends the retained command under its original key, so it belongs to the
 * states where exactly that request can still change the outcome: an unobserved answer
 * (the key is the only way to find a Job it may have created) and a refusal whose own copy
 * names another dispatch as the next step (`RENDER_REFUSAL_RETRYABLE`). A settled refusal
 * says the opposite in the copy that reported it ("重复提交不会改变结果", "不要复用旧键"),
 * and #794 removed the handle from that state instead of promising it.
 *
 * `dismiss` ends the retained request explicitly, and belongs to the states that know the
 * request was refused: an unobserved answer must keep its original key, because a Job may
 * exist under it, and dropping the record would lose the identity that finds it.
 *
 * The sentence and the controls are asserted together by `RenderUnitPanel.test.tsx` and by
 * `tests/web/render-refusal.behavior.spec.mjs`: a control's exact label may appear in the
 * note if and only if the state renders it, and a refusal keeps the handle exactly when its
 * own operator step says the same intent may be dispatched again.
 */
export const RECOVERED_INTENT = {
  unknown: {
    replay: true,
    dismiss: false,
    note: "原请求已保存在此标签页。不会自动重放；结果未确认时用「按原键重放单元请求」核对它，不要更换意图。",
  },
  kept: {
    replay: true,
    dismiss: true,
    note: "原请求已保存在此标签页。这次派发被拒绝、没有创建作业，但该拒绝的下一步是稍后再派发，所以「按原键重放单元请求」仍是同一次派发，本页不自动重试；不想再等时用「结束已拒绝单元请求」结束它。",
  },
  settled: {
    replay: false,
    dismiss: true,
    note: "原请求已保存在此标签页。服务端已确定性拒绝这次派发，没有创建作业：同一次请求再发送不会改变结果，所以这里禁用了按原键重放。用「结束已拒绝单元请求」明确结束它，再按那次拒绝的处置说明重新开始。",
  },
} as const;

export type RecoveredIntent = keyof typeof RECOVERED_INTENT;

/** The control labels the note may name; exactly one per `RECOVERED_INTENT` flag. */
export const RECOVERED_INTENT_CONTROLS = {
  replay: "按原键重放单元请求",
  dismiss: "结束已拒绝单元请求",
} as const;

/**
 * The state of a retained command, from the journal's two flags plus the live answer.
 *
 * `retryable` is the answer seen in this tab, never a persisted fact: the journal keeps one
 * refusal bit, which cannot tell a terminal refusal from a retryable one, so a reloaded
 * rejection reads as `settled` -- the reading that never re-offers a spent key -- while the
 * explicit end and a fresh dispatch stay available.
 */
export function recoveredIntentOf(recovery: {
  pending: UnitCommand | null;
  rejected: boolean;
  retryable: boolean;
  blocked: boolean;
}): RecoveredIntent | null {
  if (recovery.blocked || recovery.pending === null) return null;
  if (!recovery.rejected) return "unknown";
  return recovery.retryable ? "kept" : "settled";
}
