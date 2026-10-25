import { describe, expect, it, vi } from "vitest";
import { parse, WorkbenchClient } from "../manuscripts/workbench-client";
import { sample } from "../meetings/meeting-fixtures";
import { confirmation } from "./deletion-plan";
import { DeletionJournal, emptyDeletionRecord } from "./deletion-journal";

export function plan() {
  const p = parse("DeletionPlan", sample("workbenchCreateDeletionPlan"));
  p.mode = "cascade";
  p.expires_at = "2099-01-01T00:00:00Z";
  return p;
}
export function accepted() {
  return parse("Deletion", sample("workbenchCreateDeletion"));
}
export function memory() {
  const data = new Map<string, string>();
  return {
    getItem: vi.fn((key: string) => data.get(key) ?? null),
    setItem: vi.fn((key: string, value: string) => {
      data.set(key, value);
    }),
  };
}
const reply = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });

describe("deletion generated boundary and complete frozen intent", () => {
  it("deduplicates two cascade relations and excludes retained independent copies", () => {
    const p = plan();
    const target = {
      owner: "workbench" as const,
      kind: "transcript" as const,
      resource: { id: "transcript-demo", revision: "9007199254740993" },
    };
    p.dependencies = [
      { target, relation: "owned_child", disposition: "cascade" },
      { target: structuredClone(target), relation: "persistent_reference", disposition: "cascade" },
      {
        target: { ...target, resource: { ...target.resource, id: "saved-copy" } },
        relation: "independent_copy",
        disposition: "retain",
      },
    ];
    expect(confirmation(p).confirm_targets).toEqual([p.target, target]);
    p.dependencies[0]!.disposition = "blocks";
    p.eligible = false;
    expect(() => confirmation(p)).toThrow();
  });
  it("rejects a retained resource appearing in the deletion set", () => {
    const p = plan();
    p.dependencies = [{ target: p.target, relation: "independent_copy", disposition: "retain" }];
    expect(() => confirmation(p)).toThrow();
  });
  it("consumes three fixed-origin operations and checks request/response identity", async () => {
    const p = plan(),
      d = accepted();
    const fetch = vi
      .fn<typeof globalThis.fetch>()
      .mockResolvedValueOnce(reply(p))
      .mockResolvedValueOnce(reply(d, 202))
      .mockResolvedValueOnce(reply(d));
    const c = new WorkbenchClient(fetch);
    await expect(
      c.previewDeletion({ target: p.target, mode: p.mode }, "plan-key"),
    ).resolves.toEqual(p);
    await expect(c.acceptDeletion(confirmation(p), "delete-key")).resolves.toEqual(d);
    await expect(c.deletion(d.id)).resolves.toEqual(d);
    expect(fetch.mock.calls.map(([r]) => (r as Request).headers.get("Idempotency-Key"))).toEqual([
      "plan-key",
      "delete-key",
      null,
    ]);
    for (const [request] of fetch.mock.calls) {
      const r = request as Request;
      expect(new URL(r.url).origin).toBe(location.origin);
      expect(r.redirect).toBe("error");
    }
    const mismatch = new WorkbenchClient(() => Promise.resolve(reply({ ...p, mode: "ordinary" })));
    await expect(
      mismatch.previewDeletion({ target: p.target, mode: p.mode }, "plan-key"),
    ).rejects.toThrow();
    await expect(c.previewDeletion({ target: p.target, mode: p.mode }, "")).rejects.toThrow();
    expect(fetch).toHaveBeenCalledTimes(3);
    await expect(
      new WorkbenchClient(() =>
        Promise.resolve(reply({ ...d, plan_id: "wrong-plan" }, 202)),
      ).acceptDeletion(confirmation(p), "key"),
    ).rejects.toThrow();
    await expect(
      new WorkbenchClient(() => Promise.resolve(reply(d))).deletion("wrong-id"),
    ).rejects.toThrow();
  });
});

describe("deletion recovery journal", () => {
  it("roundtrips an exact original pending intent, rejects mismatched or extra fields", () => {
    const storage = memory(),
      journal = new DeletionJournal(storage),
      p = plan();
    const record = {
      ...emptyDeletionRecord(),
      request: { target: p.target, mode: p.mode },
      plan: p,
      pending: { kind: "accept" as const, key: "original-key", body: confirmation(p) },
    };
    journal.save(record);
    expect(new DeletionJournal(storage).load()).toEqual(record);
    const bad = structuredClone(record);
    bad.pending.body.plan_id = "different-plan";
    expect(() => journal.save(bad)).toThrow();
    expect(() => journal.save({ ...record, secret: "not-allowed" } as typeof record)).toThrow();
    expect(() => journal.save({ ...record, accepted: accepted() })).toThrow();
    const leaked = journal.load();
    leaked.pending!.key = "changed";
    expect(journal.load()).toEqual(record);
  });
  it("never overwrites corrupt, concurrent or failed storage", () => {
    expect(() => new DeletionJournal({ ...memory(), getItem: () => "{broken" })).toThrow();
    const storage = memory(),
      j = new DeletionJournal(storage);
    storage.getItem.mockReturnValue("external");
    expect(() => j.save(emptyDeletionRecord())).toThrow();
    expect(storage.setItem).not.toHaveBeenCalled();
    storage.getItem.mockReturnValue(null);
    storage.setItem.mockImplementation(() => {
      throw Error("quota");
    });
    expect(() => j.save(emptyDeletionRecord())).toThrow();
  });
});
