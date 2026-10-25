import { validateRequest } from "../../../../packages/contracts/src/workbench-http";
import { parse, type Contracts } from "../manuscripts/workbench-client";
import { CommandJournal, fields, type JournalStorage } from "../recovery/command-journal";

export type ExportCommand =
  | { kind: "create"; bookId: string; body: Contracts["ExportCreateRequest"]; key: string }
  | { kind: "cancel"; bookId: string; exportId: string; key: string };
function validateExport(input: unknown): ExportCommand {
  const raw = input as Partial<ExportCommand>;
  if (raw?.kind === "create") {
    const v = fields(input, ["kind", "bookId", "body", "key"]);
    validateRequest("createBookExport", {
      path: { book_id: v.bookId },
      headers: { "Idempotency-Key": v.key },
      body: v.body,
    });
    return {
      kind: "create",
      bookId: v.bookId as string,
      body: parse("ExportCreateRequest", v.body),
      key: v.key as string,
    };
  }
  const v = fields(input, ["kind", "bookId", "exportId", "key"]);
  if (v.kind !== "cancel" || typeof v.bookId !== "string" || !v.bookId)
    throw new Error("Invalid export intent");
  validateRequest("cancelBookExport", {
    path: { export_id: v.exportId },
    headers: { "Idempotency-Key": v.key },
  });
  return { kind: "cancel", bookId: v.bookId, exportId: v.exportId as string, key: v.key as string };
}
export function exportJournal(bookId: string, storage?: JournalStorage) {
  return new CommandJournal(
    `speechnexus.workbench.exports.${bookId}.pending.v1`,
    validateExport,
    storage,
  );
}
export type UnitCommand =
  | { kind: "create"; chapterId: string; body: Contracts["RenderUnitCreateRequestV2"]; key: string }
  | {
      kind: "patch";
      chapterId: string;
      unitId: string;
      body: Contracts["RenderUnitPatchV2"];
      key: string;
    };
function validateUnit(input: unknown): UnitCommand {
  const raw = input as Partial<UnitCommand>;
  if (raw?.kind === "create") {
    const v = fields(input, ["kind", "chapterId", "body", "key"]);
    validateRequest("createRenderUnitV2", {
      path: { chapter_id: v.chapterId },
      headers: { "Idempotency-Key": v.key },
      body: v.body,
    });
    return {
      kind: "create",
      chapterId: v.chapterId as string,
      body: parse("RenderUnitCreateRequestV2", v.body),
      key: v.key as string,
    };
  }
  const v = fields(input, ["kind", "chapterId", "unitId", "body", "key"]);
  if (v.kind !== "patch" || typeof v.chapterId !== "string" || !v.chapterId)
    throw new Error("Invalid unit intent");
  validateRequest("updateRenderUnitV2", {
    path: { unit_id: v.unitId },
    headers: { "Idempotency-Key": v.key },
    body: v.body,
  });
  return {
    kind: "patch",
    chapterId: v.chapterId,
    unitId: v.unitId as string,
    body: parse("RenderUnitPatchV2", v.body),
    key: v.key as string,
  };
}
export function unitJournal(chapterId: string, storage?: JournalStorage) {
  return new CommandJournal(
    `speechnexus.workbench.units.${chapterId}.pending.v1`,
    validateUnit,
    storage,
  );
}
