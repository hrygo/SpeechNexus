import { parse, type Contracts } from "../../../../packages/contracts/src/admin";
import { createValidatedClient } from "../../../../packages/contracts/src/admin-http";
export type Workspace = Contracts["Workspace"];
export type WorkspaceList = Contracts["WorkspaceList"];
export interface WorkspaceApi {
  list(cursor?: string, signal?: AbortSignal): Promise<WorkspaceList>;
  get(id: string, signal?: AbortSignal): Promise<Workspace>;
}
export class WorkspaceReadError extends Error {
  constructor(readonly code: string) {
    super(code);
  }
}
export function workspacePage(value: unknown, cursor?: string): WorkspaceList {
  const page = parse("WorkspaceList", value);
  if (
    new Set(page.data.map((w) => w.id)).size !== page.data.length ||
    (page.has_more && (!page.next_cursor || page.next_cursor === cursor))
  )
    throw new Error("workspace_page_unusable");
  return page;
}
export function workspaceDetail(value: unknown, id: string): Workspace {
  const item = parse("Workspace", value);
  if (item.id !== id) throw new Error("workspace_identity_mismatch");
  return item;
}
const deadline = (signal?: AbortSignal) =>
  AbortSignal.any([...(signal ? [signal] : []), AbortSignal.timeout(15_000)]);
/** Only A's selected GET operations exist on this client. No planned write is exposed. */
export class WorkspaceClient implements WorkspaceApi {
  readonly #client: ReturnType<typeof createValidatedClient>;
  constructor(baseUrl: string, fetch: typeof globalThis.fetch = globalThis.fetch) {
    const origin = new URL(baseUrl).origin;
    this.#client = createValidatedClient(baseUrl, 1024 * 1024, (input, init) => {
      const request = new Request(input, { ...init, redirect: "error", cache: "no-store" });
      if (new URL(request.url).origin !== origin || request.method !== "GET")
        throw new Error("workspace_read_boundary");
      return fetch(request);
    });
  }
  #check(response: Response, error: unknown) {
    if (!response.ok) throw new WorkspaceReadError(parse("ErrorResponse", error).error.code);
  }
  async list(cursor?: string, signal?: AbortSignal) {
    const result = await this.#client.GET("/v1/nexus/workspaces", {
      ...(cursor !== undefined ? { params: { query: { cursor } } } : {}),
      signal: deadline(signal),
    });
    this.#check(result.response, result.error);
    return workspacePage(result.data, cursor);
  }
  async get(id: string, signal?: AbortSignal) {
    const result = await this.#client.GET("/v1/nexus/workspaces/{workspace_id}", {
      params: { path: { workspace_id: id } },
      signal: deadline(signal),
    });
    this.#check(result.response, result.error);
    return workspaceDetail(result.data, id);
  }
}
