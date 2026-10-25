import type { FileTaskSession } from "./file-task-session";
type Snapshot = ReturnType<FileTaskSession["getSnapshot"]>;
export function canFinishTask(state: Snapshot) {
  return (
    !state.busy &&
    !state.unknown &&
    ((state.uploadRejected === true &&
      !state.asset &&
      !state.assetRecoveryId &&
      !state.recovery &&
      !state.operation &&
      !state.job) ||
      (state.job?.cleanup_state === "complete" &&
        ["succeeded", "failed", "cancelled", "expired"].includes(state.job.status)))
  );
}
