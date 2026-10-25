import { validateRequest } from "../../../../packages/contracts/src/workbench-http";
import { parse, type Contracts } from "../manuscripts/workbench-client";
import { checkDeletion, confirmation, matchesConfirmation, targetKey } from "./deletion-plan";

export const deletionJournalKey = "speechnexus.workbench.deletions.v1";
export type DeletionStorage = Pick<Storage, "getItem" | "setItem">;
export type DeletionCommand =
  | { kind: "preview"; key: string; body: Contracts["DeletionPlanRequest"] }
  | { kind: "accept"; key: string; body: Contracts["DeletionCreateRequest"] };
export type DeletionRecord = {
  version: 1;
  request: Contracts["DeletionPlanRequest"] | null;
  plan: Contracts["DeletionPlan"] | null;
  pending: DeletionCommand | null;
  accepted: Contracts["Deletion"] | null;
};
export function emptyDeletionRecord(): DeletionRecord {
  return { version: 1, request: null, plan: null, pending: null, accepted: null };
}
export class DeletionStorageError extends Error {
  constructor() {
    super("Deletion recovery unavailable");
  }
}
function check(valid: boolean): asserts valid {
  if (!valid) throw new DeletionStorageError();
}
function object(v: unknown): Record<string, unknown> {
  check(!!v && typeof v === "object" && !Array.isArray(v));
  return v as Record<string, unknown>;
}
function fields(v: Record<string, unknown>, names: string[]) {
  check(Object.keys(v).length === names.length && names.every((n) => Object.hasOwn(v, n)));
}
function validate(input: unknown): DeletionRecord {
  const r = object(input);
  fields(r, ["version", "request", "plan", "pending", "accepted"]);
  check(r.version === 1);
  const request = r.request === null ? null : parse("DeletionPlanRequest", r.request);
  if (request)
    check(
      request.target.owner === "workbench" &&
        ["project", "transcript"].includes(request.target.kind),
    );
  const plan = r.plan === null ? null : parse("DeletionPlan", r.plan);
  if (plan)
    check(
      request !== null &&
        targetKey(plan.target) === targetKey(request.target) &&
        plan.mode === request.mode,
    );
  const accepted = r.accepted === null ? null : parse("Deletion", r.accepted);
  if (accepted) {
    check(plan !== null && r.pending === null);
    checkDeletion(accepted, confirmation(plan), plan);
  }
  if (r.pending !== null) {
    const p = object(r.pending);
    fields(p, ["kind", "key", "body"]);
    check(request !== null && accepted === null);
    if (p.kind === "preview") {
      validateRequest("workbenchCreateDeletionPlan", {
        headers: { "Idempotency-Key": p.key },
        body: p.body,
      });
      const body = parse("DeletionPlanRequest", p.body);
      check(
        plan === null &&
          body.mode === request.mode &&
          targetKey(body.target) === targetKey(request.target),
      );
    } else {
      check(p.kind === "accept" && plan !== null);
      validateRequest("workbenchCreateDeletion", {
        headers: { "Idempotency-Key": p.key },
        body: p.body,
      });
      check(matchesConfirmation(parse("DeletionCreateRequest", p.body), confirmation(plan)));
    }
  }
  if (!request) check(plan === null && r.pending === null && accepted === null);
  return structuredClone(r) as DeletionRecord;
}
function bounded(raw: string) {
  check(new TextEncoder().encode(raw).byteLength <= 8 * 1024 * 1024);
}
/** Single tab writer; never overwrite corrupt or externally changed durable intent. */
export class DeletionJournal {
  #raw: string | null;
  #record: DeletionRecord;
  constructor(private readonly storage: DeletionStorage) {
    try {
      this.#raw = storage.getItem(deletionJournalKey);
      if (this.#raw !== null) bounded(this.#raw);
      this.#record =
        this.#raw === null ? emptyDeletionRecord() : validate(JSON.parse(this.#raw) as unknown);
    } catch {
      throw new DeletionStorageError();
    }
  }
  load() {
    return structuredClone(this.#record);
  }
  save(record: DeletionRecord) {
    try {
      const next = validate(record),
        raw = JSON.stringify(next);
      bounded(raw);
      check(this.storage.getItem(deletionJournalKey) === this.#raw);
      this.storage.setItem(deletionJournalKey, raw);
      this.#raw = raw;
      this.#record = next;
    } catch {
      throw new DeletionStorageError();
    }
  }
}
