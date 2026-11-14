import { describe, expect, it } from "vitest";
import workbenchSchema from "../../../../packages/contracts/src/speechnexus_contracts/workbench.schema.json" with { type: "json" };
import speechSchema from "../../../../packages/contracts/src/speechnexus_contracts/file.schema.json" with { type: "json" };
import { parse } from "../../../../packages/contracts/src/runtime.mjs";
import {
  RENDER_UNIT_DISPATCH_LIMIT,
  RENDER_UNIT_SOURCE_LIMIT,
  renderUnitLength,
} from "./render-unit-bounds";

/** Read one contract node by JSON pointer, so a moved or removed node fails loudly. */
function at(document: unknown, pointer: string): unknown {
  let node: unknown = document;
  for (const token of pointer.split("/").slice(1)) {
    if (Array.isArray(node)) node = node[Number(token)];
    else if (typeof node === "object" && node !== null)
      node = (node as Record<string, unknown>)[token];
    else return undefined;
  }
  return node;
}

function numberAt(document: unknown, pointer: string): number | undefined {
  const node = at(document, pointer);
  return typeof node === "number" ? node : undefined;
}

/**
 * `TtsJobInput` is a `oneOf`; the inline-text branch is selected by its `required`
 * shape, not by position, so reordering the branches cannot silently re-pin the test.
 */
function inlineJobTextBound(document: unknown): number | undefined {
  const branches = at(document, "/$defs/TtsJobInput/oneOf");
  if (!Array.isArray(branches)) return undefined;
  for (const branch of branches) {
    const required = at(branch, "/required");
    if (Array.isArray(required) && required.includes("text"))
      return numberAt(branch, "/properties/text/maxLength");
  }
  return undefined;
}

/**
 * The panel mirrors two contract bounds; this pins the mirror to the generated
 * bundles so a contract change fails here instead of leaving the editor showing
 * a bound the server no longer enforces.
 */
describe("render unit text bounds", () => {
  it("mirrors the source capacity the contract declares for RenderUnit.text", () => {
    expect(numberAt(workbenchSchema, "/$defs/RenderUnitText/maxLength")).toBe(
      RENDER_UNIT_SOURCE_LIMIT,
    );
  });

  it("mirrors the inline dispatch bound both speech entries declare", () => {
    // One dispatch inlines the unit text into one Job body, so the bound is the
    // one `/v1/audio/speech` `input` and the inline `text` of `TtsJobInput` declare.
    expect(numberAt(speechSchema, "/$defs/SpeechRequest/properties/input/maxLength")).toBe(
      RENDER_UNIT_DISPATCH_LIMIT,
    );
    expect(inlineJobTextBound(speechSchema)).toBe(RENDER_UNIT_DISPATCH_LIMIT);
  });

  it("states the dispatch bound in the unit's own contract description", () => {
    const description = at(workbenchSchema, "/$defs/RenderUnitText/description");
    expect(description).toContain("MAX_SPEECH_TEXT_CHARS");
    expect(description).toContain(String(RENDER_UNIT_DISPATCH_LIMIT));
  });

  it("keeps the two bounds distinct instead of collapsing them", () => {
    expect(RENDER_UNIT_SOURCE_LIMIT).toBeGreaterThan(RENDER_UNIT_DISPATCH_LIMIT);
  });

  it("counts Unicode code points, which is what the generated validator counts", () => {
    // JSON Schema `maxLength` counts code points, so 2049 astral code points are
    // under the 4096 bound even though they occupy 4098 UTF-16 code units.
    expect(() =>
      parse("SpeechRequest", { model: "tts-quality", input: "😀".repeat(2049), voice: "v" }),
    ).not.toThrow();
    expect(() =>
      parse("SpeechRequest", { model: "tts-quality", input: "😀".repeat(4096), voice: "v" }),
    ).not.toThrow();
    expect(() =>
      parse("SpeechRequest", { model: "tts-quality", input: "😀".repeat(4097), voice: "v" }),
    ).toThrow();

    expect(renderUnitLength("😀")).toBe(1);
    expect(renderUnitLength("😀".repeat(2049))).toBe(2049);
    expect(renderUnitLength("😀".repeat(4096))).toBe(4096);
    expect(renderUnitLength("😀".repeat(4097))).toBe(4097);
    expect(renderUnitLength("汉字")).toBe(2);
    expect(renderUnitLength("")).toBe(0);
    // The HTML `maxLength` unit is different: it counts these same 2049 emoji as
    // 4098 code units, so comparing against that number would report a text the
    // server accepts as over the bound.
    expect("😀".repeat(2049).length).toBe(4098);
  });
});
