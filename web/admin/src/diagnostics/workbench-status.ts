import { parse, type Contracts } from "../../../../packages/contracts/src/workbench";

export type WorkbenchStatus = Contracts["WorkbenchStatus"];

export class WorkbenchStatusError extends Error {
  constructor(readonly code: string) {
    super(code);
  }
}

/**
 * The admin entry reads the deployment's own workbench status from the same origin. It is
 * a read-only projection: neither the endpoint nor any credential is configurable here,
 * and an unreadable status must never be turned into an assumed capability.
 */
export async function loadWorkbenchStatus(signal?: AbortSignal): Promise<WorkbenchStatus> {
  let response: Response;
  try {
    response = await fetch("/v1/workbench/status", {
      headers: { accept: "application/json" },
      cache: "no-store",
      credentials: "same-origin",
      redirect: "error",
      signal: signal ?? null,
    });
  } catch {
    throw new WorkbenchStatusError("status_unreachable");
  }
  if (response.status !== 200) throw new WorkbenchStatusError(`status_${response.status}`);
  let body: unknown;
  try {
    body = await response.json();
  } catch {
    throw new WorkbenchStatusError("status_unreadable");
  }
  try {
    return parse("WorkbenchStatus", body);
  } catch {
    throw new WorkbenchStatusError("status_invalid");
  }
}
