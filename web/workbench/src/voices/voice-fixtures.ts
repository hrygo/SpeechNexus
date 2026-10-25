/** Synthetic contract fixtures, test-only; no audio payloads. */
import fixtures from "../../../../tests/fixtures/contracts/file-workflow.json";
import { parse, type FileContracts } from "../file-workflow/speech-file-client";
export const design = {
  kind: "voice_design",
  workspace_id: "ws-default",
  model: "voice-design",
  input: { description: "  合成设计😀\n", preview_text: "精确试听文本" },
} as const;
export const clone = {
  kind: "voice_clone",
  workspace_id: "ws-default",
  model: "voice-base",
  input: {
    reference_audio_asset_id: "asset-demo",
    reference_text: " 合成参考\n",
    source_use_confirmed: true,
  },
} as const;
export function job(kind: "voice_design" | "voice_clone" = "voice_design"): FileContracts["Job"] {
  const value = structuredClone(
    fixtures.examples.find((x) => x.id === "queued-job")!.value,
  ) as Record<string, unknown>;
  value.kind = kind;
  value.effective_configuration = {
    models: {
      voice: { id: kind === "voice_design" ? "voice-design" : "voice-base", revision: "1" },
    },
    policy_revision: "1",
    catalog_revision: "1",
  };
  return parse("Job", value);
}
export function reference() {
  const value = structuredClone(
    fixtures.examples.find((x) => x.id === "ready-audio")!.value,
  ) as Record<string, unknown>;
  value.purpose = "voice_reference";
  return parse("Asset", value);
}
export function storage() {
  const values = new Map<string, string>();
  return {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => {
      values.set(key, value);
    },
    removeItem: (key: string) => {
      values.delete(key);
    },
  };
}
