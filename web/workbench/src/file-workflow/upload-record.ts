import type { TaskRecord, TtsSelection } from "./task-journal";

type UploadBase = Omit<
  Extract<TaskRecord, { version: 1 }>,
  "version" | "assetId" | "workspaceId" | "pending"
> & {
  readonly assetId: string | null;
  readonly workspaceId: string | null;
  readonly pending: "upload" | "validation";
  readonly uploadKey: string;
  readonly inputFingerprint: string;
  readonly assetMediaType: string | null;
  readonly assetBytes: number | null;
  readonly assetSha256: string | null;
};
export type UploadRecord =
  | (UploadBase & ({ readonly version: 3 } | { readonly version: 5; readonly assetHintId: string }))
  | (UploadBase &
      Readonly<TtsSelection> &
      ({ readonly version: 4 } | { readonly version: 6; readonly assetHintId: string }));

export const isUploadRecord = (
  value: { version: number } | null | undefined,
): value is UploadRecord =>
  value !== null && value !== undefined && [3, 4, 5, 6].includes(value.version);
export const uploadHint = (record: UploadRecord | null): string | null =>
  record && "assetHintId" in record ? record.assetHintId : null;

const common = [
  "version",
  "model",
  "assetId",
  "workspaceId",
  "createKey",
  "cancelKey",
  "jobId",
  "pending",
  "operationId",
  "cursor",
  "uploadKey",
  "inputFingerprint",
  "assetMediaType",
  "assetBytes",
  "assetSha256",
];
const selections = ["voiceId", "voiceRevision", "profileId", "profileRevision"];
export const uploadAssetFields = [
  "assetId",
  "workspaceId",
  "assetMediaType",
  "assetBytes",
  "assetSha256",
] as const;

/** Private storage policy; public Asset and Job validation still uses A at the boundary. */
export function validUploadRecord(value: Record<string, unknown>): UploadRecord {
  const invalid = () => {
    throw new Error("Invalid upload recovery record");
  };
  const asr = value.version === 3 || value.version === 5;
  const hinted = value.version === 5 || value.version === 6;
  const fields = [...common, ...(asr ? [] : selections), ...(hinted ? ["assetHintId"] : [])];
  if (
    ![3, 4, 5, 6].includes(Number(value.version)) ||
    Object.keys(value).length !== fields.length ||
    fields.some((field) => !Object.hasOwn(value, field))
  )
    return invalid();
  if (value.pending !== "upload" && value.pending !== "validation") return invalid();
  if (
    hinted &&
    (typeof value.assetHintId !== "string" ||
      !/^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/.test(value.assetHintId))
  )
    return invalid();
  if (hinted && value.pending === "validation" && value.assetId !== value.assetHintId)
    return invalid();
  if (value.jobId !== null || value.operationId !== null || value.cursor !== null) return invalid();
  if (
    typeof value.uploadKey !== "string" ||
    !/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(value.uploadKey)
  )
    return invalid();
  if (
    typeof value.inputFingerprint !== "string" ||
    !/^sha256-chain-v1:[a-f0-9]{64}$/.test(value.inputFingerprint)
  )
    return invalid();
  if (value.pending === "upload") {
    if (uploadAssetFields.some((field) => value[field] !== null)) return invalid();
  } else {
    for (const field of ["assetId", "workspaceId", "assetMediaType"])
      if (typeof value[field] !== "string" || !value[field].length || value[field].length > 128)
        return invalid();
    if (
      typeof value.assetBytes !== "number" ||
      !Number.isSafeInteger(value.assetBytes) ||
      value.assetBytes < 0
    )
      return invalid();
    if (typeof value.assetSha256 !== "string" || !/^sha256:[a-f0-9]{64}$/.test(value.assetSha256))
      return invalid();
  }
  return Object.freeze({ ...value }) as unknown as UploadRecord;
}

export function sameUploadIntent(before: UploadRecord, after: UploadRecord) {
  const fields = [
    ...common.filter((field) => ![...uploadAssetFields, "pending", "version"].includes(field)),
    ...selections,
  ];
  if (
    (before.version === 3 || before.version === 5) !== (after.version === 3 || after.version === 5)
  )
    return false;
  if (uploadHint(before) !== null && uploadHint(before) !== uploadHint(after)) return false;
  return fields.every(
    (field) =>
      (before as unknown as Record<string, unknown>)[field] ===
      (after as unknown as Record<string, unknown>)[field],
  );
}

export function uploadJobRecord(record: UploadRecord): TaskRecord {
  if (record.assetId === null || record.workspaceId === null)
    throw new Error("Upload is not acknowledged");
  const identity = {
    model: record.model,
    assetId: record.assetId,
    workspaceId: record.workspaceId,
    createKey: record.createKey,
    cancelKey: record.cancelKey,
    jobId: null,
    pending: "create" as const,
    operationId: null,
    cursor: null,
  };
  return record.version === 3 || record.version === 5
    ? { ...identity, version: 1 }
    : {
        ...identity,
        version: 2,
        voiceId: record.voiceId,
        voiceRevision: record.voiceRevision,
        profileId: record.profileId,
        profileRevision: record.profileRevision,
      };
}
