import { describe, expect, it } from "vitest";
import fixtures from "../../../../tests/fixtures/contracts/file-workflow.json";
import { parse } from "../file-workflow/speech-file-client";
import { voiceCommand, checkVoiceReference } from "./voice-command";

const design = {
  kind: "voice_design",
  model: "voice-design",
  workspace_id: "ws-default",
  input: { description: "  温暖\n<script>中文😀</script>", preview_text: "精确试听文本" },
};
const clone = {
  kind: "voice_clone",
  model: "voice-base",
  workspace_id: "ws-default",
  input: {
    reference_audio_asset_id: "asset-demo",
    source_use_confirmed: true,
    reference_text: "  参考原文\n",
    preview_text: "换文试听",
  },
};
function asset() {
  const a = structuredClone(fixtures.examples.find((x) => x.id === "ready-audio")!.value) as Record<
    string,
    unknown
  >;
  a.purpose = "voice_reference";
  return parse("Asset", a);
}
describe("voice creation commands", () => {
  it("preserves exact design and clone input using generated parser", () => {
    expect(voiceCommand(design)).toEqual(design);
    expect(voiceCommand(clone)).toEqual(clone);
  });
  it.each([
    { ...design, input: { ...design.input, reference_audio_asset_id: "asset-demo" } },
    { ...clone, input: { ...clone.input, source_use_confirmed: false } },
    { ...clone, input: { ...clone.input, description: "hidden" } },
    { ...design, input: { ...design.input, description: "" } },
    { ...design, input: { ...design.input, preview_text: "x".repeat(2001) } },
    { ...design, workspace_id: undefined },
    { ...design, kind: "asr" },
  ])("rejects invalid/mixed/missing identity %#", (v) => expect(() => voiceCommand(v)).toThrow());
  it("accepts only ready same-scope voice reference audio", () => {
    expect(() => checkVoiceReference(voiceCommand(clone), asset())).not.toThrow();
  });
  it.each([
    { id: "wrong" },
    { workspace_id: "elsewhere" },
    { purpose: "asr_input" },
    { status: "expired" },
    { media_type: "text/plain" },
  ])("rejects a wrong or unusable reference %#", (change) => {
    expect(() => checkVoiceReference(voiceCommand(clone), { ...asset(), ...change })).toThrow();
  });
});
