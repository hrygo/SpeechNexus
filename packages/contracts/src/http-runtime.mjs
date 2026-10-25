import { createProfileParser } from "./profile-runtime.mjs";
import { ContractValidationError } from "./errors.mjs";
import { readSse } from "./sse-runtime.mjs";

function normalize(value, types, message) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return value;
  const result = { ...value };
  for (const [location, fields] of Object.entries(types)) {
    const group = value[location];
    if (!group || typeof group !== "object" || Array.isArray(group)) continue;
    const normalized = Object.create(null);
    for (const [key, original] of Object.entries(group)) {
      const name = location === "headers" ? key.toLowerCase() : key;
      if (Object.hasOwn(normalized, name)) throw new ContractValidationError(message);
      let item = original;
      if (fields[name] === "integer") {
        if (typeof item === "string" && item.length <= 17 && /^-?(0|[1-9][0-9]*)$/.test(item)) {
          item = Number(item);
        }
        if (typeof item === "number" && !Number.isSafeInteger(item)) {
          throw new ContractValidationError(message);
        }
      }
      normalized[name] = item;
    }
    result[location] = normalized;
  }
  return result;
}

function jsonContent(headers) {
  return headers.get("content-type")?.split(";", 1)[0].trim().toLowerCase() === "application/json";
}

async function boundedJson(message, limit, errorMessage) {
  if (!message.body) return {};
  const clone = message.clone();
  const reader = clone.body.getReader();
  try {
    if (!jsonContent(message.headers)) throw new Error();
    const chunks = [];
    let size = 0;
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > limit) throw new Error();
      chunks.push(value);
    }
    const bytes = new Uint8Array(size);
    let offset = 0;
    for (const chunk of chunks) {
      bytes.set(chunk, offset);
      offset += chunk.byteLength;
    }
    return { body: JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes)) };
  } catch {
    // Cancel both tee branches without waiting on the unread original branch.
    void reader.cancel().catch(() => {});
    void message.body.cancel().catch(() => {});
    throw new ContractValidationError(errorMessage);
  } finally {
    reader.releaseLock();
  }
}

export function createHttpValidator(bundle) {
  const parse = createProfileParser(bundle);
  const operations = bundle["x-nexus-http-operations"];
  function operation(name) {
    if (!Object.hasOwn(operations, name)) throw new Error("Unknown HTTP operation");
    return operations[name];
  }
  function validateRequest(name, value) {
    const meta = operation(name);
    try {
      parse(meta.request, normalize(value, meta.parameters, "Invalid HTTP request"));
    } catch (error) {
      if (!(error instanceof ContractValidationError)) throw error;
      throw new ContractValidationError("Invalid HTTP request");
    }
    return value;
  }
  function validateResponse(name, status, value) {
    const responses = operation(name).responses;
    if (!Number.isInteger(status) || !Object.hasOwn(responses, status)) {
      throw new ContractValidationError("Invalid HTTP response");
    }
    const meta = responses[status];
    try {
      let validationValue = value;
      if (meta.binary || meta.event) {
        if (
          !value ||
          typeof value !== "object" ||
          !(value.body instanceof Uint8Array || value.body instanceof ReadableStream)
        )
          throw new ContractValidationError("Invalid HTTP response");
        // Only represent a checked native binary transport as a schema string.
        // Payload bytes/streams remain untouched and unread.
        validationValue = { ...value, body: "" };
      }
      parse(
        meta.schema,
        normalize(validationValue, { headers: meta.headers }, "Invalid HTTP response"),
      );
    } catch (error) {
      if (!(error instanceof ContractValidationError)) throw error;
      throw new ContractValidationError("Invalid HTTP response");
    }
    return value;
  }
  function middleware(baseUrl, maxJsonBytes) {
    if (!Number.isSafeInteger(maxJsonBytes) || maxJsonBytes < 1) {
      throw new Error("A positive safe JSON byte limit is required");
    }
    function locate(schemaPath, request) {
      const entry = Object.entries(operations).find(
        ([, meta]) => meta.path === schemaPath && meta.method === request.method.toLowerCase(),
      );
      if (!entry) throw new Error("Unknown HTTP operation");
      return entry;
    }
    return {
      async onRequest({ request, schemaPath, params, options }) {
        if (request.redirect !== "error") throw new ContractValidationError("Invalid HTTP request");
        const [name, meta] = locate(schemaPath, request);
        if (
          Object.values(meta.responses).some((reply) => reply.binary || reply.event) &&
          options.parseAs !== "stream"
        ) {
          throw new ContractValidationError("Invalid HTTP request");
        }
        // Headers has already merged case variants; reject ambiguous parameter input first.
        normalize({ headers: params.header }, { headers: {} }, "Invalid HTTP request");
        const actual = new URL(request.url);
        const query = Object.create(null);
        for (const [key, value] of actual.searchParams) {
          if (Object.hasOwn(query, key)) throw new ContractValidationError("Invalid HTTP request");
          query[key] = value;
        }
        const envelope = {
          path: params.path ?? {},
          query,
          headers: Object.fromEntries(request.headers),
          ...(await boundedJson(request, maxJsonBytes, "Invalid HTTP request")),
        };
        validateRequest(name, envelope);
        const path = meta.path.replace(/\{([^}]+)\}/g, (_, key) =>
          encodeURIComponent(envelope.path[key]),
        );
        const expected = new URL(baseUrl.replace(/\/$/, "") + path);
        if (actual.origin !== expected.origin || actual.pathname !== expected.pathname) {
          throw new ContractValidationError("Invalid HTTP request");
        }
      },
      async onResponse({ request, schemaPath, response }) {
        const [name, meta] = locate(schemaPath, request);
        try {
          const reply = meta.responses[response.status];
          if (!reply) throw new ContractValidationError("Invalid HTTP response");
          if (reply.binary || reply.event) {
            if (
              response.headers.get("content-type")?.split(";", 1)[0].trim().toLowerCase() !==
              (reply.event ? "text/event-stream" : "application/octet-stream")
            ) {
              throw new ContractValidationError("Invalid HTTP response");
            }
            validateResponse(name, response.status, {
              headers: Object.fromEntries(response.headers),
              body: response.body ?? new Uint8Array(0),
            });
          } else {
            validateResponse(name, response.status, {
              headers: Object.fromEntries(response.headers),
              ...(await boundedJson(response, maxJsonBytes, "Invalid HTTP response")),
            });
          }
        } catch (error) {
          void response.body?.cancel().catch(() => {});
          throw error;
        }
      },
    };
  }
  function validateEvent(name, value) {
    const replies = Object.values(operation(name).responses).filter((reply) => reply.event);
    if (replies.length !== 1) throw new Error("Unknown HTTP event operation");
    try {
      parse(replies[0].event, value);
    } catch (error) {
      if (!(error instanceof ContractValidationError)) throw error;
      throw new ContractValidationError("Invalid HTTP event");
    }
    return value;
  }
  function readEvents(name, source, request, maxEventBytes) {
    validateRequest(name, request);
    const meta = operation(name);
    const normalized = normalize(request, meta.parameters, "Invalid HTTP request");
    if (Object.values(meta.responses).filter((reply) => reply.event).length !== 1) {
      throw new Error("Unknown HTTP event operation");
    }
    return readSse(
      source,
      (value) => validateEvent(name, value),
      normalized.path ?? {},
      normalized.headers?.["last-event-id"],
      maxEventBytes,
    );
  }
  return { validateRequest, validateResponse, validateEvent, readEvents, middleware };
}
