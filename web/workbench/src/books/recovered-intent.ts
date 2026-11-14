import type { UnitCommand } from "./book-journal";

/**
 * The states a retained render-unit command can be in, and what each one may offer (#794).
 *
 * The panel keeps the operator's original request in a tab-scoped journal, so a submission
 * that may have crossed the network is never silently replaced. The journal records two
 * facts -- `uncertain` (no answer was observed) and `rejected` (a refusal was observed) --
 * and this table is the one place that turns them into a sentence and two controls, so the
 * paragraph cannot name a handle its state disables (#780/#782's equation, applied to that
 * aside).
 *
 * The table also keeps the panel from claiming more than it knows. `rejected` is not the
 * same bit as the file-task panel's `settled`: this panel writes it for *any* 4xx except
 * 408, including the retryable 429 and the cleanup wait, and the journal persists one bit
 * with no category, so a record restored by a reload cannot say what kind of refusal it
 * holds. Only an answer observed in this tab can be classified:
 *
 * - `unknown`: no answer was observed. The key is the only way to find a Job the request
 *   may have created, so the handle stays and the explicit end stays away.
 * - `kept`: this tab saw a refusal whose own step is to dispatch the same intent again
 *   (`RENDER_REFUSAL_RETRYABLE`). The key is still the right request.
 * - `settled`: this tab saw a refusal whose copy says repeating the submit cannot change
 *   the outcome ("重复提交不会改变结果", "不要复用旧键"). The replay is disabled and the
 *   aside explains why (#794 removed the handle from that state instead of promising it).
 * - `recorded`: a refusal this tab cannot classify -- restored from the journal by a
 *   reload. It is still a refusal (the record says so), so replay stays off, but the aside
 *   may not call it deterministic, because the record does not know that.
 *
 * The aside also never restates the Job facts of the answer: the paragraph that reported
 * the refusal already states them per code, and the stored record does not hold them.
 *
 * The sentence and the controls are asserted together by `RenderUnitPanel.test.tsx` and by
 * `tests/web/render-refusal.behavior.spec.mjs`: a control's exact label may appear in the
 * note if and only if the state renders it.
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
    note: "原请求已保存在此标签页。这次派发被服务端拒绝，而该拒绝的下一步是稍后再派发，所以「按原键重放单元请求」仍是同一次派发，本页不自动重试；不想再等时用「结束已拒绝单元请求」结束它。",
  },
  settled: {
    replay: false,
    dismiss: true,
    note: "原请求已保存在此标签页。服务端已确定性拒绝这次派发：同一次请求再发送不会改变结果，所以这里禁用了按原键重放。用「结束已拒绝单元请求」明确结束它，再按那次拒绝的处置说明重新开始。",
  },
  recorded: {
    replay: false,
    dismiss: true,
    note: "原请求已保存在此标签页。这里的记录只说明服务端拒绝过这次派发：刷新之后无法判断该拒绝是否可重试，因此本页不提供按原键重放。请按那次拒绝的处置说明处理，或用「结束已拒绝单元请求」结束它后重新派发。",
  },
} as const;

export type RecoveredIntent = keyof typeof RECOVERED_INTENT;

/** What this tab observed about the retained command's last answer, if anything. */
export type ObservedAnswer = "none" | Extract<RecoveredIntent, "kept" | "settled">;

/** The control labels the note may name; exactly one per `RECOVERED_INTENT` flag. */
export const RECOVERED_INTENT_CONTROLS = {
  replay: "按原键重放单元请求",
  dismiss: "结束已拒绝单元请求",
} as const;

/**
 * The state of a retained command, from the journal's flags plus the answer this tab saw.
 *
 * `answer` is the live observation, never a persisted fact: the journal keeps one refusal
 * bit, which cannot tell a terminal refusal from a retryable one, so a rejection restored by
 * a reload reads as `recorded` -- the reading that neither re-offers a possibly spent key
 * nor claims the refusal was deterministic -- while the explicit end and a fresh dispatch
 * stay available (#798 carries the persisted category this reads instead).
 */
export function recoveredIntentOf(recovery: {
  pending: UnitCommand | null;
  rejected: boolean;
  answer: ObservedAnswer;
  blocked: boolean;
}): RecoveredIntent | null {
  if (recovery.blocked || recovery.pending === null) return null;
  if (!recovery.rejected) return "unknown";
  return recovery.answer === "none" ? "recorded" : recovery.answer;
}
