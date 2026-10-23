import createClient from "openapi-fetch";
import type { paths } from "./speech.generated";
import type { FileContracts } from "./file.generated";
import { ContractValidationError, parse } from "./runtime.mjs";

export { ContractValidationError, parse };
export type { FileContracts } from "./file.generated";
export type { components, paths } from "./speech.generated";

/** Caller supplies the deployment URL and may inject its local fetch transport. */
export function createSpeechClient(baseUrl: string, fetch?: typeof globalThis.fetch) {
  return createClient<paths>({ baseUrl, ...(fetch ? { fetch } : {}) });
}

/** Binary transport replaces only the schema's string/binary placeholder. */
export type AssetUpload = Omit<FileContracts["AssetUploadRequest"], "file"> & { file: Blob };

/** One original upload; unknown results are returned without an automatic retry. */
export async function uploadAsset(
  client: ReturnType<typeof createSpeechClient>,
  input: AssetUpload,
  headers: { idempotencyKey?: string; requestId?: string } = {},
) {
  if (!(input.file instanceof Blob)) throw new ContractValidationError("Invalid Asset file");
  const metadata = parse("AssetUploadRequest", { ...input, file: "" });
  const form = new FormData();
  for (const [name, value] of Object.entries(metadata)) {
    if (name === "file") {
      form.append(name, input.file, input.file instanceof File ? input.file.name : "asset");
    } else if (value !== undefined) {
      form.append(name, String(value));
    }
  }
  const result = await client.POST("/v1/nexus/assets", {
    body: metadata,
    bodySerializer: () => form,
    params: {
      header: {
        ...(headers.idempotencyKey !== undefined
          ? { "Idempotency-Key": headers.idempotencyKey }
          : {}),
        ...(headers.requestId !== undefined ? { "X-Request-ID": headers.requestId } : {}),
      },
    },
  });
  if (result.data === undefined && result.error === undefined) {
    throw new ContractValidationError("Missing Asset response");
  }
  if (result.data !== undefined) parse("Asset", result.data);
  if (result.error !== undefined) parse("ErrorResponse", result.error);
  return result;
}
