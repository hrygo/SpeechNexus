import type { Contracts } from "../manuscripts/workbench-client";
import { WorkbenchResponseError } from "../manuscripts/workbench-client";

/**
 * The published catalogue is read from the deployment, never assumed. A read that has not
 * finished, a catalogue that really is empty, and a catalogue the deployment could not serve
 * are three different facts, and the operator must be able to tell them apart:
 *
 * - unread / loading: nothing is known yet, so no catalogue claim is made.
 * - ready with no rows: the deployment answered, and it has published nothing.
 * - problem: the deployment did not answer, so the rows are unknown, not absent.
 *
 * Collapsing the last two is the failure this module exists to prevent: an empty picker on a
 * 503 reads as "there are no voice models", when the truth is "the catalogue is unavailable".
 */
export type CatalogueRow = Contracts["VoiceSummary"];

export type CatalogueRead =
  | { readonly kind: "unread" }
  | { readonly kind: "loading" }
  | { readonly kind: "ready"; readonly voices: readonly CatalogueRow[] }
  | { readonly kind: "problem"; readonly problem: CatalogueProblem };

export type CatalogueProblem = "unavailable" | "unknown";

/**
 * A 5xx or a transport failure means the catalogue could not be read now, so the operator is
 * offered a retry. Anything else is an unexpected answer and is reported as unconfirmed rather
 * than dressed up as an empty catalogue.
 */
export function catalogueProblemOf(error: unknown): CatalogueProblem {
  if (error instanceof WorkbenchResponseError) {
    return error.status >= 500 ? "unavailable" : "unknown";
  }
  return error instanceof TypeError ? "unavailable" : "unknown";
}

/**
 * The rows a picker may offer. An unread, loading or failed catalogue yields no rows, so the
 * picker can never imply that an unread catalogue is an empty one.
 */
export function catalogueRows(read: CatalogueRead): readonly CatalogueRow[] {
  return read.kind === "ready" ? read.voices : [];
}

/** Shared so both catalogue consumers describe the same situation with the same words. */
export const catalogueCopy = {
  loading: "正在读取已发布音色目录…",
  empty: "当前部署尚未发布可用音色：语音底座发布后此处才会出现选项。",
  unavailable: "音色目录暂时不可用：语音底座未配置或读取失败，当前无法确认已发布音色。",
  unknown: "音色目录读取结果未确认，请重新读取。",
  retry: "重新读取音色目录",
} as const;
