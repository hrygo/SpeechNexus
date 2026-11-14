import { describe, expect, it } from "vitest";
import workbenchSchema from "../../../../packages/contracts/src/speechnexus_contracts/workbench.schema.json" with { type: "json" };
import speechSchema from "../../../../packages/contracts/src/speechnexus_contracts/file.schema.json" with { type: "json" };
import {
  RENDER_UNIT_DISPATCH_LIMIT,
  RENDER_UNIT_SOURCE_LIMIT,
  renderUnitLength,
} from "./render-unit-bounds";

/** Read one contract bound by JSON pointer, so a moved or removed node fails loudly. */
function boundAt(document: unknown, pointer: string): number | undefined {
  let node: unknown = document;
  for (const token of pointer.split("/").slice(1)) {
    if (Array.isArray(node)) node = node[Number(token)];
    else if (typeof node === "object" && node !== null)
      node = (node as Record<string, unknown>)[token];
    else return undefined;
  }
  return typeof node === "number" ? node : undefined;
}

/**
 * The panel mirrors two contract bounds; this pins the mirror to the generated
 * bundles so a contract change fails here instead of leaving the editor showing
 * a bound the server no longer enforces.
 */
describe("render unit text bounds", () => {
  it("mirrors the source capacity the contract declares for RenderUnit.text", () => {
    expect(boundAt(workbenchSchema, "/$defs/RenderUnitText/maxLength")).toBe(
      RENDER_UNIT_SOURCE_LIMIT,
    );
  });

  it("mirrors the inline dispatch bound both speech entries declare", () => {
    // One dispatch inlines the unit text into one Job body, so the bound is the
    // one `/v1/audio/speech` `input` and `TtsJobInput.text` declare.
    expect(boundAt(speechSchema, "/$defs/SpeechRequest/properties/input/maxLength")).toBe(
      RENDER_UNIT_DISPATCH_LIMIT,
    );
    expect(boundAt(speechSchema, "/$defs/TtsJobInput/oneOf/0/properties/text/maxLength")).toBe(
      RENDER_UNIT_DISPATCH_LIMIT,
    );
  });

  it("keeps the two bounds distinct instead of collapsing them", () => {
    expect(RENDER_UNIT_SOURCE_LIMIT).toBeGreaterThan(RENDER_UNIT_DISPATCH_LIMIT);
  });

  it("counts UTF-16 code units exactly as maxLength does", () => {
    // An astral code point is one code point but two UTF-16 code units; counting
    // code points would show half the real length and misjudge the bound.
    expect(renderUnitLength("𝐀")).toBe(2);
    expect(renderUnitLength("𝐀".repeat(2048))).toBe(4096);
    expect(renderUnitLength("𝐀".repeat(2049))).toBe(4098);
    expect(renderUnitLength("汉字")).toBe(2);
    expect(renderUnitLength("")).toBe(0);
  });
});
