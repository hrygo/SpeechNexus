import {
  createSpeechClient,
  parse as parseError,
  type components,
} from "../../../../packages/contracts/src";
import { parse, type Contracts } from "../../../../packages/contracts/src/realtime";
import { parse as parseEvent } from "../../../../packages/contracts/src/realtime_events";

export type RealtimeRequest = Contracts["RealtimeSessionCreateRequest"];
export type RealtimeSession = Contracts["RealtimeSession"];
export type CancelMode = components["schemas"]["RealtimeSessionCancelRequest"]["mode"];
export class RealtimeResponseError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
  ) {
    super("Realtime request failed");
  }
}
export function identity(value: boolean): asserts value {
  if (!value) throw new Error("Realtime resource mismatch");
}
export function checkKey(key: string) {
  identity(key.length >= 1 && key.length <= 128 && !/[\r\n]/u.test(key));
}
export function checkMode(mode: CancelMode) {
  parseEvent("RealtimeEvent", { type: "nexus.session.close", event_id: "validate-mode", mode });
}
export function checkSession(
  value: unknown,
  expectedId?: string,
  request?: RealtimeRequest,
): RealtimeSession {
  const session = parse("RealtimeSession", value);
  if (expectedId !== undefined) identity(session.id === expectedId);
  identity(
    new Set(session.terminal_operations.map((o) => o.operation_id)).size ===
      session.terminal_operations.length,
  );
  if (request) {
    const cfg = session.effective_configuration;
    identity(
      cfg.profile?.id === request.profile.id && cfg.profile.revision === request.profile.revision,
    );
    for (const kind of ["asr", "tts"] as const)
      identity(
        cfg.models[kind]?.id === request.models[kind]?.id &&
          cfg.models[kind]?.revision === request.models[kind]?.revision,
      );
    if (request.models.tts) identity(!!cfg.voice);
    if (request.voice)
      identity(cfg.voice?.id === request.voice.id && cfg.voice.revision === request.voice.revision);
  }
  return session;
}
/** Only the deployment's pre-created session route; no endpoint or bearer material from input. */
export function connectionUrl(session: RealtimeSession): URL {
  identity(session.connection_url !== null);
  const url = new URL(session.connection_url, location.origin);
  const expected = location.protocol === "https:" ? "wss:" : "ws:";
  if (url.protocol === location.protocol) url.protocol = expected;
  identity(
    url.protocol === expected &&
      url.host === location.host &&
      !url.username &&
      !url.password &&
      !url.hash,
  );
  identity(
    url.pathname === "/v1/realtime" &&
      [...url.searchParams].length === 1 &&
      url.searchParams.get("session_id") === session.id,
  );
  return url;
}
function result(
  value: { response: Response; data?: unknown; error?: unknown },
  id?: string,
  request?: RealtimeRequest,
  statuses = [200],
) {
  if (!value.response.ok) {
    let code = "realtime_unavailable";
    try {
      code = parseError("ErrorResponse", value.error).error.code;
    } catch {
      /* No raw proxy body in UI. */
    }
    throw new RealtimeResponseError(value.response.status, code);
  }
  identity(statuses.includes(value.response.status));
  return checkSession(value.data, id, request);
}
/** The generated client owns route/request typing; this adapter bounds the JSON transport. */
export class RealtimeClient {
  readonly #api: ReturnType<typeof createSpeechClient>;
  constructor(transport: typeof globalThis.fetch = (input, init) => globalThis.fetch(input, init)) {
    const origin = location.origin,
      limit = 8 * 1024 * 1024;
    this.#api = createSpeechClient(origin, async (input, init) => {
      const callerSignal = init?.signal ?? (input instanceof Request ? input.signal : null);
      const request = new Request(input, {
        ...init,
        redirect: "error",
        cache: "no-store",
        credentials: "same-origin",
        signal: AbortSignal.any([
          ...(callerSignal ? [callerSignal] : []),
          AbortSignal.timeout(15000),
        ]),
      });
      identity(new URL(request.url).origin === origin);
      const response = await transport(request);
      if (
        !response.ok &&
        !/^application\/json(?:\s*;|$)/iu.test(response.headers.get("Content-Type") ?? "")
      ) {
        void response.body?.cancel();
        throw new RealtimeResponseError(response.status, "realtime_unavailable");
      }
      identity(/^application\/json(?:\s*;|$)/iu.test(response.headers.get("Content-Type") ?? ""));
      const declared = response.headers.get("Content-Length");
      identity(declared === null || (/^\d+$/u.test(declared) && Number(declared) <= limit));
      let bytes = 0;
      const body = response.body?.pipeThrough(
        new TransformStream<Uint8Array, Uint8Array>({
          transform(chunk, controller) {
            bytes += chunk.byteLength;
            identity(bytes <= limit);
            controller.enqueue(chunk);
          },
        }),
      );
      return new Response(body, {
        status: response.status,
        statusText: response.statusText,
        headers: response.headers,
      });
    });
  }
  async create(input: RealtimeRequest, key: string) {
    const body = parse("RealtimeSessionCreateRequest", input);
    checkKey(key);
    return result(
      await this.#api.POST("/v1/nexus/realtime/sessions", {
        body,
        params: { header: { "Idempotency-Key": key } },
      }),
      undefined,
      body,
      [201],
    );
  }
  async current(id: string, signal?: AbortSignal) {
    return result(
      await this.#api.GET("/v1/nexus/realtime/sessions/{session_id}", {
        params: { path: { session_id: id } },
        signal: signal ?? null,
      }),
      id,
    );
  }
  async cancel(id: string, mode: CancelMode, key: string) {
    checkKey(key);
    checkMode(mode);
    return result(
      await this.#api.POST("/v1/nexus/realtime/sessions/{session_id}/cancellations", {
        params: { path: { session_id: id }, header: { "Idempotency-Key": key } },
        body: { mode },
      }),
      id,
      undefined,
      [200, 202],
    );
  }
}
