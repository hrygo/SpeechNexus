/**
 * The two `RenderUnit.text` bounds, which are different things (#745).
 *
 * `RenderUnitText` (`contracts/workbench-openapi.yaml` → workbench schema
 * `$defs.RenderUnitText`) declares `maxLength: 200000`. That is the unit's
 * **source capacity** — the editing and provenance boundary — so it may be an
 * input bound on the editor.
 *
 * One dispatch inlines the whole unit text into one public Job request body,
 * whose inline text bound is `MAX_SPEECH_TEXT_CHARS` (4096), the same bound
 * `SpeechRequest.input` and `TtsJobInput.text` declare. 4096 describes the shape
 * of one request, never the size of the source: a longer unit stays valid to
 * store, it just cannot be dispatched in one request and has to be split into
 * units of at most 4096. The registered dispatch operation refuses it with
 * `422` `render_submission_rejected` instead of truncating.
 *
 * Both values are mirrored from the generated contract bundles; the pair is
 * pinned to those bundles by `render-unit-bounds.test.ts`, so a contract change
 * fails here instead of drifting silently.
 */
export const RENDER_UNIT_SOURCE_LIMIT = 200000;
export const RENDER_UNIT_DISPATCH_LIMIT = 4096;

/**
 * Text length as `maxLength` counts it: UTF-16 code units (`String#length`),
 * not Unicode code points. Everything the panel shows or compares uses this
 * unit, so an operator reading the counter is measuring exactly what the input
 * bound and the dispatch bound measure.
 */
export function renderUnitLength(text: string): number {
  return text.length;
}
