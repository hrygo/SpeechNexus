import {
  workspaceDetail,
  workspacePage,
  WorkspaceReadError,
  type Workspace,
  type WorkspaceApi,
  type WorkspaceList,
} from "./workspace-client";
type Request = { kind: "list"; cursor?: string } | { kind: "detail"; id: string };
type Snapshot = {
  page: WorkspaceList | null;
  pageCurrent: boolean;
  selectedId: string | null;
  detail: Workspace | null;
  detailCurrent: boolean;
  busy: boolean;
  error: string | null;
};
function frozen<T>(value: T): T {
  if (value && typeof value === "object") {
    Object.values(value).forEach(frozen);
    Object.freeze(value);
  }
  return value;
}
/** User-driven GETs only; a new read supersedes the old request, never another resource's identity. */
export class WorkspaceSession {
  #view: Snapshot = frozen({
    page: null,
    pageCurrent: false,
    selectedId: null,
    detail: null,
    detailCurrent: false,
    busy: false,
    error: null,
  });
  #listeners = new Set<() => void>();
  #epoch = 0;
  #abort: AbortController | null = null;
  #active = true;
  #disposed = false;
  #last: Request | null = null;
  #cursors = new Set<string>();
  constructor(private readonly api: WorkspaceApi) {}
  snapshot = () => this.#view;
  subscribe = (listener: () => void) => {
    this.#listeners.add(listener);
    return () => {
      this.#listeners.delete(listener);
    };
  };
  #set(patch: Partial<Snapshot>) {
    this.#view = frozen(structuredClone({ ...this.#view, ...patch }));
    this.#listeners.forEach((f) => f());
  }
  async #read(request: Request) {
    if (!this.#active || this.#disposed) return;
    this.#abort?.abort();
    const abort = new AbortController();
    this.#abort = abort;
    const epoch = ++this.#epoch;
    this.#last = request;
    const live = () => this.#active && !this.#disposed && epoch === this.#epoch;
    const signal = AbortSignal.any([abort.signal, AbortSignal.timeout(15_000)]);
    this.#set({
      busy: true,
      error: null,
      detail: null,
      detailCurrent: false,
      selectedId: request.kind === "detail" ? request.id : null,
      ...(request.kind === "list" ? { pageCurrent: false } : {}),
    });
    if (request.kind === "list" && request.cursor === undefined) this.#cursors.clear();
    try {
      if (request.kind === "list") {
        const page = workspacePage(await this.api.list(request.cursor, signal), request.cursor);
        if (!live()) return;
        if (page.has_more && page.next_cursor && this.#cursors.has(page.next_cursor))
          throw Error("workspace_cursor_cycle");
        if (page.has_more && page.next_cursor) this.#cursors.add(page.next_cursor);
        this.#set({ page, pageCurrent: true });
      } else {
        const detail = workspaceDetail(await this.api.get(request.id, signal), request.id);
        if (live()) this.#set({ detail, detailCurrent: true });
      }
    } catch (error) {
      if (live())
        this.#set({
          error: error instanceof WorkspaceReadError ? error.code : "workspace_read_unavailable",
        });
    } finally {
      if (live()) this.#set({ busy: false });
    }
  }
  list = (cursor?: string) =>
    this.#read({ kind: "list", ...(cursor !== undefined ? { cursor } : {}) });
  get = (id: string) => this.#read({ kind: "detail", id });
  async retry() {
    if (this.#last) await this.#read(this.#last);
  }
  setActive(active: boolean) {
    if (this.#disposed || active === this.#active) return;
    this.#active = active;
    if (!active) {
      ++this.#epoch;
      this.#abort?.abort();
      this.#set({ busy: false, pageCurrent: false, detailCurrent: false });
    }
  }
  dispose() {
    if (this.#disposed) return;
    this.setActive(false);
    this.#disposed = true;
    this.#listeners.clear();
  }
}
