import type { FileTaskSession } from "./file-task-session";
type Snapshot = ReturnType<FileTaskSession["getSnapshot"]>;
export function canFinishTask(state: Snapshot) {
  return (
    !state.busy &&
    !state.unknown &&
    // A terminally refused intent can only be abandoned, so it must offer the same
    // explicit end-of-page path as a refused upload (#707). A settled cancel attempt
    // (#723) still names a readable Job, but its intent is spent too: neither replay nor
    // another cancellation can move the page forward, and the earlier refusal is the
    // protocol's own answer, so the same explicit path applies. Every unknown outcome
    // keeps this false: only an explicit refusal is settleable.
    (state.rejected === true ||
      (state.uploadRejected === true &&
        !state.asset &&
        !state.assetRecoveryId &&
        !state.recovery &&
        !state.operation &&
        !state.job) ||
      (state.job?.cleanup_state === "complete" &&
        ["succeeded", "failed", "cancelled", "expired"].includes(state.job.status)))
  );
}
