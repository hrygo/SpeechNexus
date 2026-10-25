import { validateRequest } from "../../../../packages/contracts/src/workbench-http";
import { parse, type Contracts } from "../manuscripts/workbench-client";
import { CommandJournal, fields, type JournalStorage } from "../recovery/command-journal";

export type AssistantCommand =
  | {
      kind: "create";
      projectId: string;
      body: Contracts["AssistantTurnCreateRequest"];
      key: string;
    }
  | { kind: "cancel"; turnId: string; key: string };
function validate(input: unknown): AssistantCommand {
  const raw = input as Partial<AssistantCommand>;
  if (raw?.kind === "create") {
    const value = fields(input, ["kind", "projectId", "body", "key"]);
    validateRequest("createAssistantTurn", {
      path: { project_id: value.projectId },
      headers: { "Idempotency-Key": value.key },
      body: value.body,
    });
    return {
      kind: "create",
      projectId: value.projectId as string,
      body: parse("AssistantTurnCreateRequest", value.body),
      key: value.key as string,
    };
  }
  const value = fields(input, ["kind", "turnId", "key"]);
  if (value.kind !== "cancel") throw new Error("Invalid assistant intent");
  validateRequest("cancelAssistantTurn", {
    path: { turn_id: value.turnId },
    headers: { "Idempotency-Key": value.key },
  });
  return { kind: "cancel", turnId: value.turnId as string, key: value.key as string };
}
export function assistantJournal(storage?: JournalStorage) {
  return new CommandJournal("speechnexus.workbench.assistant.pending.v1", validate, storage);
}
