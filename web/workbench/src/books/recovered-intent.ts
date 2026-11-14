import type { UnitCommand } from "./book-journal";
import { refusedReplayable, type RecordedRefusal } from "../recovery/command-journal";

/**
 * The states a retained render-unit command can be in, and what each one may offer
 * (#794, #798).
 *
 * The panel keeps the operator's original request in a tab-scoped journal, so a
 * submission that may have crossed the network is never silently replaced. The journal
 * records two facts -- `uncertain` (no answer was observed) and `refused` (a refusal was
 * observed, with its category) -- and this table is the one place that turns them into a
 * sentence and two controls, so the paragraph cannot name a handle its state disables
 * (#780/#782's equation, applied to that aside).
 *
 * The state is a function of the journal alone, never of "what this tab happened to
 * observe". That split is what made a reloaded 429 read differently from the same refusal
 * before the reload: the tab knew the refusal was retryable and the record knew only that
 * something had been refused (#798). What a reload reads is the category the tab wrote
 * down, so the same refusal offers the same handle on both sides of a reload.
 *
 * - `unknown`: no answer was observed. The key is the only way to find a Job the request
 *   may have created, so the handle stays and the explicit end stays away.
 * - `kept`: a refusal whose own step is to dispatch the same intent again
 *   (`RENDER_REFUSAL_RETRYABLE`, persisted as `retryable`). The key is still the right
 *   request, so the replay stays usable -- and its copy still names the wait.
 * - `settled`: a refusal whose copy says repeating the submit cannot change the outcome
 *   ("重复提交不会改变结果", "不要复用旧键"), persisted as `terminal`. The replay is
 *   disabled and the aside explains why (#794 removed the handle from that state instead of
 *   promising it).
 * - `recorded`: a refusal recorded without a category -- one written before #798, or one
 *   written before #803 by a panel that had no classification to write. It is still a
 *   refusal, so replay stays off (the reading `refusedReplayable` states once), but the
 *   aside may not call it deterministic, because the record does not know that.
 *
 * The aside also never restates the Job facts of the answer: the paragraph that reported
 * the refusal already states them per code, and the stored record does not hold them.
 *
 * `replay` is `refusedReplayable`'s answer for the state's refusal, not a second opinion:
 * the same function decides the export and assistant panels' replay controls, so one
 * refusal cannot offer a handle in one panel and withhold it in another (#803). `dismiss`
 * is the other shared half -- a recorded refusal is ended explicitly or not at all.
 *
 * The sentence and the controls are asserted together by `RenderUnitPanel.test.tsx` and by
 * `tests/web/render-refusal.behavior.spec.mjs`: a control's exact label may appear in the
 * note if and only if the state renders it.
 */
export const RECOVERED_INTENT = {
  unknown: {
    replay: refusedReplayable(null),
    dismiss: false,
    note: "原请求已保存在此标签页。不会自动重放；结果未确认时用「按原键重放单元请求」核对它，不要更换意图。",
  },
  kept: {
    replay: refusedReplayable("retryable"),
    dismiss: true,
    note: "原请求已保存在此标签页。这次派发被服务端拒绝，而该拒绝可重试：下一步就是等待后用原键重新派发，所以「按原键重放单元请求」仍是同一次派发，本页不自动重试；不想再等时用「结束已拒绝单元请求」结束它。",
  },
  settled: {
    replay: refusedReplayable("terminal"),
    dismiss: true,
    note: "原请求已保存在此标签页。服务端已确定性拒绝这次派发：同一次请求再发送不会改变结果，所以这里禁用了按原键重放。用「结束已拒绝单元请求」明确结束它，再按那次拒绝的处置说明重新开始。",
  },
  recorded: {
    replay: refusedReplayable("unclassified"),
    dismiss: true,
    note: "原请求已保存在此标签页。这条记录只说明服务端拒绝过这次派发，没有保存该拒绝是否可重试，因此本页不提供按原键重放。请按那次拒绝的处置说明处理，或用「结束已拒绝单元请求」结束它后重新派发。",
  },
} as const;

export type RecoveredIntent = keyof typeof RECOVERED_INTENT;

/** The control labels the note may name; exactly one per `RECOVERED_INTENT` flag. */
export const RECOVERED_INTENT_CONTROLS = {
  replay: "按原键重放单元请求",
  dismiss: "结束已拒绝单元请求",
} as const;

/**
 * The state of a retained command, from the journal's own record.
 *
 * The mapping is total and single-sourced: `refused === null` is the unobserved outcome,
 * `retryable` keeps the key, `terminal` spends it, and `unclassified` -- a record written
 * before #798, or one whose panel has no classification -- keeps it spent without claiming
 * the refusal was deterministic. Nothing here reads what "this tab observed"; that second
 * reading is what #798 removed.
 */
export function recoveredIntentOf(recovery: {
  pending: UnitCommand | null;
  refused: RecordedRefusal | null;
  blocked: boolean;
}): RecoveredIntent | null {
  if (recovery.blocked || recovery.pending === null) return null;
  switch (recovery.refused) {
    case null:
      return "unknown";
    case "retryable":
      return "kept";
    case "terminal":
      return "settled";
    case "unclassified":
      return "recorded";
  }
}
