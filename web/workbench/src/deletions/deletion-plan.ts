import { parse, type Contracts } from "../../../../packages/contracts/src/workbench";

export type DeletionTarget = Contracts["DeletionPlan"]["target"];
export function targetKey(t: DeletionTarget): string {
  return JSON.stringify([t.owner, t.kind, t.resource.id, t.resource.revision]);
}
export function sameTargets(a: DeletionTarget[], b: DeletionTarget[]): boolean {
  const keys = new Set(a.map(targetKey));
  return a.length === keys.size && a.length === b.length && b.every((t) => keys.has(targetKey(t)));
}
/** Uses generated wire types; the confirmation is a set, dependencies remain a full list. */
export function confirmation(input: Contracts["DeletionPlan"]): Contracts["DeletionCreateRequest"] {
  const p = parse("DeletionPlan", input);
  if (
    !p.eligible ||
    p.target.owner !== "workbench" ||
    p.dependencies.some(
      (d) =>
        d.disposition === "blocks" ||
        (d.disposition === "cascade" && d.target.owner !== p.target.owner),
    )
  )
    throw Error("Deletion plan cannot be confirmed");
  const targets = [
    p.target,
    ...p.dependencies.filter((d) => d.disposition === "cascade").map((d) => d.target),
  ];
  const deleting = new Set(targets.map(targetKey));
  if (p.dependencies.some((d) => d.disposition === "retain" && deleting.has(targetKey(d.target))))
    throw Error("Retained resource cannot be deleted");
  return parse("DeletionCreateRequest", {
    plan_id: p.id,
    expected_plan_revision: p.plan_revision,
    plan_digest: p.plan_digest,
    confirm_targets: [...new Map(targets.map((t) => [targetKey(t), t])).values()],
  });
}
export function matchesConfirmation(
  a: Contracts["DeletionCreateRequest"],
  b: Contracts["DeletionCreateRequest"],
): boolean {
  return (
    a.plan_id === b.plan_id &&
    a.expected_plan_revision === b.expected_plan_revision &&
    a.plan_digest === b.plan_digest &&
    sameTargets(a.confirm_targets, b.confirm_targets)
  );
}
export function checkDeletion(
  d: Contracts["Deletion"],
  body: Contracts["DeletionCreateRequest"],
  plan?: Contracts["DeletionPlan"],
) {
  if (
    d.plan_id !== body.plan_id ||
    d.plan_digest !== body.plan_digest ||
    (d.result && !sameTargets(d.result.deleted, body.confirm_targets))
  )
    throw Error("Deletion response mismatch");
  if (plan && d.result) {
    const retained = [
      ...new Map(
        plan.dependencies
          .filter((d) => d.relation === "independent_copy" && d.disposition === "retain")
          .map((d) => [targetKey(d.target), d.target]),
      ).values(),
    ];
    if (!sameTargets(d.result.retained_independent_copies, retained))
      throw Error("Retained copy response mismatch");
  }
}
