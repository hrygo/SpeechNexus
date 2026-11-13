import type { FileTaskSession } from "./file-task-session";
type Snapshot = ReturnType<FileTaskSession["getSnapshot"]>;
export function canFinishTask(state: Snapshot) {
  return (
    !state.busy &&
    !state.unknown &&
    // A terminally refused create attempt owns no Job and can only be abandoned, so it
    // must offer the same explicit end-of-page path as a refused upload (#707). Every
    // unknown outcome keeps this false: only an explicit refusal is settleable.
    ((state.rejected === true && !state.job) ||
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
