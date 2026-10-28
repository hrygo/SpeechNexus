import { describe, expect, it } from "vitest";
import { catalogueProblemOf, catalogueRows, type CatalogueRead } from "./catalogue-state";
import { WorkbenchResponseError, parse } from "../manuscripts/workbench-client";

function rows(count: number) {
  return Array.from({ length: count }, (_, index) =>
    parse("VoiceSummary", {
      voice_id: `voice_${index}`,
      revision: "1",
      display_name: `合成音色 ${index}`,
      source_type: "designed",
      status: "published",
      languages: ["zh"],
      audio_formats: ["wav"],
    }),
  );
}

function unavailable(status = 503) {
  return new WorkbenchResponseError(
    status,
    parse("ErrorResponse", {
      error: {
        code: "voice_catalogue_unavailable",
        type: "service_unavailable_error",
        message: "Voice catalogue is unavailable.",
        request_id: "request-demo",
        param: null,
        retryable: true,
      },
    }),
  );
}

describe("catalogue read states", () => {
  it("offers no rows until the deployment has actually answered", () => {
    // An unread or in-flight catalogue must never look like an empty one.
    expect(catalogueRows({ kind: "unread" })).toEqual([]);
    expect(catalogueRows({ kind: "loading" })).toEqual([]);
    expect(catalogueRows({ kind: "problem", problem: "unavailable" })).toEqual([]);
  });

  it("offers the published rows only when the read succeeded", () => {
    const read: CatalogueRead = { kind: "ready", voices: rows(2) };
    expect(catalogueRows(read).map((voice) => voice.voice_id)).toEqual(["voice_0", "voice_1"]);
    // An answered-but-empty catalogue is still a successful read with zero rows.
    expect(catalogueRows({ kind: "ready", voices: [] })).toEqual([]);
  });

  it("separates a served failure from an unconfirmed answer", () => {
    expect(catalogueProblemOf(unavailable(503))).toBe("unavailable");
    expect(catalogueProblemOf(unavailable(500))).toBe("unavailable");
    // A transport failure is not evidence that the catalogue is empty either.
    expect(catalogueProblemOf(new TypeError("Failed to fetch"))).toBe("unavailable");
    // A 4xx answer is unexpected, so it is unconfirmed rather than unavailable.
    expect(catalogueProblemOf(unavailable(404))).toBe("unknown");
    expect(catalogueProblemOf(new Error("boom"))).toBe("unknown");
  });
});
